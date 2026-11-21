"""Battery sensor for Yeelight Candela."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CandelaConfigEntry
from .candela.device import CandelaDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CandelaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the battery sensor for a config entry."""
    async_add_entities([CandelaBattery(entry.runtime_data, entry.unique_id, entry.title)])


class CandelaBattery(SensorEntity):
    """Battery level the lamp reports; unknown until it has answered once.

    The lamp only reports through a backend that delivers its notifications (an
    ESPHome proxy), and it is read with the regular state poll.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: CandelaDevice, address: str, title: str) -> None:
        self._device = device
        self._attr_unique_id = f"{address}_battery"
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            manufacturer="Yeelight",
            model="Candela",
            name=title,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.register_callback(self._handle_device_update))

    @callback
    def _handle_device_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        return self._device.state.battery

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """Raw values of the last report, kept for decoding them further."""
        report = self._device.state.battery_report
        if report is None or report.input_raw is None or report.battery_raw is None:
            return None
        return {
            "status": report.status,
            "input_raw": report.input_raw,
            "battery_raw": report.battery_raw,
        }
