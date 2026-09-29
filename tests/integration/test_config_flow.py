"""Tests for the Yeelight Candela config flow."""

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.yeelight_candela.const import DOMAIN
from tests.integration.helpers import (
    ADDRESS,
    ADDRESS_2,
    NOT_CANDELA_PAYLOAD,
    SERVICE_UUID,
    make_entry,
    service_info,
)

SETUP = "custom_components.yeelight_candela.async_setup_entry"
DISCOVERED = "custom_components.yeelight_candela.config_flow.async_discovered_service_info"


async def test_bluetooth_discovery_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Yeelight Candela DEDA"
    assert result["data"] == {CONF_ADDRESS: ADDRESS}
    assert result["result"].unique_id == ADDRESS


async def test_bluetooth_discovery_from_passive_scanner(hass: HomeAssistant) -> None:
    """A passive proxy only sees the company id; the lamp must still be offered."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(payload=b"")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"


async def test_user_step_offers_lamps_seen_by_passive_scanner(hass: HomeAssistant) -> None:
    with patch(DISCOVERED, return_value=[service_info(ADDRESS, b"")]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    choices = next(iter(result["data_schema"].schema.values())).container
    assert choices == {ADDRESS: "Yeelight Candela DEDA"}


async def test_user_step_skips_other_yeelight_devices_with_empty_payload(
    hass: HomeAssistant,
) -> None:
    """Other Yeelight devices can also advertise an empty Yeelink payload."""
    other = service_info(ADDRESS_2, b"", name="yeelight_other", service_uuids=[])
    with patch(DISCOVERED, return_value=[service_info(ADDRESS, b""), other]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    choices = next(iter(result["data_schema"].schema.values())).container
    assert choices == {ADDRESS: "Yeelight Candela DEDA"}


async def test_user_step_offers_passive_lamp_known_by_service_uuid_only(
    hass: HomeAssistant,
) -> None:
    lamp = service_info(ADDRESS, b"", name="", service_uuids=[SERVICE_UUID])
    with patch(DISCOVERED, return_value=[lamp]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM


async def test_bluetooth_discovery_already_configured(hass: HomeAssistant) -> None:
    make_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_discovery_rejects_other_yeelight_devices(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=service_info(payload=NOT_CANDELA_PAYLOAD),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_user_step_offers_only_unconfigured_candelas(hass: HomeAssistant) -> None:
    make_entry(hass, ADDRESS)
    discovered = [
        service_info(ADDRESS),
        service_info(ADDRESS_2),
        service_info("F8:24:41:00:00:01", NOT_CANDELA_PAYLOAD),
    ]
    with patch(DISCOVERED, return_value=discovered):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    choices = next(iter(result["data_schema"].schema.values())).container
    assert choices == {ADDRESS_2: "Yeelight Candela A098"}
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS_2}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Yeelight Candela A098"
    assert result["data"] == {CONF_ADDRESS: ADDRESS_2}


async def test_user_step_without_devices_aborts(hass: HomeAssistant) -> None:
    with patch(DISCOVERED, return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
