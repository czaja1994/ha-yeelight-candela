"""Constants for the Yeelight Candela integration."""

from datetime import timedelta

DOMAIN = "yeelight_candela"
EFFECT_CANDLE = "Candle"
CONF_KEEP_CONNECTED = "keep_connected"
# Bluetooth source (adapter or proxy address) to connect through.
CONF_BLUETOOTH_SOURCE = "bluetooth_source"
# Let Home Assistant pick the path with the best signal (the default).
SOURCE_AUTOMATIC = "auto"
POLL_INTERVAL = timedelta(minutes=5)
# Budget for the first state read when setting up a lamp that is not kept connected;
# shorter than a command's deadline so a missing lamp does not hold up startup.
SETUP_TIMEOUT_SECONDS = 20.0
