"""Tests for the pure helper modules."""

from homeassistant.core import State

from custom_components.kidshub.config_builder import DEFAULT_COLORS, build_config
from custom_components.kidshub.room import classify
from custom_components.kidshub.sorting import sort_episodes, sort_tracks
from custom_components.kidshub.sunrise import lamp_brightness, sunrise_color
from custom_components.kidshub.store import DEFAULT_SETTINGS


def test_sunrise():
    assert sunrise_color(0) == (90, 10, 5)
    assert sunrise_color(1) == (255, 225, 160)
    assert sunrise_color(0.5) == (228, 83, 23)
    assert sunrise_color(-1) == sunrise_color(0)
    assert sunrise_color(2) == (255, 225, 160)
    assert lamp_brightness(0.01) == 0
    assert lamp_brightness(0.5) == 128
    assert lamp_brightness(1) == 255


def test_classify():
    def s(eid, **attrs):
        return State(eid, "on", attrs)

    assert classify(s("light.x", supported_color_modes=["hs"])) == "color"
    assert classify(s("light.x", supported_color_modes=["color_temp", "xy"])) == "color"
    assert classify(s("light.x", supported_color_modes=["brightness"])) == "dimmer"
    assert classify(s("light.x", supported_color_modes=["color_temp"])) == "dimmer"
    assert classify(s("light.x", supported_color_modes=["onoff"])) == "switch"
    assert classify(s("light.x")) == "switch"
    assert classify(s("cover.x")) == "cover"
    assert classify(s("switch.x")) == "switch"
    assert classify(s("fan.x")) == "switch"
    assert classify(s("input_boolean.x")) == "switch"


def _config(options, sendspin=None):
    states = {
        "light.a": State("light.a", "on", {"supported_color_modes": ["hs"],
                                           "friendly_name": "A", "icon": "mdi:lamp"}),
        "switch.b": State("switch.b", "off", {"friendly_name": "B"}),
    }
    return build_config(
        version=3, child_name="Paul", settings=DEFAULT_SETTINGS, options=options,
        hass_states=states.get, sendspin=sendspin,
    )


def test_build_config_sendspin():
    info = {"url": "ws://h:8927/sendspin", "client_id": "kidshub-abc", "name": "KidsHub Paul"}
    assert _config({}, info)["sendspin"] == info
    assert _config({})["sendspin"] is None


def test_build_config_room_and_keys():
    cfg = _config({
        "room_entities": ["light.a", "switch.b", "light.gone"],
        "room_names": {"light.a": "Leselampe"},
        "stop_button_entity": "button.x",
    })
    assert set(cfg) == {
        "type", "version", "child_name", "tiles", "settings_visible", "music_enabled",
        "max_volume", "alarm_max_volume", "alarm", "sleep_timer", "night",
        "day_brightness", "sendspin", "room",
    }
    assert cfg["type"] == "config" and cfg["version"] == 3
    assert set(cfg["alarm"]) == {"enabled", "time", "weekdays", "lead_minutes",
                                 "with_light", "button_required"}
    assert cfg["alarm"]["button_required"] is True
    assert _config({})["alarm"]["button_required"] is False
    a, b = cfg["room"]
    assert [r["entity_id"] for r in cfg["room"]] == ["light.a", "switch.b"]
    assert a["name"] == "Leselampe" and a["kind"] == "color"
    assert a["icon"] == "mdi:lamp" and len(a["colors"]) == 6
    assert a["colors"] == DEFAULT_COLORS
    assert b["name"] == "B" and b["kind"] == "switch"
    assert b["icon"] is None and b["colors"] == []


def test_sort_episodes():
    items = [
        {"uri": "old", "stars": 5, "_published": 1.0},
        {"uri": "new", "stars": None, "_published": 3.0},
        {"uri": "mid", "stars": 4, "_published": 2.0},
        {"uri": "part", "stars": None, "_published": 0.5,
         "progress": {"position_ms": 1000, "duration_ms": 9000, "finished": False}},
        {"uri": "done", "stars": None, "_published": 4.0,
         "progress": {"position_ms": 9000, "duration_ms": 9000, "finished": True}},
    ]
    res = sort_episodes(items, "default")
    assert [i["uri"] for i in res] == ["part", "done", "new", "mid", "old"]
    assert all("_published" not in i for i in res)
    assert [i["uri"] for i in sort_episodes(items, "stars")] == [
        "old", "mid", "done", "new", "part"]
    assert "_published" in items[0]


def test_sort_tracks():
    items = [{"uri": "a", "stars": None}, {"uri": "b", "stars": 3},
             {"uri": "c", "stars": None}, {"uri": "d", "stars": 5}]
    assert [i["uri"] for i in sort_tracks(items, "stars")] == ["d", "b", "a", "c"]
    assert sort_tracks(items, "default") == items
