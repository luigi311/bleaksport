from __future__ import annotations

import asyncio
import contextlib
import re
import time
from cmath import log
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger
from pyftms import FitnessMachine, ResultCode, get_client, get_client_from_address

from bleaksport.linux_bluez import bluez_disconnect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bleak.backends.device import BLEDevice
    from pyftms import MachineType


# Search for InProgress exceptions
INPROGRESS_RE = re.compile(r"InProgress", re.IGNORECASE)


@dataclass
class TrainerSample:
    """Normalized indoor-training telemetry (FTMS update events).

    Fields are best-effort: many machines report only a subset.
    """

    timestamp: float

    # Common across indoor bikes / trainers
    speed_kmh: float | None = None
    cadence_rpm: float | None = None
    power_watts: float | None = None
    resistance_level: float | None = None
    heart_rate_bpm: float | None = None
    elapsed_s: float | None = None
    distance_m: float | None = None
    inclination: float | None = None

    target_inclination: float | None = None
    target_power: int | None = None
    target_resistance: float | None = None
    target_speed: float | None = None

    # Machine meta / raw passthrough
    machine_type: MachineType | None = None
    raw: dict[str, Any] | None = None


class TrainerMux:
    """
    Orchestrates a single FTMS machine using pyftms.

    Features:
      - one address
      - stable connect loop with backoff
      - all BLE operations serialized under a shared ble_lock
      - exposes a connected event so control writes never race connect
      - best-effort BlueZ disconnect on failure to clear "InProgress" states

    on_link(addr, connected, info_dict)
    """

    def __init__(
        self,
        *,
        addr: str | BLEDevice | None = None,
        device: BLEDevice | None = None,
        machine_type: MachineType | None = None,
        on_sample: Callable[[TrainerSample], Awaitable[None] | None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_link: Callable[[str, bool, dict[str, Any]], None] | None = None,
        ble_lock: asyncio.Lock | None = None,
        reconnect_backoff_s: float = 2.0,
        scan_timeout_s: float = 8.0,
        sticky_ttl_s: float | None = 5.0,
    ) -> None:
        logger.debug(f"TrainerMux init: addr={addr}, device={device}, machine_type={machine_type}")

        def _addr_of(x):
            if x is None:
                return None
            if isinstance(x, str):
                return x
            return getattr(x, "address", None)

        self.addr = _addr_of(addr)
        self._device = (
            device if device is not None else (addr if not isinstance(addr, str) else None)
        )
        self._provided_machine_type = machine_type
        self._on_sample = on_sample or (lambda _s: None)
        self._on_status = on_status or (lambda _m: None)
        self._on_link = on_link or (lambda *_: None)

        self._ble_lock = ble_lock or asyncio.Lock()
        self._reconnect_backoff_s = float(reconnect_backoff_s)
        self._scan_timeout_s = float(scan_timeout_s)

        self._stop_evt = asyncio.Event()
        self._task: asyncio.Task | None = None

        # Connection state
        self._connected_evt = asyncio.Event()

        # pyftms machine client (created on connect)
        self._machine: FitnessMachine | None = None
        self._machine_type: MachineType | None = None

        # How long a metric can remain "sticky" without being updated.
        # None => never expire.
        self._sticky_ttl_s = None if sticky_ttl_s is None else float(sticky_ttl_s)

        # Store the last sample for sticky states as some only report changes every so often
        self._last: TrainerSample | None = None

        # Per-field timestamp of last time we saw a non-None update for that field.
        self._last_seen_ts: dict[str, float] = {}

    # ---------------- public API ----------------

    async def start(self) -> None:
        """Run until stop() is called."""
        logger.debug("TrainerMux starting")
        if not self.addr:
            msg = "no device configured"
            self._on_status(msg)
            logger.debug(msg)
            return

        self._stop_evt.clear()
        self._task = asyncio.create_task(self._run())
        await self._task

    async def stop(self) -> None:
        """Stop + disconnect (best-effort)."""
        logger.debug("TrainerMux stopping")
        self._stop_evt.set()
        await asyncio.sleep(0)

        if self._task:
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
            self._task = None

        await self._disconnect()
        with contextlib.suppress(Exception):
            if self.addr:
                await bluez_disconnect(self.addr)

    async def wait_connected(self, timeout_s: float = 20.0) -> None:
        """Wait until connected (useful for callers before sending ERG commands)."""
        try:
            await asyncio.wait_for(self._connected_evt.wait(), timeout=timeout_s)
        except TimeoutError:
            msg = "Timed out waiting for connection; not connected"
            logger.warning(msg)
            raise TimeoutError(msg)

    @property
    def is_connected(self) -> bool:
        """Whether currently connected (best-effort, may be slightly stale)."""
        return self._connected_evt.is_set()

    @property
    def machine_type(self) -> MachineType | None:
        """The type of the connected machine, if available."""
        return self._machine_type

    # ---- control helpers (serialized & connection-safe) ----

    async def set_target_power(self, watts: int, *, timeout_s: float = 10.0) -> int:
        """ERG mode (Target Power). Returns the watts set on success, raises on failure."""
        watts = int(watts)
        logger.debug(f"set_target_power: watts={watts}")
        await self.wait_connected(timeout_s=timeout_s)
        if self._machine is None:
            msg = "not connected"
            logger.warning(msg)
            raise RuntimeError(msg)

        async with self._ble_lock:
            result = await self._machine.set_target_power(watts)

            if result == ResultCode.SUCCESS:
                logger.debug("set_target_power succeeded")
                return watts

            msg = f"set_target_power failed with ResultCode: {result}"
            logger.warning(msg)
            raise RuntimeError(msg)

    def get_target_power(self) -> int | None:
        """Get current target power setting. Returns watts on success, None on failure."""
        if self._machine is None:
            msg = "not connected"
            logger.warning(msg)
            return None

        watts = self._machine.target_power
        if watts is not None:
            try:
                return int(watts)
            except Exception:
                return None
        return None

    async def set_target_resistance(self, level: float, *, timeout_s: float = 10.0) -> float:
        """
        Resistance mode (Target Resistance Level or similar).

        Returns the level set on success, raises on failure.
        """
        logger.debug(f"set_target_resistance: level={level}")
        await self.wait_connected(timeout_s=timeout_s)

        if not self._machine:
            msg = "not connected"
            logger.warning(msg)
            raise RuntimeError(msg)

        async with self._ble_lock:
            result = await self._machine.set_target_resistance(level)
            if result == ResultCode.SUCCESS:
                logger.debug("set_target_resistance succeeded")
                return level

            msg = f"set_target_resistance failed with ResultCode: {result}"
            logger.warning(msg)
            raise RuntimeError(msg)

    def get_target_resistance(self) -> float | None:
        """Get current target resistance level. Returns level on success, None on failure."""
        if self._machine is None:
            msg = "not connected"
            logger.warning(msg)
            return None

        level = self._machine.target_resistance
        if level is not None:
            try:
                return float(level)
            except Exception:
                return None
        return None

    async def set_target_speed(
        self,
        speed_kmh: float,
        *,
        timeout_s: float = 10.0,
    ) -> float:
        """Sets target speed in km/h. Returns the speed set on success, raises on failure."""
        logger.debug(f"set_target_speed: speed_kmh={speed_kmh}")
        await self.wait_connected(timeout_s=timeout_s)

        if not self._machine:
            msg = "not connected"
            logger.warning(msg)
            raise RuntimeError(msg)

        async with self._ble_lock:
            result = await self._machine.set_target_speed(speed_kmh)

            if result == ResultCode.SUCCESS:
                logger.debug("set_target_speed succeeded")
                return speed_kmh

            msg = f"set_target_speed failed with ResultCode: {result}"
            logger.warning(msg)
            raise RuntimeError(msg)

    def get_target_speed(self) -> float | None:
        """Get current target speed. Returns speed in km/h on success, None on failure."""
        if self._machine is None:
            msg = "not connected"
            logger.warning(msg)
            return None

        speed_kmh = self._machine.target_speed
        if speed_kmh is not None:
            try:
                return float(speed_kmh)
            except Exception:
                return None
        return None

    async def set_target_inclination(
        self,
        incline_percent: float,
        *,
        timeout_s: float = 10.0,
    ) -> float:
        """Sets target inclination in percent. Returns the incline set on success, raises on failure."""
        logger.debug(f"set_target_inclination: incline_percent={incline_percent}")
        await self.wait_connected(timeout_s=timeout_s)

        if not self._machine:
            msg = "Not connected"
            logger.warning(msg)
            raise RuntimeError(msg)

        async with self._ble_lock:
            result = await self._machine.set_target_inclination(incline_percent)

            if result == ResultCode.SUCCESS:
                logger.debug("set_target_inclination succeeded")
                return incline_percent

            msg = f"set_target_inclination failed with ResultCode: {result}"
            logger.warning(msg)
            raise RuntimeError(msg)

    def get_target_inclination(self) -> float | None:
        """Get current target inclination. Returns incline in percent on success, None on failure."""
        if self._machine is None:
            msg = "Not connected"
            logger.warning(msg)
            return None

        incline_percent = self._machine.target_inclination
        if incline_percent is not None:
            try:
                return float(incline_percent)
            except Exception:
                return None
        return None

    # ---------------- internals ----------------

    async def _run(self) -> None:
        logger.debug("TrainerMux run loop starting")
        # Mimic MuxBase behavior: loop with backoff, clean disconnect, robust finally.
        while not self._stop_evt.is_set():
            try:
                await self._connect_and_stream()

                # Stay alive until stop requested or machine disappears.
                while not self._stop_evt.is_set() and self._machine is not None:
                    await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                return
            except Exception as e:
                if INPROGRESS_RE.search(str(e)):
                    logger.warning("TrainerMux ble connection in progress, will retry")
                else:
                    msg = f"TrainerMux error @ {self.addr}: {type(e).__name__}: {e}"
                    logger.error(msg)
                    self._on_status(msg)
            finally:
                # Mirror MuxBase: always emit disconnected and try to clean up BlueZ state.
                with contextlib.suppress(Exception):
                    self._on_link(self.addr or "-", False, {})
                await self._disconnect()
                with contextlib.suppress(Exception):
                    if self.addr:
                        await bluez_disconnect(self.addr)

            if not self._stop_evt.is_set():
                await asyncio.sleep(self._reconnect_backoff_s)

    async def _connect_and_stream(self) -> None:
        logger.debug("TrainerMux: connecting and streaming")

        machine_type = self._provided_machine_type

        def _on_ftms_event(event: Any) -> None:
            try:
                if getattr(event, "event_id", None) != "update":
                    return
                data = dict(getattr(event, "event_data", {}) or {})
                sample = self._to_sample(data, machine_type=machine_type)
                sample = self._merge_last(sample)
                self._last = sample

                res = self._on_sample(sample)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                self._on_status(f"TrainerMux event handler error: {type(e).__name__}: {e}")

        async with self._ble_lock:
            if self._device is not None and self._provided_machine_type is not None:
                logger.debug(
                    "TrainerMux: using provided device and machine type to connect without scanning"
                )
                machine = get_client(
                    self._device,
                    self._provided_machine_type,
                    timeout=5,
                    on_ftms_event=_on_ftms_event,
                )
            elif self.addr:
                logger.debug(
                    f"attempting to connect via {self.addr} with scan_timeout={self._scan_timeout_s}"
                )
                machine = await get_client_from_address(
                    self.addr,
                    scan_timeout=self._scan_timeout_s,
                    timeout=5,
                    on_ftms_event=_on_ftms_event,
                )
            else:
                msg = "cannot connect without device or address"
                logger.error(msg)
                raise RuntimeError(msg)

            await machine.connect()

        self._machine = machine

        logger.debug(f"TrainerMux connected to machine: {machine}")
        logger.debug(f"Supported properties: {machine.supported_properties}")
        logger.debug(f"Supported settings: {machine.supported_settings}")
        logger.debug(f"Supported ranges: {machine.supported_ranges}")

        self._machine_type = machine_type
        self._connected_evt.set()

        info = {"ftms": True, "machine_type": machine_type}
        with contextlib.suppress(Exception):
            self._on_link(self.addr, True, info)

        await self.wait_connected(timeout_s=5.0)

        async with self._ble_lock:
            # Send start/resume after setting up event handler and emitting connected;
            # some machines require this to start sending updates.
            await machine.start_resume()

    async def _disconnect(self) -> None:
        m = self._machine
        self._machine = None
        self._connected_evt.clear()
        if not m:
            return

        # Wipe sticky cache
        self._last = None
        self._last_seen_ts.clear()

        async with self._ble_lock:
            with contextlib.suppress(Exception):
                await m.disconnect()

    def _merge_last(self, new: TrainerSample) -> TrainerSample:
        prev = self._last
        if prev is None:
            now = new.timestamp
            for field in (
                "speed_kmh",
                "cadence_rpm",
                "power_watts",
                "resistance_level",
                "heart_rate_bpm",
                "elapsed_s",
                "distance_m",
                "inclination",
            ):
                v = getattr(new, field)
                if v is not None:
                    self._last_seen_ts[field] = now

            return new

        ttl = self._sticky_ttl_s
        now = new.timestamp

        def merged_value(field: str):
            v_new = getattr(new, field)
            if v_new is not None:
                self._last_seen_ts[field] = now
                return v_new

            v_prev = getattr(prev, field)
            if v_prev is None:
                return None

            # If no TTL, always keep previous.
            if ttl is None:
                return v_prev

            last_seen = self._last_seen_ts.get(field, prev.timestamp)
            if (now - last_seen) > ttl:
                return None

            return v_prev

        merged = TrainerSample(
            timestamp=now,
            speed_kmh=merged_value("speed_kmh"),
            cadence_rpm=merged_value("cadence_rpm"),
            power_watts=merged_value("power_watts"),
            heart_rate_bpm=merged_value("heart_rate_bpm"),
            elapsed_s=merged_value("elapsed_s"),
            distance_m=merged_value("distance_m"),
            resistance_level=merged_value("resistance_level"),
            inclination=merged_value("inclination"),
            # Non-sticky fields (just take new)
            target_inclination=new.target_inclination,
            target_power=new.target_power,
            target_resistance=new.target_resistance,
            target_speed=new.target_speed,
            machine_type=new.machine_type,
            raw=new.raw,
        )

        logger.trace(f"merging samples (ttl={ttl})")
        logger.bind(data=prev).trace("Previous")
        logger.bind(data=new).trace("New")
        logger.bind(data=merged).trace("Merged")

        return merged

    def _to_sample(
        self,
        data: dict[str, Any],
        *,
        machine_type: MachineType | None,
    ) -> TrainerSample:
        ts = time.time()

        speed_kmh = data.get("speed_instant")
        cadence = data.get("cadence_instant")
        power = data.get("power_instant")
        resistance = data.get("resistance_level")
        hr = data.get("heart_rate")
        elapsed = data.get("time_elapsed")
        distance = data.get("distance_total")
        inclination = data.get("inclination")

        target_inclination = self.get_target_inclination()
        target_power = self.get_target_power()
        target_resistance = self.get_target_resistance()
        target_speed = self.get_target_speed()

        sample = TrainerSample(
            timestamp=ts,
            speed_kmh=speed_kmh,
            cadence_rpm=cadence,
            power_watts=power,
            resistance_level=resistance,
            heart_rate_bpm=hr,
            elapsed_s=elapsed,
            distance_m=distance,
            inclination=inclination,
            machine_type=machine_type,
            target_inclination=target_inclination,
            target_power=target_power,
            target_resistance=target_resistance,
            target_speed=target_speed,
            raw=data,
        )

        logger.trace(f"TrainerMux converted data to sample: {sample}")
        return sample
