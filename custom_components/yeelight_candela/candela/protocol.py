"""Yeelight Candela BLE protocol: frame builders and notification parser."""

from __future__ import annotations

from dataclasses import dataclass

SERVICE_UUID = "0000fe87-0000-1000-8000-00805f9b34fb"
COMMAND_CHAR_UUID = "aa7d3f34-2d4f-41e0-807f-52fbf8cf7443"
NOTIFY_CHAR_UUID = "8f65073d-9f57-4aaa-afea-397d19d5bbeb"

FRAME_LENGTH = 18
PREFIX = 0x43

CMD_POWER = 0x40
CMD_BRIGHTNESS = 0x42
CMD_GET_STATE = 0x44
CMD_FLICKER = 0x67
# Battery query and report (Yeelight app: READGINGKOBATTERY / BATTERYNOTIFICATION).
CMD_GET_BATTERY = 0xA0
RSP_STATE = 0x45
RSP_FLICKER = 0x63
RSP_BATTERY = 0xA1

POWER_ON = 0x01
POWER_OFF = 0x02
FLICKER_START = 0x02
FLICKER_ON = 0x01
FLICKER_OFF = 0x03

MIN_BRIGHTNESS = 1
MAX_BRIGHTNESS = 100


@dataclass(frozen=True)
class StateUpdate:
    """Power and brightness reported by the lamp."""

    is_on: bool
    brightness: int


@dataclass(frozen=True)
class FlickerUpdate:
    """Candle flicker mode reported by the lamp."""

    active: bool


# Battery report, firmware V1.F (byte 2 = status, byte 3 = level, bytes 4-5 and 6-7 =
# big-endian raw readings, the second one behaves like the battery voltage):
#   on the base, full:        03 64 0A 71 08 EB  (first reading well above the second)
#   no power:                 03 64 08 E2 08 D4  (both readings about equal)
#   on the cable, charging:   02 64 04 AC 08 D2  (first reading well below the second)
BATTERY_STATUS_CHARGING = 0x02
# The first reading is this much above the second while the lamp sits on a full charge.
ON_POWER_MIN_DIFFERENCE = 150


@dataclass(frozen=True)
class BatteryUpdate:
    """Battery report: level in percent, status byte and two raw readings."""

    level: int
    status: int | None = None
    input_raw: int | None = None
    battery_raw: int | None = None

    @property
    def charging(self) -> bool | None:
        """Whether the battery is charging; None when not reported."""
        return None if self.status is None else self.status == BATTERY_STATUS_CHARGING

    @property
    def on_power(self) -> bool | None:
        """Whether the lamp is on external power (charging, or full on its charger)."""
        if self.charging:
            return True
        if self.input_raw is None or self.battery_raw is None:
            return None
        return self.input_raw - self.battery_raw > ON_POWER_MIN_DIFFERENCE


def _frame(*payload: int) -> bytes:
    return bytes((PREFIX, *payload)).ljust(FRAME_LENGTH, b"\x00")


def clamp_brightness(pct: int) -> int:
    """Clamp a brightness percentage to the range the lamp accepts."""
    return max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, pct))


def build_on() -> bytes:
    return _frame(CMD_POWER, POWER_ON)


def build_off() -> bytes:
    return _frame(CMD_POWER, POWER_OFF)


def build_brightness(pct: int) -> bytes:
    return _frame(CMD_BRIGHTNESS, clamp_brightness(pct))


def build_flicker() -> bytes:
    return _frame(CMD_FLICKER, FLICKER_START)


def build_get_state() -> bytes:
    return _frame(CMD_GET_STATE)


def build_get_battery() -> bytes:
    return _frame(CMD_GET_BATTERY, 0x00)


def build_exit_flicker(pct: int) -> list[bytes]:
    """Frames that leave candle flicker mode and keep the lamp on at `pct`.

    Checked on hardware (firmware V1.F, 2026-09-29): a brightness command stops
    the flicker and keeps the lamp on.
    """
    return [build_brightness(pct)]


def parse_notification(data: bytes) -> StateUpdate | FlickerUpdate | BatteryUpdate | None:
    """Decode a notification; return None for anything not understood."""
    if len(data) < 3 or data[0] != PREFIX:
        return None
    kind, value = data[1], data[2]
    if kind == RSP_STATE:
        if len(data) < 4 or value not in (POWER_ON, POWER_OFF):
            return None
        return StateUpdate(is_on=value == POWER_ON, brightness=clamp_brightness(data[3]))
    if kind == RSP_FLICKER and value in (FLICKER_ON, FLICKER_OFF):
        return FlickerUpdate(active=value == FLICKER_ON)
    if kind == RSP_BATTERY and len(data) >= 4 and 0 <= data[3] <= 100:
        # Byte 3 is the level in percent; see the battery report notes above.
        if len(data) >= 8:
            return BatteryUpdate(
                level=data[3],
                status=value,
                input_raw=int.from_bytes(data[4:6], "big"),
                battery_raw=int.from_bytes(data[6:8], "big"),
            )
        return BatteryUpdate(level=data[3], status=value)
    return None
