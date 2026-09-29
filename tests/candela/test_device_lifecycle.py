"""Tests for CandelaDevice connection lifecycle."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice

from custom_components.yeelight_candela.candela import device as device_module
from custom_components.yeelight_candela.candela import protocol
from custom_components.yeelight_candela.candela.device import CandelaDevice, CandelaError
from tests.fakes import FakeClient, FakeLamp, make_establish

ADDRESS = "F8:24:41:C4:DE:DA"
ESTABLISH = "custom_components.yeelight_candela.candela.device.establish_connection"


@pytest.fixture
def lamp() -> FakeLamp:
    return FakeLamp()


@pytest.fixture
def clients(lamp: FakeLamp) -> Iterator[list[FakeClient]]:
    establish, clients = make_establish({ADDRESS: lamp})
    with patch(ESTABLISH, establish):
        yield clients


@pytest.fixture
async def device(clients: list[FakeClient]) -> AsyncIterator[CandelaDevice]:
    device = CandelaDevice(
        BLEDevice(ADDRESS, "yeelight_ms", {}),
        idle_timeout=0.2,
        response_timeout=0.05,
        command_timeout=0.5,
        retry_pause=0.01,
    )
    yield device
    await device.disconnect()


async def test_idle_connection_is_closed(device, clients):
    await device.turn_on()
    assert device.is_connected
    await asyncio.sleep(0.3)
    assert not device.is_connected
    assert not clients[-1].is_connected


async def test_each_command_restarts_idle_timer(device):
    await device.turn_on()
    await asyncio.sleep(0.15)
    await device.set_brightness(50)
    await asyncio.sleep(0.15)
    assert device.is_connected
    await asyncio.sleep(0.15)
    assert not device.is_connected


async def test_dropped_connection_reconnects_on_next_command(device, lamp, clients):
    await device.set_flicker()
    await device.turn_off()
    clients[-1].drop()  # the lamp drops the link after OFF in flicker mode
    assert not device.is_connected
    await device.turn_on()
    assert lamp.connection_attempts == 2
    assert lamp.frames[-1] == protocol.build_on()
    assert device.state.is_on is True


async def test_stale_connection_is_retried_once(device, lamp):
    await device.turn_on()
    lamp.fail_writes = 1
    await device.turn_off()
    assert lamp.connection_attempts == 2
    assert lamp.frames == [protocol.build_on(), protocol.build_off()]
    assert device.state.is_on is False


async def test_failure_on_fresh_connection_is_retried(device, lamp):
    lamp.fail_writes = 1
    await device.turn_off()
    assert lamp.connection_attempts == 2
    assert lamp.frames == [protocol.build_off()]
    assert device.state.is_on is False


async def test_repeated_write_failures_are_retried_on_new_connections(device, lamp):
    await device.turn_on()
    lamp.fail_writes = 3
    await device.turn_off()
    assert lamp.connection_attempts == 4
    assert lamp.frames == [protocol.build_on(), protocol.build_off()]
    assert device.state.is_on is False


async def test_writes_failing_until_the_deadline_raise(device, lamp):
    await device.turn_on()
    lamp.fail_writes = 10**6
    with pytest.raises(CandelaError):
        await device.turn_off()
    assert device.state.is_on is True
    assert lamp.frames == [protocol.build_on()]
    assert lamp.connection_attempts > 3


async def test_connect_failure_raises_candela_error(device, lamp):
    lamp.fail_connect = True
    with pytest.raises(CandelaError):
        await device.turn_on()
    assert device.state.is_on is None


async def test_command_keeps_connecting_until_the_lamp_answers(device, lamp):
    """A lamp that refuses many connections still gets the command within the deadline."""
    lamp.fail_connects = 7  # far more than one establish_connection round
    await device.turn_off()
    assert lamp.connection_attempts == 8
    assert lamp.frames == [protocol.build_off()]
    assert device.state.is_on is False


async def test_lamp_that_never_connects_fails_at_the_deadline(device, lamp):
    lamp.fail_connect = True
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(CandelaError, match="Timed out"):
        await device.turn_off()
    elapsed = loop.time() - start
    assert 0.4 < elapsed < 1.5  # kept trying for the whole 0.5 s deadline
    assert lamp.connection_attempts > 3
    assert device.state.is_on is None
    assert not device.is_connected


def test_default_command_deadline_is_90_seconds():
    assert device_module.COMMAND_TIMEOUT_SECONDS == 90.0


async def test_disconnect_when_idle_is_noop(device):
    await device.disconnect()
    assert not device.is_connected


async def test_next_connection_uses_latest_ble_device(device, lamp):
    newer = BLEDevice(ADDRESS, "yeelight_ms", {"via": "proxy"})
    device.set_ble_device(newer)
    await device.update()
    assert lamp.devices[-1] is newer


async def test_cancelled_command_still_disconnects_when_idle(device, lamp, clients):
    await device.turn_on()
    lamp.write_delay = 0.5
    task = asyncio.create_task(device.turn_off())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lamp.write_delay = 0
    await asyncio.sleep(0.3)
    assert not device.is_connected
    assert not clients[-1].is_connected


async def test_disconnect_error_after_failed_command_still_raises_candela_error(
    device, lamp, clients
):
    lamp.fail_writes = 10**6  # every connection fails until the deadline
    lamp.disconnect_error = TimeoutError()
    with pytest.raises(CandelaError):
        await device.turn_off()
    # The deadline may hit right after a fresh connect; the idle timer closes that one.
    await asyncio.sleep(0.3)
    assert len(clients) > 1
    assert not device.is_connected
    assert not any(client.is_connected for client in clients)


async def test_disconnect_swallows_backend_errors(device, lamp, clients):
    await device.turn_on()
    lamp.disconnect_error = EOFError()
    await device.disconnect()
    assert not device.is_connected
    assert not clients[-1].is_connected


async def test_poll_uses_short_connection(device, lamp, clients):
    lamp.is_on = False
    await device.poll()
    assert lamp.frames == [protocol.build_get_state()]
    assert device.state.is_on is False
    assert not device.is_connected
    assert not clients[-1].is_connected


async def test_poll_keeps_open_connection(device, lamp, clients):
    await device.turn_on()
    await device.poll()
    assert lamp.frames[-1] == protocol.build_get_state()
    assert lamp.connection_attempts == 1
    assert device.is_connected
    await asyncio.sleep(0.3)  # the idle timer still closes it
    assert not device.is_connected


@pytest.fixture
async def slow_device(clients: list[FakeClient]) -> AsyncIterator[CandelaDevice]:
    device = CandelaDevice(
        BLEDevice(ADDRESS, "yeelight_ms", {}),
        idle_timeout=0.2,
        response_timeout=0.05,
        command_timeout=0.2,
    )
    yield device
    await device.disconnect()


async def test_unreachable_lamp_fails_within_command_timeout(slow_device, lamp):
    lamp.connect_delay = 5
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(CandelaError, match="Timed out"):
        await slow_device.turn_on()
    assert loop.time() - start < 1
    assert not slow_device.is_connected


async def test_queued_command_shares_the_deadline(slow_device, lamp):
    lamp.connect_delay = 5
    loop = asyncio.get_running_loop()
    start = loop.time()
    results = await asyncio.gather(
        slow_device.turn_on(), slow_device.set_brightness(40), return_exceptions=True
    )
    # Waiting for the lock counts against the deadline: both fail at ~0.2 s, not 0.4 s.
    assert loop.time() - start < 0.35
    assert all(isinstance(result, CandelaError) for result in results)
    assert slow_device.state.is_on is None


async def test_timeout_on_open_connection_leaves_it_to_the_idle_timer(slow_device, lamp, clients):
    await slow_device.turn_on()
    lamp.write_delay = 5
    with pytest.raises(CandelaError, match="Timed out"):
        await slow_device.turn_off()
    assert slow_device.is_connected  # still tracked, so the idle timer closes it
    lamp.write_delay = 0
    await asyncio.sleep(0.3)
    assert not slow_device.is_connected
    assert not clients[-1].is_connected


async def test_connection_attempts_are_limited(device, lamp):
    await device.turn_on()
    assert lamp.connect_kwargs["max_attempts"] == 3


async def test_errors_name_the_lamp(clients, lamp):
    device = CandelaDevice(
        BLEDevice(ADDRESS, "yeelight_ms", {}), name="Lamp X", command_timeout=0.2, retry_pause=0.01
    )
    lamp.fail_connect = True
    with pytest.raises(CandelaError, match="Lamp X"):
        await device.turn_on()
    assert device.name == "Lamp X"


def test_name_defaults_to_address():
    device = CandelaDevice(BLEDevice(ADDRESS, "yeelight_ms", {}))
    assert device.name == ADDRESS
