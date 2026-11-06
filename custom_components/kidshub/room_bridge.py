"""Live room-entity states and the ``room`` request of one KidsHub device."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from .device import InvalidRequest, KidDevice, NotAllowed

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = "unavailable"
_COVER_ACTIONS = {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"}
_ACTIONS = {"toggle", "set_brightness", "set_color", *_COVER_ACTIONS}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_or_none(value: Any) -> int | None:
    if _is_int(value):
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def entity_state_message(entity_id: str, state: State | None) -> dict[str, Any]:
    """Build the protocol ``entity_state`` message for ``entity_id``."""
    msg: dict[str, Any] = {
        "type": "entity_state",
        "entity_id": entity_id,
        "on": False,
        "brightness": None,
        "rgb": None,
        "position": None,
        "available": False,
    }
    if state is None or state.state == _UNAVAILABLE:
        return msg
    domain = entity_id.split(".", 1)[0]
    attrs = state.attributes
    msg["available"] = True
    msg["on"] = state.state != "closed" if domain == "cover" else state.state == "on"
    msg["brightness"] = _int_or_none(attrs.get("brightness"))
    rgb = attrs.get("rgb_color")
    if isinstance(rgb, (list, tuple)) and len(rgb) == 3 and all(map(_is_int, rgb)):
        msg["rgb"] = list(rgb)
    msg["position"] = _int_or_none(attrs.get("current_position"))
    return msg


class RoomBridge:
    """Mirror the room allowlist to the app and execute room actions."""

    def __init__(self, hass: HomeAssistant, device: KidDevice) -> None:
        self.hass = hass
        self.device = device
        self._unsub: Callable[[], None] | None = None
        device.register_handler("room", self._handle_room)
        device.register_connected_callback(self.push_all)

    @property
    def allowlist(self) -> list[str]:
        """Return the configured room entities of this device."""
        return list(self.device.entry.options.get("room_entities") or [])

    def start(self) -> None:
        """Subscribe to the allowlisted entities."""
        self.stop()
        entities = self.allowlist
        if entities:
            self._unsub = async_track_state_change_event(
                self.hass, entities, self._on_change
            )

    def stop(self) -> None:
        """Drop the state subscription."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def refresh(self) -> None:
        """Resubscribe after an options change and resend all states."""
        self.start()
        await self.push_all()

    async def push_all(self) -> None:
        """Send one ``entity_state`` per room entity."""
        for entity_id in self.allowlist:
            await self.device.push(
                entity_state_message(entity_id, self.hass.states.get(entity_id))
            )

    @callback
    def _on_change(self, event: Event[EventStateChangedData]) -> None:
        if not self.device.connected:
            return
        entity_id = event.data["entity_id"]
        msg = entity_state_message(entity_id, event.data["new_state"])
        self.hass.async_create_background_task(
            self.device.push(msg), f"kidshub_entity_state_{entity_id}"
        )

    async def _handle_room(self, msg: dict[str, Any]) -> dict[str, Any]:
        entity_id = msg.get("entity_id")
        action = msg.get("action")
        if not isinstance(entity_id, str) or not isinstance(action, str):
            raise InvalidRequest("entity_id and action must be strings")
        if entity_id not in self.allowlist:
            raise NotAllowed("entity not allowed")
        if action not in _ACTIONS:
            raise InvalidRequest("unknown action")
        domain = entity_id.split(".", 1)[0]
        value = msg.get("value")
        if action == "toggle":
            service = ("homeassistant", "toggle", {})
        elif action in _COVER_ACTIONS:
            if domain != "cover":
                raise InvalidRequest(f"{action} needs a cover")
            service = ("cover", _COVER_ACTIONS[action], {})
        elif action == "set_brightness":
            if domain != "light":
                raise InvalidRequest("set_brightness needs a light")
            if not _is_int(value) or not 0 <= value <= 255:
                raise InvalidRequest("brightness must be an int 0..255")
            service = ("light", "turn_on", {"brightness": value})
        else:
            if domain != "light":
                raise InvalidRequest("set_color needs a light")
            if (
                not isinstance(value, list)
                or len(value) != 3
                or not all(_is_int(c) and 0 <= c <= 255 for c in value)
            ):
                raise InvalidRequest("color must be [r, g, b] ints 0..255")
            service = ("light", "turn_on", {"rgb_color": list(value)})
        svc_domain, svc, extra = service
        try:
            await self.hass.services.async_call(
                svc_domain, svc, {"entity_id": entity_id, **extra}, blocking=True
            )
        except (HomeAssistantError, vol.Invalid) as err:
            raise InvalidRequest(str(err) or "service call failed") from err
        return {}
