"""Classification of Home Assistant entities into KidsHub room kinds."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from homeassistant.core import State

Kind = Literal["switch", "dimmer", "cover", "color"]

MAX_BRIGHTNESS_OPTION = "room_max_brightness"  # options: {entity_id: percent 1..100}

_COLOR_MODES = {"hs", "rgb", "rgbw", "rgbww", "xy"}
_DIMMER_MODES = {"brightness", "color_temp", "white"}


def classify(state: State) -> Kind:
    """Return the room kind for an entity state."""
    domain = state.entity_id.split(".", 1)[0]
    if domain == "cover":
        return "cover"
    if domain == "light":
        modes = {str(m) for m in (state.attributes.get("supported_color_modes") or [])}
        if modes & _COLOR_MODES:
            return "color"
        if modes & _DIMMER_MODES:
            return "dimmer"
    return "switch"


def max_brightness(options: Mapping[str, Any], entity_id: str) -> int | None:
    """Return the brightness limit (1..255) of a light, None without a limit.

    The options store a percentage per light; 100 % (or none) means no limit.
    """
    if not entity_id.startswith("light."):
        return None
    pct = (options.get(MAX_BRIGHTNESS_OPTION) or {}).get(entity_id)
    if isinstance(pct, bool) or not isinstance(pct, int | float):
        return None
    if not 1 <= pct < 100:
        return None
    return max(1, round(pct * 255 / 100))
