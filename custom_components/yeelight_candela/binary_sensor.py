"""External power sensor for Yeelight Candela."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
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
    """Add the external power sensor for a config entry."""
    async_add_entities([CandelaPower(entry.runtime_data, entry.unique_id, entry.title)])


class CandelaPower(BinarySensorEntity):
    """On when the lamp runs from its base, off on battery; unknown until it reports.

    Derived from the raw readings in the battery report, read with the state poll.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PLUG
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: CandelaDevice, address: str, title: str) -> None:
        self._device = device
        self._attr_unique_id = f"{address}_external_power"
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
    def is_on(self) -> bool | None:
        report = self._device.state.battery_report
        return None if report is None else report.on_power
