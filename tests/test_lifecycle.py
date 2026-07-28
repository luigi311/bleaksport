# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, ANN204, ARG002, D101, D102, I001, PT009, PT027, SIM117, SLF001

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bleak import BleakError
from dbus_fast import DBusError, Message, MessageType
from pyftms import ResultCode

from bleaksport import linux_bluez
from bleaksport.mux_base import MuxBase
from bleaksport.trainer import TrainerMux


class _FakeBus:
    def __init__(
        self,
        *,
        fail_connect=False,
        fail_call=False,
        error_reply=False,
        fail_disconnect=False,
    ):
        self.fail_connect = fail_connect
        self.fail_call = fail_call
        self.error_reply = error_reply
        self.fail_disconnect = fail_disconnect
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
        if self.error_reply:
            return Message(
                message_type=MessageType.ERROR,
                reply_serial=_message.serial or 1,
                error_name="org.bluez.Error.Failed",
                signature="s",
                body=["BlueZ rejected disconnect"],
            )
        return Message(
            message_type=MessageType.METHOD_RETURN,
            reply_serial=_message.serial or 1,
        )

    def disconnect(self):
        self.disconnect_called = True
        if self.fail_disconnect:
            msg = "disconnect cleanup failed"
            raise RuntimeError(msg)

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

    async def test_bluez_error_reply_raises_dbus_error_and_closes_bus(self):
        bus = _FakeBus(error_reply=True)
        with (
            patch.object(linux_bluez, "_is_linux", return_value=True),
            patch.object(linux_bluez, "MessageBus", return_value=bus),
            self.assertRaises(DBusError),
        ):
            await linux_bluez.bluez_disconnect("AA:BB:CC:DD:EE:FF")

        self.assertTrue(bus.disconnect_called)
        self.assertTrue(bus.wait_called)

    async def test_disconnect_cleanup_error_does_not_replace_original_error(self):
        bus = _FakeBus(fail_call=True, fail_disconnect=True)
        with (
            patch.object(linux_bluez, "_is_linux", return_value=True),
            patch.object(linux_bluez, "MessageBus", return_value=bus),
            self.assertRaisesRegex(RuntimeError, "method call failed"),
        ):
            await linux_bluez.bluez_disconnect("AA:BB:CC:DD:EE:FF")

        self.assertTrue(bus.disconnect_called)
        self.assertTrue(bus.wait_called)


class _FakeBleakClient:
    def __init__(self, *, connect_error=None, disconnect_error=False):
        self.connect_error = connect_error
        self.is_connected = False
        self.disconnect_error = disconnect_error
        self.disconnect_calls = 0
        self.services = None

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.is_connected = True

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.disconnect_error:
            msg = "disconnect failed"
            raise RuntimeError(msg)
        self.is_connected = False


class _LifecycleMux(MuxBase):
    def __init__(self, *, start_error=None):
        self.statuses = []
        self.status_event = asyncio.Event()

        def on_status(message):
            self.statuses.append(message)
            self.status_event.set()

        super().__init__(roles_to_addrs={"sensor": "AA:BB"}, on_status=on_status)
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
    async def test_non_inprogress_bleak_error_reports_device_context(self):
        client = _FakeBleakClient(connect_error=BleakError("connection rejected"))
        mux = _LifecycleMux()

        with patch("bleaksport.mux_base.BleakClient", return_value=client):
            task = asyncio.create_task(mux._run_device("AA:BB", {"sensor"}))
            await asyncio.wait_for(mux.status_event.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(
            mux.statuses,
            ["Bleak error @ AA:BB: BleakError: connection rejected"],
        )

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

    async def test_stale_machine_notifications_are_ignored_after_disconnect(self):
        machine = _FakeMachine()
        samples = []
        captured_callback = None

        def make_client(*_args, **kwargs):
            nonlocal captured_callback
            captured_callback = kwargs["on_ftms_event"]
            return machine

        mux = TrainerMux(addr=_Device(), machine_type=object(), on_sample=samples.append)
        with patch("bleaksport.trainer.get_client", side_effect=make_client):
            await mux._connect_and_stream()

        await mux._disconnect()
        captured_callback(
            SimpleNamespace(
                event_id="update",
                event_data={"heart_rate": 99, "resistance_level": 19},
            ),
        )

        self.assertEqual(samples, [])


if __name__ == "__main__":
    unittest.main()
