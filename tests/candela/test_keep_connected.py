"""Tests for the keep-connected mode (persistent connection with reconnect)."""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice

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
        keep_connected=True,
        idle_timeout=0.05,
        response_timeout=0.05,
        command_timeout=0.5,
        retry_pause=0.01,
        reconnect_delays=(0.02, 0.05),
    )
    yield device
    await device.disconnect()


async def test_connection_is_not_closed_when_idle(device):
    await device.turn_on()
    await asyncio.sleep(0.15)
    assert device.is_connected


async def test_poll_keeps_the_connection_it_opened(device):
    await device.poll()
    assert device.is_connected
    assert device.state.is_on is True


async def test_connect_opens_and_keeps_the_connection(device, lamp):
    await device.connect()
    assert device.is_connected
    assert lamp.frames == []


async def test_dropped_connection_is_restored_in_background(device, lamp, clients):
    await device.connect()
    clients[-1].drop()
    assert not device.is_connected
    await asyncio.sleep(0.1)
    assert device.is_connected
    assert lamp.connection_attempts == 2


async def test_reconnect_keeps_trying_until_it_succeeds(device, lamp, clients):
    await device.connect()
    lamp.fail_connect = True
    clients[-1].drop()
    await asyncio.sleep(0.15)
    assert not device.is_connected
    failed_attempts = lamp.connection_attempts
    assert failed_attempts >= 3
    lamp.fail_connect = False
    await asyncio.sleep(0.1)
    assert device.is_connected


async def test_reconnect_reads_the_state(device, lamp, clients):
    await device.connect()
    fired: list[bool] = []
    device.register_callback(lambda: fired.append(True))
    lamp.is_on = False  # switched off by hand while the link was down
    clients[-1].drop()
    await asyncio.sleep(0.1)
    assert device.is_connected
    assert device.state.is_on is False
    assert fired


async def test_failed_command_schedules_a_reconnect(device, lamp):
    lamp.fail_connect = True
    with pytest.raises(CandelaError):
        await device.turn_on()
    lamp.fail_connect = False
    await asyncio.sleep(0.1)
    assert device.is_connected


async def test_disconnect_stops_reconnecting(device, lamp, clients):
    await device.connect()
    clients[-1].drop()
    await device.disconnect()
    attempts = lamp.connection_attempts
    await asyncio.sleep(0.15)
    assert not device.is_connected
    assert lamp.connection_attempts == attempts


async def test_disconnect_stops_reconnecting_with_a_command_queued(device, lamp):
    """A command failing after disconnect() (e.g. unload) must not start a new loop."""
    lamp.fail_connect = True
    lamp.connect_delay = 0.03
    running = asyncio.create_task(device.turn_on())
    queued = asyncio.create_task(device.update())  # waits for the lock
    await asyncio.sleep(0.01)
    await device.disconnect()
    await asyncio.gather(running, queued, return_exceptions=True)
    assert device._reconnect_task is None or device._reconnect_task.done()
    attempts = lamp.connection_attempts
    await asyncio.sleep(0.2)
    assert lamp.connection_attempts == attempts
    assert not device.is_connected


async def test_disconnect_does_not_wait_for_a_retrying_command(device, lamp):
    lamp.fail_connect = True
    command = asyncio.create_task(device.turn_on())
    await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await device.disconnect()
    assert loop.time() - start < 0.2  # not the whole 0.5 s command deadline
    with pytest.raises(CandelaError):
        await command


async def test_reconnect_continues_when_the_link_drops_right_away(device, lamp, clients):
    await device.connect()
    lamp.drop_after_write = True  # the loop's state read succeeds, then the link dies
    clients[-1].drop()
    await asyncio.sleep(0.3)
    assert device.is_connected
    assert lamp.connection_attempts == 3


async def test_commands_use_the_kept_connection(device, lamp):
    await device.connect()
    await device.turn_off()
    await asyncio.sleep(0.1)
    await device.turn_on()
    assert lamp.connection_attempts == 1
    assert lamp.frames == [protocol.build_off(), protocol.build_on()]


async def test_default_mode_does_not_reconnect(clients, lamp):
    device = CandelaDevice(
        BLEDevice(ADDRESS, "yeelight_ms", {}), response_timeout=0.05, reconnect_delays=(0.02,)
    )
    try:
        await device.turn_on()
        clients[-1].drop()
        await asyncio.sleep(0.1)
        assert not device.is_connected
        assert lamp.connection_attempts == 1
    finally:
        await device.disconnect()


async def test_start_connects_at_once(device, lamp):
    device.start()
    await asyncio.sleep(0.01)
    assert device.is_connected
    assert lamp.frames == [protocol.build_get_state()]


async def test_later_reconnect_failures_are_not_reported_as_unreachable_at_setup(
    device, lamp, clients, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG, logger="custom_components.yeelight_candela")
    device.start()
    await asyncio.sleep(0.02)
    assert device.is_connected  # the first background attempt succeeded
    lamp.fail_connect = True
    clients[-1].drop()  # hours later the link drops and the lamp is gone
    await asyncio.sleep(0.15)
    assert lamp.connection_attempts >= 3
    assert not [record for record in caplog.records if record.levelno >= logging.INFO]


async def test_disconnect_callback_fires_when_the_link_goes_down(device, lamp, clients):
    fired: list[bool] = []
    remove = device.register_disconnect_callback(lambda: fired.append(True))
    await device.connect()
    lamp.fail_connect = True
    clients[-1].drop()  # the link drops
    assert fired == [True]
    lamp.fail_connect = False
    await asyncio.sleep(0.1)
    assert device.is_connected
    lamp.fail_writes = 1  # a silently dead link found by a write
    await device.update()
    assert fired == [True, True]
    remove()
    await device.disconnect()
    assert fired == [True, True]
