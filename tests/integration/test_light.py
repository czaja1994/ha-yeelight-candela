"""Tests for the Candela light entity."""

import time

import pytest
from bleak.exc import BleakError
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_SUPPORTED_COLOR_MODES,
    EFFECT_OFF,
    ColorMode,
)
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.yeelight_candela.candela import protocol
from custom_components.yeelight_candela.light import to_ha_brightness, to_lamp_brightness
from tests.integration.helpers import ADDRESS, ADDRESS_2, inject, make_entry, service_info

ENTITY_ID = "light.yeelight_candela_deda"
ENTITY_ID_2 = "light.yeelight_candela_a098"


async def test_assumed_state_when_lamp_cannot_report(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    lamps[ADDRESS].start_notify_error = BleakError("no client config descriptor")
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).attributes.get(ATTR_ASSUMED_STATE) is True
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_no_assumed_state_when_lamp_reports(hass: HomeAssistant, setup_entry) -> None:
    assert ATTR_ASSUMED_STATE not in hass.states.get(ENTITY_ID).attributes


async def _call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
    )


@pytest.mark.parametrize(("ha", "pct"), [(1, 1), (3, 1), (128, 50), (255, 100)])
def test_to_lamp_brightness(ha: int, pct: int) -> None:
    assert to_lamp_brightness(ha) == pct


@pytest.mark.parametrize(("pct", "ha"), [(1, 3), (50, 128), (100, 255)])
def test_to_ha_brightness(pct: int, ha: int) -> None:
    assert to_ha_brightness(pct) == ha


async def test_initial_state(hass: HomeAssistant, setup_entry) -> None:
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 255
    assert state.attributes[ATTR_EFFECT] == EFFECT_OFF
    assert state.attributes[ATTR_EFFECT_LIST] == ["Candle"]
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.BRIGHTNESS]


async def test_turn_off(hass: HomeAssistant, setup_entry, lamps) -> None:
    await _call(hass, SERVICE_TURN_OFF)
    assert lamps[ADDRESS].frames[-1] == protocol.build_off()
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def test_turn_on_with_brightness_from_off(hass: HomeAssistant, setup_entry, lamps) -> None:
    await _call(hass, SERVICE_TURN_OFF)
    await _call(hass, SERVICE_TURN_ON, **{ATTR_BRIGHTNESS: 128})
    assert lamps[ADDRESS].frames[-2:] == [protocol.build_on(), protocol.build_brightness(50)]
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128


async def test_brightness_pct_one_maps_to_lamp_one(hass: HomeAssistant, setup_entry, lamps) -> None:
    await _call(hass, SERVICE_TURN_ON, **{ATTR_BRIGHTNESS_PCT: 1})
    assert lamps[ADDRESS].frames[-1] == protocol.build_brightness(1)


async def test_candle_effect(hass: HomeAssistant, setup_entry, lamps) -> None:
    await _call(hass, SERVICE_TURN_ON, **{ATTR_EFFECT: "Candle"})
    assert lamps[ADDRESS].frames[-2:] == [protocol.build_on(), protocol.build_flicker()]
    assert hass.states.get(ENTITY_ID).attributes[ATTR_EFFECT] == "Candle"


async def test_turn_on_while_flickering_stops_effect(
    hass: HomeAssistant, setup_entry, lamps
) -> None:
    await _call(hass, SERVICE_TURN_ON, **{ATTR_EFFECT: "Candle"})
    await _call(hass, SERVICE_TURN_ON)
    assert lamps[ADDRESS].frames[-1:] == protocol.build_exit_flicker(100)
    assert hass.states.get(ENTITY_ID).attributes[ATTR_EFFECT] == EFFECT_OFF


async def test_effect_off_stops_flicker_set_outside_home_assistant(
    hass: HomeAssistant, setup_entry, lamps
) -> None:
    lamps[ADDRESS].flicker = True  # e.g. set by hand; HA still shows no effect
    await _call(hass, SERVICE_TURN_ON, **{ATTR_EFFECT: EFFECT_OFF})
    assert lamps[ADDRESS].frames[-1:] == protocol.build_exit_flicker(100)
    assert lamps[ADDRESS].flicker is False
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_EFFECT] == EFFECT_OFF


async def test_effect_off_with_brightness(hass: HomeAssistant, setup_entry, lamps) -> None:
    await _call(hass, SERVICE_TURN_ON, **{ATTR_EFFECT: "Candle"})
    await _call(hass, SERVICE_TURN_ON, **{ATTR_EFFECT: EFFECT_OFF, ATTR_BRIGHTNESS: 128})
    assert lamps[ADDRESS].frames[-1:] == protocol.build_exit_flicker(50)
    assert lamps[ADDRESS].flicker is False
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_EFFECT] == EFFECT_OFF
    assert state.attributes[ATTR_BRIGHTNESS] == 128


async def test_command_failure_raises_home_assistant_error(
    hass: HomeAssistant, setup_entry, lamps
) -> None:
    await setup_entry.runtime_data.disconnect()
    lamps[ADDRESS].fail_connect = True
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, SERVICE_TURN_OFF)
    assert err.value.translation_key == "command_failed"
    assert err.value.translation_placeholders == {"name": "Yeelight Candela DEDA"}
    assert hass.states.get(ENTITY_ID).state == STATE_ON


async def test_group_switches_both_lamps(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    entries = []
    for address in (ADDRESS, ADDRESS_2):
        inject(hass, service_info(address))
        entry = make_entry(hass, address)
        assert await hass.config_entries.async_setup(entry.entry_id)
        entries.append(entry)
    await hass.async_block_till_done()

    for lamp in lamps.values():
        lamp.write_delay = 0.2
    start = time.monotonic()
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: [ENTITY_ID, ENTITY_ID_2]}, blocking=True
    )
    # Driven concurrently: one write time, not two.
    assert time.monotonic() - start < 0.35
    assert lamps[ADDRESS].frames[-1] == protocol.build_off()
    assert lamps[ADDRESS_2].frames[-1] == protocol.build_off()
    assert hass.states.get(ENTITY_ID).state == STATE_OFF
    assert hass.states.get(ENTITY_ID_2).state == STATE_OFF

    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
