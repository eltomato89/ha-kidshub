"""Build the protocol ``config`` message (protocol section 3.2)."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.core import State

from .room import classify

DEFAULT_COLORS: list[list[int]] = [
    [255, 92, 92],
    [255, 210, 63],
    [76, 217, 123],
    [155, 107, 255],
    [58, 160, 255],
    [255, 243, 214],
]


def build_config(
    *,
    version: int,
    child_name: str,
    settings: dict[str, Any],
    options: Mapping[str, Any],
    hass_states: Callable[[str], State | None],
    sendspin: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the full ``config`` message for one device."""
    names = options.get("room_names") or {}
    colors = options.get("colors") or DEFAULT_COLORS
    room: list[dict[str, Any]] = []
    for entity_id in options.get("room_entities") or []:
        state = hass_states(entity_id)
        if state is None:
            continue
        kind = classify(state)
        room.append(
            {
                "entity_id": entity_id,
                "name": names.get(entity_id)
                or state.attributes.get("friendly_name")
                or entity_id,
                "icon": state.attributes.get("icon"),
                "kind": kind,
                "colors": copy.deepcopy(colors) if kind == "color" else [],
            }
        )
    alarm = dict(copy.deepcopy(settings["alarm"]))
    alarm["button_required"] = bool(options.get("stop_button_entity"))
    return {
        "type": "config",
        "version": version,
        "child_name": child_name,
        "tiles": copy.deepcopy(settings["tiles"]),
        "settings_visible": settings["settings_visible"],
        "music_enabled": settings["music_enabled"],
        "max_volume": settings["max_volume"],
        "alarm_max_volume": settings["alarm_max_volume"],
        "alarm": {
            "enabled": alarm["enabled"],
            "time": alarm["time"],
            "weekdays": alarm["weekdays"],
            "lead_minutes": alarm["lead_minutes"],
            "with_light": alarm["with_light"],
            "button_required": alarm["button_required"],
        },
        "sleep_timer": copy.deepcopy(settings["sleep_timer"]),
        "night": copy.deepcopy(settings["night"]),
        "day_brightness": settings["day_brightness"],
        "sendspin": sendspin,
        "room": room,
    }
