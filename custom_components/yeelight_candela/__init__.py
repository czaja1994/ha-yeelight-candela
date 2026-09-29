"""Yeelight Candela integration."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .candela import advertisement
from .candela.device import CandelaDevice, CandelaError
from .const import CONF_KEEP_CONNECTED, DOMAIN, SETUP_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT]

type CandelaConfigEntry = ConfigEntry[CandelaDevice]


async def async_setup_entry(hass: HomeAssistant, entry: CandelaConfigEntry) -> bool:
    """Set up a Candela lamp from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"address": address},
        )

    keep_connected = entry.options.get(CONF_KEEP_CONNECTED, False)
    device = CandelaDevice(ble_device, name=entry.title, keep_connected=keep_connected)
    if not keep_connected:
        try:
            # Its own, shorter budget: a missing lamp must not hold up startup for long.
            await device.poll(time_limit=SETUP_TIMEOUT_SECONDS, persistent=True)
        except CandelaError as err:
            await device.disconnect()
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"address": address},
            ) from err

    @callback
    def _async_on_advertisement(
        service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        device.set_ble_device(service_info.device)
        info = advertisement.parse_manufacturer_data(service_info.manufacturer_data)
        if info is not None and (is_on := advertisement.adv_is_on(info)) is not None:
            device.update_from_advertisement(is_on)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_on_advertisement,
            BluetoothCallbackMatcher(address=address, connectable=True),
            BluetoothScanningMode.PASSIVE,
        )
    )

    async def _async_stop(_event: Event) -> None:
        # HA does not unload entries on stop; do not leave a BLE link open.
        await device.disconnect()

    # async_listen rather than async_listen_once: removing a once-listener that already
    # fired (unload after stop) logs "Unable to remove unknown job listener".
    entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_stop))
    entry.runtime_data = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if keep_connected:
        # Connect in the background: a lamp that is slow to accept a connection must
        # not block startup. The entity shows an unknown state until the lamp reports.
        device.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CandelaConfigEntry) -> bool:
    """Unload a config entry and release the BLE connection."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.disconnect()
    return unload_ok
