"""Test doubles that simulate a Yeelight Candela lamp behind a bleak client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.yeelight_candela.candela import protocol


def pad(*values: int) -> bytes:
    return bytes(values).ljust(protocol.FRAME_LENGTH, b"\x00")


class FakeLamp:
    """Lamp-side protocol simulation, with switches for failure modes."""

    def __init__(self, *, is_on: bool = True, brightness: int = 100, notify: bool = True) -> None:
        self.is_on = is_on
        self.brightness = brightness
        self.flicker = False
        self.notify = notify
        self.frames: list[bytes] = []
        self.devices: list[BLEDevice] = []
        self.fail_connect = False
        # Number of upcoming connection attempts that fail (then the lamp answers).
        self.fail_connects = 0
        self.fail_writes = 0
        self.start_notify_error: Exception | None = None
        self.notify_delay: float | None = None
        self.write_delay = 0.0
        self.disconnect_error: Exception | None = None
        self.connect_delay = 0.0
        # Drop the link right after the next successful write (reply already sent).
        self.drop_after_write = False
        self.connect_kwargs: dict[str, Any] = {}
        # ESPHome-proxy simulation: `start_notify` fails (no CCCD) but the proxy API
        # can register the handle directly and then forwards what the lamp pushes.
        self.proxy_backend = False
        self.raw_notify_error: Exception | None = None

    @property
    def connection_attempts(self) -> int:
        return len(self.devices)

    def state_frame(self) -> bytes:
        power = protocol.POWER_ON if self.is_on else protocol.POWER_OFF
        return pad(protocol.PREFIX, protocol.RSP_STATE, power, self.brightness, 0x02, 0x07, 0x08)

    def respond(self, frame: bytes) -> list[bytes]:
        command = frame[1]
        if command == protocol.CMD_POWER:
            self.is_on = frame[2] == protocol.POWER_ON
            replies = [self.state_frame()]
            if not self.is_on and self.flicker:
                self.flicker = False
                replies.append(pad(protocol.PREFIX, protocol.RSP_FLICKER, protocol.FLICKER_OFF))
            return replies
        if command == protocol.CMD_BRIGHTNESS:
            self.brightness = frame[2]
            self.flicker = False
            return [self.state_frame()]
        if command == protocol.CMD_GET_STATE:
            return [self.state_frame()]
        if command == protocol.CMD_FLICKER:
            self.flicker = True
            return [pad(protocol.PREFIX, protocol.RSP_FLICKER, protocol.FLICKER_ON)]
        return []


NOTIFY_HANDLE = 0x21
PROXY_ADDRESS_INT = 0xF82441C4DEDA


class FakeServices:
    """Just enough of BleakGATTServiceCollection."""

    def get_characteristic(self, uuid: str) -> SimpleNamespace | None:
        if uuid == protocol.NOTIFY_CHAR_UUID:
            return SimpleNamespace(uuid=uuid, handle=NOTIFY_HANDLE)
        return None


class FakeESPHomeAPI:
    """Stand-in for aioesphomeapi's APIClient notify registration."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.registrations: list[tuple[int, int]] = []
        self.removed = 0

    async def bluetooth_gatt_start_notify(
        self,
        address: int,
        handle: int,
        on_bluetooth_gatt_notify: Callable[[int, bytearray], None],
        timeout: float = 10.0,  # noqa: ASYNC109 - mirrors aioesphomeapi's signature
    ) -> tuple[Callable[[], Any], Callable[[], None]]:
        if self.client.lamp.raw_notify_error is not None:
            raise self.client.lamp.raw_notify_error
        self.registrations.append((address, handle))
        self.client.notify_callback = lambda _char, data: on_bluetooth_gatt_notify(handle, data)

        async def stop_notify() -> None:
            self.client.notify_callback = None

        def remove_callback() -> None:
            self.removed += 1
            self.client.notify_callback = None

        return stop_notify, remove_callback


class FakeESPHomeBackend:
    """Stand-in for bleak_esphome's ESPHomeClient internals used by the workaround."""

    def __init__(self, client: FakeClient) -> None:
        self._client = FakeESPHomeAPI(client)
        self._address_as_int = PROXY_ADDRESS_INT
        self._notify_cancels: dict[int, tuple[Callable[[], Any], Callable[[], None]]] = {}


class FakeClient:
    """Minimal stand-in for BleakClientWithServiceCache."""

    def __init__(self, lamp: FakeLamp, disconnected_callback: Callable[[Any], None] | None) -> None:
        self.lamp = lamp
        self._connected = True
        self._disconnected_callback = disconnected_callback
        self.notify_callback: Callable[[Any, bytearray], None] | None = None
        self.services = FakeServices()
        self._backend = FakeESPHomeBackend(self) if lamp.proxy_backend else None

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def start_notify(self, char: str, callback: Callable[[Any, bytearray], None]) -> None:
        assert char == protocol.NOTIFY_CHAR_UUID
        if self.lamp.start_notify_error is not None:
            raise self.lamp.start_notify_error
        self.notify_callback = callback

    async def write_gatt_char(self, char: str, data: bytes, response: bool | None = None) -> None:
        assert char == protocol.COMMAND_CHAR_UUID
        assert response is True
        await asyncio.sleep(self.lamp.write_delay)
        if not self._connected:
            raise BleakError("Not connected")
        if self.lamp.fail_writes:
            self.lamp.fail_writes -= 1
            raise BleakError("Write failed")
        frame = bytes(data)
        self.lamp.frames.append(frame)
        for reply in self.lamp.respond(frame):
            if self.lamp.notify_delay is None:
                self.push(reply)
            else:
                asyncio.get_running_loop().call_later(self.lamp.notify_delay, self.push, reply)
        if self.lamp.drop_after_write:
            self.lamp.drop_after_write = False
            self.drop()

    def push(self, reply: bytes) -> None:
        """Deliver a notification (also used for unsolicited pushes)."""
        if self.lamp.notify and self.notify_callback is not None:
            self.notify_callback(None, bytearray(reply))

    async def disconnect(self) -> None:
        self.drop()
        if self.lamp.disconnect_error is not None:
            raise self.lamp.disconnect_error

    def drop(self) -> None:
        """Simulate the link going down."""
        if self._connected:
            self._connected = False
            if self._disconnected_callback is not None:
                self._disconnected_callback(self)


def make_establish(lamps: Mapping[str, FakeLamp]) -> tuple[Callable[..., Any], list[FakeClient]]:
    """Return a drop-in for bleak_retry_connector.establish_connection and the clients it made."""
    clients: list[FakeClient] = []

    async def establish(
        client_class: type,
        device: BLEDevice,
        name: str,
        disconnected_callback: Callable[[Any], None] | None = None,
        **kwargs: Any,
    ) -> FakeClient:
        lamp = lamps[device.address]
        lamp.devices.append(device)
        lamp.connect_kwargs = kwargs
        await asyncio.sleep(lamp.connect_delay)
        if lamp.fail_connect:
            raise BleakError(f"{name}: connect failed")
        if lamp.fail_connects:
            lamp.fail_connects -= 1
            raise BleakError(f"{name}: connect failed")
        client = FakeClient(lamp, disconnected_callback)
        clients.append(client)
        return client

    return establish, clients
