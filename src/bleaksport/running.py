from __future__ import annotations

import asyncio
import contextlib
import re
import struct
import time
from typing import TYPE_CHECKING

from bleak import BleakError

from bleaksport.core import s
from bleaksport.models import RunningSample
from bleaksport.mux_base import MuxBase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bleak import BleakClient
    from bleak.backends.device import BLEDevice

# RSCS
UUID_RSCS = s(0x1814)
UUID_RSC_MEAS = s(0x2A53)
# CPS (power, e.g., Stryd over BLE)
UUID_CPS = s(0x1818)
UUID_CP_MEAS = s(0x2A63)


INPROGRESS_RE = re.compile(r"InProgress", re.IGNORECASE)


class RunningSession:
    """
    A lightweight decoder that subscribes to RSCS (and CPS if present) on an
    already-connected BleakClient and emits RunningSample via callbacks.
    """

    CHAR_RSCS = UUID_RSC_MEAS
    CHAR_CPS = UUID_CP_MEAS

    def __init__(self) -> None:
        self._callbacks: list[Callable[[RunningSample], Awaitable[None] | None]] = []
        self._last: RunningSample | None = None
        self._started = False

    def on_running(self, cb: Callable[[RunningSample], Awaitable[None] | None]) -> None:
        """Register a callback for new samples."""
        self._callbacks.append(cb)

    async def start(self, client: BleakClient) -> None:
        """Subscribe to characteristics on an already-connected client."""
        if self._started:
            return

        started_any = False
        # RSCS is preferred for running metrics; CPS is optional (power).
        with contextlib.suppress(Exception):
            await client.start_notify(self.CHAR_RSCS, self._handle_rsc)  # ty:ignore[invalid-argument-type]
            started_any = True
        with contextlib.suppress(Exception):
            await client.start_notify(self.CHAR_CPS, self._handle_cp)  # ty:ignore[invalid-argument-type]
            started_any = True

        if not started_any:
            msg = "No running characteristics could be subscribed"
            raise RuntimeError(msg)

        self._started = started_any

    async def stop(self, client: BleakClient) -> None:
        """Unsubscribe from notifications (ignores missing chars)."""
        if not self._started:
            return
        for uuid in (self.CHAR_RSCS, self.CHAR_CPS):
            with contextlib.suppress(Exception):
                await client.stop_notify(uuid)
        self._started = False

    # ---- internal emit ----
    def _emit(self, sample: RunningSample) -> None:
        # carry forward last-known values so consumers see a "complete" sample
        if self._last is not None:
            sample = RunningSample(
                timestamp_ms=sample.timestamp_ms
                if sample.timestamp_ms is not None
                else self._last.timestamp_ms,
                speed_mps=sample.speed_mps
                if sample.speed_mps is not None
                else self._last.speed_mps,
                cadence_spm=sample.cadence_spm
                if sample.cadence_spm is not None
                else self._last.cadence_spm,
                stride_length_m=sample.stride_length_m
                if sample.stride_length_m is not None
                else self._last.stride_length_m,
                distance_m=sample.distance_m
                if sample.distance_m is not None
                else self._last.distance_m,
                is_running=sample.is_running
                if sample.is_running is not None
                else self._last.is_running,
                power_watts=sample.power_watts
                if sample.power_watts is not None
                else self._last.power_watts,
            )
        self._last = sample

        async def _dispatch() -> None:
            tasks = []
            for cb in self._callbacks:
                res = cb(sample)
                if asyncio.iscoroutine(res):
                    tasks.append(asyncio.create_task(res))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        task = asyncio.create_task(_dispatch())

        def _done(t: asyncio.Task) -> None:
            exc = t.exception()
            if exc:
                print(f"Callback error: {exc!r}")

        task.add_done_callback(_done)

    # ---- RSCS (0x2A53) ----
    def _handle_rsc(self, _h: int, data: bytearray) -> None:
        """Parse RSCS Measurement characteristic."""
        time_ms = int(time.time() * 1000)
        off = 0
        flags = data[off]
        off += 1
        stride_present = bool(flags & 0x01)
        dist_present = bool(flags & 0x02)
        is_running = bool(flags & 0x04)

        speed_raw = struct.unpack_from("<H", data, off)[0]
        off += 2
        cadence_spm = data[off] if len(data) > off else 0
        off += 1
        speed_mps = speed_raw / 256.0

        stride_m = None
        if stride_present and len(data) >= off + 2:
            stride_cm = struct.unpack_from("<H", data, off)[0]
            off += 2
            stride_m = stride_cm / 100.0

        total_distance_m = None
        if dist_present and len(data) >= off + 4:
            # SIG says meters with 1/10 resolution for some libs
            dist_raw = struct.unpack_from("<I", data, off)[0]
            off += 4
            total_distance_m = float(dist_raw) * 0.1

        power = self._last.power_watts if self._last else None
        sample = RunningSample(
            timestamp_ms=time_ms,
            speed_mps=speed_mps,
            cadence_spm=int(cadence_spm),
            stride_length_m=stride_m,
            distance_m=total_distance_m,
            is_running=is_running,
            power_watts=power,
        )
        self._emit(sample)

    # ---- CPS (0x2A63) ----
    def _handle_cp(self, _h: int, data: bytearray) -> None:
        """Parse Cycling Power Measurement characteristic (for power only)."""
        time_ms = int(time.time() * 1000)
        off = 0
        if len(data) < 4:
            return
        off += 2  # flags (unused)
        inst_power = struct.unpack_from("<h", data, off)[0]
        off += 2

        prev = self._last
        sample = RunningSample(
            timestamp_ms=time_ms,
            speed_mps=(prev.speed_mps if prev else None),
            cadence_spm=(prev.cadence_spm if prev else None),
            stride_length_m=(prev.stride_length_m if prev else None),
            distance_m=(prev.distance_m if prev else None),
            is_running=(prev.is_running if prev else None),
            power_watts=int(inst_power),
        )
        self._emit(sample)


class RunningMux(MuxBase):
    """
    Roles:
      - 'rsc' → RSCS (speed/cadence/stride/distance)
      - 'cps' → CPS (power)
    on_link(addr, connected, has_rsc, has_cps).
    """

    def __init__(
        self,
        *,
        speed_addr: str | BLEDevice | None = None,
        cadence_addr: str | BLEDevice | None = None,
        power_addr: str | BLEDevice | None = None,
        on_sample: Callable[[RunningSample], Awaitable[None] | None] | None = None,
        on_status: Callable[[str], None] | None = None,
        ble_lock: asyncio.Lock | None = None,
        reconnect_backoff_s: float = 2.0,
        on_link: Callable[[str, bool, dict[str, bool]], None] | None = None,
    ) -> None:
        # collapse speed+cadence roles to a single RSCS address if both point to the same device
        def _addr_of(x):
            if x is None:
                return None
            if isinstance(x, str):
                return x
            return getattr(x, "address", None)

        rsc_addr = _addr_of(speed_addr) or _addr_of(cadence_addr)
        roles_to_addrs = {
            "rsc": rsc_addr,
            "cps": _addr_of(power_addr),
        }

        super().__init__(
            roles_to_addrs=roles_to_addrs,
            on_status=on_status or (lambda _m: None),
            ble_lock=ble_lock,
            reconnect_backoff_s=reconnect_backoff_s,
            on_link=on_link or (lambda *_: None),
        )

        self._user_on_sample = on_sample or (lambda _s: None)
        self._last: RunningSample | None = None

    # ---- MuxBase overrides ----
    async def _make_session(self, client: BleakClient) -> RunningSession:
        sess = RunningSession()
        # Wire session → mux fuser
        sess.on_running(self._on_partial_sample)
        return sess

    async def _start_session(self, session: RunningSession, client: BleakClient) -> None:
        await session.start(client)

    async def _stop_session(self, session: RunningSession, client: BleakClient) -> None:
        await session.stop(client)

    def _on_partial_sample(self, part: RunningSample) -> None:
        if self._last is None:
            self._last = part.model_copy()
        else:
            self._last = RunningSample(
                timestamp_ms=part.timestamp_ms
                if part.timestamp_ms is not None
                else self._last.timestamp_ms,
                speed_mps=part.speed_mps if part.speed_mps is not None else self._last.speed_mps,
                cadence_spm=part.cadence_spm
                if part.cadence_spm is not None
                else self._last.cadence_spm,
                stride_length_m=part.stride_length_m
                if part.stride_length_m is not None
                else self._last.stride_length_m,
                distance_m=part.distance_m
                if part.distance_m is not None
                else self._last.distance_m,
                is_running=part.is_running
                if part.is_running is not None
                else self._last.is_running,
                power_watts=part.power_watts
                if part.power_watts is not None
                else self._last.power_watts,
            )
        res = self._user_on_sample(self._last)
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)

    def _role_presence_from_client(self, client: BleakClient) -> dict[str, bool]:
        has_rsc = False
        has_cps = False
        if hasattr(client, "services") and client.services is not None:
            with contextlib.suppress(Exception):
                has_rsc = bool(client.services.get_characteristic(UUID_RSC_MEAS))
            with contextlib.suppress(Exception):
                has_cps = bool(client.services.get_characteristic(UUID_CP_MEAS))
        return {"rsc": has_rsc, "cps": has_cps}

    def _format_roles_for_status(self, roles):
        # Ensure stable order in status string
        order = ["rsc", "cps"]
        present = [r for r in order if r in roles]
        # Include any extra roles at the end just in case
        extras = sorted(set(roles) - set(order))
        return ",".join(present + extras)

    async def reset_distance(self) -> bool:
        """Reset the cumulative distance on the connected RSCS device.

        This writes a zero cumulative distance to the first active session's client (RSCS),
        which resets the device-reported total distance if the device supports the operation.

        Returns:
            bool: True if a reset was attempted (write dispatched), False if no active
            session/client was available to perform the reset.
        """
        # RSCS: distance in 0.1 m; for “reset” we just write 0
        for addr, roles in self._roles_by_addr.items():
            if "rsc" in roles and addr in self._clients:
                return await self._sc_cp_set_cumulative(self._clients[addr], 0)
        return False
