"""Shared base class of the KidsHub entities."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import CONF_CHILD_NAME, DOMAIN
from .device import KidDevice


def device_info(device: KidDevice) -> DeviceInfo:
    """Return the device registry info of a KidsHub device."""
    return DeviceInfo(
        identifiers={(DOMAIN, device.device_id)},
        name=f"KidsHub {device.entry.data[CONF_CHILD_NAME]}",
        manufacturer="KidsHub",
        model="Echo Show 5",
        sw_version=device.app_version,
    )


class KidEntity(Entity):
    """Entity bound to one KidDevice, refreshed by its dispatcher signal."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Settings entities stay usable (and editable) while the app is offline.
    _always_available = False

    def __init__(self, device: KidDevice, key: str) -> None:
        self.device = device
        self._attr_translation_key = key
        self._attr_unique_id = f"{device.device_id}_{key}"
        self._attr_device_info = device_info(device)

    @property
    def available(self) -> bool:
        """Return whether the entity is usable."""
        return self._always_available or self.device.connected

    @property
    def state_data(self) -> dict[str, Any]:
        """Return the last reported app state (untrusted, may be empty)."""
        state = self.device.last_state
        return state if isinstance(state, dict) else {}

    async def async_added_to_hass(self) -> None:
        """Subscribe to device changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.device.signal, self._handle_device_update
            )
        )

    @callback
    def _handle_device_update(self) -> None:
        self.async_write_ha_state()
