from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from bleakheart import HeartRate

from bleaksport.models import HeartRateSample
from bleaksport.mux_base import MuxBase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from bleak import BleakClient
    from bleak.backends.device import BLEDevice

# 16-bit SIG-assigned UUID for the standard Heart Rate Service
HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
UUID_HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"


class HeartRateSession:
    """
    Subscribes to the Heart Rate Measurement characteristic on an already-connected
    BleakClient and emits HeartRateSample via callbacks.

    Wraps bleakheart's HeartRate for parsing; mirrors the RunningSession API.
    """

    def __init__(self) -> None:
        self._callbacks: list[Callable[[HeartRateSample], Awaitable[None] | None]] = []
        self._started = False
        self._hr: HeartRate | None = None
        self._queue: asyncio.Queue | None = None
        self._consumer_task: asyncio.Task | None = None

    def on_heart_rate(self, cb: Callable[[HeartRateSample], Awaitable[None] | None]) -> None:
        """Register a callback for new heart rate samples."""
        self._callbacks.append(cb)

    async def start(self, client: BleakClient) -> None:
        """Subscribe to heart rate notifications on an already-connected client."""
        if self._started:
            return

        self._queue = asyncio.Queue()
        self._hr = HeartRate(client, queue=self._queue, instant_rate=True, unpack=True)
        await self._hr.start_notify()

        # Start a background task to drain the queue and emit samples
        self._consumer_task = asyncio.create_task(self._consume())
        self._started = True

    async def stop(self, client: BleakClient) -> None:
        """Unsubscribe from notifications and clean up."""
        if not self._started:
            return

        # Signal the consumer to exit
        if self._queue is not None:
            self._queue.put_nowait(("QUIT",))

        if self._consumer_task is not None:
            with contextlib.suppress(Exception):
                await self._consumer_task
            self._consumer_task = None

        if self._hr is not None:
            with contextlib.suppress(Exception):
                await self._hr.stop_notify()
            self._hr = None

        self._queue = None
        self._started = False

    async def _consume(self) -> None:
        """Drain the bleakheart queue and emit HeartRateSample for each frame."""
        if self._queue is None:
            return

        while True:
            event = await self._queue.get()
            if event[0] == "QUIT":
                break

            try:
                # bleakheart unpack=True format: ("DATA", t_ns, (bpm, rr), energy)
                _, t_ns, (bpm, rr), energy = event
                t_ms = t_ns / 1e6

                sample = HeartRateSample(
                    timestamp_ms=t_ms,
                    heart_rate_bpm=int(bpm),
                    rr_interval_ms=float(rr) if rr is not None else None,
                    energy_expended_kcal=float(energy) if energy is not None else None,
                )
                self._emit(sample)
            except Exception as exc:
                print(f"HeartRateSession parse error: {exc!r}")

    def _emit(self, sample: HeartRateSample) -> None:
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
                print(f"HeartRateSession callback error: {exc!r}")

        task.add_done_callback(_done)


class HeartRateMux(MuxBase):
    """
    Manages a single BLE heart rate monitor with automatic reconnection.

    Roles:
      - 'hr' → Heart Rate Service (UUID 0x180D / 0x2A37)

    on_link(addr, connected, {'hr': bool})

    Usage::

        mux = HeartRateMux(
            addr="AA:BB:CC:DD:EE:FF",
            on_sample=lambda s: print(s.heart_rate_bpm),
            on_status=lambda msg: print(msg),
        )
        await mux.start()   # blocks until stop() is called
    """

    def __init__(
        self,
        *,
        addr: str | BLEDevice | None = None,
        name: str | None = None,
        on_sample: Callable[[HeartRateSample], Awaitable[None] | None] | None = None,
        on_status: Callable[[str], None] | None = None,
        ble_lock: asyncio.Lock | None = None,
        reconnect_backoff_s: float = 2.0,
        on_link: Callable[[str, bool, dict[str, bool]], None] | None = None,
    ) -> None:
        def _addr_of(x):
            if x is None:
                return None
            if isinstance(x, str):
                return x
            return getattr(x, "address", None)

        roles_to_addrs = {"hr": _addr_of(addr)}

        super().__init__(
            roles_to_addrs=roles_to_addrs,
            on_status=on_status or (lambda _m: None),
            ble_lock=ble_lock,
            reconnect_backoff_s=reconnect_backoff_s,
            on_link=on_link or (lambda *_: None),
        )

        self._user_on_sample = on_sample or (lambda _s: None)

    # ---- MuxBase overrides ----

    async def _make_session(self, client: BleakClient) -> HeartRateSession:
        sess = HeartRateSession()
        sess.on_heart_rate(self._on_partial_sample)
        return sess

    async def _start_session(self, session: HeartRateSession, client: BleakClient) -> None:
        await session.start(client)

    async def _stop_session(self, session: HeartRateSession, client: BleakClient) -> None:
        await session.stop(client)

    def _on_partial_sample(self, part: HeartRateSample) -> None:
        res = self._user_on_sample(part)
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)

    def _role_presence_from_client(self, client: BleakClient) -> dict[str, bool]:
        has_hr = False
        if hasattr(client, "services") and client.services is not None:
            with contextlib.suppress(Exception):
                has_hr = bool(client.services.get_characteristic(UUID_HR_MEASUREMENT))
        return {"hr": has_hr}

    def _format_roles_for_status(self, roles: Iterable[str]) -> str:
        return "hr" if "hr" in roles else "-"