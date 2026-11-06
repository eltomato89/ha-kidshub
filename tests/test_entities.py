"""Tests for the KidsHub device entities."""

import asyncio
import hashlib

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kidshub.const import DOMAIN

WS_URL = "/api/kidshub/ws"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-a",
        data={
            "entry_type": "device",
            "device_id": "dev-a",
            "child_name": "Paul",
            "token_hash": hashlib.sha256(b"tok-a").hexdigest(),
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


async def _connect(hass, hass_client_no_auth):
    client = await hass_client_no_auth()
    ws = await client.ws_connect(WS_URL)
    await ws.send_json(
        {"type": "auth", "device_id": "dev-a", "token": "tok-a", "app_version": "1.2.3"}
    )
    assert (await ws.receive_json())["type"] == "auth_ok"
    assert (await ws.receive_json())["type"] == "config"
    assert (await ws.receive_json())["type"] == "now_playing"
    await hass.async_block_till_done()
    return ws


async def _wait_state(hass, entity_id: str, expected: str) -> None:
    """Wait until the entity reaches ``expected`` (the WS message is async)."""
    for _ in range(100):
        await hass.async_block_till_done()
        if hass.states.get(entity_id).state == expected:
            return
        await asyncio.sleep(0.02)
    assert hass.states.get(entity_id).state == expected


def _entity_id(hass, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"dev-a_{key}")
    assert entity_id, key
    return entity_id


async def test_number_sets_setting_and_pushes(hass, hass_client_no_auth) -> None:
    await _setup(hass)
    ws = await _connect(hass, hass_client_no_auth)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.kidshub_paul_max_volume", "value": 45},
        blocking=True,
    )
    device = hass.data[DOMAIN].devices["dev-a"]
    assert device.store.settings["max_volume"] == 45
    config = await ws.receive_json()
    assert config["type"] == "config"
    assert config["max_volume"] == 45
    assert hass.states.get("number.kidshub_paul_max_volume").state == "45"
    await ws.close()


async def test_weekday_switch(hass, hass_client_no_auth) -> None:
    await _setup(hass)
    mon = _entity_id(hass, "switch", "alarm_mon")
    sat = _entity_id(hass, "switch", "alarm_sat")
    device = hass.data[DOMAIN].devices["dev-a"]
    await hass.services.async_call("switch", "turn_off", {"entity_id": mon}, blocking=True)
    assert device.store.settings["alarm"]["weekdays"] == [1, 2, 3, 4]
    await hass.services.async_call("switch", "turn_on", {"entity_id": sat}, blocking=True)
    assert device.store.settings["alarm"]["weekdays"] == [1, 2, 3, 4, 5]
    assert hass.states.get(sat).state == "on"
    assert hass.states.get(mon).state == "off"


async def test_time_entity(hass) -> None:
    await _setup(hass)
    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": _entity_id(hass, "time", "alarm_time"), "time": "06:15:00"},
        blocking=True,
    )
    device = hass.data[DOMAIN].devices["dev-a"]
    assert device.store.settings["alarm"]["time"] == "06:15"
    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": _entity_id(hass, "time", "night_end"), "time": "07:00:00"},
        blocking=True,
    )
    assert device.store.settings["night"]["end"] == "07:00"


async def test_state_message_updates_entities(hass, hass_client_no_auth) -> None:
    await _setup(hass)
    ws = await _connect(hass, hass_client_no_auth)
    await ws.send_json(
        {
            "type": "state",
            "playback": "playing",
            "volume": 45,
            "alarm_state": "ringing",
            "sleep_remaining_s": 600,
        }
    )
    await _wait_state(hass, "sensor.kidshub_paul_alarm_state", "ringing")
    assert hass.states.get("sensor.kidshub_paul_sleep_remaining").state == "600"
    assert hass.states.get("binary_sensor.kidshub_paul_online").state == "on"
    assert hass.states.get("sensor.kidshub_paul_app_version").state == "1.2.3"
    from homeassistant.helpers import device_registry as dr

    dev = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "dev-a"), hass.config_entries.async_entries(DOMAIN)[0].entry_id)
    assert dev.sw_version == "1.2.3"
    assert dev.name == "KidsHub Paul"
    await ws.send_json(
        {"type": "state", "alarm_state": "bogus", "sleep_remaining_s": "x"}
    )
    await _wait_state(hass, "sensor.kidshub_paul_alarm_state", "unknown")
    assert hass.states.get("sensor.kidshub_paul_sleep_remaining").state == "unknown"
    await ws.close()


async def test_offline_availability(hass) -> None:
    await _setup(hass)
    assert hass.states.get("sensor.kidshub_paul_alarm_state").state == "unavailable"
    assert hass.states.get("binary_sensor.kidshub_paul_online").state == "off"
    assert hass.states.get("number.kidshub_paul_max_volume").state != "unavailable"
    assert (
        hass.states.get(_entity_id(hass, "button", "stop_alarm")).state == "unavailable"
    )


async def test_top_rated(hass, hass_client_no_auth) -> None:
    await _setup(hass)
    device = hass.data[DOMAIN].devices["dev-a"]
    device.store.set_rating("u1", "track", 5, "A", "x", None)
    device.store.set_rating("u2", "track", 4, "B", "y", None)
    device.store.set_rating("u3", "track", 2, "C", "z", None)
    ws = await _connect(hass, hass_client_no_auth)
    state = hass.states.get("sensor.kidshub_paul_top_rated")
    assert state.state == "2"
    assert state.attributes["items"][0] == {
        "title": "A",
        "subtitle": "x",
        "kind": "track",
        "stars": 5,
    }
    await ws.close()


async def test_stop_alarm_button(hass, hass_client_no_auth) -> None:
    await _setup(hass)
    ws = await _connect(hass, hass_client_no_auth)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity_id(hass, "button", "stop_alarm")},
        blocking=True,
    )
    assert await ws.receive_json() == {"type": "alarm_cmd", "cmd": "stop"}
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity_id(hass, "button", "cancel_sleep_timer")},
        blocking=True,
    )
    assert await ws.receive_json() == {"type": "sleep_cmd", "cmd": "cancel"}
    await ws.close()


async def test_non_finite_sleep_remaining(hass, hass_client_no_auth) -> None:
    await _setup(hass)
    ws = await _connect(hass, hass_client_no_auth)
    sleep = "sensor.kidshub_paul_sleep_remaining"
    alarm = "sensor.kidshub_paul_alarm_state"
    await ws.send_json({"type": "state", "alarm_state": "off", "sleep_remaining_s": 60})
    await _wait_state(hass, sleep, "60")
    for raw, alarm_state in (
        ("Infinity", "sunrise"),
        ("-Infinity", "ringing"),
        ("NaN", "stopped"),
        ("1e30", "off"),
        ("1" + "0" * 400, "sunrise"),
    ):
        await ws.send_str(
            '{"type":"state","alarm_state":"%s","sleep_remaining_s":%s}'
            % (alarm_state, raw)
        )
        await _wait_state(hass, alarm, alarm_state)
        assert hass.states.get(sleep).state == "unknown", raw
    await ws.close()


async def test_sleep_remaining_whole_minutes(hass, hass_client_no_auth) -> None:
    """Floored to 60 s so the 2 s state updates do not write ~1800 rows/night."""
    await _setup(hass)
    ws = await _connect(hass, hass_client_no_auth)
    sleep = "sensor.kidshub_paul_sleep_remaining"
    alarm = "sensor.kidshub_paul_alarm_state"
    changes: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda e: changes.append(e.data["new_state"].state)
        if e.data["entity_id"] == sleep
        else None,
    )
    for remaining, alarm_state in ((812, "off"), (790, "sunrise"), (780, "off"), (59, "sunrise")):
        await ws.send_json(
            {"type": "state", "alarm_state": alarm_state, "sleep_remaining_s": remaining}
        )
        await _wait_state(hass, alarm, alarm_state)
    assert changes == ["780", "0"]
    await ws.close()
