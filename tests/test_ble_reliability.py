import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pyplejd.ble import ble_characteristics as gatt
from pyplejd.ble import LastData, PlejdMesh
from pyplejd.ble.crypto import encrypt_decrypt


class FakeClient:
    def __init__(self):
        self.is_connected = True
        self.disconnect_callback = None
        self.notify_callbacks = {}
        self.disconnect_calls = 0

    def set_disconnected_callback(self, callback):
        self.disconnect_callback = callback

    async def start_notify(self, characteristic, callback):
        self.notify_callbacks[characteristic] = callback

    async def stop_notify(self, _characteristic):
        return None

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False
        if self.disconnect_callback:
            self.disconnect_callback(self)


class Manager:
    def __init__(self):
        self.button_events_enabled = False
        self.reconnect_after_control_write = True
        self.control_confirmation_timeout = 0
        self.control_write_flush_delay = 0
        self.connect_callback = Mock()
        self.lastdata_callback = AsyncMock()
        self.lightlevel_callback = AsyncMock()


def make_node(address="001122334455", device_addresses=()):
    node = SimpleNamespace(
        BLEaddress=address,
        connectable=True,
        rssi=-45,
        bleDevice=SimpleNamespace(address=address, name="Plejd"),
        is_gateway=False,
        devices=[SimpleNamespace(address=item) for item in device_addresses],
    )
    node.update = Mock()
    return node


class PlejdMeshReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = Manager()
        self.mesh = PlejdMesh(self.manager)
        self.mesh.set_key("00" * 16)

    async def test_direct_authentication_does_not_double_connect(self):
        node = make_node()
        client = FakeClient()
        self.mesh.expect_device(node)

        with (
            patch("pyplejd.ble.establish_connection", new=AsyncMock(return_value=client)) as establish,
            patch.object(self.mesh, "_authenticate", new=AsyncMock(return_value=True)),
            patch.object(self.mesh, "poll", new=AsyncMock()),
        ):
            self.assertTrue(await self.mesh.connect())

        self.assertEqual(establish.await_count, 1)
        self.assertEqual(client.disconnect_calls, 0)
        self.assertEqual(self.mesh.diagnostics["direct_auth_successes"], 1)
        self.assertTrue(self.mesh.connected)

    async def test_failed_direct_authentication_uses_fallback_once(self):
        node = make_node()
        direct_client = FakeClient()
        fallback_client = FakeClient()
        self.mesh.expect_device(node)

        with (
            patch(
                "pyplejd.ble.establish_connection",
                new=AsyncMock(side_effect=[direct_client, fallback_client]),
            ) as establish,
            patch.object(
                self.mesh,
                "_authenticate",
                new=AsyncMock(side_effect=[False, True]),
            ),
            patch.object(self.mesh, "poll", new=AsyncMock()),
            patch.object(asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            self.assertTrue(await self.mesh.connect())

        self.assertEqual(establish.await_count, 2)
        self.assertEqual(direct_client.disconnect_calls, 1)
        sleep.assert_awaited_once_with(5)
        self.assertEqual(self.mesh.diagnostics["fallback_auth_attempts"], 1)

    async def test_button_event_does_not_request_another_event_when_disabled(self):
        node = make_node()
        client = FakeClient()
        self.mesh.expect_device(node)

        with (
            patch("pyplejd.ble.establish_connection", new=AsyncMock(return_value=client)),
            patch.object(self.mesh, "_authenticate", new=AsyncMock(return_value=True)),
            patch.object(self.mesh, "poll", new=AsyncMock()),
            patch.object(self.mesh, "poll_buttons", new=AsyncMock()) as poll_buttons,
        ):
            self.assertTrue(await self.mesh.connect())
            event = LastData(command=LastData.CMD_EVENT_FIRED)
            encrypted = encrypt_decrypt(
                "00" * 16, node.BLEaddress, bytearray(event.data)
            )
            await client.notify_callbacks[gatt.PLEJD_LASTDATA](None, encrypted)

        self.manager.lastdata_callback.assert_awaited_once()
        poll_buttons.assert_not_awaited()

    async def test_non_gateway_control_write_reconnects_once(self):
        node = make_node(device_addresses=(1,))
        self.mesh._gateway_node = node
        self.mesh._client = FakeClient()
        command = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[1],
        )

        with (
            patch.object(self.mesh, "_write", new=AsyncMock(return_value=True)),
            patch.object(self.mesh, "_reconnect", new=AsyncMock(return_value=True)) as reconnect,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        reconnect.assert_awaited_once_with(preserve_availability=True)
        self.assertEqual(self.mesh.diagnostics["command_reconnects"], 1)

    async def test_confirmed_non_gateway_write_skips_reconnect(self):
        node = make_node(device_addresses=(1,))
        self.mesh._gateway_node = node
        self.mesh._client = FakeClient()
        self.manager.control_confirmation_timeout = 1
        command = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[1],
        )

        async def write_and_confirm(_payloads):
            self.mesh._resolve_control_confirmation(2, True)
            return True

        with (
            patch.object(self.mesh, "_write", new=AsyncMock(side_effect=write_and_confirm)),
            patch.object(self.mesh, "_reconnect", new=AsyncMock(return_value=True)) as reconnect,
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        reconnect.assert_not_awaited()
        self.assertEqual(self.mesh.diagnostics["control_confirmations"], 1)
        self.assertEqual(self.mesh.diagnostics["confirmation_timeouts"], 0)

    async def test_gateway_control_write_does_not_reconnect(self):
        node = make_node(device_addresses=(1,))
        self.mesh._gateway_node = node
        self.mesh._client = FakeClient()
        command = LastData(
            address=1,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[1],
        )

        with (
            patch.object(self.mesh, "_write", new=AsyncMock(return_value=True)),
            patch.object(self.mesh, "_reconnect", new=AsyncMock(return_value=True)) as reconnect,
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        reconnect.assert_not_awaited()

    async def test_failed_write_reconnects_and_retries_once(self):
        node = make_node(device_addresses=(1,))
        self.mesh._gateway_node = node
        self.mesh._client = FakeClient()
        command = LastData(
            address=1,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[0],
        )

        with (
            patch.object(
                self.mesh, "_write", new=AsyncMock(side_effect=[False, True])
            ) as write,
            patch.object(self.mesh, "_reconnect", new=AsyncMock(return_value=True)) as reconnect,
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        self.assertEqual(write.await_count, 2)
        reconnect.assert_awaited_once()
        self.assertEqual(self.mesh.diagnostics["write_retries"], 1)


if __name__ == "__main__":
    unittest.main()
