"""Fixtures for Home Assistant integration tests."""

from collections.abc import AsyncIterator, Iterator
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.fakes import FakeClient, FakeLamp, make_establish
from tests.integration.helpers import ADDRESS, ADDRESS_2, inject, make_entry, service_info

ESTABLISH = "custom_components.yeelight_candela.candela.device.establish_connection"
DEVICE = "custom_components.yeelight_candela.candela.device"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading custom_components in every integration test."""


@pytest.fixture(autouse=True)
def auto_mock_bluetooth(mock_bluetooth: None) -> None:
    """Keep HA's Bluetooth stack away from real radios."""


@pytest.fixture(autouse=True)
def short_command_deadline() -> Iterator[None]:
    """Commands retry until their deadline; keep that and the waits short in tests."""
    with (
        patch(f"{DEVICE}.COMMAND_TIMEOUT_SECONDS", 2.0),
        patch(f"{DEVICE}.CONNECT_RETRY_PAUSE_SECONDS", 0.01),
        patch(f"{DEVICE}.RESPONSE_TIMEOUT_SECONDS", 0.1),
        patch("custom_components.yeelight_candela.SETUP_TIMEOUT_SECONDS", 0.3),
    ):
        yield


@pytest.fixture
async def bluetooth_ready(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "bluetooth", {})
    await hass.async_block_till_done()


@pytest.fixture
def lamps() -> dict[str, FakeLamp]:
    return {ADDRESS: FakeLamp(), ADDRESS_2: FakeLamp()}


@pytest.fixture
def clients(lamps: dict[str, FakeLamp]) -> Iterator[list[FakeClient]]:
    establish, clients = make_establish(lamps)
    with patch(ESTABLISH, establish):
        yield clients


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, bluetooth_ready: None, clients: list[FakeClient]
) -> AsyncIterator[MockConfigEntry]:
    inject(hass, service_info(ADDRESS))
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
