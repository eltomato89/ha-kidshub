"""Sensors of a KidsHub device."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import KidEntity

ALARM_STATES = ["off", "sunrise", "ringing", "stopped"]
TOP_RATED_MIN_STARS = 4
MAX_SLEEP_REMAINING_S = 86400
TOP_RATED_MAX_ITEMS = 20


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensors."""
    device = hass.data[DOMAIN].devices[entry.data[CONF_DEVICE_ID]]
    async_add_entities(
        [
            AlarmStateSensor(device),
            SleepRemainingSensor(device),
            AppVersionSensor(device),
            TopRatedSensor(device),
        ]
    )


class AlarmStateSensor(KidEntity, SensorEntity):
    """Alarm state reported by the app."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ALARM_STATES

    def __init__(self, device) -> None:
        super().__init__(device, "alarm_state")

    @property
    def native_value(self) -> str | None:
        """Return the reported alarm state if it is a known value."""
        value = self.state_data.get("alarm_state")
        return value if isinstance(value, str) and value in ALARM_STATES else None


class SleepRemainingSensor(KidEntity, SensorEntity):
    """Remaining seconds of the sleep timer."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, device) -> None:
        super().__init__(device, "sleep_remaining")

    @property
    def native_value(self) -> int | None:
        """Return the remaining seconds, None without a (valid) timer."""
        value = self.state_data.get("sleep_remaining_s")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if not 0 <= value <= MAX_SLEEP_REMAINING_S:
            return None
        return int(value)


class AppVersionSensor(KidEntity, SensorEntity):
    """App version reported at authentication."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _always_available = True

    def __init__(self, device) -> None:
        super().__init__(device, "app_version")

    @property
    def native_value(self) -> str | None:
        """Return the last reported app version."""
        return self.device.app_version


class TopRatedSensor(KidEntity, SensorEntity):
    """Number of highly rated items; the items are an attribute."""

    _always_available = True

    def __init__(self, device) -> None:
        super().__init__(device, "top_rated")

    def _top(self):
        return [
            r
            for r in self.device.store.ratings()
            if r.stars >= TOP_RATED_MIN_STARS
        ]

    @property
    def native_value(self) -> int:
        """Return the number of ratings of at least four stars."""
        return len(self._top())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the best rated items."""
        return {
            "items": [
                {
                    "title": r.title,
                    "subtitle": r.subtitle,
                    "kind": r.kind,
                    "stars": r.stars,
                }
                for r in self._top()[:TOP_RATED_MAX_ITEMS]
            ]
        }
