from __future__ import annotations

from typing import TYPE_CHECKING

from bleak import BleakScanner
from loguru import logger
from pyftms import discover_ftms_devices as pyftms_discover_ftms_devices

from bleaksport.core import s

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice
    from pyftms import MachineType

UUID_RSCS = s(0x1814)
UUID_CSCS = s(0x1816)
UUID_CPS = s(0x1818)

async def discover_speed_cadence_devices(
    scan_timeout: float = 5.0,
    name_contains: str | None = None,
) -> list[BLEDevice]:
    """Find devices advertising RSCS or CSCS (e.g., footpods, bike speed/cadence sensors)."""
    devices = await BleakScanner.discover(
        timeout=scan_timeout,
        service_uuids=[UUID_RSCS, UUID_CSCS],
    )
    return _filter_by_name(devices, name_contains)


async def discover_running_devices(
    scan_timeout: float = 5.0,
    name_contains: str | None = None,
) -> list[BLEDevice]:
    """Find devices advertising RSCS (e.g., footpods)."""
    devices = await BleakScanner.discover(timeout=scan_timeout, service_uuids=[UUID_RSCS])
    return _filter_by_name(devices, name_contains)


async def discover_cycling_devices(
    scan_timeout: float = 5.0,
    name_contains: str | None = None,
) -> list[BLEDevice]:
    """Find devices advertising CSCS (e.g., bike speed/cadence sensors)."""
    devices = await BleakScanner.discover(timeout=scan_timeout, service_uuids=[UUID_CSCS])
    return _filter_by_name(devices, name_contains)


async def discover_power_devices(
    scan_timeout: float = 5.0,
    name_contains: str | None = None,
) -> list[BLEDevice]:
    """Find devices advertising CPS (e.g., bike power meters or Stryd pods exposing power)."""
    devices = await BleakScanner.discover(timeout=scan_timeout, service_uuids=[UUID_CPS])
    return _filter_by_name(devices, name_contains)


async def discover_ftms_devices(
    scan_timeout: float = 5.0,
    name_contains: str | None = None,
) -> list[tuple[BLEDevice, MachineType]]:
    """Find devices advertising FTMS (Fitness Machine Service)."""
    devices = []
    async for dev, mtype in pyftms_discover_ftms_devices(discover_time=scan_timeout):
        logger.debug(f"Discovered FTMS device: {dev} with machine type {mtype}")
        if name_contains:
            n = (dev.name or "").lower()
            if name_contains.lower() not in n:
                logger.debug(f"Skipping {dev} due to name filter '{name_contains}' not in '{n}'")
                continue
        devices.append((dev, mtype))

    return devices


def _filter_by_name(devices: list[BLEDevice], name_contains: str | None) -> list[BLEDevice]:
    if not name_contains:
        return devices
    needle = name_contains.lower()
    return [d for d in devices if d.name and needle in d.name.lower()]
