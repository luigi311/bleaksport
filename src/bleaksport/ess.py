from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bleaksport.core import s
from bleaksport.mux_base import MuxBase
from bleaksport.utils import altitude_from_pressure

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from bleak import BleakClient

# ---- UUIDs (Environmental Sensing) ----
UUID_ESS = s(0x181A)
UUID_PRESSURE = s(0x2A6D)     # uint32, Pascals (Pa)
UUID_TEMPERATURE = s(0x2A6E)  # sint16, 0.01°C
UUID_HUMIDITY = s(0x2A6F)     # uint16, 0.01%RH

@dataclass
class ESSample:
    timestamp: float
    pressure_pa: float | None = None
    altitude_m: float | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None

class ESSession:
    """
    Subscribes (or polls) Environmental Sensing characteristics on an already-connected client.
    Emits ESSample via callbacks. If notifications aren't supported, falls back to polling.
    """

    CHAR_PRESSURE = UUID_PRESSURE
    CHAR_TEMP = UUID_TEMPERATURE
    CHAR_HUM = UUID_HUMIDITY

    def __init__(
        self,
        *,
        sea_level_pa: float = 101_325.0,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._callbacks: list[Callable[[ESSample], Awaitable[None] | None]] = []
        self._last: ESSample | None = None
        self._started = False
        self._sea_level_pa = sea_level_pa
        self._poll_interval_s = poll_interval_s
        self._poll_task: asyncio.Task | None = None
        self._notify_enabled: dict[str, bool] = {}

    def on_ess(self, cb: Callable[[ESSample], Awaitable[None] | None]) -> None:
        self._callbacks.append(cb)

    async def start(self, client: BleakClient) -> None:
        if self._started:
            return

        # Try to subscribe to notify if the characteristics exist.
        for char in (self.CHAR_PRESSURE, self.CHAR_TEMP, self.CHAR_HUM):
            with contextlib.suppress(Exception):
                await client.start_notify(char, self._on_notify)
                self._notify_enabled[char] = True

        # If none notified, start polling loop (some ESS servers are read-only).
        if not any(self._notify_enabled.values()):
            self._poll_task = asyncio.create_task(self._poll_loop(client))

        self._started = True

    async def stop(self, client: BleakClient) -> None:
        if not self._started:
            return
        for char in (self.CHAR_PRESSURE, self.CHAR_TEMP, self.CHAR_HUM):
            with contextlib.suppress(Exception):
                await client.stop_notify(char)
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(Exception):
                await self._poll_task
            self._poll_task = None
        self._started = False

    # ---- polling fallback ----
    async def _poll_loop(self, client: BleakClient) -> None:
        while client.is_connected:
            await self._read_once(client)
            await asyncio.sleep(self._poll_interval_s)

    async def _read_once(self, client: BleakClient) -> None:
        now = time.time()
        pressure_pa = temperature_c = humidity_pct = None

        with contextlib.suppress(Exception):
            data = await client.read_gatt_char(self.CHAR_PRESSURE)
            pressure_pa = self._parse_pressure(data)
        with contextlib.suppress(Exception):
            data = await client.read_gatt_char(self.CHAR_TEMP)
            temperature_c = self._parse_temperature(data)
        with contextlib.suppress(Exception):
            data = await client.read_gatt_char(self.CHAR_HUM)
            humidity_pct = self._parse_humidity(data)

        if any(v is not None for v in (pressure_pa, temperature_c, humidity_pct)):
            self._emit(self._merge(now, pressure_pa, temperature_c, humidity_pct))

    # ---- notify path ----
    def _on_notify(self, _h: int, data: bytearray) -> None:
        # We don't get which UUID here; rely on the handle mapping only if needed.
        # Instead, we parse by payload size heuristics:
        #  - Pressure: 4 bytes (uint32)
        #  - Temperature: 2 bytes (sint16, 0.01°C)
        #  - Humidity: 2 bytes (uint16, 0.01%)
        ts = time.time()
        pressure_pa = temperature_c = humidity_pct = None
        try:
            if len(data) == 4:
                pressure_pa = self._parse_pressure(data)
            elif len(data) == 2:
                # Can't distinguish temp vs humidity by length alone; try both safely.
                # Prefer temp interpretation if sign bit is set (typical around 20°C).
                temperature_c = self._parse_temperature(data)
                if temperature_c is None:
                    humidity_pct = self._parse_humidity(data)
            else:
                # Some stacks may send multiple fields; try pressure first then temp/hum slices.
                if len(data) >= 4:
                    pressure_pa = self._parse_pressure(data[:4])
                if len(data) >= 6:
                    temperature_c = self._parse_temperature(data[4:6])
                if len(data) >= 8:
                    humidity_pct = self._parse_humidity(data[6:8])
        except Exception:
            return

        if any(v is not None for v in (pressure_pa, temperature_c, humidity_pct)):
            self._emit(self._merge(ts, pressure_pa, temperature_c, humidity_pct))

    # ---- parsers (Bluetooth GATT formats) ----
    @staticmethod
    def _parse_pressure(data: bytes) -> float | None:
        if len(data) < 4:
            return None
        # org.bluetooth.characteristic.pressure → uint32 in Pascals (Pa).
        # Many devices use full Pa. (If you ever see deciPa, scale by 0.1.)
        raw = struct.unpack_from("<I", data, 0)[0]
        return float(raw)

    @staticmethod
    def _parse_temperature(data: bytes) -> float | None:
        if len(data) < 2:
            return None
        raw = struct.unpack_from("<h", data, 0)[0]
        return raw / 100.0  # 0.01 °C resolution

    @staticmethod
    def _parse_humidity(data: bytes) -> float | None:
        if len(data) < 2:
            return None
        raw = struct.unpack_from("<H", data, 0)[0]
        return raw / 100.0  # 0.01 %RH resolution

    # ---- emit/fuse ----
    def _merge(
        self,
        ts: float,
        pressure_pa: float | None,
        temperature_c: float | None,
        humidity_pct: float | None,
    ) -> ESSample:
        if self._last is None:
            alt = altitude_from_pressure(pressure_pa, self._sea_level_pa) if pressure_pa else None
            return ESSample(
                timestamp=ts,
                pressure_pa=pressure_pa,
                altitude_m=alt,
                temperature_c=temperature_c,
                humidity_pct=humidity_pct,
            )

        sample = ESSample(
            timestamp=ts or self._last.timestamp,
            pressure_pa=pressure_pa if pressure_pa is not None else self._last.pressure_pa,
            temperature_c=(
                temperature_c if temperature_c is not None else self._last.temperature_c
            ),
            humidity_pct=(humidity_pct if humidity_pct is not None else self._last.humidity_pct),
            altitude_m=None,  # computed below
        )
        if sample.pressure_pa is not None:
            sample.altitude_m = altitude_from_pressure(sample.pressure_pa, self._sea_level_pa)
        else:
            sample.altitude_m = self._last.altitude_m
        return sample

    def _emit(self, sample: ESSample) -> None:
        self._last = sample

        async def _dispatch() -> None:
            tasks = []
            for cb in self._callbacks:
                res = cb(sample)
                if asyncio.iscoroutine(res):
                    tasks.append(asyncio.create_task(res))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        t = asyncio.create_task(_dispatch())
        t.add_done_callback(lambda tt: tt.exception())

class ESSMux(MuxBase):
    """
    Single-role mux for ESS devices.
      - role 'ess' → Environmental Sensing (pressure/altitude + optional temp/humidity).

    on_link(addr, connected, has_pressure, has_temp, has_hum)
    """

    def __init__(
        self,
        *,
        ess_addr: str | None,
        sea_level_pa: float = 101_325.0,
        poll_interval_s: float = 1.0,
        on_sample: Callable[[ESSample], Awaitable[None] | None] | None = None,
        on_status: Callable[[str], None] | None = None,
        ble_lock: asyncio.Lock | None = None,
        reconnect_backoff_s: float = 2.0,
        on_link: Callable[[str, bool, bool, bool, bool], None] | None = None,
    ) -> None:
        super().__init__(
            roles_to_addrs={"ess": ess_addr},
            on_status=on_status or (lambda _m: None),
            ble_lock=ble_lock,
            reconnect_backoff_s=reconnect_backoff_s,
            on_link=on_link or (lambda *_: None),
        )
        self._user_on_sample = on_sample or (lambda _s: None)
        self._sea_level_pa = sea_level_pa
        self._poll_interval_s = poll_interval_s
        self._last: ESSample | None = None

    async def _make_session(self, client: BleakClient) -> ESSession:
        sess = ESSession(sea_level_pa=self._sea_level_pa, poll_interval_s=self._poll_interval_s)
        sess.on_ess(self._on_partial_sample)
        return sess

    async def _start_session(self, session: ESSession, client: BleakClient) -> None:
        await session.start(client)

    async def _stop_session(self, session: ESSession, client: BleakClient) -> None:
        await session.stop(client)

    def _on_partial_sample(self, part: ESSample) -> None:
        self._last = replace(part) if self._last is None else ESSample(
            timestamp=part.timestamp or self._last.timestamp,
            pressure_pa=part.pressure_pa if part.pressure_pa is not None else self._last.pressure_pa,
            altitude_m=part.altitude_m if part.altitude_m is not None else self._last.altitude_m,
            temperature_c=(
                part.temperature_c
                if part.temperature_c is not None
                else self._last.temperature_c
            ),
            humidity_pct=(
                part.humidity_pct if part.humidity_pct is not None else self._last.humidity_pct
            ),
        )
        res = self._user_on_sample(self._last)
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)

    def _role_presence_from_client(self, client: BleakClient) -> dict[str, bool]:
        has_p = has_t = has_h = False
        if hasattr(client, "services") and client.services is not None:
            with contextlib.suppress(Exception):
                has_p = bool(client.services.get_characteristic(UUID_PRESSURE))
            with contextlib.suppress(Exception):
                has_t = bool(client.services.get_characteristic(UUID_TEMPERATURE))
            with contextlib.suppress(Exception):
                has_h = bool(client.services.get_characteristic(UUID_HUMIDITY))
        # Return keys in the stable order we’ll expand in on_link
        return {"pressure": has_p, "temperature": has_t, "humidity": has_h}

    def _format_roles_for_status(self, roles):
        # For status strings/logs
        order = ["pressure", "temperature", "humidity"]
        present = [r for r in order if r in roles]
        extras = sorted(set(roles) - set(order))
        return ",".join(present + extras)
