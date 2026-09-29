"""Parser for Yeelight Candela manufacturer data in BLE advertisements."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

YEELINK_COMPANY_ID = 0x0164
MODEL_MARKER = b"yl_candela"
SERVICE_UUID = "0000fe87-0000-1000-8000-00805f9b34fb"
LOCAL_NAME = "yeelight_ms"

_MAC_SLICE = slice(4, 8)
_STATE_INDEX = 8

# Whether the byte after the MAC is known to carry the on/off state.
# Checked on hardware (firmware V1.F, 2026-09-29): the payload does not change
# when the lamp is switched by hand, so state is polled instead.
ADV_STATE_VERIFIED = False
ADV_STATE_ON = 0x01


@dataclass(frozen=True)
class AdvInfo:
    """Fields decoded from a Candela advertisement."""

    mac_suffix: str
    state_flag: int


def parse_manufacturer_data(manufacturer_data: Mapping[int, bytes]) -> AdvInfo | None:
    """Return decoded fields, or None if the data is not from a Candela lamp."""
    payload = manufacturer_data.get(YEELINK_COMPANY_ID)
    if payload is None or len(payload) <= _STATE_INDEX or MODEL_MARKER not in payload:
        return None
    mac_suffix = ":".join(f"{byte:02X}" for byte in reversed(payload[_MAC_SLICE]))
    return AdvInfo(mac_suffix=mac_suffix, state_flag=payload[_STATE_INDEX])


def is_candela(
    manufacturer_data: Mapping[int, bytes],
    *,
    service_uuids: Iterable[str] = (),
    local_name: str | None = None,
) -> bool:
    """Whether an advertisement may come from a Candela lamp.

    The lamp's primary advertisement carries only the Yeelink company id with an
    empty payload; the MAC and the `yl_candela` marker arrive in the scan
    response, which passive scanners (e.g. ESPHome Bluetooth proxies) never
    request. A non-empty payload must contain the marker. An empty one, which
    other Yeelight devices can send too, is accepted only together with the
    lamp's FE87 service UUID or its `yeelight_ms` local name.
    """
    payload = manufacturer_data.get(YEELINK_COMPANY_ID)
    if payload is None:
        return False
    if payload:
        return MODEL_MARKER in payload
    return local_name == LOCAL_NAME or any(uuid.lower() == SERVICE_UUID for uuid in service_uuids)


def adv_is_on(info: AdvInfo) -> bool | None:
    """On/off state from an advertisement, or None while the flag is unverified."""
    if not ADV_STATE_VERIFIED:
        return None
    return info.state_flag == ADV_STATE_ON
