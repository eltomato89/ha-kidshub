"""Switch entities (flags, tiles, alarm weekdays) of a KidsHub device."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import KidEntity
from .settings_util import get_path, patch_for

FLAGS: dict[str, tuple[str, ...]] = {
    "alarm_enabled": ("alarm", "enabled"),
    "alarm_with_light": ("alarm", "with_light"),
    "music_enabled": ("music_enabled",),
    "settings_visible": ("settings_visible",),
    "tile_music": ("tiles", "music"),
    "tile_audiobooks": ("tiles", "audiobooks"),
    "tile_podcasts": ("tiles", "podcasts"),
}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the switch entities."""
    device = hass.data[DOMAIN].devices[entry.data[CONF_DEVICE_ID]]
    entities: list[KidEntity] = [
        KidFlagSwitch(device, key, path) for key, path in FLAGS.items()
    ]
    entities += [KidWeekdaySwitch(device, i) for i in range(len(WEEKDAYS))]
    async_add_entities(entities)


class KidFlagSwitch(KidEntity, SwitchEntity):
    """A boolean setting."""

    _always_available = True

    def __init__(self, device, key: str, path: tuple[str, ...]) -> None:
        super().__init__(device, key)
        self._path = path

    @property
    def is_on(self) -> bool | None:
        """Return the stored flag."""
        value = get_path(self.device.store.settings, self._path)
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the flag."""
        await self.device.update_settings(patch_for(self._path, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the flag."""
        await self.device.update_settings(patch_for(self._path, False))


class KidWeekdaySwitch(KidEntity, SwitchEntity):
    """One weekday of the alarm (0 = Monday)."""

    _always_available = True

    def __init__(self, device, day: int) -> None:
        super().__init__(device, f"alarm_{WEEKDAYS[day]}")
        self._day = day

    def _days(self) -> list[int]:
        days = get_path(self.device.store.settings, ("alarm", "weekdays"))
        return days if isinstance(days, list) else []

    @property
    def is_on(self) -> bool:
        """Return whether the alarm rings on this weekday."""
        return self._day in self._days()

    async def _set(self, on: bool) -> None:
        days = set(self._days())
        days.add(self._day) if on else days.discard(self._day)
        await self.device.update_settings({"alarm": {"weekdays": sorted(days)}})

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable this weekday."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable this weekday."""
        await self._set(False)
