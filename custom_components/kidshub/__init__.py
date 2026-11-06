"""The KidsHub integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_ID,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_DEVICE,
    PLATFORMS,
)
from .device import KidDevice
from .pairing import PairingRegistry
from .views import PairView, WsView

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class KidsHubData:
    """Runtime data shared by all KidsHub config entries."""

    devices: dict[str, Any] = field(default_factory=dict)  # device_id -> KidDevice
    pairing: PairingRegistry = field(default_factory=PairingRegistry)
    views_registered: bool = False


def async_get_data(hass: HomeAssistant) -> KidsHubData:
    """Return the shared runtime data, registering the views once."""
    data: KidsHubData = hass.data.setdefault(DOMAIN, KidsHubData())
    if not data.views_registered:
        hass.http.register_view(PairView())
        hass.http.register_view(WsView())
        data.views_registered = True
    return data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the KidsHub component (views are needed before any entry)."""
    async_get_data(hass)
    return True


def _track_sw_version(
    hass: HomeAssistant, entry: ConfigEntry, device: KidDevice
) -> None:
    """Mirror the reported app version into the device registry."""

    @callback
    def _update() -> None:
        registry = dr.async_get(hass)
        dev = registry.async_get_device_by_identifier(
            (DOMAIN, device.device_id), entry.entry_id
        )
        if dev is not None and dev.sw_version != device.app_version:
            registry.async_update_device(dev.id, sw_version=device.app_version)

    entry.async_on_unload(async_dispatcher_connect(hass, device.signal, _update))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up KidsHub from a config entry."""
    data = async_get_data(hass)
    if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_DEVICE:
        device = KidDevice(hass, entry)
        await device.async_load()
        data.devices[entry.data[CONF_DEVICE_ID]] = device
        _track_sw_version(hass, entry, device)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.data.get(ENTRY_TYPE) != ENTRY_TYPE_DEVICE:
        return True
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        device = hass.data[DOMAIN].devices.pop(entry.data[CONF_DEVICE_ID], None)
        if device is not None:
            await device.async_close()
    return unloaded
