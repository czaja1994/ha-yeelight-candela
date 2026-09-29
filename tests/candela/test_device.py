"""Tests for CandelaDevice command handling and state."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.yeelight_candela.candela import protocol
from custom_components.yeelight_candela.candela.device import CandelaDevice, CandelaError
from tests.fakes import FakeClient, FakeLamp, make_establish, pad

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
        response_timeout=0.05,
        command_timeout=0.5,
        retry_pause=0.01,
    )
    yield device
    await device.disconnect()


async def test_turn_off_sends_frame_and_takes_state_from_notification(device, lamp):
    await device.turn_off()
    assert lamp.frames == [protocol.build_off()]
    assert device.state.is_on is False
    assert device.state.brightness == 100


async def test_update_reads_state(device, lamp):
    lamp.brightness = 0x69  # the lamp once reported 105
    await device.update()
    assert lamp.frames == [protocol.build_get_state()]
    assert device.state.is_on is True
    assert device.state.brightness == 100


async def test_brightness_when_off_turns_on_first(device, lamp):
    await device.turn_off()
    await device.set_brightness(40)
    assert lamp.frames[1:] == [protocol.build_on(), protocol.build_brightness(40)]
    assert device.state.is_on is True
    assert device.state.brightness == 40


async def test_brightness_when_on_sends_single_frame(device, lamp):
    await device.update()
    await device.set_brightness(150)
    assert lamp.frames[1:] == [protocol.build_brightness(100)]


async def test_flicker_turns_on_and_starts_effect(device, lamp):
    await device.set_flicker()
    assert lamp.frames == [protocol.build_on(), protocol.build_flicker()]
    assert device.state.flicker is True
    assert device.state.is_on is True


async def test_turn_on_while_flickering_exits_flicker(device, lamp):
    await device.update()
    await device.set_flicker()
    await device.turn_on()
    assert lamp.frames[-1:] == protocol.build_exit_flicker(100)
    assert device.state.flicker is False


async def test_turn_off_ends_flicker(device):
    await device.set_flicker()
    await device.turn_off()
    assert device.state.flicker is False
    assert device.state.is_on is False


async def test_optimistic_state_when_notifications_unavailable(device, lamp):
    lamp.start_notify_error = BleakError("attribute could not be found")
    await device.turn_off()
    assert lamp.frames == [protocol.build_off()]
    assert device.state.is_on is False
    await device.set_brightness(30)
    assert lamp.frames[1:] == [protocol.build_on(), protocol.build_brightness(30)]
    assert device.state.is_on is True
    assert device.state.brightness == 30


async def test_lamp_report_wins_over_optimistic_value(device, lamp):
    lamp.respond = lambda frame: [pad(0x43, 0x45, 0x01, 0x46)]  # lamp settles at 70
    await device.set_brightness(40)
    assert device.state.brightness == 70


async def test_brightness_zero_is_clamped_to_one(device, lamp):
    await device.update()
    await device.set_brightness(0)
    assert lamp.frames[-1] == protocol.build_brightness(1)
    assert device.state.brightness == 1


async def test_commands_reuse_one_connection(device, lamp):
    await device.turn_on()
    await device.turn_off()
    await device.update()
    assert lamp.connection_attempts == 1


async def test_rapid_brightness_changes_end_at_the_last_one(device, lamp):
    await device.update()
    await asyncio.gather(*(device.set_brightness(pct) for pct in (10, 20, 30, 40, 50)))
    written = [frame[2] for frame in lamp.frames[1:]]
    # Queued changes are superseded by newer ones; whatever is written stays in order.
    assert written[-1] == 50
    assert written == sorted(written)
    assert device.state.brightness == 50
    assert lamp.brightness == 50


async def test_newer_command_supersedes_one_still_retrying(device, lamp):
    lamp.fail_connect = True
    seen: list[bool | None] = []
    device.register_callback(lambda: seen.append(device.state.is_on))
    turn_off = asyncio.create_task(device.turn_off())
    await asyncio.sleep(0.05)  # turn_off is retrying against the unreachable lamp
    turn_on = asyncio.create_task(device.turn_on())
    await asyncio.sleep(0.05)
    lamp.fail_connect = False
    assert await turn_off is None  # returns quietly, no error for HA
    await turn_on
    assert lamp.frames == [protocol.build_on()]
    assert lamp.is_on is True
    assert device.state.is_on is True
    assert False not in seen  # the superseded state was never applied


async def test_superseded_command_releases_the_lock_promptly(clients, lamp):
    device = CandelaDevice(
        BLEDevice(ADDRESS, "yeelight_ms", {}),
        response_timeout=0.05,
        command_timeout=10.0,
        retry_pause=5.0,
    )
    try:
        lamp.fail_connect = True
        turn_off = asyncio.create_task(device.turn_off())
        await asyncio.sleep(0.05)  # first attempt failed; now in the long pause
        lamp.fail_connect = False
        await asyncio.wait_for(device.set_brightness(40), 1.0)
        assert await asyncio.wait_for(turn_off, 1.0) is None
        assert protocol.build_off() not in lamp.frames
        assert device.state.brightness == 40
    finally:
        await device.disconnect()


async def test_reads_are_neither_superseded_nor_superseding(device, lamp):
    lamp.fail_connect = True
    update = asyncio.create_task(device.update())
    await asyncio.sleep(0.05)
    turn_off = asyncio.create_task(device.turn_off())
    poll = asyncio.create_task(device.poll(persistent=True))
    await asyncio.sleep(0.05)
    lamp.fail_connect = False
    await asyncio.gather(update, turn_off, poll)
    assert lamp.frames == [
        protocol.build_get_state(),
        protocol.build_off(),
        protocol.build_get_state(),
    ]
    assert device.state.is_on is False


async def test_off_then_brightness_race_turns_lamp_back_on(device, lamp):
    await device.update()
    await asyncio.gather(device.turn_off(), device.set_brightness(40))
    assert lamp.frames[1:] == [
        protocol.build_off(),
        protocol.build_on(),
        protocol.build_brightness(40),
    ]
    assert device.state.is_on is True


async def test_unsolicited_notification_updates_state_and_fires_callback(device, clients):
    await device.update()
    fired: list[bool] = []
    remove = device.register_callback(lambda: fired.append(True))
    clients[-1].push(pad(0x43, 0x45, 0x02, 0x32))
    assert device.state.is_on is False
    assert device.state.brightness == 50
    assert fired == [True]
    remove()
    clients[-1].push(pad(0x43, 0x45, 0x01, 0x32))
    assert fired == [True]


async def test_command_fires_callback_once(device):
    fired: list[bool] = []
    device.register_callback(lambda: fired.append(True))
    await device.set_brightness(40)
    assert fired == [True]


async def test_update_from_advertisement_fires_only_on_change(device):
    fired: list[bool] = []
    device.register_callback(lambda: fired.append(True))
    device.update_from_advertisement(True)
    device.update_from_advertisement(True)
    device.update_from_advertisement(False)
    assert fired == [True, True]
    assert device.state.is_on is False


async def test_send_raw_writes_frame_verbatim(device, lamp):
    raw = bytes.fromhex("434228").ljust(18, b"\x00")
    await device.send_raw(raw)
    assert lamp.frames == [raw]


async def test_late_reply_to_earlier_frame_does_not_override_final_state(device, lamp):
    lamp.is_on = False
    lamp.write_delay = 0.02  # the reply to frame 1 lands while frame 2 is being written
    lamp.notify_delay = 0.01
    await device.update()
    fired: list[bool] = []
    device.register_callback(lambda: fired.append(True))
    await device.set_brightness(40)
    assert lamp.frames[1:] == [protocol.build_on(), protocol.build_brightness(40)]
    assert device.state.brightness == 40
    assert device.state.is_on is True
    assert fired == [True]


async def test_start_notify_failure_other_than_bleak_error_does_not_leak_connection(
    device, lamp, clients
):
    lamp.start_notify_error = TimeoutError()
    with pytest.raises(CandelaError):
        await device.turn_on()
    assert not clients[-1].is_connected
    assert not device.is_connected


async def test_stop_flicker_exits_even_when_state_says_no_flicker(device, lamp):
    await device.update()
    lamp.flicker = True  # set by hand; the device does not know
    await device.stop_flicker()
    assert lamp.frames[1:] == protocol.build_exit_flicker(100)
    assert lamp.flicker is False
    assert device.state.flicker is False
    assert device.state.is_on is True


async def test_stop_flicker_with_brightness(device, lamp):
    await device.set_flicker()
    await device.stop_flicker(40)
    assert lamp.frames[-1:] == protocol.build_exit_flicker(40)
    assert device.state.flicker is False
    assert device.state.brightness == 40


async def test_stop_flicker_keeps_last_brightness(device, lamp):
    await device.set_brightness(30)
    await device.stop_flicker()
    assert lamp.frames[-1:] == protocol.build_exit_flicker(30)
    assert device.state.brightness == 30


async def test_stop_flicker_when_off_turns_on_first(device, lamp):
    await device.turn_off()
    await device.stop_flicker()
    assert lamp.frames[1:] == [protocol.build_on(), *protocol.build_exit_flicker(100)]
    assert device.state.is_on is True


async def test_queued_stop_flicker_uses_brightness_set_before_it(device, lamp):
    await device.update()
    await asyncio.gather(device.set_brightness(30), device.stop_flicker())
    assert lamp.frames[-1:] == protocol.build_exit_flicker(30)
    assert device.state.brightness == 30
