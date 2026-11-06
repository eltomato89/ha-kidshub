"""Constants for the KidsHub integration."""

from homeassistant.const import Platform

DOMAIN = "kidshub"

ENTRY_TYPE = "entry_type"
ENTRY_TYPE_DEVICE = "device"
ENTRY_TYPE_SETTINGS = "settings"

CONF_CHILD_NAME = "child_name"
CONF_DEVICE_ID = "device_id"
CONF_TOKEN_HASH = "token_hash"

PLATFORMS: list[Platform] = []
