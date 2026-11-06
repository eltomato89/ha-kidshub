"""Time entities (alarm time, windows) of a KidsHub device."""

from __future__ import annotations

import datetime as dt

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import KidEntity
from .settings_util import get_path, patch_for

SPECS: dict[str, tuple[str, ...]] = {
    "alarm_time": ("alarm", "time"),
    "sleep_window_start": ("sleep_timer", "window_start"),
    "sleep_window_end": ("sleep_timer", "window_end"),
    "night_start": ("night", "start"),
    "night_end": ("night", "end"),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the time entities."""
    device = hass.data[DOMAIN].devices[entry.data[CONF_DEVICE_ID]]
    async_add_entities(KidTime(device, key, path) for key, path in SPECS.items())


class KidTime(KidEntity, TimeEntity):
    """A time-of-day setting."""

    _always_available = True

    def __init__(self, device, key: str, path: tuple[str, ...]) -> None:
        super().__init__(device, key)
        self._path = path

    @property
    def native_value(self) -> dt.time | None:
        """Return the stored time."""
        raw = get_path(self.device.store.settings, self._path)
        try:
            return dt.time.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    async def async_set_value(self, value: dt.time) -> None:
        """Store the time as ``HH:MM`` and push the config."""
        await self.device.update_settings(
            patch_for(self._path, value.strftime("%H:%M"))
        )
