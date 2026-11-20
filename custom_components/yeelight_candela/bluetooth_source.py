"""Connect to a lamp through one chosen Bluetooth adapter or proxy.

Home Assistant picks the connection path itself on every connect: the adapter or
proxy with the best signal and a free slot. That path is not always the one that
works best for a Candela. A local BlueZ adapter, for example, cannot subscribe to
the lamp's notifications (the characteristic has no CCCD), so the lamp's real state
only comes back through an ESPHome proxy. When the local adapter hears the lamp a
few dB better, Home Assistant keeps choosing it.

``pinned_client_class`` returns a client class that only connects through the
chosen source while that source is registered. If the source is gone (proxy
offline, adapter removed) it falls back to Home Assistant's normal choice, so the
lamp stays controllable.

This builds on Home Assistant's Bluetooth client wrapper (habluetooth). If that
wrapper is not in use or does not look as expected, ``pinned_client_class``
returns None and the caller uses the normal client.
"""

from __future__ import annotations

import logging
from typing import Any

import bleak_retry_connector
from bleak.exc import BleakError

_LOGGER = logging.getLogger(__name__)

# The wrapper keeps the target address in a name-mangled private attribute.
_ADDRESS_ATTRIBUTE = "_HaBleakClientWrapper__address"
_PICKER = "_async_get_best_available_backend_and_device"
_BACKEND_FOR_DEVICE = "_async_get_backend_for_ble_device"


def pinned_client_class(source: str) -> type | None:
    """Return a client class bound to `source`, or None if pinning is not possible."""
    base = bleak_retry_connector.BleakClientWithServiceCache
    if not source or not callable(getattr(base, _PICKER, None)):
        return None
    if not callable(getattr(base, _BACKEND_FOR_DEVICE, None)):
        return None

    class PinnedBleakClient(base):  # type: ignore[valid-type, misc]
        """Connects only through the pinned adapter or proxy while it is registered."""

        pinned_source = source

        def _async_get_best_available_backend_and_device(self, manager: Any) -> Any:
            address = getattr(self, _ADDRESS_ATTRIBUTE, None)
            if not isinstance(address, str):
                return super()._async_get_best_available_backend_and_device(manager)
            if manager.async_scanner_by_source(self.pinned_source) is None:
                _LOGGER.debug(
                    "%s: Bluetooth source %s is not available; using the best other path",
                    address,
                    self.pinned_source,
                )
                return super()._async_get_best_available_backend_and_device(manager)

            seen = False
            for path in manager.async_scanner_devices_by_address(address, True):
                if path.scanner.source != self.pinned_source:
                    continue
                seen = True
                backend = self._async_get_backend_for_ble_device(
                    manager, path.scanner, path.ble_device
                )
                if backend is not None:
                    _LOGGER.debug(
                        "%s: connecting through pinned Bluetooth source %s (rssi %s)",
                        address,
                        path.scanner.name,
                        path.advertisement.rssi,
                    )
                    return backend
            if seen:
                msg = (
                    f"{address}: the pinned Bluetooth source {self.pinned_source} has no free"
                    " connection slot"
                )
            else:
                msg = (
                    f"{address}: the pinned Bluetooth source {self.pinned_source} does not see"
                    " the lamp right now"
                )
            raise BleakError(msg)

    return PinnedBleakClient
