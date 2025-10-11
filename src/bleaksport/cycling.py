from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bleaksport.core import s
from bleaksport.mux_base import MuxBase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bleak import BleakClient
    from bleak.backends.device import BLEDevice

# Services / Characteristics
UUID_CSCS = s(0x1816)
UUID_CSC_MEAS = s(0x2A5B)
UUID_CPS = s(0x1818)
UUID_CP_MEAS = s(0x2A63)


@dataclass
class CyclingSample:
    """A fused sample from CSCS and CPS."""

    timestamp: float
    cum_wheel_revs: int | None = None
    last_wheel_event_time_s: float | None = None
    cum_crank_revs: int | None = None
    last_crank_event_time_s: float | None = None
    power_watts: int | None = None
    speed_mps: float | None = None
    wheel_rpm: float | None = None
    cadence_rpm: float | None = None


class CyclingSession:
    """
    Decoder that subscribes to CSCS (+ CPS if present) on an already-connected client.
    Emits fused CyclingSample via callbacks.
    """

    CHAR_CSCS = UUID_CSC_MEAS
    CHAR_CPS = UUID_CP_MEAS

    def __init__(self, *, wheel_circumference_m: float | None = None) -> None:
        self._callbacks: list[Callable[[CyclingSample], Awaitable[None] | None]] = []
        self._wheel_prev = None  # (cum:uint32, t_s)
        self._crank_prev = None  # (cum:uint16, t_s)
        self._last: CyclingSample | None = None
        self._started = False
        self.wheel_circumference_m = wheel_circumference_m

    def on_cycling(self, cb: Callable[[CyclingSample], Awaitable[None] | None]) -> None:
        """Register a callback for new samples."""
        self._callbacks.append(cb)

    async def start(self, client: BleakClient) -> None:
        """Subscribe to CSCS (+ CPS if available)."""
        if self._started:
            return
        await client.start_notify(self.CHAR_CSCS, self._handle_csc)
        with contextlib.suppress(Exception):
            await client.start_notify(self.CHAR_CPS, self._handle_cp)
        self._started = True

    async def stop(self, client: BleakClient) -> None:
        """Unsubscribe from CSCS and CPS."""
        if not self._started:
            return
        for uuid in (self.CHAR_CSCS, self.CHAR_CPS):
            with contextlib.suppress(Exception):
                await client.stop_notify(uuid)
        self._started = False

    def _emit(self, sample: CyclingSample) -> None:
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
        task.add_done_callback(lambda t: t.exception())

    # ---- CSCS handler ----
    def _handle_csc(self, _h: int, data: bytearray) -> None:
        ts = time.time()
        off = 0
        flags = data[off]
        off += 1
        wheel_present = bool(flags & 0x01)
        crank_present = bool(flags & 0x02)

        cum_wheel = last_wheel_s = None
        if wheel_present:
            cum_wheel = struct.unpack_from("<I", data, off)[0]
            off += 4
            last_wheel_evt_1024 = struct.unpack_from("<H", data, off)[0]
            off += 2
            last_wheel_s = last_wheel_evt_1024 / 1024.0

        cum_crank = last_crank_s = None
        if crank_present:
            cum_crank = struct.unpack_from("<H", data, off)[0]
            off += 2
            last_crank_evt_1024 = struct.unpack_from("<H", data, off)[0]
            off += 2
            last_crank_s = last_crank_evt_1024 / 1024.0

        power = self._last.power_watts if self._last else None
        sample = CyclingSample(
            timestamp=ts,
            cum_wheel_revs=cum_wheel,
            last_wheel_event_time_s=last_wheel_s,
            cum_crank_revs=cum_crank,
            last_crank_event_time_s=last_crank_s,
            power_watts=power,
        )

        # Derived (speed / rpm)
        if wheel_present and self.wheel_circumference_m is not None and last_wheel_s is not None:
            if self._wheel_prev is not None and cum_wheel is not None:
                prev_revs, prev_t = self._wheel_prev
                d_revs = (cum_wheel - prev_revs) & 0xFFFFFFFF
                dt = (last_wheel_s - prev_t) % 64.0
                if dt > 0:
                    sample.speed_mps = (d_revs * self.wheel_circumference_m) / dt
                    sample.wheel_rpm = (d_revs / dt) * 60.0
            self._wheel_prev = (cum_wheel, last_wheel_s)

        if crank_present and last_crank_s is not None:
            if self._crank_prev is not None and cum_crank is not None:
                prev_revs, prev_t = self._crank_prev
                d_revs = (cum_crank - prev_revs) & 0xFFFF
                dt = (last_crank_s - prev_t) % 64.0
                if dt > 0:
                    sample.cadence_rpm = (d_revs / dt) * 60.0
            self._crank_prev = (cum_crank, last_crank_s)

        self._emit(sample)

    # ---- CPS handler ----
    def _handle_cp(self, _h: int, data: bytearray) -> None:
        ts = time.time()
        off = 0
        off += 2  # flags
        inst_power = struct.unpack_from("<h", data, off)[0]
        off += 2

        prev = self._last
        sample = CyclingSample(
            timestamp=ts,
            cum_wheel_revs=prev.cum_wheel_revs if prev else None,
            last_wheel_event_time_s=prev.last_wheel_event_time_s if prev else None,
            cum_crank_revs=prev.cum_crank_revs if prev else None,
            last_crank_event_time_s=prev.last_crank_event_time_s if prev else None,
            power_watts=int(inst_power),
            speed_mps=prev.speed_mps if prev else None,
            wheel_rpm=prev.wheel_rpm if prev else None,
            cadence_rpm=prev.cadence_rpm if prev else None,
        )
        self._emit(sample)


class CyclingMux(MuxBase):
    """
    Roles:
      - 'csc' → CSCS (speed/cadence/derived)   [UUID 0x1816 / 0x2A5B]
      - 'cps' → CPS (power)                    [UUID 0x1818 / 0x2A63].

    on_link(addr, connected, has_csc, has_cps)
    """

    def __init__(
        self,
        *,
        csc_addr: str | BLEDevice | None = None,
        cps_addr: str | BLEDevice | None = None,
        wheel_circumference_m: float | None = None,
        on_sample: Callable[[CyclingSample], Awaitable[None] | None] | None = None,
        on_status: Callable[[str], None] | None = None,
        ble_lock: asyncio.Lock | None = None,
        reconnect_backoff_s: float = 2.0,
        on_link: Callable[[str, bool, bool, bool], None] | None = None,
    ) -> None:
        def _addr_of(x):
            if x is None:
                return None
            if isinstance(x, str):
                return x
            return getattr(x, "address", None)

        roles_to_addrs = {
            "csc": _addr_of(csc_addr),
            "cps": _addr_of(cps_addr),
        }

        super().__init__(
            roles_to_addrs=roles_to_addrs,
            on_status=on_status or (lambda _m: None),
            ble_lock=ble_lock,
            reconnect_backoff_s=reconnect_backoff_s,
            on_link=on_link or (lambda *_: None),
        )

        self._user_on_sample = on_sample or (lambda _s: None)
        self._last: CyclingSample | None = None
        self._wheel_circumference_m = wheel_circumference_m

    # ---- MuxBase overrides ----
    async def _make_session(self, client: BleakClient) -> CyclingSession:
        sess = CyclingSession(wheel_circumference_m=self._wheel_circumference_m)
        sess.on_cycling(self._on_partial_sample)
        return sess

    async def _start_session(self, session: CyclingSession, client: BleakClient) -> None:
        await session.start(client)

    async def _stop_session(self, session: CyclingSession, client: BleakClient) -> None:
        await session.stop(client)

    def _on_partial_sample(self, part: CyclingSample) -> None:
        # Fuse across devices (e.g., CSCS from one, CPS power from another)
        if self._last is None:
            self._last = replace(part)
        else:
            self._last = CyclingSample(
                timestamp=part.timestamp or self._last.timestamp,
                cum_wheel_revs=part.cum_wheel_revs
                if part.cum_wheel_revs is not None
                else self._last.cum_wheel_revs,
                last_wheel_event_time_s=part.last_wheel_event_time_s
                if part.last_wheel_event_time_s is not None
                else self._last.last_wheel_event_time_s,
                cum_crank_revs=part.cum_crank_revs
                if part.cum_crank_revs is not None
                else self._last.cum_crank_revs,
                last_crank_event_time_s=part.last_crank_event_time_s
                if part.last_crank_event_time_s is not None
                else self._last.last_crank_event_time_s,
                power_watts=part.power_watts
                if part.power_watts is not None
                else self._last.power_watts,
                speed_mps=part.speed_mps if part.speed_mps is not None else self._last.speed_mps,
                wheel_rpm=part.wheel_rpm if part.wheel_rpm is not None else self._last.wheel_rpm,
                cadence_rpm=part.cadence_rpm
                if part.cadence_rpm is not None
                else self._last.cadence_rpm,
            )
        res = self._user_on_sample(self._last)
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)

    def _role_presence_from_client(self, client: BleakClient) -> dict[str, bool]:
        has_csc = False
        has_cps = False
        if hasattr(client, "services") and client.services is not None:
            with contextlib.suppress(Exception):
                has_csc = bool(client.services.get_characteristic(UUID_CSC_MEAS))
            with contextlib.suppress(Exception):
                has_cps = bool(client.services.get_characteristic(UUID_CP_MEAS))
        return {"cps": has_cps, "csc": has_csc}

    def _format_roles_for_status(self, roles):
        order = ["csc", "cps"]
        present = [r for r in order if r in roles]
        extras = sorted(set(roles) - set(order))
        return ",".join(present + extras)
