import contextlib
import platform

from dbus_fast import BusType, DBusError, Message, MessageType
from dbus_fast.aio import MessageBus


def _is_linux() -> bool:
    return platform.system().lower() == "linux"


def mac_to_path(mac: str, adapter: str = "hci0") -> str:
    return f"/org/bluez/{adapter}/dev_{mac.replace(':', '_').upper()}"


async def bluez_disconnect(mac: str) -> None:
    """Politely ask BlueZ to disconnect the device (does not clear bonding)."""
    if not _is_linux():
        return

    # MessageBus opens its socket during construction, before connect() is
    # awaited, so it must be owned before entering the try block.  Always wait
    # for finalization so repeated reconnect attempts cannot accumulate system
    # bus connections or selector readers.
    bus = MessageBus(bus_type=BusType.SYSTEM)
    try:
        await bus.connect()

        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=mac_to_path(mac),
                interface="org.bluez.Device1",
                member="Disconnect",
            )
        )
        if reply.message_type == MessageType.ERROR:
            error_name = reply.error_name or "org.bluez.Error.Failed"
            text = reply.body[0] if reply.body else error_name
            raise DBusError(error_name, text, reply=reply)
    finally:
        bus.disconnect()
        with contextlib.suppress(Exception):
            await bus.wait_for_disconnect()
