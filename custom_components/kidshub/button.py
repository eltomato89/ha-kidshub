"""Buttons of a KidsHub device."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import KidEntity

COMMANDS: dict[str, dict[str, Any]] = {
    "stop_alarm": {"type": "alarm_cmd", "cmd": "stop"},
    "cancel_sleep_timer": {"type": "sleep_cmd", "cmd": "cancel"},
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the buttons."""
    device = hass.data[DOMAIN].devices[entry.data[CONF_DEVICE_ID]]
    async_add_entities(KidButton(device, key, msg) for key, msg in COMMANDS.items())


class KidButton(KidEntity, ButtonEntity):
    """Sends a command to the app."""

    def __init__(self, device, key: str, msg: dict[str, Any]) -> None:
        super().__init__(device, key)
        self._msg = msg

    async def async_press(self) -> None:
        """Send the command."""
        await self.device.push(dict(self._msg))
