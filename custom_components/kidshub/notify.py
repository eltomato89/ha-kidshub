"""Announcement entity of a KidsHub device (``notify.send_message``)."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import KidEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the announcement entity."""
    device = hass.data[DOMAIN].devices[entry.data[CONF_DEVICE_ID]]
    async_add_entities([KidAnnounceNotify(device)])


class KidAnnounceNotify(KidEntity, NotifyEntity):
    """Speaks a message on the device (an offline device raises an error)."""

    # stays available so that an automation sees the "not connected" error
    _always_available = True

    def __init__(self, device) -> None:
        super().__init__(device, "announcement")

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Announce ``message`` (a title is spoken first)."""
        text = f"{title.strip()}. {message}" if title and title.strip() else message
        await self.device.announce(text)
