"""Tests for the wake lamp and the stop button."""

import hashlib
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.kidshub.const import DOMAIN

COLOR = {"supported_color_modes": ["rgb"]}


async def _setup(hass: HomeAssistant, options=None, with_light=True):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-a",
        data={
            "entry_type": "device",
            "device_id": "dev-a",
            "child_name": "Paul",
            "token_hash": hashlib.sha256(b"tok-a").hexdigest(),
        },
        options=options
        if options is not None
        else {"wake_lamp_entity": "light.wake", "stop_button_entity": "button.stop"},
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    device = hass.data[DOMAIN].devices["dev-a"]
    if with_light:
        await device.store.async_update_settings({"alarm": {"with_light": True}})
    device.push = AsyncMock()
    return entry, device


def _state(alarm_state, progress=0.0):
    return {
        "type": "state",
        "alarm_state": alarm_state,
        "sunrise_progress": progress,
    }


async def test_lamp_follows_sunrise(hass: HomeAssistant) -> None:
    hass.states.async_set("light.wake", "off", COLOR)
    _, device = await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await device.async_on_state(_state("sunrise", 0.5))
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == "light.wake"
    assert calls[0].data["brightness"] == 128
    assert tuple(calls[0].data["rgb_color"]) == (228, 83, 23)
    assert calls[0].data["transition"] == 15


async def test_lamp_dimmer_has_no_color(hass: HomeAssistant) -> None:
    hass.states.async_set("light.wake", "off", {"supported_color_modes": ["brightness"]})
    _, device = await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await device.async_on_state(_state("sunrise", 0.5))
    await hass.async_block_till_done()
    assert "rgb_color" not in calls[0].data
    assert calls[0].data["brightness"] == 128


async def test_lamp_ignored_without_with_light(hass: HomeAssistant) -> None:
    hass.states.async_set("light.wake", "off", COLOR)
    _, device = await _setup(hass, with_light=False)
    calls = async_mock_service(hass, "light", "turn_on")
    await device.async_on_state(_state("sunrise", 0.5))
    await hass.async_block_till_done()
    assert calls == []


async def test_lamp_only_on_progress_change(hass: HomeAssistant) -> None:
    hass.states.async_set("light.wake", "off", COLOR)
    _, device = await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await device.async_on_state(_state("sunrise", 0.5))
    await device.async_on_state(_state("sunrise", 0.5))
    device.notify()
    await hass.async_block_till_done()
    assert len(calls) == 1
    await device.async_on_state(_state("sunrise", 0.6))
    await hass.async_block_till_done()
    assert len(calls) == 2


async def test_ringing_full_once_and_stopped_untouched(hass: HomeAssistant) -> None:
    hass.states.async_set("light.wake", "on", COLOR)
    _, device = await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await device.async_on_state(_state("ringing", 1.0))
    await device.async_on_state(_state("ringing", 1.0))
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["brightness"] == 255
    assert tuple(calls[0].data["rgb_color"]) == (255, 225, 160)
    await device.async_on_state(_state("stopped", 1.0))
    await device.async_on_state(_state("off", 0.0))
    await hass.async_block_till_done()
    assert len(calls) == 1
    await device.async_on_state(_state("sunrise", 0.5))  # next day restarts
    await hass.async_block_till_done()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"alarm_state": "sunrise", "sunrise_progress": float("nan")},
        {"alarm_state": "sunrise", "sunrise_progress": 1.5},
        {"alarm_state": "sunrise", "sunrise_progress": True},
        {"alarm_state": "sunrise", "sunrise_progress": "0.5"},
        {"alarm_state": "sunrise"},
        {"alarm_state": "bogus", "sunrise_progress": 0.5},
        {"alarm_state": ["sunrise"], "sunrise_progress": 0.5},
    ],
)
async def test_untrusted_state_ignored(hass: HomeAssistant, bad) -> None:
    hass.states.async_set("light.wake", "off", COLOR)
    _, device = await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await device.async_on_state({"type": "state", **bad})
    await hass.async_block_till_done()
    assert calls == []


async def test_service_failure_does_not_raise(hass: HomeAssistant) -> None:
    hass.states.async_set("light.wake", "off", COLOR)
    _, device = await _setup(hass)
    # no light service registered -> ServiceNotFound
    await device.async_on_state(_state("sunrise", 0.5))
    await hass.async_block_till_done()


async def test_stop_button_only_when_ringing(hass: HomeAssistant) -> None:
    hass.states.async_set("button.stop", "unknown")
    _, device = await _setup(hass)
    await device.async_on_state(_state("off"))
    hass.states.async_set("button.stop", "2026-11-06T07:00:00+00:00")
    await hass.async_block_till_done()
    device.push.assert_not_called()
    await device.async_on_state(_state("ringing", 1.0))
    hass.states.async_set("button.stop", "2026-11-06T07:01:00+00:00")
    await hass.async_block_till_done()
    device.push.assert_awaited_once_with({"type": "alarm_cmd", "cmd": "stop"})


async def test_stop_button_ignores_availability_and_attribute_changes(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("button.stop", "2026-11-06T07:00:00+00:00")
    _, device = await _setup(hass)
    await device.async_on_state(_state("ringing", 1.0))
    hass.states.async_set("button.stop", "unavailable")
    hass.states.async_set("button.stop", "2026-11-06T07:00:00+00:00")
    hass.states.async_set("button.stop", "2026-11-06T07:00:00+00:00", {"x": 1})
    hass.states.async_set("button.stop", "unknown")
    await hass.async_block_till_done()
    device.push.assert_not_called()


async def test_stop_binary_sensor_and_event(hass: HomeAssistant) -> None:
    hass.states.async_set("binary_sensor.stop", "off")
    _, device = await _setup(hass, {"stop_button_entity": "binary_sensor.stop"})
    await device.async_on_state(_state("ringing", 1.0))
    hass.states.async_set("binary_sensor.stop", "unavailable")
    hass.states.async_set("binary_sensor.stop", "on")  # not from off
    await hass.async_block_till_done()
    device.push.assert_not_called()
    hass.states.async_set("binary_sensor.stop", "off")
    hass.states.async_set("binary_sensor.stop", "on")
    await hass.async_block_till_done()
    assert device.push.await_count == 1


async def test_stop_event_entity(hass: HomeAssistant) -> None:
    hass.states.async_set("event.stop", "2026-11-06T07:00:00+00:00")
    _, device = await _setup(hass, {"stop_button_entity": "event.stop"})
    await device.async_on_state(_state("ringing", 1.0))
    hass.states.async_set("event.stop", "2026-11-06T07:00:05+00:00")
    await hass.async_block_till_done()
    assert device.push.await_count == 1


async def test_options_update_resubscribes(hass: HomeAssistant) -> None:
    hass.states.async_set("button.stop", "unknown")
    hass.states.async_set("button.other", "unknown")
    entry, device = await _setup(hass)
    await device.async_on_state(_state("ringing", 1.0))
    hass.config_entries.async_update_entry(
        entry, options={"stop_button_entity": "button.other"}
    )
    await hass.async_block_till_done()
    hass.states.async_set("button.stop", "2026-11-06T07:00:00+00:00")
    await hass.async_block_till_done()
    stop = {"type": "alarm_cmd", "cmd": "stop"}
    assert stop not in [c.args[0] for c in device.push.await_args_list]
    hass.states.async_set("button.other", "2026-11-06T07:00:00+00:00")
    await hass.async_block_till_done()
    assert [c.args[0] for c in device.push.await_args_list].count(stop) == 1


def _app_state(alarm_state, progress=None):
    """State shaped like the real app: sunrise_progress only in sunrise/ringing."""
    msg = {"type": "state", "alarm_state": alarm_state, "sleep_remaining_s": None}
    if progress is not None:
        msg["sunrise_progress"] = progress
    return msg


async def test_lamp_rearms_without_progress_key_lead_zero(hass: HomeAssistant) -> None:
    """Two consecutive alarms with lead 0: off/stopped carry no progress key."""
    hass.states.async_set("light.wake", "off", COLOR)
    _, device = await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await device.async_on_state(_app_state("off"))
    await device.async_on_state(_app_state("ringing", 1.0))
    await hass.async_block_till_done()
    assert len(calls) == 1
    await device.async_on_state(_app_state("stopped"))
    await device.async_on_state(_app_state("off"))
    await device.async_on_state(_app_state("ringing", 1.0))  # next day
    await hass.async_block_till_done()
    assert len(calls) == 2
    assert calls[1].data["brightness"] == 255


async def test_ringing_without_progress_key(hass: HomeAssistant) -> None:
    hass.states.async_set("light.wake", "off", COLOR)
    _, device = await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await device.async_on_state(_app_state("ringing"))
    await hass.async_block_till_done()
    assert len(calls) == 1 and calls[0].data["brightness"] == 255


async def test_stop_button_works_with_real_app_shape(hass: HomeAssistant) -> None:
    hass.states.async_set("button.stop", "unknown")
    _, device = await _setup(hass)
    await device.async_on_state(_app_state("ringing"))
    hass.states.async_set("button.stop", "2026-11-06T07:01:00+00:00")
    await hass.async_block_till_done()
    device.push.assert_awaited_once_with({"type": "alarm_cmd", "cmd": "stop"})


async def test_refresh_resets_lamp_key(hass: HomeAssistant) -> None:
    hass.states.async_set("light.wake", "off", COLOR)
    entry, device = await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await device.async_on_state(_app_state("ringing", 1.0))
    await hass.async_block_till_done()
    hass.data[DOMAIN].extras[entry.entry_id].refresh()
    await device.async_on_state(_app_state("ringing", 1.0))
    await hass.async_block_till_done()
    assert len(calls) == 2
