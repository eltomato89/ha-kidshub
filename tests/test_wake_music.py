"""Tests for the child's own wake-up music (``set_wake_music``, protocol 3.5)."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.kidshub.const import DOMAIN

from .fake_ma import item
from .test_playback import (
    ALBUM,
    ALBUM_TRACKS,
    OPTIONS,
    PL1,
    PLAYER,
    TRACK_A,
    _connect,
    _device,
    _fake,
    _next,
    _request,
    _setup,
)

PL2 = item("library://playlist/2", "playlist", "Wecklieder", image="http://ma/pl2.png")
TRACK_C = item("library://track/c", "track", "Hahn", "Hof", image="http://ma/c.png")
WAKE_OPTIONS = {**OPTIONS, "playlists": [PL1.uri, PL2.uri], "wake_media_uri": PL1.uri}


def _wake_fake():
    fake = _fake()
    fake._library["playlist"] = [PL1, PL2]
    fake._children[PL2.uri] = [TRACK_C]
    return fake


async def _visible(hass):
    await _device(hass).update_settings({"settings_visible": True})


def _entity(hass, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"dev-a_{key}")
    assert entity_id, key
    return entity_id


async def test_set_wake_music_track_in_playlist(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _wake_fake()
    await _setup(hass, fake, WAKE_OPTIONS)
    await _visible(hass)
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "set_wake_music", uri=TRACK_C.uri)
    assert res["success"] is True and res["result"] == {}, res
    config = await _next(ws, "config")
    assert config["alarm"]["wake_music"] == {
        "uri": TRACK_C.uri,
        "kind": "track",
        "title": "Hahn",
        "image_url": "/api/kidshub/img?u=http%3A%2F%2Fma%2Fc.png&s=512",
    }
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "sensor", "wake_music")).state == "Hahn"
    res = await _request(ws, 2, "wake_play")
    assert res["success"] is True, res
    assert fake.play_calls[-1] == (PLAYER, [TRACK_C.uri], 0, 0)
    await ws.close()


async def test_set_wake_music_playlist_and_reset(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _wake_fake()
    await _setup(hass, fake, WAKE_OPTIONS)
    await _visible(hass)
    ws = await _connect(await hass_client_no_auth())
    assert (await _request(ws, 1, "set_wake_music", uri=PL2.uri))["success"]
    config = await _next(ws, "config")
    assert config["alarm"]["wake_music"]["kind"] == "playlist"
    assert (await _request(ws, 2, "set_wake_music", uri=None))["success"]
    config = await _next(ws, "config")
    assert config["alarm"]["wake_music"] is None
    await ws.close()


async def test_set_wake_music_rules(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _wake_fake()
    await _setup(hass, fake, WAKE_OPTIONS)
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "set_wake_music", uri=TRACK_C.uri)
    assert res["error"]["code"] == "not_allowed"  # settings not visible
    await _visible(hass)
    for id_, uri, code in (
        (2, "library://track/unknown", "not_allowed"),
        (3, ALBUM.uri, "invalid"),
        (4, ALBUM_TRACKS[0].uri, "invalid"),
        (5, 5, "invalid"),
        (6, "kidshub://favorites/track", "invalid"),
    ):
        res = await _request(ws, id_, "set_wake_music", uri=uri)
        assert res["error"]["code"] == code, (uri, res)
    res = await _request(ws, 7, "set_wake_music")
    assert res["error"]["code"] == "invalid"
    assert _device(hass).store.settings["alarm"]["wake_music"] is None
    await ws.close()


async def test_wake_play_falls_back_when_no_longer_allowed(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _wake_fake()
    entry = await _setup(hass, fake, WAKE_OPTIONS)
    await _visible(hass)
    ws = await _connect(await hass_client_no_auth())
    assert (await _request(ws, 1, "set_wake_music", uri=TRACK_C.uri))["success"]
    hass.config_entries.async_update_entry(
        entry, options={**WAKE_OPTIONS, "playlists": [PL1.uri]}
    )
    await hass.async_block_till_done()
    res = await _request(ws, 2, "wake_play")
    assert res["success"] is True, res
    assert fake.play_calls[-1] == (PLAYER, [PL1.uri], 0, 0)
    await ws.close()


async def test_reset_wake_music_button(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _wake_fake()
    await _setup(hass, fake, WAKE_OPTIONS)
    await _visible(hass)
    ws = await _connect(await hass_client_no_auth())
    assert (await _request(ws, 1, "set_wake_music", uri=TRACK_A.uri))["success"]
    await _next(ws, "config")
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity(hass, "button", "reset_wake_music")},
        blocking=True,
    )
    config = await _next(ws, "config")
    assert config["alarm"]["wake_music"] is None
    sensor = hass.states.get(_entity(hass, "sensor", "wake_music"))
    assert sensor.state == "unknown"
    assert sensor.attributes["uri"] is None
    await ws.close()
