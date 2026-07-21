from __future__ import annotations

import asyncio
import contextlib
import re
import struct
from typing import TYPE_CHECKING, Any

from bleak import BleakClient, BleakError
from loguru import logger

from bleaksport.core import s
from bleaksport.linux_bluez import bluez_disconnect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

INPROGRESS_RE = re.compile(r"InProgress", re.IGNORECASE)
UUID_SC_CONTROL_POINT = s(0x2A55)


class MuxBase:
    """
    Shared orchestration for BLE devices exposing one or more roles (e.g. 'rsc', 'cps').
    Subclasses must implement:
      - _make_session(client) -> session obj (and wire its callbacks to self._on_partial_sample)
      - _start_session(session, client)
      - _stop_session(session, client)
      - _on_partial_sample(part)  -> fuse+emit to user callback
      - _role_presence_from_client(client) -> dict[str,bool] for advertised roles
      - _format_roles_for_status(roles:set[str]) -> str  (optional).
    """

    def __init__(
        self,
        *,
        roles_to_addrs: Mapping[str, str | None | Any],
        on_status,
        ble_lock: asyncio.Lock | None = None,
        reconnect_backoff_s: float = 2.0,
        on_link: Callable[[str, bool, dict[str, bool]], None] | None = None,
    ) -> None:
        self._on_status = on_status or (lambda _m: None)
        self._on_link = on_link or (lambda *_: None)
        self._ble_lock = ble_lock or asyncio.Lock()
        self._reconnect_backoff_s = reconnect_backoff_s

        # Group desired roles by BLE address (strings only; extract .address if object)
        def _addr_of(x) -> str | None:
            if not x:
                return None
            if isinstance(x, str):
                return x
            return getattr(x, "address", None)

        self._roles_by_addr: dict[str, set[str]] = {}
        for role, maybe_addr in roles_to_addrs.items():
            addr = _addr_of(maybe_addr)
            if addr:
                self._roles_by_addr.setdefault(addr, set()).add(role)

        # Runtime
        self._clients: dict[str, BleakClient] = {}
        self._sessions: dict[str, Any] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop_evt = asyncio.Event()

    # ---- abstract hooks (subclasses must implement) ----
    async def _make_session(self, client: BleakClient) -> Any:  # session
        raise NotImplementedError

    async def _start_session(self, session: Any, client: BleakClient) -> None:
        raise NotImplementedError

    async def _stop_session(self, session: Any, client: BleakClient) -> None:
        raise NotImplementedError

    def _on_partial_sample(self, part: Any) -> None:
        raise NotImplementedError

    def _role_presence_from_client(self, client: BleakClient) -> dict[str, bool]:
        """Return a mapping of role -> bool (e.g., {'rsc': True, 'cps': False})."""
        return {}

    def _format_roles_for_status(self, roles: Iterable[str]) -> str:
        return ",".join(sorted(roles)) or "-"

    # ---- public API ----
    async def start(self) -> None:
        """
        Start the multiplexed BLE sessions.
        This will run until stop() is called or an unhandled exception occurs.
        """
        if not self._roles_by_addr:
            self._on_status(f"{type(self).__name__}: no devices configured")
            return

        self._stop_evt.clear()
        for addr, roles in self._roles_by_addr.items():
            self._tasks.append(asyncio.create_task(self._run_device(addr, roles)))
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        """
        Stop all sessions and disconnect.
        This will attempt a clean shutdown of all sessions and BLE connections
        but will not raise if errors occur.
        """
        self._stop_evt.set()
        await asyncio.sleep(0)

        logger.debug(f"Stop event received, cancelling {len(self._tasks)} device tasks")
        for t in self._tasks:
            t.cancel()

        with contextlib.suppress(Exception):
            await asyncio.gather(*self._tasks, return_exceptions=True)

        self._tasks.clear()

    # ---- core loop per address ----
    async def _run_device(self, addr: str, roles: set[str]) -> None:
        while not self._stop_evt.is_set():
            client: BleakClient | None = None
            session: Any = None
            needs_bluez_fallback = False
            try:
                # Connect + start session under the BLE lock
                async with self._ble_lock:
                    client = BleakClient(addr, disconnected_callback=lambda _c: None)
                    await client.connect()
                    # Register resources as soon as they exist. If session setup
                    # fails or the task is cancelled, finally can still close the
                    # Bleak client's per-connection D-Bus bus.
                    self._clients[addr] = client

                    session = await self._make_session(client)
                    self._sessions[addr] = session
                    await self._start_session(session, client)

                # Best-effort service discovery for role presence
                try:
                    if hasattr(client, "get_services"):
                        await client.get_services()
                except Exception as e:
                    logger.warning(f"Failed to get services for {addr} {e}")

                role_presence = {}
                if hasattr(client, "services") and client.services is not None:
                    with contextlib.suppress(Exception):
                        role_presence = self._role_presence_from_client(client)

                # on_link signature is user-defined per subclass; common case:
                # (addr, connected, *role_presence_by_order)
                try:
                    if role_presence:
                        self._on_link(
                            addr,
                            True,
                            role_presence,
                        )
                    else:
                        self._on_link(addr, True, {})
                except Exception as e:
                    logger.warning(f"on_link call failed for {addr} {e}")

                # Stay alive until disconnected or stop
                while client.is_connected and not self._stop_evt.is_set():
                    await asyncio.sleep(1.0)

            except BleakError as e:
                msg = str(e)
                if INPROGRESS_RE.search(msg):
                    needs_bluez_fallback = True
                    await asyncio.sleep(1.5)
                else:
                    self._on_status(f"Bleak error @ {addr}: {type(e).__name__}: {e}")
            except Exception as e:
                self._on_status(f"Unexpected error @ {addr}: {type(e).__name__}: {e}")
            finally:
                # Clean shutdown. The direct BlueZ call is only a fallback for
                # an InProgress state or a Bleak disconnect that did not finish.
                logger.debug(f"Cleaning up {addr} device")

                with contextlib.suppress(Exception):
                    self._on_link(addr, False, {})
                    logger.debug("on_link nulled")

                session_to_stop = self._sessions.pop(addr, None) or session
                client_to_disconnect = self._clients.pop(addr, None) or client

                async with self._ble_lock:
                    if session_to_stop is not None and client_to_disconnect is not None:
                        try:
                            logger.debug(f"Stopping sessions for {addr}")
                            await self._stop_session(session_to_stop, client_to_disconnect)
                            logger.debug(f"Session for {addr} stopped")
                        except Exception as e:
                            logger.warning(f"Stopping sessions failed for {addr} {e}")

                    if client_to_disconnect is not None:
                        try:
                            logger.debug(f"Disconnecting {addr}")
                            await client_to_disconnect.disconnect()
                            logger.debug(f"Disconnected {addr} successfully")
                        except Exception as e:
                            needs_bluez_fallback = True
                            logger.warning(f"Disconnect failed for {addr} {e}")

                        with contextlib.suppress(Exception):
                            needs_bluez_fallback |= bool(client_to_disconnect.is_connected)

                if needs_bluez_fallback:
                    try:
                        logger.debug(f"BlueZ fallback disconnecting {addr}")
                        await bluez_disconnect(addr)
                        logger.debug(f"BlueZ fallback disconnected {addr} successfully")
                    except Exception as e:
                        logger.warning(f"BlueZ fallback disconnect failed for {addr} {e}")

                if not self._stop_evt.is_set():
                    await asyncio.sleep(self._reconnect_backoff_s)

    async def _sc_cp_set_cumulative(
        self,
        client: BleakClient | None,
        value_u32: int,
        timeout_s: float = 3.0,
    ) -> bool:
        """
        SC Control Point (0x2A55) 'Set Cumulative Value' for RSCS/CSCS.
        - value_u32: raw uint32 parameter (RSCS: distance in 0.1 m; CSCS: wheel revs).

        Returns:
            bool: True on 'success' response, False otherwise.
        """
        if not client or not client.is_connected:
            return False

        OPCODE_SET_CUM = 0x01
        OPCODE_RSP = 0x10

        loop = asyncio.get_running_loop()
        ack = loop.create_future()

        def _on_ind(_h: int, data: bytearray) -> None:
            if (
                len(data) >= 3
                and data[0] == OPCODE_RSP
                and data[1] == OPCODE_SET_CUM
                and not ack.done()
            ):
                # result code 0x01 = success per spec
                ack.set_result(data[2] == 0x01)

        # Ensure indications on the SCP char
        with contextlib.suppress(Exception):
            await client.start_notify(UUID_SC_CONTROL_POINT, _on_ind)

        try:
            payload = bytes([OPCODE_SET_CUM]) + struct.pack("<I", int(value_u32) & 0xFFFFFFFF)
            await client.write_gatt_char(UUID_SC_CONTROL_POINT, payload, response=True)
            try:
                return bool(await asyncio.wait_for(ack, timeout=timeout_s))
            except TimeoutError:
                return False
        finally:
            with contextlib.suppress(Exception):
                await client.stop_notify(UUID_SC_CONTROL_POINT)
