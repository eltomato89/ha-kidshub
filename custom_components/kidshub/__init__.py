"""The KidsHub integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS


@dataclass
class KidsHubData:
    """Runtime data shared by all KidsHub config entries."""

    devices: dict[str, Any] = field(default_factory=dict)  # device_id -> KidDevice
    views_registered: bool = False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up KidsHub from a config entry."""
    hass.data.setdefault(DOMAIN, KidsHubData())
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
