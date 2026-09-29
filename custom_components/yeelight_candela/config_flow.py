"""Config flow for Yeelight Candela."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .candela.advertisement import is_candela
from .const import CONF_KEEP_CONNECTED, DOMAIN


def _is_candela(info: BluetoothServiceInfoBleak) -> bool:
    return is_candela(
        info.manufacturer_data, service_uuids=info.service_uuids, local_name=info.name
    )


def entry_title(address: str) -> str:
    """Human-friendly title from the last two address bytes."""
    return f"Yeelight Candela {address.replace(':', '')[-4:].upper()}"


class CandelaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Yeelight Candela."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> CandelaOptionsFlow:
        return CandelaOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not _is_candela(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": entry_title(discovery_info.address)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        title = entry_title(self._discovery.address)
        if user_input is not None:
            return self.async_create_entry(
                title=title, data={CONF_ADDRESS: self._discovery.address}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders={"name": title}
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=entry_title(address), data={CONF_ADDRESS: address})

        configured = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in configured or info.address in self._discovered:
                continue
            if not _is_candela(info):
                continue
            self._discovered[info.address] = entry_title(info.address)

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(self._discovered)}),
        )


class CandelaOptionsFlow(OptionsFlowWithReload):
    """Options for one lamp; the entry reloads when they change."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_KEEP_CONNECTED,
                        default=self.config_entry.options.get(CONF_KEEP_CONNECTED, False),
                    ): bool,
                }
            ),
        )
