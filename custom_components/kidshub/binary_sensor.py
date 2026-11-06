"""Binary sensors of a KidsHub device."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import KidEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the binary sensors."""
    device = hass.data[DOMAIN].devices[entry.data[CONF_DEVICE_ID]]
    async_add_entities([OnlineSensor(device)])


class OnlineSensor(KidEntity, BinarySensorEntity):
    """Whether the app is connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _always_available = True

    def __init__(self, device) -> None:
        super().__init__(device, "online")

    @property
    def is_on(self) -> bool:
        """Return whether a session is attached."""
        return self.device.connected
