"""Light platform for Yeelight Candela."""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from datetime import datetime
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    EFFECT_OFF,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import CandelaConfigEntry
from .candela import advertisement
from .candela.device import CandelaDevice, CandelaError
from .const import DOMAIN, EFFECT_CANDLE, POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


def to_lamp_brightness(ha_brightness: int) -> int:
    """HA brightness (1-255) to lamp percent (1-100)."""
    return max(1, min(100, round(ha_brightness * 100 / 255)))


def to_ha_brightness(pct: int) -> int:
    """Lamp percent (1-100) to HA brightness (1-255)."""
    return max(1, min(255, round(pct * 255 / 100)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CandelaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the light entity for a config entry."""
    async_add_entities([CandelaLight(entry.runtime_data, entry.unique_id, entry.title)])


class CandelaLight(LightEntity):
    """A Yeelight Candela lamp."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [EFFECT_CANDLE]

    def __init__(self, device: CandelaDevice, address: str, title: str) -> None:
        self._device = device
        self._attr_unique_id = address
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            manufacturer="Yeelight",
            model="Candela",
            name=title,
        )

    async def async_added_to_hass(self) -> None:
        address = self._device.address
        self.async_on_remove(self._device.register_callback(self._handle_device_update))
        self.async_on_remove(self._device.register_disconnect_callback(self._handle_disconnect))
        self.async_on_remove(
            bluetooth.async_track_unavailable(
                self.hass, self._handle_unavailable, address, connectable=True
            )
        )
        self.async_on_remove(
            bluetooth.async_register_callback(
                self.hass,
                self._handle_advertisement,
                BluetoothCallbackMatcher(address=address, connectable=True),
                BluetoothScanningMode.PASSIVE,
            )
        )
        if not advertisement.ADV_STATE_VERIFIED:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass,
                    self._async_poll,
                    POLL_INTERVAL,
                    name="yeelight_candela poll",
                    cancel_on_shutdown=True,
                )
            )

    @callback
    def _handle_unavailable(self, _service_info: BluetoothServiceInfoBleak) -> None:
        if self._device.is_connected:
            # A connected lamp may stop advertising; the open link shows it is there.
            return
        self._attr_available = False
        self.async_write_ha_state()

    @callback
    def _handle_disconnect(self) -> None:
        # HA reports a disappearance only once, and it is ignored while connected; so
        # when the link goes down, check whether the lamp is still advertising.
        if not self._attr_available:
            return
        if bluetooth.async_address_present(self.hass, self._device.address, connectable=True):
            return
        self._attr_available = False
        self.async_write_ha_state()

    @callback
    def _handle_advertisement(
        self, _service_info: BluetoothServiceInfoBleak, _change: BluetoothChange
    ) -> None:
        if not self._attr_available:
            self._attr_available = True
            self.async_write_ha_state()

    async def _async_poll(self, _now: datetime) -> None:
        if not self.available:
            return
        # Without the lamp's reports a poll can't learn anything, so don't spend a proxy
        # slot on it; a kept link costs nothing extra and the poll finds it if it died.
        if not self._device.reports_state and not self._device.keep_connected:
            return
        try:
            await self._device.poll()
        except CandelaError as err:
            _LOGGER.debug("Poll of %s failed: %s", self._device.name, err)

    @callback
    def _handle_device_update(self) -> None:
        self.async_write_ha_state()

    @property
    def assumed_state(self) -> bool:
        """True when the state only reflects the commands sent (lamp can't report)."""
        return not self._device.reports_state

    @property
    def is_on(self) -> bool | None:
        return self._device.state.is_on

    @property
    def brightness(self) -> int | None:
        pct = self._device.state.brightness
        return None if pct is None else to_ha_brightness(pct)

    @property
    def effect(self) -> str | None:
        if not self._device.state.is_on:
            return None
        return EFFECT_CANDLE if self._device.state.flicker else EFFECT_OFF

    async def async_turn_on(self, **kwargs: Any) -> None:
        effect = kwargs.get(ATTR_EFFECT)
        if effect == EFFECT_CANDLE:
            await self._run(self._device.set_flicker())
        elif effect == EFFECT_OFF:
            # Always sent, so a flicker HA does not know about (e.g. set by hand) stops too.
            brightness = kwargs.get(ATTR_BRIGHTNESS)
            pct = None if brightness is None else to_lamp_brightness(brightness)
            await self._run(self._device.stop_flicker(pct))
        elif ATTR_BRIGHTNESS in kwargs:
            await self._run(
                self._device.set_brightness(to_lamp_brightness(kwargs[ATTR_BRIGHTNESS]))
            )
        else:
            await self._run(self._device.turn_on())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(self._device.turn_off())

    async def _run(self, command: Awaitable[None]) -> None:
        try:
            await command
        except CandelaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"name": self._device.name},
            ) from err
