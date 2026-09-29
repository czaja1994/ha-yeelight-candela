"""Notifications through an ESPHome Bluetooth proxy without a CCCD.

The lamp's notify characteristic has no client configuration descriptor (CCCD), yet
the lamp pushes notifications anyway. bleak_esphome registers the handle on the proxy
and then gives up because it cannot write the CCCD, so the proxy never forwards
anything. This registers the handle on the proxy directly, skipping the CCCD write.

It relies on bleak_esphome / aioesphomeapi internals (`_backend`, `_client`,
`_address_as_int`, `_notify_cancels`) and returns False whenever they don't look as
expected, so callers can fall back to optimistic state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_START_TIMEOUT_SECONDS = 10.0


async def start_notify_without_cccd(
    client: Any, char_uuid: str, callback: Callable[[Any, bytearray], None]
) -> bool:
    """Register for notifications through an ESPHome proxy; True if registered."""
    backend = getattr(client, "_backend", None)
    api = getattr(backend, "_client", None)
    address = getattr(backend, "_address_as_int", None)
    start_notify = getattr(api, "bluetooth_gatt_start_notify", None)
    if start_notify is None or not isinstance(address, int):
        return False
    characteristic = client.services.get_characteristic(char_uuid)
    if characteristic is None:
        return False

    cancels = await start_notify(
        address,
        characteristic.handle,
        lambda _handle, data: callback(characteristic, bytearray(data)),
        _START_TIMEOUT_SECONDS,
    )
    if not (isinstance(cancels, tuple) and len(cancels) == 2 and callable(cancels[1])):
        # Not the (stop, remove) pair bleak_esphome keeps; its cleanup could not use it.
        if callable(cancels):
            cancels()
        return False
    registry = getattr(backend, "_notify_cancels", None)
    if not isinstance(registry, dict):
        # Nothing would remove our callback on disconnect; don't leave it behind.
        _stop, remove_callback = cancels
        remove_callback()
        return False
    # bleak_esphome drops everything in this registry when the connection ends.
    registry[characteristic.handle] = cancels
    return True
