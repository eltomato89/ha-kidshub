"""Constants for the KidsHub integration."""

from homeassistant.const import Platform

DOMAIN = "kidshub"

ENTRY_TYPE = "entry_type"
ENTRY_TYPE_DEVICE = "device"
ENTRY_TYPE_SETTINGS = "settings"

CONF_CHILD_NAME = "child_name"
CONF_DEVICE_ID = "device_id"
CONF_TOKEN_HASH = "token_hash"
CONF_TTS_ENGINE = "tts_engine"
CONF_TTS_LANGUAGE = "tts_language"
CONF_TTS_OPTIONS = "tts_options"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]
