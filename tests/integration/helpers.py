"""Shared helpers for Home Assistant integration tests."""

from __future__ import annotations

import time

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.yeelight_candela.config_flow import entry_title
from custom_components.yeelight_candela.const import DOMAIN

ADDRESS = "F8:24:41:C4:DE:DA"
ADDRESS_2 = "F8:24:41:C6:A0:98"
SERVICE_UUID = "0000fe87-0000-1000-8000-00805f9b34fb"
CANDELA_PAYLOADS = {
    ADDRESS: bytes.fromhex("64016401dadec441010000da00796c5f63616e64656c61000000000000"),
    ADDRESS_2: bytes.fromhex("6401640198a0c6410100009800796c5f63616e64656c61000000000000"),
}
NOT_CANDELA_PAYLOAD = bytes.fromhex("64016401dadec441010000da00796c5f6f74686572000000000000")


def service_info(
    address: str = ADDRESS,
    payload: bytes | None = None,
    *,
    name: str = "yeelight_ms",
    service_uuids: list[str] | None = None,
) -> BluetoothServiceInfoBleak:
    manufacturer_data = {0x0164: payload if payload is not None else CANDELA_PAYLOADS[address]}
    service_uuids = [SERVICE_UUID] if service_uuids is None else service_uuids
    advertisement = AdvertisementData(
        local_name=name,
        manufacturer_data=manufacturer_data,
        service_data={},
        service_uuids=service_uuids,
        tx_power=-127,
        rssi=-45,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-45,
        manufacturer_data=manufacturer_data,
        service_data={},
        service_uuids=service_uuids,
        source="local",
        device=BLEDevice(address, name, {}),
        advertisement=advertisement,
        connectable=True,
        time=time.monotonic(),
        tx_power=-127,
    )


def payload_with_flag(address: str, flag: int) -> bytes:
    """A real payload with the state byte replaced.

    HA drops advertisements identical to the previous one, so tests that need a
    second advertisement must change its content.
    """
    payload = bytearray(CANDELA_PAYLOADS[address])
    payload[8] = flag
    return bytes(payload)


def inject(hass: HomeAssistant, info: BluetoothServiceInfoBleak) -> None:
    """Feed an advertisement into HA's Bluetooth manager (identical repeats are ignored by HA)."""
    bluetooth.async_get_advertisement_callback(hass)(info)


def make_entry(
    hass: HomeAssistant, address: str = ADDRESS, options: dict | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=address,
        title=entry_title(address),
        data={CONF_ADDRESS: address},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry
