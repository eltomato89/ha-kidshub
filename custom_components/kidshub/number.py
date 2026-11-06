"""Number entities (volume, timers, brightness) of a KidsHub device."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import KidEntity
from .settings_util import get_path, patch_for


@dataclass(frozen=True)
class NumberSpec:
    """Description of one settings number."""

    key: str
    path: tuple[str, ...]
    minimum: int
    maximum: int
    step: int = 1
    unit: str | None = PERCENTAGE


SPECS = (
    NumberSpec("max_volume", ("max_volume",), 0, 100),
    NumberSpec("alarm_max_volume", ("alarm_max_volume",), 0, 100),
    NumberSpec(
        "sleep_minutes",
        ("sleep_timer", "minutes"),
        5,
        120,
        5,
        UnitOfTime.MINUTES,
    ),
    NumberSpec(
        "alarm_lead_minutes",
        ("alarm", "lead_minutes"),
        0,
        60,
        1,
        UnitOfTime.MINUTES,
    ),
    NumberSpec("night_brightness", ("night", "brightness"), 1, 100),
    NumberSpec("day_brightness", ("day_brightness",), 10, 100),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the number entities."""
    device = hass.data[DOMAIN].devices[entry.data[CONF_DEVICE_ID]]
    async_add_entities(KidNumber(device, spec) for spec in SPECS)


class KidNumber(KidEntity, NumberEntity):
    """A numeric setting."""

    _attr_mode = NumberMode.SLIDER
    _always_available = True

    def __init__(self, device, spec: NumberSpec) -> None:
        super().__init__(device, spec.key)
        self._spec = spec
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit

    @property
    def native_value(self) -> float | None:
        """Return the stored setting."""
        value = get_path(self.device.store.settings, self._spec.path)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return value

    async def async_set_native_value(self, value: float) -> None:
        """Store the setting and push the config."""
        await self.device.update_settings(patch_for(self._spec.path, int(value)))
