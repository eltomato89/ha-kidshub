"""Tests for room control: options, live states and the ``room`` request."""

import asyncio
import hashlib

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.kidshub.const import DOMAIN

WS_URL = "/api/kidshub/ws"


async def _setup(hass: HomeAssistant, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-a",
        data={
            "entry_type": "device",
            "device_id": "dev-a",
            "child_name": "Paul",
            "token_hash": hashlib.sha256(b"tok-a").hexdigest(),
        },
        options=options if options is not None else {"room_entities": ["light.decke"]},
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


async def _connect(client):
    ws = await client.ws_connect(WS_URL)
    await ws.send_json(
        {"type": "auth", "device_id": "dev-a", "token": "tok-a", "app_version": "1"}
    )
    assert (await ws.receive_json())["type"] == "auth_ok"
    config = await ws.receive_json()
    assert config["type"] == "config"
    assert (await ws.receive_json())["type"] == "now_playing"
    return ws, config


async def _request(ws, id_, **fields):
    await ws.send_json({"type": "room", "id": id_, **fields})
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), 2)
        if msg["type"] == "result" and msg["id"] == id_:
            return msg


async def test_room_action_allowed(hass: HomeAssistant, hass_client_no_auth) -> None:
    hass.states.async_set("light.decke", "on", {"supported_color_modes": ["brightness"]})
    await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(
        ws, 1, entity_id="light.decke", action="set_brightness", value=128
    )
    assert res["success"] is True
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": "light.decke", "brightness": 128}
    await ws.close()


async def test_room_set_color_and_toggle_and_cover(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    hass.states.async_set("light.decke", "on")
    hass.states.async_set("cover.rollo", "open")
    await _setup(hass, {"room_entities": ["light.decke", "cover.rollo"]})
    turn_on = async_mock_service(hass, "light", "turn_on")
    toggle = async_mock_service(hass, "homeassistant", "toggle")
    close = async_mock_service(hass, "cover", "close_cover")
    ws, _ = await _connect(await hass_client_no_auth())
    assert (
        await _request(
            ws, 1, entity_id="light.decke", action="set_color", value=[1, 2, 3]
        )
    )["success"]
    assert turn_on[0].data["rgb_color"] == [1, 2, 3]
    assert (await _request(ws, 2, entity_id="light.decke", action="toggle"))["success"]
    assert toggle[0].data == {"entity_id": "light.decke"}
    assert (await _request(ws, 3, entity_id="cover.rollo", action="close"))["success"]
    assert close[0].data == {"entity_id": "cover.rollo"}
    await ws.close()


async def test_room_action_not_allowed(hass: HomeAssistant, hass_client_no_auth) -> None:
    hass.states.async_set("light.wohnzimmer", "on")
    await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(
        ws, 1, entity_id="light.wohnzimmer", action="set_brightness", value=10
    )
    assert res["success"] is False
    assert res["error"]["code"] == "not_allowed"
    assert calls == []
    await ws.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "set_brightness", "value": True},
        {"action": "set_brightness", "value": 256},
        {"action": "set_brightness", "value": -1},
        {"action": "set_brightness", "value": 1.5},
        {"action": "set_brightness"},
        {"action": "set_color", "value": [1, 2]},
        {"action": "set_color", "value": [1, 2, 256]},
        {"action": "set_color", "value": [1, 2, True]},
        {"action": "set_color", "value": "red"},
        {"action": "open"},  # cover action on a light
        {"action": "explode"},
        {"action": 5},
    ],
)
async def test_room_invalid(hass: HomeAssistant, hass_client_no_auth, fields) -> None:
    hass.states.async_set("light.decke", "on")
    await _setup(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, entity_id="light.decke", **fields)
    assert res["error"]["code"] == "invalid"
    assert calls == []
    await ws.close()


async def test_room_service_error_is_invalid(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    from homeassistant.exceptions import HomeAssistantError

    hass.states.async_set("light.decke", "on")
    await _setup(hass)

    async def boom(call):
        raise HomeAssistantError("lamp broke")

    hass.services.async_register("light", "turn_on", boom)
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(
        ws, 1, entity_id="light.decke", action="set_brightness", value=1
    )
    assert res["error"]["code"] == "invalid"
    assert "lamp broke" in res["error"]["message"]
    await ws.close()


async def test_entity_state_on_connect_and_push(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    hass.states.async_set(
        "light.decke", "off", {"supported_color_modes": ["brightness"]}
    )
    await _setup(hass)
    ws, _ = await _connect(await hass_client_no_auth())
    first = await asyncio.wait_for(ws.receive_json(), 2)
    assert first == {
        "type": "entity_state",
        "entity_id": "light.decke",
        "on": False,
        "brightness": None,
        "rgb": None,
        "position": None,
        "available": True,
    }
    hass.states.async_set(
        "light.decke", "on", {"brightness": 200, "rgb_color": (255, 0, 0)}
    )
    msg = await asyncio.wait_for(ws.receive_json(), 2)
    assert msg["type"] == "entity_state"
    assert msg["on"] is True
    assert msg["brightness"] == 200
    assert msg["rgb"] == [255, 0, 0]
    hass.states.async_set("light.decke", "unavailable")
    msg = await asyncio.wait_for(ws.receive_json(), 2)
    assert msg["available"] is False
    assert msg["on"] is False and msg["brightness"] is None
    await ws.close()


async def test_cover_state_and_removed_entity(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    hass.states.async_set("cover.rollo", "open", {"current_position": 40})
    await _setup(hass, {"room_entities": ["cover.rollo"]})
    ws, _ = await _connect(await hass_client_no_auth())
    msg = await asyncio.wait_for(ws.receive_json(), 2)
    assert msg["on"] is True and msg["position"] == 40
    hass.states.async_remove("cover.rollo")
    msg = await asyncio.wait_for(ws.receive_json(), 2)
    assert msg["available"] is False and msg["position"] is None
    assert msg["on"] is False
    await ws.close()


async def test_options_change_repushes_config(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    hass.states.async_set("light.decke", "on", {"friendly_name": "Decke"})
    entry = await _setup(hass)
    ws, config = await _connect(await hass_client_no_auth())
    assert config["room"][0]["name"] == "Decke"
    await asyncio.wait_for(ws.receive_json(), 2)  # initial entity_state

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.MENU
    assert set(flow["menu_options"]) >= {"room", "alarm_extras"}
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "room"}
    )
    assert flow["step_id"] == "room"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"room_entities": ["light.decke"]}
    )
    assert flow["step_id"] == "room_names"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"light.decke": "Lampe"}
    )
    assert flow["step_id"] == "colors"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"color_1": [1, 2, 3]}
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["room_names"] == {"light.decke": "Lampe"}
    assert entry.options["colors"][0] == [1, 2, 3]
    assert len(entry.options["colors"]) == 6

    msgs = []
    while len(msgs) < 2:
        msgs.append(await asyncio.wait_for(ws.receive_json(), 2))
    assert msgs[0]["type"] == "config"
    assert msgs[0]["version"] > config["version"]
    assert msgs[0]["room"][0]["name"] == "Lampe"
    assert msgs[1]["type"] == "entity_state"
    await ws.close()


async def test_options_room_change_resubscribes(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    hass.states.async_set("light.decke", "on")
    hass.states.async_set("light.neu", "on")
    entry = await _setup(hass)
    ws, _ = await _connect(await hass_client_no_auth())
    await asyncio.wait_for(ws.receive_json(), 2)
    hass.config_entries.async_update_entry(
        entry, options={"room_entities": ["light.neu"]}
    )
    await hass.async_block_till_done()
    assert (await asyncio.wait_for(ws.receive_json(), 2))["type"] == "config"
    st = await asyncio.wait_for(ws.receive_json(), 2)
    assert st["entity_id"] == "light.neu"
    hass.states.async_set("light.decke", "off")  # no longer tracked
    hass.states.async_set("light.neu", "off")
    st = await asyncio.wait_for(ws.receive_json(), 2)
    assert st["entity_id"] == "light.neu"
    await ws.close()


async def test_stop_button_sets_button_required(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    hass.states.async_set("button.stop", "unknown")
    entry = await _setup(hass)
    ws, config = await _connect(await hass_client_no_auth())
    assert config["alarm"]["button_required"] is False
    await asyncio.wait_for(ws.receive_json(), 2)

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "alarm_extras"}
    )
    assert flow["step_id"] == "alarm_extras"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"stop_button_entity": "button.stop", "wake_lamp_entity": "light.decke"},
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["stop_button_entity"] == "button.stop"
    assert entry.options["wake_lamp_entity"] == "light.decke"
    assert entry.options["room_entities"] == ["light.decke"]  # preserved
    config = await asyncio.wait_for(ws.receive_json(), 2)
    assert config["type"] == "config"
    assert config["alarm"]["button_required"] is True
    await ws.close()


async def test_settings_entry_options_are_the_sendspin_override(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"entry_type": "settings"})
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "settings"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_entity_state_non_finite_attributes(bad) -> None:
    from homeassistant.core import State

    from custom_components.kidshub.room_bridge import entity_state_message

    msg = entity_state_message(
        "light.decke", State("light.decke", "on", {"brightness": bad})
    )
    assert msg["brightness"] is None and msg["available"] is True
    msg = entity_state_message(
        "cover.rollo", State("cover.rollo", "open", {"current_position": bad})
    )
    assert msg["position"] is None
