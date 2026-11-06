"""Classification of Home Assistant entities into KidsHub room kinds."""

from __future__ import annotations

from typing import Literal

from homeassistant.core import State

Kind = Literal["switch", "dimmer", "cover", "color"]

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
