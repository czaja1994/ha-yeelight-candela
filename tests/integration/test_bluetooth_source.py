"""Tests for pinning a lamp to one Bluetooth adapter or proxy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import bleak_retry_connector
import pytest
from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.yeelight_candela.bluetooth_source import pinned_client_class
from custom_components.yeelight_candela.config_flow import source_options
from custom_components.yeelight_candela.const import (
    CONF_BLUETOOTH_SOURCE,
    CONF_KEEP_CONNECTED,
    SOURCE_AUTOMATIC,
)
from tests.integration.helpers import ADDRESS, inject, make_entry, service_info

PROXY = "AA:BB:CC:DD:EE:01"
LOCAL = "00:11:22:33:44:55"
CONFIG_FLOW = "custom_components.yeelight_candela.config_flow"


class FakeWrapper:
    """Just the parts of Home Assistant's client wrapper that the pinned client uses."""

    def __init__(self, address: str, *args: Any, **kwargs: Any) -> None:
        self._HaBleakClientWrapper__address = address

    def _async_get_best_available_backend_and_device(self, manager: Any) -> str:
        return "automatic"

    def _async_get_backend_for_ble_device(
        self, manager: Any, scanner: Any, ble_device: Any
    ) -> tuple[str, str] | None:
        return None if scanner.full else ("backend", scanner.source)


def path(source: str, rssi: int, *, full: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        scanner=SimpleNamespace(source=source, name=f"scanner {source}", full=full),
        ble_device=object(),
        advertisement=SimpleNamespace(rssi=rssi),
    )


class FakeManager:
    def __init__(self, paths: list[SimpleNamespace], online: set[str]) -> None:
        self.paths = paths
        self.online = online

    def async_scanner_by_source(self, source: str) -> object | None:
        return object() if source in self.online else None

    def async_scanner_devices_by_address(
        self, address: str, connectable: bool
    ) -> list[SimpleNamespace]:
        assert address == ADDRESS
        assert connectable
        return self.paths


@pytest.fixture
def wrapper() -> Any:
    with patch.object(bleak_retry_connector, "BleakClientWithServiceCache", FakeWrapper):
        yield FakeWrapper


def test_pinned_source_wins_over_a_better_signal(wrapper) -> None:
    client = pinned_client_class(PROXY)(ADDRESS)
    manager = FakeManager([path(LOCAL, -60), path(PROXY, -75)], {LOCAL, PROXY})
    assert client._async_get_best_available_backend_and_device(manager) == ("backend", PROXY)


def test_pinned_source_that_does_not_see_the_lamp_fails(wrapper) -> None:
    client = pinned_client_class(PROXY)(ADDRESS)
    manager = FakeManager([path(LOCAL, -60)], {LOCAL, PROXY})
    with pytest.raises(BleakError, match="does not see the lamp"):
        client._async_get_best_available_backend_and_device(manager)


def test_pinned_source_without_a_free_slot_fails(wrapper) -> None:
    client = pinned_client_class(PROXY)(ADDRESS)
    manager = FakeManager([path(LOCAL, -60), path(PROXY, -75, full=True)], {LOCAL, PROXY})
    with pytest.raises(BleakError, match="no free connection slot"):
        client._async_get_best_available_backend_and_device(manager)


def test_offline_pinned_source_falls_back_to_automatic(wrapper) -> None:
    client = pinned_client_class(PROXY)(ADDRESS)
    manager = FakeManager([path(LOCAL, -60)], {LOCAL})
    assert client._async_get_best_available_backend_and_device(manager) == "automatic"


def test_no_pinning_without_home_assistants_wrapper() -> None:
    with patch.object(bleak_retry_connector, "BleakClientWithServiceCache", object):
        assert pinned_client_class(PROXY) is None


def test_no_pinning_without_a_source(wrapper) -> None:
    assert pinned_client_class("") is None


async def test_pinned_entry_connects_with_the_pinned_client(
    hass: HomeAssistant, bluetooth_ready, clients, wrapper
) -> None:
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass, options={CONF_BLUETOOTH_SOURCE: PROXY})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data._client_class.pinned_source == PROXY

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_automatic_entry_uses_the_default_client(hass: HomeAssistant, setup_entry) -> None:
    assert setup_entry.runtime_data._client_class is None


def _scanners() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(source=LOCAL, name="hci0", connectable=True),
        SimpleNamespace(source=PROXY, name="ble-proxy", connectable=True),
        SimpleNamespace(source="AA:BB:CC:DD:EE:02", name="shelly", connectable=False),
    ]


def test_source_options_list_connectable_sources_by_signal(hass: HomeAssistant) -> None:
    with (
        patch(f"{CONFIG_FLOW}.async_current_scanners", return_value=_scanners()),
        patch(
            f"{CONFIG_FLOW}.async_scanner_devices_by_address",
            return_value=[path(LOCAL, -80), path(PROXY, -60)],
        ),
    ):
        options = source_options(hass, ADDRESS, SOURCE_AUTOMATIC)
    assert [option["value"] for option in options] == [SOURCE_AUTOMATIC, PROXY, LOCAL]
    assert options[1]["label"] == "ble-proxy (-60 dBm)"


def test_source_options_keep_an_offline_choice(hass: HomeAssistant) -> None:
    with (
        patch(f"{CONFIG_FLOW}.async_current_scanners", return_value=_scanners()[:1]),
        patch(f"{CONFIG_FLOW}.async_scanner_devices_by_address", return_value=[]),
    ):
        options = source_options(hass, ADDRESS, PROXY)
    assert [option["value"] for option in options] == [SOURCE_AUTOMATIC, LOCAL, PROXY]
    assert options[1]["label"] == "hci0 (does not hear the lamp)"
    assert options[2]["label"] == f"{PROXY} (offline)"


async def test_options_flow_stores_the_chosen_source(hass: HomeAssistant, setup_entry) -> None:
    with (
        patch(f"{CONFIG_FLOW}.async_current_scanners", return_value=_scanners()),
        patch(f"{CONFIG_FLOW}.async_scanner_devices_by_address", return_value=[]),
    ):
        result = await hass.config_entries.options.async_init(setup_entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_KEEP_CONNECTED: False, CONF_BLUETOOTH_SOURCE: PROXY}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert setup_entry.options == {CONF_KEEP_CONNECTED: False, CONF_BLUETOOTH_SOURCE: PROXY}
