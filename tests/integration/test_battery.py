"""Tests for the battery, external power and charging entities."""

from bleak.exc import BleakError
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from tests.integration.helpers import ADDRESS, inject, make_entry, service_info

BATTERY = "sensor.yeelight_candela_deda_battery"
PLUG = "binary_sensor.yeelight_candela_deda_plug"
CHARGING = "binary_sensor.yeelight_candela_deda_charging"


async def test_setup_reads_the_battery(hass: HomeAssistant, setup_entry) -> None:
    battery = hass.states.get(BATTERY)
    assert battery.state == "100"
    assert battery.attributes["status"] == 0x03
    assert battery.attributes["input_raw"] == 0x0A71
    assert battery.attributes["battery_raw"] == 0x08EB
    assert hass.states.get(PLUG).state == STATE_ON
    assert hass.states.get(CHARGING).state == STATE_OFF


async def test_charging_lamp(hass: HomeAssistant, bluetooth_ready, clients, lamps) -> None:
    lamps[ADDRESS].battery = 57
    lamps[ADDRESS].battery_status = 0x02
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(BATTERY).state == "57"
    assert hass.states.get(CHARGING).state == STATE_ON
    assert hass.states.get(PLUG).state == STATE_ON
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_battery_unknown_when_the_lamp_cannot_report(
    hass: HomeAssistant, bluetooth_ready, clients, lamps
) -> None:
    lamps[ADDRESS].start_notify_error = BleakError("no client config descriptor")
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(BATTERY).state == "unknown"
    assert hass.states.get(PLUG).state == "unknown"
    assert hass.states.get(CHARGING).state == "unknown"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
