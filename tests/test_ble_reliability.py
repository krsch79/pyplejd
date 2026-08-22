import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pyplejd import PlejdManager
from pyplejd.ble import ble_characteristics as gatt
from pyplejd.ble import LastData, PlejdMesh
from pyplejd.ble.crypto import encrypt_decrypt


class FakeClient:
    def __init__(self):
        self.is_connected = True
        self.disconnect_callback = None
        self.notify_callbacks = {}
        self.disconnect_calls = 0
        self.writes = []

    def set_disconnected_callback(self, callback):
        self.disconnect_callback = callback

    async def start_notify(self, characteristic, callback):
        self.notify_callbacks[characteristic] = callback

    async def write_gatt_char(self, characteristic, payload, response=True):
        self.writes.append((characteristic, bytes(payload), response))

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
        self.route_control_writes_directly = False
        self.reconnect_after_control_write = True
        self.control_confirmation_timeout = 0
        self.reauthenticate_before_reconnect = False
        self.reauth_confirmation_timeout = 1
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
        devices=[SimpleNamespace(address=item, set_available=Mock()) for item in device_addresses],
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

    async def test_control_write_switches_directly_to_target_node(self):
        current = make_node("001122334455", device_addresses=(1,))
        target = make_node("AABBCCDDEEFF", device_addresses=(2,))
        target.connectable = False  # Persistent blacklist must not block routing.
        old_client = FakeClient()
        new_client = FakeClient()
        self.mesh.expect_device(current)
        self.mesh.expect_device(target)
        self.mesh._gateway_node = current
        current.is_gateway = True
        self.mesh._client = old_client
        self.manager.route_control_writes_directly = True
        command = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[1],
        )

        with (
            patch(
                "pyplejd.ble.establish_connection",
                new=AsyncMock(return_value=new_client),
            ) as establish,
            patch.object(self.mesh, "_authenticate", new=AsyncMock(return_value=True)),
            patch.object(self.mesh, "poll", new=AsyncMock()),
            patch.object(self.mesh, "_write", new=AsyncMock(return_value=True)) as write,
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        establish.assert_awaited_once()
        self.assertIs(establish.await_args.args[1], target.bleDevice)
        self.assertEqual(old_client.disconnect_calls, 1)
        self.assertIs(self.mesh._gateway_node, target)
        write.assert_awaited_once()
        self.assertEqual(self.mesh.diagnostics["direct_control_routes"], 1)
        self.assertEqual(self.mesh.diagnostics["direct_gateway_switches"], 1)

    async def test_repeated_control_write_keeps_target_connection(self):
        target = make_node("AABBCCDDEEFF", device_addresses=(2,))
        self.mesh.expect_device(target)
        self.mesh._gateway_node = target
        target.is_gateway = True
        self.mesh._client = FakeClient()
        self.manager.route_control_writes_directly = True
        command = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE_AND_LEVEL,
            payload=[1, 128, 128],
        )

        with (
            patch.object(self.mesh, "connect", new=AsyncMock()) as connect,
            patch.object(self.mesh, "disconnect", new=AsyncMock()) as disconnect,
            patch.object(self.mesh, "_write", new=AsyncMock(return_value=True)),
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        connect.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.assertEqual(self.mesh.diagnostics["direct_control_routes"], 1)
        self.assertEqual(self.mesh.diagnostics["direct_gateway_switches"], 0)

    async def test_on_demand_direct_write_confirms_and_disconnects(self):
        target = make_node("AABBCCDDEEFF", device_addresses=(2,))
        client = FakeClient()
        self.mesh.expect_device(target)
        self.mesh._gateway_node = target
        target.is_gateway = True
        self.mesh._client = client
        self.manager.route_control_writes_directly = True
        self.manager.disconnect_after_direct_control_write = True
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
            patch.object(self.mesh, "poll", new=AsyncMock()) as poll,
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        poll.assert_awaited_once()
        self.assertEqual(client.disconnect_calls, 1)
        self.assertFalse(self.mesh.connected)
        self.assertEqual(self.mesh.diagnostics["control_confirmations"], 1)
        self.assertEqual(self.mesh.diagnostics["on_demand_disconnects"], 1)
        self.manager.connect_callback.assert_not_called()

    async def test_failed_direct_route_marks_target_hardware_unavailable(self):
        target = make_node("AABBCCDDEEFF", device_addresses=(2,))
        self.mesh.expect_device(target)
        self.manager.route_control_writes_directly = True
        command = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[1],
        )

        with patch.object(self.mesh, "connect", new=AsyncMock(return_value=False)):
            self.assertFalse(await self.mesh.write(command.hex))

        target.devices[0].set_available.assert_called_once_with(False)

    def test_advertisement_marks_only_matching_hardware_available(self):
        matching = SimpleNamespace(devices=[Mock(), Mock()])
        other = SimpleNamespace(devices=[Mock()])
        manager = PlejdManager.__new__(PlejdManager)
        manager.hardware = {
            "AABBCCDDEEFF": matching,
            "001122334455": other,
        }

        self.assertTrue(manager.advertisement_callback("AA:BB:CC:DD:EE:FF"))
        for device in matching.devices:
            device.set_available.assert_called_once_with(True)
        other.devices[0].set_available.assert_not_called()
        self.assertFalse(manager.advertisement_callback("FF:FF:FF:FF:FF:FF"))

    async def test_non_control_write_does_not_change_gateway(self):
        current = make_node("001122334455", device_addresses=(1,))
        self.mesh._gateway_node = current
        self.mesh._client = FakeClient()
        self.manager.route_control_writes_directly = True
        command = LastData(command=LastData.CMD_EVENT_PREPARE)

        with (
            patch.object(self.mesh, "connect", new=AsyncMock()) as connect,
            patch.object(self.mesh, "disconnect", new=AsyncMock()) as disconnect,
            patch.object(self.mesh, "_write", new=AsyncMock(return_value=True)),
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        connect.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.assertEqual(self.mesh.diagnostics["direct_control_routes"], 0)

    def test_mixed_known_and_unknown_targets_are_not_directly_routed(self):
        target = make_node("AABBCCDDEEFF", device_addresses=(2,))
        self.mesh.expect_device(target)
        known = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[1],
        )
        unknown = LastData(
            address=99,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[1],
        )

        self.assertIsNone(
            self.mesh._target_hardware_for_control_write(
                [bytearray(known.data), bytearray(unknown.data)]
            )
        )

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

    async def test_lastdata_echo_does_not_confirm_non_gateway_write(self):
        node = make_node(device_addresses=(1,))
        client = FakeClient()
        self.mesh.expect_device(node)
        self.manager.control_confirmation_timeout = 0
        command = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[1],
        )

        with (
            patch("pyplejd.ble.establish_connection", new=AsyncMock(return_value=client)),
            patch.object(self.mesh, "_authenticate", new=AsyncMock(return_value=True)),
            patch.object(self.mesh, "poll", new=AsyncMock()),
        ):
            self.assertTrue(await self.mesh.connect())

        async def write_and_echo(_payloads):
            encrypted = encrypt_decrypt(
                "00" * 16, node.BLEaddress, bytearray(command.data)
            )
            await client.notify_callbacks[gatt.PLEJD_LASTDATA](None, encrypted)
            return True

        with (
            patch.object(self.mesh, "_write", new=AsyncMock(side_effect=write_and_echo)),
            patch.object(self.mesh, "poll", new=AsyncMock()),
            patch.object(self.mesh, "_reconnect", new=AsyncMock(return_value=True)) as reconnect,
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        reconnect.assert_awaited_once_with(preserve_availability=True)
        self.assertEqual(self.mesh.diagnostics["control_confirmations"], 0)
        self.assertEqual(self.mesh.diagnostics["confirmation_timeouts"], 1)

    async def test_lightlevel_requires_matching_dim_level(self):
        node = make_node(device_addresses=(1,))
        self.mesh._gateway_node = node
        self.mesh._client = FakeClient()
        self.manager.control_confirmation_timeout = 0
        command = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE_AND_LEVEL,
            payload=[1, 128, 128],
        )
        confirmation = self.mesh._start_control_confirmation([bytearray(command.data)])

        self.mesh._resolve_control_confirmation(2, True, 64 * 257)
        self.assertFalse(confirmation.done())

        self.mesh._resolve_control_confirmation(2, True, 128 * 257)
        self.assertTrue(confirmation.done())
        self.mesh._clear_control_confirmation(confirmation)

    async def test_matching_lightlevel_dim_confirms_without_reconnect(self):
        node = make_node(device_addresses=(1,))
        client = FakeClient()
        self.mesh.expect_device(node)
        self.manager.control_confirmation_timeout = 1
        command = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE_AND_LEVEL,
            payload=[1, 128, 128],
        )

        with (
            patch("pyplejd.ble.establish_connection", new=AsyncMock(return_value=client)),
            patch.object(self.mesh, "_authenticate", new=AsyncMock(return_value=True)),
            patch.object(self.mesh, "poll", new=AsyncMock()),
        ):
            self.assertTrue(await self.mesh.connect())

        async def write_and_report(_payloads):
            lightlevel = bytearray([2, 1, 0, 0, 0, 128, 128, 0, 0, 0])
            await client.notify_callbacks[gatt.PLEJD_LIGHTLEVEL](None, lightlevel)
            return True

        with (
            patch.object(self.mesh, "_write", new=AsyncMock(side_effect=write_and_report)),
            patch.object(self.mesh, "poll", new=AsyncMock()),
            patch.object(self.mesh, "_reconnect", new=AsyncMock(return_value=True)) as reconnect,
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        reconnect.assert_not_awaited()
        self.assertEqual(self.mesh.diagnostics["control_confirmations"], 1)

    async def test_in_place_reauthentication_can_confirm_without_reconnect(self):
        node = make_node(device_addresses=(1,))
        self.mesh._gateway_node = node
        self.mesh._client = FakeClient()
        self.manager.reauthenticate_before_reconnect = True
        command = LastData(
            address=2,
            command=LastData.CMD_GROUP_OUTPUT_STATE,
            payload=[1],
        )

        async def reauthenticate_and_confirm():
            self.mesh._resolve_control_confirmation(2, True)
            return True

        with (
            patch.object(self.mesh, "_write", new=AsyncMock(return_value=True)),
            patch.object(
                self.mesh,
                "_reauthenticate_current_client",
                new=AsyncMock(side_effect=reauthenticate_and_confirm),
            ) as reauthenticate,
            patch.object(self.mesh, "_reconnect", new=AsyncMock(return_value=True)) as reconnect,
        ):
            self.assertTrue(await self.mesh.write(command.hex))

        reauthenticate.assert_awaited_once()
        reconnect.assert_not_awaited()
        self.assertEqual(self.mesh.diagnostics["in_place_reauth_attempts"], 1)
        self.assertEqual(self.mesh.diagnostics["in_place_reauth_successes"], 1)

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
