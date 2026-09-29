"""Tests for the manufacturer-data parser."""

import pytest

from custom_components.yeelight_candela.candela import advertisement
from custom_components.yeelight_candela.candela.advertisement import (
    AdvInfo,
    adv_is_on,
    is_candela,
    parse_manufacturer_data,
)

# Payloads captured from the two real lamps on 2026-09-29 (company id stripped).
LAMP_1 = bytes.fromhex("64016401dadec441010000da00796c5f63616e64656c61000000000000")
LAMP_2 = bytes.fromhex("6401640198a0c6410100009800796c5f63616e64656c61000000000000")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (LAMP_1, AdvInfo(mac_suffix="41:C4:DE:DA", state_flag=0x01)),
        (LAMP_2, AdvInfo(mac_suffix="41:C6:A0:98", state_flag=0x01)),
    ],
)
def test_parses_real_candela_payloads(payload: bytes, expected: AdvInfo) -> None:
    assert parse_manufacturer_data({0x0164: payload}) == expected


@pytest.mark.parametrize(
    "manufacturer_data",
    [
        {},
        {0x004C: LAMP_1},
        {0x0164: LAMP_1[:8]},
        {0x0164: LAMP_1.replace(b"yl_candela", b"yl_other__")},
    ],
)
def test_rejects_non_candela_data(manufacturer_data: dict[int, bytes]) -> None:
    assert parse_manufacturer_data(manufacturer_data) is None


FE87 = "0000fe87-0000-1000-8000-00805f9b34fb"


@pytest.mark.parametrize(
    ("manufacturer_data", "service_uuids", "local_name", "expected"),
    [
        # Active scan (e.g. macOS): scan response carries MAC + model marker.
        ({0x0164: LAMP_1}, [], None, True),
        # Passive scan (ESPHome proxy): the primary advertisement only carries
        # the company id, captured from a real proxy on 2026-09-29, together with
        # the FE87 service UUID and the `yeelight_ms` name.
        ({0x0164: b""}, [FE87], "yeelight_ms", True),
        ({0x0164: b""}, [FE87.upper()], None, True),
        ({0x0164: b""}, [], "yeelight_ms", True),
        # Other Yeelight devices can also send an empty Yeelink payload.
        ({0x0164: b""}, [], None, False),
        ({0x0164: b""}, ["0000fe95-0000-1000-8000-00805f9b34fb"], "yeelight_other", False),
        ({0x0164: LAMP_1.replace(b"yl_candela", b"yl_other__")}, [FE87], "yeelight_ms", False),
        ({0x004C: LAMP_1}, [FE87], "yeelight_ms", False),
        ({}, [FE87], "yeelight_ms", False),
    ],
)
def test_is_candela(
    manufacturer_data: dict[int, bytes],
    service_uuids: list[str],
    local_name: str | None,
    expected: bool,
) -> None:
    assert (
        is_candela(manufacturer_data, service_uuids=service_uuids, local_name=local_name)
        is expected
    )


def test_is_candela_empty_payload_needs_more_evidence_by_default() -> None:
    assert is_candela({0x0164: b""}) is False


def test_state_unknown_until_verified() -> None:
    assert adv_is_on(AdvInfo(mac_suffix="x", state_flag=0x01)) is None


def test_state_from_flag_once_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(advertisement, "ADV_STATE_VERIFIED", True)
    assert adv_is_on(AdvInfo(mac_suffix="x", state_flag=0x01)) is True
    assert adv_is_on(AdvInfo(mac_suffix="x", state_flag=0x00)) is False
