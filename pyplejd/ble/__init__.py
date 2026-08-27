import asyncio
import binascii
import logging
import os
from datetime import datetime, timedelta
from typing import Callable
import time

from bleak import BleakClient, BleakError
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache

from .crypto import auth_response, encrypt_decrypt
from . import ble_characteristics as gatt
from . import payload_encode
from .lastdata import LastData, MiniPkg
from .lightlevel import parse_lightlevels, LightLevel
from .ble_characteristics import PLEJD_SERVICE
from .debug import rec_log

_LOGGER = logging.getLogger(__name__)
_CONNECTION_LOG = logging.getLogger("pyplejd.ble.connection")


class MeshDevice:
    BLEaddress: str
    connectable: bool
    last_seen: datetime = None
    rssi: int = None
    bleDevice: BLEDevice = None
    is_gateway: bool = False

    def see(self, rssi, bleDevice: BLEDevice) -> bool:
        # Returns true if first seen
        if first_seen := (self.rssi is None):
            self.bleDevice = bleDevice
            self.rssi = rssi

        self.last_seen = datetime.now()
        self.rssi = max(self.rssi, rssi)

        return first_seen

    def update():
        pass


def normalize_address(addr: str) -> str:
    return addr.replace(":", "").upper()


class PlejdMesh:
    def __init__(self, manager):
        self.manager = manager
        self._mesh_devices: dict[str, MeshDevice] = {}
        self._gateway_node: MeshDevice | None = None
        self._crypto_key: bytearray = None
        self._client: BleakClient = None

        self._ble_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()

        self._connection_attempts = 0
        self._direct_auth_successes = 0
        self._fallback_auth_attempts = 0
        self._command_reconnects = 0
        self._write_retries = 0
        self._control_confirmations = 0
        self._confirmation_timeouts = 0
        self._in_place_reauth_attempts = 0
        self._in_place_reauth_successes = 0
        self._direct_control_routes = 0
        self._direct_gateway_switches = 0
        self._direct_route_failures = 0
        self._on_demand_disconnects = 0
        self._ignored_stale_notifications = 0
        self._filtered_cross_node_updates = 0
        self._filtered_direct_state_echoes = 0
        self._suppress_disconnect_notification = False
        self._control_confirmation = None

    @property
    def connected(self):
        return self._client is not None and bool(
            getattr(self._client, "is_connected", False)
        )

    @property
    def diagnostics(self):
        return {
            "connected": self.connected,
            "gateway": self._gateway_node.BLEaddress if self._gateway_node else None,
            "connection_attempts": self._connection_attempts,
            "direct_auth_successes": self._direct_auth_successes,
            "fallback_auth_attempts": self._fallback_auth_attempts,
            "command_reconnects": self._command_reconnects,
            "write_retries": self._write_retries,
            "control_confirmations": self._control_confirmations,
            "confirmation_timeouts": self._confirmation_timeouts,
            "in_place_reauth_attempts": self._in_place_reauth_attempts,
            "in_place_reauth_successes": self._in_place_reauth_successes,
            "direct_control_routes": self._direct_control_routes,
            "direct_gateway_switches": self._direct_gateway_switches,
            "direct_route_failures": self._direct_route_failures,
            "on_demand_disconnects": self._on_demand_disconnects,
            "ignored_stale_notifications": self._ignored_stale_notifications,
            "filtered_cross_node_updates": self._filtered_cross_node_updates,
            "filtered_direct_state_echoes": self._filtered_direct_state_echoes,
            "button_polling": bool(
                getattr(self.manager, "button_events_enabled", True)
            ),
        }

    def expect_device(self, node: MeshDevice = None):
        self._mesh_devices[node.BLEaddress] = node

    def see_device(self, node: BLEDevice, rssi: int) -> bool:
        _CONNECTION_LOG.debug(f"Saw device {node} (rssi: {rssi})")
        addr = normalize_address(node.address)
        if hw := self._mesh_devices.get(addr):
            return hw.see(rssi, node)
        return False

    def set_key(self, key: str):
        self._crypto_key = key

    async def disconnect(self, preserve_availability: bool = False):
        client = self._client
        if client is None:
            return False

        previous_suppression = self._suppress_disconnect_notification
        self._suppress_disconnect_notification = (
            previous_suppression or preserve_availability
        )
        try:
            if getattr(client, "is_connected", False):
                await client.stop_notify(gatt.PLEJD_LASTDATA)
                await client.stop_notify(gatt.PLEJD_LIGHTLEVEL)
                await client.disconnect()
        except BleakError:
            pass
        finally:
            self._mark_disconnected(client)
            self._suppress_disconnect_notification = previous_suppression
        return True

    def _mark_disconnected(self, client: BleakClient | None = None):
        """Clear connection state without letting an old callback clear a new client."""
        if client is not None and self._client not in (None, client):
            return
        if self._client is None and self._gateway_node is None:
            return
        self._client = None
        if self._gateway_node:
            self._gateway_node.is_gateway = False
            if not self._suppress_disconnect_notification:
                self._gateway_node.update()
            self._gateway_node = None
        if not self._suppress_disconnect_notification:
            self.manager.connect_callback(False)

    async def connect(self, preferred_node: MeshDevice | None = None):
        async with self._connect_lock:
            if self.connected and (
                preferred_node is None or self._gateway_node is preferred_node
            ):
                return True
            if self._client is not None:
                await self.disconnect()
            _CONNECTION_LOG.debug("Trying to connect to BLE mesh")

            # Each connect() invocation owns its notification callbacks. Keep the
            # exact client and node in the closure so a delayed callback from a
            # disconnected session cannot be decrypted or applied as if it came
            # from the next directly connected Plejd node.
            listener_client = None
            listener_node = None

            def _notification_is_current() -> bool:
                current = (
                    self.connected
                    and self._client is listener_client
                    and self._gateway_node is listener_node
                )
                if not current:
                    self._ignored_stale_notifications += 1
                return current

            def _direct_target_addresses() -> set[int] | None:
                if not getattr(
                    self.manager, "filter_direct_state_updates", False
                ) or listener_node is None:
                    return None
                addresses = set()
                for device in getattr(listener_node, "devices", set()):
                    for attribute in ("address", "rxAddress"):
                        address = getattr(device, attribute, None)
                        if address is not None:
                            addresses.add(address)
                return addresses

            def _disconnect(client: BleakClient):
                _CONNECTION_LOG.debug("Disconnected from BLE mesh (%s)", client)
                self._mark_disconnected(client)

            async def _lastdata_listener(_arg, lastdata: bytearray):
                if not _notification_is_current():
                    return

                data = encrypt_decrypt(
                    self._crypto_key, listener_node.BLEaddress, lastdata
                )

                ld = LastData(data)
                rec_log(f"lastdata {ld}")
                target_addresses = _direct_target_addresses()
                if target_addresses is not None:
                    if ld.address not in target_addresses:
                        self._filtered_cross_node_updates += 1
                        return
                    if ld.command in {
                        LastData.CMD_GROUP_OUTPUT_STATE,
                        LastData.CMD_GROUP_OUTPUT_STATE_AND_LEVEL,
                        LastData.CMD_OUTPUT_STATE_AND_LEVEL,
                    }:
                        # Firmware 6.43.3 can replay stale off/on control echoes
                        # as a direct connection opens. They are not physical
                        # state evidence; the LightLevel poll below is.
                        self._filtered_direct_state_echoes += 1
                        return
                await self.manager.lastdata_callback(ld)

                if ld.command == LastData.CMD_EVENT_FIRED and getattr(
                    self.manager, "button_events_enabled", True
                ):
                    await self.poll_buttons()
                return True

            async def _lightlevel_listener(_, lightlevel: bytearray):
                if not _notification_is_current():
                    return

                rec_log(f"lightlevel {lightlevel}")
                levels = parse_lightlevels(lightlevel)
                target_addresses = _direct_target_addresses()
                if target_addresses is not None:
                    filtered_levels = [
                        level for level in levels if level.address in target_addresses
                    ]
                    self._filtered_cross_node_updates += len(levels) - len(
                        filtered_levels
                    )
                    levels = filtered_levels
                for level in levels:
                    self._resolve_control_confirmation(
                        level.address, level.state, level.dim
                    )
                if levels:
                    await self.manager.lightlevel_callback(levels)

            if preferred_node is not None:
                # An address-specific control write deliberately overrides the
                # persistent gateway blacklist. The blacklist still controls
                # normal/automatic gateway selection.
                sorted_nodes = (
                    [preferred_node]
                    if preferred_node.rssi is not None
                    and preferred_node.bleDevice is not None
                    else []
                )
            else:
                # Try to connect to nodes in order of decreasing RSSI.
                filtered_nodes = filter(
                    lambda n: n.connectable and n.rssi is not None,
                    self._mesh_devices.values(),
                )
                sorted_nodes = sorted(
                    filtered_nodes, key=lambda n: n.rssi, reverse=True
                )

            if not sorted_nodes:
                return False
            client = None
            for node in sorted_nodes:
                try:
                    self._connection_attempts += 1
                    _CONNECTION_LOG.debug("Attempting direct connection to %s", node)
                    client = await establish_connection(
                        BleakClientWithServiceCache,
                        node.bleDevice,
                        node.bleDevice.name,
                        max_attempts=2,
                    )
                    client.set_disconnected_callback(_disconnect)

                    if await self._authenticate(client):
                        self._direct_auth_successes += 1
                    else:
                        # Some firmware still needs the old double-connect workaround.
                        self._fallback_auth_attempts += 1
                        _CONNECTION_LOG.debug(
                            "Direct authentication failed; retrying after 5 seconds"
                        )
                        await client.disconnect()
                        await asyncio.sleep(5)
                        client = await establish_connection(
                            BleakClientWithServiceCache,
                            node.bleDevice,
                            node.bleDevice.name,
                            _disconnect,
                        )
                        if not await self._authenticate(client):
                            await client.disconnect()
                            continue

                    self._gateway_node = node
                    node.is_gateway = True
                    self._gateway_node.update()
                    self._client = client
                    listener_client = client
                    listener_node = node
                    await client.start_notify(
                        gatt.PLEJD_LASTDATA, _lastdata_listener
                    )
                    await client.start_notify(
                        gatt.PLEJD_LIGHTLEVEL, _lightlevel_listener
                    )
                    self.manager.connect_callback(True)
                    await self.poll()
                    return True

                except (BleakError, asyncio.TimeoutError) as e:
                    _CONNECTION_LOG.warning(
                        "Failed to connect to %s: %s", node, str(e)
                    )
                    if client is not None:
                        try:
                            await client.disconnect()
                        except BleakError:
                            pass
                        finally:
                            self._mark_disconnected(client)

            else:
                _CONNECTION_LOG.warning(
                    "Failed to connect to plejd mesh - %s", sorted_nodes
                )
                return False

    async def poll(self):
        if not self.connected:
            return

        _LOGGER.debug("Polling mesh for current state")
        await self._client.write_gatt_char(
            gatt.PLEJD_LIGHTLEVEL, b"\x01", response=True
        )

    async def poll_buttons(self):
        await self.write(LastData(command=LastData.CMD_EVENT_PREPARE).hex)

    async def ping(self):
        async with self._ble_lock:
            if not await self.connect():
                return False
            if not await self._ping(self._client):
                await self.disconnect()
                return False

        await self.poll()
        if getattr(self.manager, "button_events_enabled", True):
            await self.poll_buttons()
        return True

    async def poll_time(self, address: int):
        if not self.connected:
            return

        payloads = payload_encode.request_time(self, address)
        await self.write(payloads)

        retval = await self._client.read_gatt_char(gatt.PLEJD_LASTDATA)
        data = encrypt_decrypt(self._crypto_key, self._gateway_node.BLEaddress, retval)
        ts = int.from_bytes(data[5:9], "little")
        dt = datetime.fromtimestamp(ts)

        now = datetime.now() + timedelta(seconds=3600 * time.daylight)
        if abs(dt - now) > timedelta(seconds=60):
            _LOGGER.debug(f"Device {address} repported the wrong time {dt} ({now=})")
            return True
        return False

    async def broadcast_time(self):
        payloads = payload_encode.set_time(self)
        await self.write(payloads)

    async def write(self, *payloads: list[str]):
        raw_payloads = [
            binascii.a2b_hex(payload.replace(" ", "")) for payload in payloads
        ]

        async with self._command_lock:
            direct_target = None
            direct_on_demand = False
            if getattr(self.manager, "route_control_writes_directly", False):
                direct_target = self._target_hardware_for_control_write(raw_payloads)
                route_result = await self._route_control_write_directly(raw_payloads)
                if route_result is False:
                    return False
                direct_on_demand = bool(
                    route_result is True
                    and direct_target is not None
                    and getattr(
                        self.manager,
                        "disconnect_after_direct_control_write",
                        False,
                    )
                )
            if not self.connected and not await self.connect():
                return False
            control_write = self._is_non_gateway_control_write(raw_payloads)
            requires_confirmation = direct_on_demand or (
                control_write
                and getattr(self.manager, "reconnect_after_control_write", False)
            )
            confirmation = (
                self._start_control_confirmation(raw_payloads)
                if requires_confirmation
                else None
            )

            try:
                _LOGGER.debug(f"Write: {payloads}")
                success = await self._write(self._encrypt_payloads(raw_payloads))

                if not success:
                    self._clear_control_confirmation(confirmation)
                    confirmation = None
                    self._write_retries += 1
                    _LOGGER.warning("Retrying Plejd write after reconnect")
                    reconnected = (
                        await self._reconnect(
                            preserve_availability=True,
                            preferred_node=direct_target,
                        )
                        if direct_on_demand
                        else await self._reconnect()
                    )
                    if not reconnected:
                        return False
                    confirmation = (
                        self._start_control_confirmation(raw_payloads)
                        if requires_confirmation
                        else None
                    )
                    success = await self._write(
                        self._encrypt_payloads(raw_payloads)
                    )

                if success and direct_on_demand:
                    await self.poll()
                    if confirmation is not None:
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(confirmation),
                                timeout=getattr(
                                    self.manager,
                                    "control_confirmation_timeout",
                                    1.5,
                                ),
                            )
                            self._control_confirmations += 1
                        except asyncio.TimeoutError:
                            self._confirmation_timeouts += 1
                    return success

                if (
                    success
                    and control_write
                    and getattr(
                        self.manager, "reconnect_after_control_write", False
                    )
                ):
                    if confirmation is not None:
                        # PLEJD_LASTDATA may echo the exact outgoing control packet
                        # even when firmware has only buffered it. Polling
                        # PLEJD_LIGHTLEVEL is the first independent observation of
                        # the physical state, so only that notification may resolve
                        # the confirmation future.
                        await self.poll()
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(confirmation),
                                timeout=getattr(
                                    self.manager,
                                    "control_confirmation_timeout",
                                    1.5,
                                ),
                            )
                            self._control_confirmations += 1
                            return True
                        except asyncio.TimeoutError:
                            self._confirmation_timeouts += 1

                        if getattr(
                            self.manager,
                            "reauthenticate_before_reconnect",
                            True,
                        ):
                            self._in_place_reauth_attempts += 1
                            if await self._reauthenticate_current_client():
                                try:
                                    await asyncio.wait_for(
                                        asyncio.shield(confirmation),
                                        timeout=getattr(
                                            self.manager,
                                            "reauth_confirmation_timeout",
                                            1.0,
                                        ),
                                    )
                                    self._control_confirmations += 1
                                    self._in_place_reauth_successes += 1
                                    return True
                                except asyncio.TimeoutError:
                                    pass

                    # Firmware 6.43.x can buffer a command to a non-gateway node
                    # until the authenticated BLE session is closed. Reconnect only
                    # when no matching state confirmation arrived in time.
                    await asyncio.sleep(
                        getattr(self.manager, "control_write_flush_delay", 0.05)
                    )
                    self._command_reconnects += 1
                    success = await self._reconnect(
                        preserve_availability=True
                    )

                return success
            finally:
                self._clear_control_confirmation(confirmation)
                if direct_on_demand and self.connected:
                    self._on_demand_disconnects += 1
                    await self.disconnect(preserve_availability=True)

    async def _route_control_write_directly(self, raw_payloads) -> bool | None:
        """Connect directly to the hardware addressed by a control command.

        Firmware 6.43.x can acknowledge or echo a mesh write without applying it
        on a remote node. A direct authenticated BLE write to the target hardware
        avoids that unreliable forwarding path. The connection remains on the
        selected node so successive dim updates do not reconnect repeatedly.
        """
        target = self._target_hardware_for_control_write(raw_payloads)
        if target is None:
            return None

        self._direct_control_routes += 1
        if self.connected and self._gateway_node is target:
            return True

        _CONNECTION_LOG.info(
            "Routing control write directly to Plejd node %s", target.BLEaddress
        )
        self._direct_gateway_switches += 1
        if self.connected:
            await self.disconnect(preserve_availability=True)
        if await self.connect(preferred_node=target):
            return True

        self._direct_route_failures += 1
        for device in getattr(target, "devices", set()):
            device.set_available(False)
        _CONNECTION_LOG.warning(
            "Could not route control write directly to Plejd node %s",
            target.BLEaddress,
        )
        return False

    def _target_hardware_for_control_write(self, raw_payloads):
        controls = self._control_commands(raw_payloads)
        if not controls or any(command.address == 0 for command in controls):
            return None

        targets = []
        for command in controls:
            matching_nodes = [
                node
                for node in self._mesh_devices.values()
                if any(
                    getattr(device, "address", None) == command.address
                    for device in getattr(node, "devices", set())
                )
            ]
            if len(matching_nodes) != 1:
                return None
            targets.append(matching_nodes[0])

        if any(target is not targets[0] for target in targets[1:]):
            return None
        return targets[0]

    def _encrypt_payloads(self, raw_payloads):
        return [
            encrypt_decrypt(
                self._crypto_key,
                self._gateway_node.BLEaddress,
                payload,
            )
            for payload in raw_payloads
        ]

    def _is_non_gateway_control_write(self, raw_payloads) -> bool:
        controls = self._control_commands(raw_payloads)
        if not controls:
            return False
        if self._gateway_node is None:
            return True
        gateway_addresses = {
            getattr(device, "address", None)
            for device in getattr(self._gateway_node, "devices", set())
        }
        return any(
            command.address == 0 or command.address not in gateway_addresses
            for command in controls
        )

    def _control_commands(self, raw_payloads):
        control_commands = {
            LastData.CMD_SCENE,
            LastData.CMD_GROUP_OUTPUT_STATE,
            LastData.CMD_GROUP_OUTPUT_STATE_AND_LEVEL,
            LastData.CMD_OUTPUT_STATE_AND_LEVEL,
            LastData.CMD_OUTPUT_SET,
            LastData.CMD_TUNABLE_WHITE_TEMPERATURE,
            LastData.CMD_TRM_TEMPERATURE_REGULATING_SETPOINT,
            LastData.CMD_TRM_OPERATING_MODE,
            LastData.CMD_TRM_PWM_DUTY,
            LastData.CMD_TRM_RESET_OPERATING_MODE,
        }
        commands = [LastData(payload) for payload in raw_payloads]
        return [command for command in commands if command.command in control_commands]

    def _start_control_confirmation(self, raw_payloads):
        state_commands = {
            LastData.CMD_GROUP_OUTPUT_STATE,
            LastData.CMD_GROUP_OUTPUT_STATE_AND_LEVEL,
            LastData.CMD_OUTPUT_STATE_AND_LEVEL,
        }
        for raw_payload in raw_payloads:
            command = LastData(raw_payload)
            if command.command in state_commands and command.payload:
                expected_dim = None
                if (
                    command.command
                    in {
                        LastData.CMD_GROUP_OUTPUT_STATE_AND_LEVEL,
                        LastData.CMD_OUTPUT_STATE_AND_LEVEL,
                    }
                    and bool(command.payload[0])
                    and len(command.payload) >= 3
                ):
                    expected_dim = int.from_bytes(
                        command.payload[1:3], byteorder="little"
                    )
                future = asyncio.get_running_loop().create_future()
                self._control_confirmation = (
                    command.address,
                    bool(command.payload[0]),
                    expected_dim,
                    future,
                )
                return future
        return None

    def _resolve_control_confirmation(
        self, address: int, state: bool, dim: int | None = None
    ):
        confirmation = self._control_confirmation
        if confirmation is None:
            return
        expected_address, expected_state, expected_dim, future = confirmation
        if (
            address == expected_address
            and bool(state) == expected_state
            and (expected_dim is None or dim == expected_dim)
            and not future.done()
        ):
            future.set_result(True)

    def _clear_control_confirmation(self, future):
        if future is None:
            return
        confirmation = self._control_confirmation
        if confirmation is not None and confirmation[3] is future:
            self._control_confirmation = None
        if not future.done():
            future.cancel()

    async def _reconnect(
        self,
        preserve_availability: bool = False,
        preferred_node: MeshDevice | None = None,
    ):
        await self.disconnect(preserve_availability=preserve_availability)
        return await self.connect(preferred_node=preferred_node)

    async def _reauthenticate_current_client(self):
        async with self._ble_lock:
            if not self.connected:
                return False
            client = self._client
            if not await self._authenticate(client):
                return False
        await self.poll()
        return True

    async def _write(self, payloads):
        if not self.connected:
            return

        try:
            async with self._ble_lock:
                for payload in payloads:
                    _LOGGER.debug("Writing to plejd mesh: %s", payload.hex())
                    await self._client.write_gatt_char(
                        gatt.PLEJD_DATA, payload, response=True
                    )
        except (BleakError, asyncio.TimeoutError) as e:
            _LOGGER.warning("Writing to plejd mesh failed: %s", str(e))
            return False
        return True

    async def _ping(self, client):
        if client is None:
            return False
        try:
            ping = bytearray(os.urandom(1))
            _LOGGER.debug("Ping(%s)", int.from_bytes(ping, "little"))
            await client.write_gatt_char(gatt.PLEJD_PING, ping, response=True)
            pong = await client.read_gatt_char(gatt.PLEJD_PING)
            _LOGGER.debug("Pong(%s)", int.from_bytes(pong, "little"))
            if (ping[0] + 1) & 0xFF == pong[0]:
                return True
        except (BleakError, asyncio.TimeoutError) as e:
            _LOGGER.warning("Plejd mesh keepalive signal failed: %s", str(e))
        return False

    async def _authenticate(self, client: BleakClient):
        if client is None:
            return False
        try:
            _CONNECTION_LOG.debug("Authenticating with plejd mesh")
            await client.write_gatt_char(gatt.PLEJD_AUTH, b"\x00", response=True)
            _CONNECTION_LOG.debug("Requested auth")
            challenge = await client.read_gatt_char(gatt.PLEJD_AUTH)
            _CONNECTION_LOG.debug("Got challenge")
            response = auth_response(self._crypto_key, challenge)
            await client.write_gatt_char(gatt.PLEJD_AUTH, response, response=True)
            _CONNECTION_LOG.debug("Wrote response")
            if not await self._ping(client):
                _CONNECTION_LOG.debug("Authentication failed!")
                return False
            _CONNECTION_LOG.debug("Authentication successful")
            return True
        except (BleakError, asyncio.TimeoutError) as e:
            _CONNECTION_LOG.warning("Plejd mesh authentication failed: %s", str(e))
        return False
