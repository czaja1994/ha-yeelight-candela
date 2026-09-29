"""Tests for state updates between commands."""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from unittest.mock import patch

import pytest
from bleak.exc import BleakError
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.yeelight_candela.candela import advertisement, protocol
from custom_components.yeelight_candela.const import POLL_INTERVAL
from tests.integration.helpers import ADDRESS, inject, make_entry, payload_with_flag, service_info

ENTITY_ID = "light.yeelight_candela_deda"


async def test_advertisement_state_applied_when_verified(
    hass: HomeAssistant, setup_entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(advertisement, "ADV_STATE_VERIFIED", True)
    inject(hass, service_info(ADDRESS, payload_with_flag(ADDRESS, 0x00)))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def test_advertisement_state_ignored_when_unverified(
    hass: HomeAssistant, setup_entry
) -> None:
    inject(hass, service_info(ADDRESS, payload_with_flag(ADDRESS, 0x00)))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_ON


@pytest.fixture
async def polled_entry(
    hass: HomeAssistant, bluetooth_ready, clients
) -> AsyncIterator[tuple[MockConfigEntry, Callable[[datetime], Awaitable[None]]]]:
    """A loaded entry and its periodic poll job.

    Tests call the job directly: firing the 5-minute tick also fires HA's own
    unavailable check on the test scanner, and which of the two runs first depends
    on the event loop's timer heap.
    """
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    with patch(
        "custom_components.yeelight_candela.light.async_track_time_interval",
        wraps=async_track_time_interval,
    ) as track:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    yield entry, track.call_args.args[1]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_no_poll_when_lamp_cannot_report(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    """Without notifications a poll can't learn anything, so it must not connect."""
    lamps[ADDRESS].start_notify_error = BleakError("no client config descriptor")
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    with patch(
        "custom_components.yeelight_candela.light.async_track_time_interval",
        wraps=async_track_time_interval,
    ) as track:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    poll = track.call_args.args[1]
    attempts = lamps[ADDRESS].connection_attempts
    await poll(dt_util.utcnow())  # call directly: the entity is available here
    assert lamps[ADDRESS].connection_attempts == attempts
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_poll_corrects_state_changed_by_hand(
    hass: HomeAssistant, polled_entry, lamps
) -> None:
    entry, poll = polled_entry
    lamps[ADDRESS].is_on = False  # someone touched the lamp
    attempts = lamps[ADDRESS].connection_attempts
    await poll(dt_util.utcnow())
    await hass.async_block_till_done()
    assert lamps[ADDRESS].frames[-1] == protocol.build_get_state()
    assert lamps[ADDRESS].connection_attempts == attempts + 1
    assert not entry.runtime_data.is_connected  # the poll used a short connection
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def test_poll_failure_is_swallowed(hass: HomeAssistant, polled_entry, lamps) -> None:
    entry, poll = polled_entry
    await entry.runtime_data.disconnect()
    lamps[ADDRESS].fail_connect = True
    await poll(dt_util.utcnow())
    await hass.async_block_till_done()
    assert lamps[ADDRESS].connection_attempts > 1
    assert hass.states.get(ENTITY_ID).state == STATE_ON


async def test_no_poll_when_advertisement_state_verified(
    hass: HomeAssistant, bluetooth_ready, clients, lamps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(advertisement, "ADV_STATE_VERIFIED", True)
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    frames_after_setup = len(lamps[ADDRESS].frames)
    async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL)
    await hass.async_block_till_done()
    assert len(lamps[ADDRESS].frames) == frames_after_setup
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unavailable_and_back(hass: HomeAssistant, bluetooth_ready, clients) -> None:
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    with patch("homeassistant.components.bluetooth.async_track_unavailable") as track:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    on_unavailable = track.call_args.args[1]

    on_unavailable(service_info(ADDRESS))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE

    inject(
        hass, service_info(ADDRESS, payload_with_flag(ADDRESS, 0x00))
    )  # changed, so HA forwards it
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_ON

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_no_poll_while_unavailable(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    with patch("homeassistant.components.bluetooth.async_track_unavailable") as track:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    track.call_args.args[1](service_info(ADDRESS))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    frames_before = len(lamps[ADDRESS].frames)
    attempts_before = lamps[ADDRESS].connection_attempts

    async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL)
    await hass.async_block_till_done()
    assert len(lamps[ADDRESS].frames) == frames_before
    assert lamps[ADDRESS].connection_attempts == attempts_before

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_poll_timer_is_named_and_cancelled_on_shutdown(
    hass: HomeAssistant, bluetooth_ready, clients
) -> None:
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    with patch(
        "custom_components.yeelight_candela.light.async_track_time_interval",
        wraps=async_track_time_interval,
    ) as track:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert track.call_args.args[2] == POLL_INTERVAL
    assert track.call_args.kwargs == {"name": "yeelight_candela poll", "cancel_on_shutdown": True}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
