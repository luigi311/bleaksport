import contextlib
import platform
import subprocess

from loguru import logger


def _is_linux() -> bool:
    return platform.system().lower() == "linux"


async def bluez_disconnect(mac: str) -> None:
    """Politely ask BlueZ to disconnect the device (does not clear bonding)."""
    if not _is_linux():
        return
    try:
        subprocess.run(
            ["bluetoothctl", "disconnect", mac],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning(f"Bluez disconnect failed for {mac} {e}")
