"""Notifications through an ESPHome proxy for a characteristic without a CCCD."""

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.yeelight_candela.candela import protocol
from custom_components.yeelight_candela.candela.device import CandelaDevice
from custom_components.yeelight_candela.candela.esphome_notify import start_notify_without_cccd
from tests.fakes import (
    NOTIFY_HANDLE,
    PROXY_ADDRESS_INT,
    FakeClient,
    FakeESPHomeAPI,
    FakeLamp,
    FakeServices,
    make_establish,
)

ADDRESS = "F8:24:41:C4:DE:DA"
ESTABLISH = "custom_components.yeelight_candela.candela.device.establish_connection"
# Message bleak_esphome raises for the lamp's notify characteristic (seen on hardware).
NO_CCCD = BleakError(
    "Characteristic 8f65073d-9f57-4aaa-afea-397d19d5bbeb does not have a characteristic "
    "client config descriptor."
)


@pytest.fixture
def lamp() -> FakeLamp:
    lamp = FakeLamp()
    lamp.proxy_backend = True
    lamp.start_notify_error = NO_CCCD
    return lamp


@pytest.fixture
def clients(lamp: FakeLamp) -> Iterator[list[FakeClient]]:
    establish, clients = make_establish({ADDRESS: lamp})
    with patch(ESTABLISH, establish):
        yield clients


@pytest.fixture
async def device(clients: list[FakeClient]) -> AsyncIterator[CandelaDevice]:
    device = CandelaDevice(
        BLEDevice(ADDRESS, "yeelight_ms", {}),
        response_timeout=0.05,
        command_timeout=0.5,
        retry_pause=0.01,
    )
    yield device
    await device.disconnect()


async def test_state_is_read_through_proxy_without_cccd(device, lamp, clients):
    lamp.brightness = 40
    await device.update()  # no expected values: state can only come from a notification
    assert device.state.brightness == 40
    assert device.reports_state is True
    backend = clients[-1]._backend
    assert backend._client.registrations == [(PROXY_ADDRESS_INT, NOTIFY_HANDLE)]
    # Stored where bleak_esphome keeps its own registrations, so it cleans up on disconnect.
    assert NOTIFY_HANDLE in backend._notify_cancels


async def test_falls_back_to_optimistic_when_proxy_registration_fails(device, lamp):
    lamp.raw_notify_error = TimeoutError()
    lamp.brightness = 40
    await device.update()
    assert device.state.brightness is None
    assert device.reports_state is False
    await device.turn_off()
    assert device.state.is_on is False


async def test_no_proxy_backend_keeps_optimistic_state(device, lamp):
    lamp.proxy_backend = False
    await device.update()
    assert device.state.is_on is None
    assert device.reports_state is False


def _callback(_char: object, _data: bytearray) -> None:
    """Notification sink for the helper tests."""


async def test_helper_declines_non_esphome_backends():
    client = SimpleNamespace(_backend=None, services=FakeServices())
    assert await start_notify_without_cccd(client, protocol.NOTIFY_CHAR_UUID, _callback) is False


async def test_helper_declines_unknown_characteristic():
    lamp = FakeLamp()
    lamp.proxy_backend = True
    client = FakeClient(lamp, None)
    assert (
        await start_notify_without_cccd(client, "0000ffff-0000-1000-8000-00805f9b34fb", _callback)
        is False
    )


async def test_helper_undoes_registration_without_cleanup_registry():
    lamp = FakeLamp()
    client = FakeClient(lamp, None)
    api = FakeESPHomeAPI(client)
    client._backend = SimpleNamespace(_client=api, _address_as_int=PROXY_ADDRESS_INT)
    assert await start_notify_without_cccd(client, protocol.NOTIFY_CHAR_UUID, _callback) is False
    assert api.removed == 1


def _backend_returning(result: object) -> tuple[FakeClient, SimpleNamespace]:
    async def bluetooth_gatt_start_notify(address, handle, on_notify, timeout):  # noqa: ASYNC109
        return result

    client = FakeClient(FakeLamp(), None)
    backend = SimpleNamespace(
        _client=SimpleNamespace(bluetooth_gatt_start_notify=bluetooth_gatt_start_notify),
        _address_as_int=PROXY_ADDRESS_INT,
        _notify_cancels={},
    )
    client._backend = backend
    return client, backend


async def test_helper_undoes_a_bare_callable_result():
    """An API that returns a single remove function instead of a (stop, remove) pair."""
    removed: list[bool] = []
    client, backend = _backend_returning(lambda: removed.append(True))
    assert await start_notify_without_cccd(client, protocol.NOTIFY_CHAR_UUID, _callback) is False
    assert removed == [True]
    assert backend._notify_cancels == {}


@pytest.mark.parametrize(
    "result",
    [None, (lambda: None, "not callable"), (lambda: None,), (1, 2, 3)],
    ids=["none", "second-not-callable", "one-tuple", "three-tuple"],
)
async def test_helper_declines_unexpected_cancel_results(result: object):
    client, backend = _backend_returning(result)
    assert await start_notify_without_cccd(client, protocol.NOTIFY_CHAR_UUID, _callback) is False
    assert backend._notify_cancels == {}
