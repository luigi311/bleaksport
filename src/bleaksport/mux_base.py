from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING, Any

from bleak import BleakClient, BleakError

from bleaksport.linux_bluez import bluez_disconnect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

INPROGRESS_RE = re.compile(r"InProgress", re.IGNORECASE)


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
        on_link: Callable[[str, bool, bool, bool], None] | None = None,
    ) -> None:
        self._on_status = on_status or (lambda _m: None)
        self._on_link = on_link or (lambda *_: None)
        self._ble_lock = ble_lock or asyncio.Lock()
        self._reconnect_backoff_s = reconnect_backoff_s

        # Group desired roles by BLE address (strings only; extract .address if object)
        def _addr_of(x):
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
        if not self._roles_by_addr:
            self._on_status(f"{type(self).__name__}: no devices configured")
            return

        self._stop_evt.clear()
        for addr, roles in self._roles_by_addr.items():
            self._tasks.append(asyncio.create_task(self._run_device(addr, roles)))
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stop_evt.set()
        await asyncio.sleep(0)  # let loops observe stop

        for t in self._tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Stop sessions + disconnect
        for addr, sess in list(self._sessions.items()):
            client = self._clients.get(addr)
            if client:
                with contextlib.suppress(Exception):
                    await self._stop_session(sess, client)
        for addr, client in list(self._clients.items()):
            with contextlib.suppress(Exception):
                await client.disconnect()
            with contextlib.suppress(Exception):
                await bluez_disconnect(addr)

        self._sessions.clear()
        self._clients.clear()

    # ---- core loop per address ----
    async def _run_device(self, addr: str, roles: set[str]) -> None:
        while not self._stop_evt.is_set():
            client: BleakClient | None = None
            try:
                # Connect + start session under the BLE lock
                async with self._ble_lock:
                    client = BleakClient(addr, disconnected_callback=lambda _c: None)
                    await client.connect()

                    session = await self._make_session(client)
                    await self._start_session(session, client)

                self._clients[addr] = client
                self._sessions[addr] = session

                # Best-effort service discovery for role presence
                with contextlib.suppress(Exception):
                    if hasattr(client, "get_services"):
                        await client.get_services()

                role_presence = {}
                if hasattr(client, "services") and client.services is not None:
                    with contextlib.suppress(Exception):
                        role_presence = self._role_presence_from_client(client)

                self._on_status(f"Connected: {addr} ({self._format_roles_for_status(roles)})")
                # on_link signature is user-defined per subclass; common case: (addr, connected, *role_presence_by_order)
                try:
                    if role_presence:
                        self._on_link(
                            addr,
                            True,
                            *[role_presence.get(r, False) for r in sorted(role_presence)],
                        )
                    else:
                        self._on_link(addr, True, False, False)
                except Exception:
                    # Don't break if user provided a different arity; it’s just telemetry.
                    pass

                # Stay alive until disconnected or stop
                while client.is_connected and not self._stop_evt.is_set():
                    await asyncio.sleep(1.0)

            except BleakError as e:
                msg = str(e)
                if INPROGRESS_RE.search(msg):
                    await asyncio.sleep(1.5)
                else:
                    self._on_status(f"BLE error @ {addr}: {e}")
            except Exception as e:
                self._on_status(f"Unexpected error @ {addr}: {type(e).__name__}: {e}")
            finally:
                # Clean shutdown (serialize under lock; then tell BlueZ)
                with contextlib.suppress(Exception):
                    self._on_link(addr, False, False, False)

                try:
                    async with self._ble_lock:
                        if addr in self._sessions:
                            with contextlib.suppress(Exception):
                                await self._stop_session(
                                    self._sessions[addr], self._clients.get(addr)
                                )
                            self._sessions.pop(addr, None)
                        if addr in self._clients:
                            with contextlib.suppress(Exception):
                                await self._clients[addr].disconnect()
                            self._clients.pop(addr, None)
                    with contextlib.suppress(Exception):
                        await bluez_disconnect(addr)
                except Exception:
                    pass

                if not self._stop_evt.is_set():
                    await asyncio.sleep(self._reconnect_backoff_s)
