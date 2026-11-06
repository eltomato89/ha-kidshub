"""Config flow for KidsHub (steps are added in later tasks)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class KidsHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for KidsHub."""

    VERSION = 1
