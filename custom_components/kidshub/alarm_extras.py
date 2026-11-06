"""Alarm extras of one KidsHub device: wake lamp and stop button."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .device import KidDevice
from .room import classify
from .sunrise import lamp_brightness, sunrise_color

_LOGGER = logging.getLogger(__name__)

_ALARM_STATES = {"off", "sunrise", "ringing", "stopped"}
_IGNORED = {"unavailable", "unknown"}
SUNRISE_TRANSITION = 15


def _alarm_state(state: Any) -> tuple[str, float | None] | None:
    """Validate the untrusted app ``state`` and return (alarm_state, progress).

    ``sunrise_progress`` is only required (finite, 0..1) in ``sunrise``; the app
    omits it in ``off``/``stopped`` and may omit it in ``ringing`` (protocol 3.3).
    """
    if not isinstance(state, dict):
        return None
    alarm = state.get("alarm_state")
    if not isinstance(alarm, str) or alarm not in _ALARM_STATES:
        return None
    progress = state.get("sunrise_progress")
    if progress is None and alarm != "sunrise":
        return alarm, None
    if (
        isinstance(progress, bool)
        or not isinstance(progress, (int, float))
        or not math.isfinite(progress)
        or not 0 <= progress <= 1
    ):
        return None
    return alarm, float(progress)


class AlarmExtras:
    """Drive the wake lamp from the sunrise and stop a ringing alarm by button."""

    def __init__(self, hass: HomeAssistant, device: KidDevice) -> None:
        self.hass = hass
        self.device = device
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_signal: Callable[[], None] | None = None
        self._lamp_key: tuple[str, float] | None = None

    @property
    def _options(self) -> dict[str, Any]:
        return dict(self.device.entry.options)

    def start(self) -> None:
        """Subscribe to the device signal and the stop-button entity."""
        self.stop()
        self._unsub_signal = async_dispatcher_connect(
            self.hass, self.device.signal, self._on_device_update
        )
        entity_id = self._options.get("stop_button_entity")
        if isinstance(entity_id, str) and entity_id:
            self._unsub_state = async_track_state_change_event(
                self.hass, [entity_id], self._on_button_change
            )

    def stop(self) -> None:
        """Drop all subscriptions."""
        for unsub in (self._unsub_signal, self._unsub_state):
            if unsub is not None:
                unsub()
        self._unsub_signal = self._unsub_state = None

    def refresh(self) -> None:
        """Resubscribe after an options change and re-arm the lamp."""
        self._lamp_key = None
        self.start()

    # --- wake lamp -------------------------------------------------------

    @callback
    def _on_device_update(self) -> None:
        parsed = _alarm_state(self.device.last_state)
        if parsed is None:
            return
        alarm, progress = parsed
        if alarm in ("off", "stopped"):
            self._lamp_key = None  # lamp stays untouched; re-arm for next run
            return
        lamp = self._options.get("wake_lamp_entity")
        alarm_settings = self.device.store.settings.get("alarm") or {}
        if not lamp or alarm_settings.get("with_light") is not True:
            return
        if alarm == "sunrise":
            assert progress is not None  # required for sunrise
            key = ("sunrise", round(progress, 3))
        else:
            key = ("ringing", 1.0)
        if key == self._lamp_key:
            return
        self._lamp_key = key
        p = progress if alarm == "sunrise" and progress is not None else 1.0
        data: dict[str, Any] = {"entity_id": lamp, "brightness": lamp_brightness(p)}
        if alarm == "ringing":
            data["brightness"] = 255
        else:
            data["transition"] = SUNRISE_TRANSITION
        lamp_state = self.hass.states.get(lamp)
        if lamp_state is not None and classify(lamp_state) == "color":
            data["rgb_color"] = sunrise_color(p)
        if data["brightness"] == 0:
            return  # below the visible threshold: nothing to turn on yet
        self.hass.async_create_task(
            self._call("light", "turn_on", data), "kidshub_wake_lamp"
        )

    async def _call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=False)
        except Exception as err:  # noqa: BLE001 - never raise into the dispatcher
            _LOGGER.warning(
                "Wake lamp call %s.%s for %s failed: %s",
                domain,
                service,
                data.get("entity_id"),
                err,
            )

    # --- stop button -----------------------------------------------------

    @callback
    def _on_button_change(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None or new.state in _IGNORED or old.state == "unavailable":
            return
        domain = new.entity_id.split(".", 1)[0]
        if domain == "binary_sensor":
            pressed = old.state == "off" and new.state == "on"
        else:  # button, input_button, event: a new timestamp is a press
            pressed = (
                new.state != old.state and dt_util.parse_datetime(new.state) is not None
            )
        if not pressed:
            return
        parsed = _alarm_state(self.device.last_state)
        if parsed is None or parsed[0] != "ringing":
            return
        self.hass.async_create_task(
            self._push_stop(), "kidshub_alarm_stop_button"
        )

    async def _push_stop(self) -> None:
        try:
            await self.device.push({"type": "alarm_cmd", "cmd": "stop"})
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not send alarm stop", exc_info=True)
