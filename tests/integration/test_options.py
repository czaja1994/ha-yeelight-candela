"""Tests for the keep-connected option."""

import asyncio
import logging
import time
from unittest.mock import patch

import pytest
from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from custom_components.yeelight_candela.candela import protocol
from custom_components.yeelight_candela.const import CONF_KEEP_CONNECTED
from tests.integration.helpers import ADDRESS, inject, make_entry, service_info

DELAYS = "custom_components.yeelight_candela.candela.device.RECONNECT_DELAYS_SECONDS"
ENTITY_ID = "light.yeelight_candela_deda"


async def _wait_until_connected(device) -> None:
    """Keep-connected entries connect in the background after setup."""
    for _ in range(100):
        if device.is_connected:
            return
        await asyncio.sleep(0.01)


async def test_default_setup_does_not_keep_the_connection(hass: HomeAssistant, setup_entry) -> None:
    assert not setup_entry.runtime_data.is_connected


async def test_keep_connected_entry_stays_connected(
    hass: HomeAssistant, bluetooth_ready, clients
) -> None:
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass, options={CONF_KEEP_CONNECTED: True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _wait_until_connected(entry.runtime_data)
    assert entry.runtime_data.is_connected
    await hass.async_block_till_done()
    assert hass.states.get("light.yeelight_candela_deda").state == "on"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not clients[-1].is_connected


async def test_keep_connected_entry_loads_while_lamp_unreachable(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    """A lamp that fails its first connection must not orphan reconnect loops."""
    lamps[ADDRESS].fail_connect = True
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass, options={CONF_KEEP_CONNECTED: True})
    with patch(DELAYS, (0.01, 0.02)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert not entry.runtime_data.is_connected
        state = hass.states.get("light.yeelight_candela_deda")
        assert state.state == STATE_UNKNOWN
        assert state.attributes.get(ATTR_ASSUMED_STATE) is True

        lamps[ADDRESS].fail_connect = False
        lamps[ADDRESS].is_on = False
        for _ in range(50):
            await asyncio.sleep(0.01)
            if entry.runtime_data.is_connected:
                break
        await hass.async_block_till_done()
        assert entry.runtime_data.is_connected
        assert hass.states.get("light.yeelight_candela_deda").state == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_keep_connected_setup_does_not_wait_for_the_lamp(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    lamps[ADDRESS].connect_delay = 10  # a lamp that is very slow to accept a connection
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass, options={CONF_KEEP_CONNECTED: True})
    start = time.monotonic()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert time.monotonic() - start < 0.5
    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get("light.yeelight_candela_deda")
    assert state.state == STATE_UNKNOWN
    assert state.attributes.get(ATTR_ASSUMED_STATE) is True
    for _ in range(100):  # the background connection starts right after setup
        if lamps[ADDRESS].connection_attempts:
            break
        await asyncio.sleep(0.01)
    assert lamps[ADDRESS].connection_attempts == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unreachable_kept_lamp_is_logged_once_at_info(
    hass: HomeAssistant, bluetooth_ready, clients, lamps, caplog: pytest.LogCaptureFixture
) -> None:
    lamps[ADDRESS].fail_connect = True
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass, options={CONF_KEEP_CONNECTED: True})
    caplog.set_level(logging.INFO, logger="custom_components.yeelight_candela")
    with patch(DELAYS, (0.01,)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for _ in range(50):
            await asyncio.sleep(0.01)
        assert lamps[ADDRESS].connection_attempts >= 3
        info = [
            record
            for record in caplog.records
            if record.levelno >= logging.INFO and "Yeelight Candela DEDA" in record.getMessage()
        ]
        assert len(info) == 1
        assert "background" in info[0].getMessage()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_connected_lamp_stays_available_when_it_stops_advertising(
    hass: HomeAssistant, bluetooth_ready, clients
) -> None:
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass, options={CONF_KEEP_CONNECTED: True})
    with patch("homeassistant.components.bluetooth.async_track_unavailable") as track:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    on_unavailable = track.call_args.args[1]
    await _wait_until_connected(entry.runtime_data)

    on_unavailable(service_info(ADDRESS))  # connected lamps may stop advertising
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_ON

    await entry.runtime_data.disconnect()
    on_unavailable(service_info(ADDRESS))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(("present", "expected"), [(False, STATE_UNAVAILABLE), (True, STATE_ON)])
async def test_lost_link_rechecks_whether_the_lamp_is_still_there(
    hass: HomeAssistant, bluetooth_ready, clients, lamps, present: bool, expected: str
) -> None:
    """HA reports a disappearance once; if it was ignored while connected, re-check."""
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass, options={CONF_KEEP_CONNECTED: True})
    with patch("homeassistant.components.bluetooth.async_track_unavailable") as track:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    await _wait_until_connected(entry.runtime_data)
    track.call_args.args[1](service_info(ADDRESS))  # ignored: still connected
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_ON

    lamps[ADDRESS].fail_connect = True  # the lamp is really gone
    with patch(
        "homeassistant.components.bluetooth.async_address_present", return_value=present
    ) as address_present:
        clients[-1].drop()
        await hass.async_block_till_done()
    address_present.assert_called_with(hass, ADDRESS, connectable=True)
    assert hass.states.get(ENTITY_ID).state == expected

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_kept_link_is_polled_even_without_lamp_reports(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    """The poll finds a silently dead kept link (it costs no extra proxy slot)."""
    lamps[ADDRESS].start_notify_error = BleakError("no client config descriptor")
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass, options={CONF_KEEP_CONNECTED: True})
    with patch(
        "custom_components.yeelight_candela.light.async_track_time_interval",
        wraps=async_track_time_interval,
    ) as track:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    poll = track.call_args.args[1]
    await _wait_until_connected(entry.runtime_data)
    assert not entry.runtime_data.reports_state
    attempts = lamps[ADDRESS].connection_attempts

    lamps[ADDRESS].fail_writes = 1  # the kept link died without the proxy noticing
    await poll(dt_util.utcnow())
    await hass.async_block_till_done()
    assert lamps[ADDRESS].frames[-1] == protocol.build_get_state()
    assert lamps[ADDRESS].connection_attempts == attempts + 1  # replaced by a new link
    assert entry.runtime_data.is_connected

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_flow_enables_keep_connected_and_reloads(
    hass: HomeAssistant, setup_entry
) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_KEEP_CONNECTED: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert setup_entry.options == {CONF_KEEP_CONNECTED: True}
    assert setup_entry.state is ConfigEntryState.LOADED
    await _wait_until_connected(setup_entry.runtime_data)
    assert setup_entry.runtime_data.is_connected  # reloaded with the new option
