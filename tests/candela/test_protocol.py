"""Tests for Candela frame builders and the notification parser."""

from pathlib import Path

import pytest

from custom_components.yeelight_candela.candela import protocol
from custom_components.yeelight_candela.candela.protocol import (
    BatteryUpdate,
    FlickerUpdate,
    StateUpdate,
)


def frame(hex_prefix: str) -> bytes:
    return bytes.fromhex(hex_prefix).ljust(18, b"\x00")


@pytest.mark.parametrize(
    ("built", "expected"),
    [
        (protocol.build_on(), "434001"),
        (protocol.build_off(), "434002"),
        (protocol.build_get_state(), "4344"),
        (protocol.build_get_battery(), "43a000"),
        (protocol.build_flicker(), "436702"),
        (protocol.build_brightness(20), "434214"),
        (protocol.build_brightness(100), "434264"),
    ],
)
def test_frames(built: bytes, expected: str) -> None:
    assert built == frame(expected)
    assert len(built) == protocol.FRAME_LENGTH


@pytest.mark.parametrize(
    ("pct", "expected"), [(-5, 1), (0, 1), (1, 1), (55, 55), (100, 100), (150, 100)]
)
def test_clamp_brightness(pct: int, expected: int) -> None:
    assert protocol.clamp_brightness(pct) == expected
    assert protocol.build_brightness(pct) == frame(f"4342{expected:02x}")


def test_exit_flicker_defaults_to_brightness_frame() -> None:
    assert protocol.build_exit_flicker(40) == [frame("434228")]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("434501640207080000000000000000000000", StateUpdate(is_on=True, brightness=100)),
        ("434502640207080000000000000000000000", StateUpdate(is_on=False, brightness=100)),
        ("434501140207080000000000000000000000", StateUpdate(is_on=True, brightness=20)),
        ("434501690207080000000000000000000000", StateUpdate(is_on=True, brightness=100)),
        ("434501000207080000000000000000000000", StateUpdate(is_on=True, brightness=1)),
        ("436301000000000000000000000000000000", FlickerUpdate(active=True)),
        ("436303000000000000000000000000000000", FlickerUpdate(active=False)),
    ],
)
def test_parse_known_notifications(data: str, expected: object) -> None:
    assert protocol.parse_notification(bytes.fromhex(data)) == expected


@pytest.mark.parametrize(
    "data",
    [
        "",
        "00",
        "4345",
        "434503640000",
        "43ff00000000000000000000000000000000",
        "444501640000",
        "436309",
    ],
)
def test_parse_ignores_unknown_or_malformed(data: str) -> None:
    assert protocol.parse_notification(bytes.fromhex(data)) is None


# Battery reports captured on firmware V1.F.
@pytest.mark.parametrize(
    ("data", "level", "charging", "on_power"),
    [
        ("43a103640a7108eb000000000000000000", 100, False, True),  # on the base, full
        ("43a1036408e208d4000000000000000000", 100, False, False),  # no power
        ("43a1026404ac08d2000000000000000000", 100, True, True),  # on the cable, charging
    ],
)
def test_parse_battery_reports(data: str, level: int, charging: bool, on_power: bool) -> None:
    update = protocol.parse_notification(bytes.fromhex(data))
    assert isinstance(update, BatteryUpdate)
    assert update.level == level
    assert update.charging is charging
    assert update.on_power is on_power


def test_short_battery_report_has_level_only() -> None:
    update = protocol.parse_notification(bytes.fromhex("43a10332"))
    assert update == BatteryUpdate(level=50, status=0x03)
    assert update.charging is False
    assert update.on_power is None


def test_battery_level_out_of_range_is_ignored() -> None:
    assert protocol.parse_notification(bytes.fromhex("43a103c80a7108eb")) is None


def test_candela_package_does_not_import_home_assistant() -> None:
    package = Path(protocol.__file__).parent
    for source in package.glob("*.py"):
        assert "homeassistant" not in source.read_text(), source.name
