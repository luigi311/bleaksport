import contextlib
import platform
import subprocess

from dbus_next import BusType
from dbus_next.aio import MessageBus
from loguru import logger


def _is_linux() -> bool:
    return platform.system().lower() == "linux"

def mac_to_path(mac: str, adapter: str = "hci0") -> str:
    return f"/org/bluez/{adapter}/dev_{mac.replace(':', '_').upper()}"

async def bluez_disconnect(mac: str) -> None:
    """Politely ask BlueZ to disconnect the device (does not clear bonding)."""
    if not _is_linux():
        return

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    path = mac_to_path(mac)
    introspection = await bus.introspect("org.bluez", path)
    obj = bus.get_proxy_object("org.bluez", path, introspection)

    device = obj.get_interface("org.bluez.Device1")

    await device.call_disconnect()
