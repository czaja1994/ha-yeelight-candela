"""Tests for config entry setup and unload."""

import time

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from custom_components.yeelight_candela import const
from custom_components.yeelight_candela.candela import protocol
from tests.integration.helpers import ADDRESS, inject, make_entry, payload_with_flag, service_info


async def test_setup_reads_initial_state(hass: HomeAssistant, setup_entry, lamps) -> None:
    assert setup_entry.state is ConfigEntryState.LOADED
    assert lamps[ADDRESS].frames == [protocol.build_get_state()]
    assert setup_entry.runtime_data.state.is_on is True


async def test_setup_reads_state_over_short_connection(
    hass: HomeAssistant, setup_entry, clients
) -> None:
    assert not setup_entry.runtime_data.is_connected
    assert not clients[-1].is_connected


async def test_setup_retries_when_lamp_not_seen(
    hass: HomeAssistant, bluetooth_ready, clients
) -> None:
    entry = make_entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_when_lamp_unreachable(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    lamps[ADDRESS].fail_connect = True
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_keeps_connecting_within_its_budget(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    lamps[ADDRESS].fail_connects = 4  # more than one establish_connection round
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert lamps[ADDRESS].frames == [protocol.build_get_state()]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_gives_up_after_its_budget_not_the_command_deadline(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    """The setup read has its own, shorter budget (patched to 0.3 s; commands: 2 s)."""
    lamps[ADDRESS].connect_delay = 10
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    start = time.monotonic()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert time.monotonic() - start < 1.0
    assert entry.state is ConfigEntryState.SETUP_RETRY


def test_setup_budget_is_shorter_than_a_command() -> None:
    assert const.SETUP_TIMEOUT_SECONDS == 20.0


async def test_unload_disconnects(hass: HomeAssistant, setup_entry, clients) -> None:
    await setup_entry.runtime_data.turn_on()  # open a connection (setup's read was short)
    assert clients[-1].is_connected
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    assert not clients[-1].is_connected


async def test_advertisement_refreshes_ble_device(hass: HomeAssistant, setup_entry, lamps) -> None:
    newer = service_info(ADDRESS, payload_with_flag(ADDRESS, 0x00))  # must differ, or HA drops it
    inject(hass, newer)
    await hass.async_block_till_done()
    await setup_entry.runtime_data.disconnect()
    await setup_entry.runtime_data.update()
    assert lamps[ADDRESS].devices[-1] is newer.device


async def test_home_assistant_stop_disconnects(hass: HomeAssistant, setup_entry, clients) -> None:
    await setup_entry.runtime_data.turn_on()
    assert clients[-1].is_connected
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert not clients[-1].is_connected
    assert not setup_entry.runtime_data.is_connected
