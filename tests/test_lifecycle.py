# ruff: noqa: ANN001, ANN201, ANN202, ANN204, ARG002, D101, D102, I001, PT009, PT027, SIM117, SLF001

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from dbus_next import MessageType
from pyftms import ResultCode

from bleaksport import linux_bluez
from bleaksport.mux_base import MuxBase
from bleaksport.trainer import TrainerMux


class _FakeBus:
    def __init__(self, *, fail_connect=False, fail_call=False):
        self.fail_connect = fail_connect
        self.fail_call = fail_call
        self.disconnect_called = False
        self.wait_called = False

    async def connect(self):
        if self.fail_connect:
            msg = "connect failed"
            raise RuntimeError(msg)
        return self

    async def call(self, _message):
        if self.fail_call:
            msg = "method call failed"
            raise RuntimeError(msg)
        return type(
            "Reply",
            (),
            {"message_type": MessageType.METHOD_RETURN, "body": [], "error_name": None},
        )()

    def disconnect(self):
        self.disconnect_called = True

    async def wait_for_disconnect(self):
        self.wait_called = True


class BlueZDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_bus_is_closed_after_success(self):
        bus = _FakeBus()
        with (
            patch.object(linux_bluez, "_is_linux", return_value=True),
            patch.object(linux_bluez, "MessageBus", return_value=bus),
        ):
            await linux_bluez.bluez_disconnect("AA:BB:CC:DD:EE:FF")

        self.assertTrue(bus.disconnect_called)
        self.assertTrue(bus.wait_called)

    async def test_bus_is_closed_when_connect_fails(self):
        bus = _FakeBus(fail_connect=True)
        with (
            patch.object(linux_bluez, "_is_linux", return_value=True),
            patch.object(linux_bluez, "MessageBus", return_value=bus),
            self.assertRaises(RuntimeError),
        ):
            await linux_bluez.bluez_disconnect("AA:BB:CC:DD:EE:FF")

        self.assertTrue(bus.disconnect_called)
        self.assertTrue(bus.wait_called)

    async def test_bus_is_closed_when_method_call_fails(self):
        bus = _FakeBus(fail_call=True)
        with (
            patch.object(linux_bluez, "_is_linux", return_value=True),
            patch.object(linux_bluez, "MessageBus", return_value=bus),
            self.assertRaises(RuntimeError),
        ):
            await linux_bluez.bluez_disconnect("AA:BB:CC:DD:EE:FF")

        self.assertTrue(bus.disconnect_called)
        self.assertTrue(bus.wait_called)


class _FakeBleakClient:
    def __init__(self, *, disconnect_error=False):
        self.is_connected = False
        self.disconnect_error = disconnect_error
        self.disconnect_calls = 0
        self.services = None

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.disconnect_error:
            msg = "disconnect failed"
            raise RuntimeError(msg)
        self.is_connected = False


class _LifecycleMux(MuxBase):
    def __init__(self, *, start_error=None):
        super().__init__(roles_to_addrs={"sensor": "AA:BB"}, on_status=lambda _msg: None)
        self.start_error = start_error
        self.session_stopped = False

    async def _make_session(self, client):
        return object()

    async def _start_session(self, session, client):
        self._stop_evt.set()
        client.is_connected = False
        if self.start_error:
            raise self.start_error

    async def _stop_session(self, session, client):
        self.session_stopped = True


class MuxLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_session_setup_still_disconnects_client(self):
        client = _FakeBleakClient()
        mux = _LifecycleMux(start_error=RuntimeError("session setup failed"))
        fallback = AsyncMock()

        with (
            patch("bleaksport.mux_base.BleakClient", return_value=client),
            patch("bleaksport.mux_base.bluez_disconnect", fallback),
        ):
            await mux._run_device("AA:BB", {"sensor"})

        self.assertTrue(mux.session_stopped)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(mux._clients, {})
        self.assertEqual(mux._sessions, {})
        fallback.assert_not_awaited()

    async def test_failed_bleak_disconnect_uses_bluez_fallback(self):
        client = _FakeBleakClient(disconnect_error=True)
        mux = _LifecycleMux()
        fallback = AsyncMock()

        with (
            patch("bleaksport.mux_base.BleakClient", return_value=client),
            patch("bleaksport.mux_base.bluez_disconnect", fallback),
        ):
            await mux._run_device("AA:BB", {"sensor"})

        fallback.assert_awaited_once_with("AA:BB")


class _TrainerBleakClient:
    def __init__(self):
        self.disconnect_calls = 0

    async def disconnect(self):
        self.disconnect_calls += 1


class _FakeMachine:
    def __init__(self, *, connect_error=None):
        self.connect_error = connect_error
        self._cli = _TrainerBleakClient()
        self.disconnect_calls = 0
        self.disconnect_callback = None
        self.machine_type = object()
        self.supported_properties = []
        self.supported_settings = []
        self.supported_ranges = {}
        self.target_resistance = None

    def set_disconnect_callback(self, callback):
        self.disconnect_callback = callback

    async def connect(self):
        if self.connect_error:
            raise self.connect_error

    async def disconnect(self):
        self.disconnect_calls += 1

    async def start_resume(self):
        return ResultCode.SUCCESS

    async def set_target_resistance(self, level):
        self.target_resistance = level
        return ResultCode.SUCCESS


class _Device:
    address = "AA:BB"


class TrainerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_machine_setup_remains_owned_for_cleanup(self):
        machine = _FakeMachine(connect_error=RuntimeError("feature read failed"))
        mux = TrainerMux(addr=_Device(), machine_type=object())

        with patch("bleaksport.trainer.get_client", return_value=machine):
            with self.assertRaises(RuntimeError):
                await mux._connect_and_stream()

        self.assertIs(mux._machine, machine)
        self.assertTrue(await mux._disconnect())
        self.assertEqual(machine.disconnect_calls, 1)
        self.assertEqual(machine._cli.disconnect_calls, 1)

    async def test_machine_disconnect_callback_clears_link_state(self):
        machine = _FakeMachine()
        mux = TrainerMux(addr=_Device(), machine_type=object())

        with patch("bleaksport.trainer.get_client", return_value=machine):
            await mux._connect_and_stream()

        self.assertTrue(mux.is_connected)
        machine.disconnect_callback(machine)
        self.assertFalse(mux.is_connected)
        await mux._disconnect()


if __name__ == "__main__":
    unittest.main()
