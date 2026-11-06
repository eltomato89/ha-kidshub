"""Tests for playback: play/resume, control, wake_play, queue tracking."""

import asyncio
import hashlib

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kidshub import playback as playback_module
from custom_components.kidshub.const import DOMAIN
from custom_components.kidshub.ma import QueueSnapshot

from .fake_ma import FakeMa, item

WS_URL = "/api/kidshub/ws"
PLAYER = "player-1"

PL1 = item("library://playlist/1", "playlist", "Lieder")
TRACK_A = item("library://track/a", "track", "A", "Band", duration_ms=180000)
TRACK_B = item("library://track/b", "track", "B", "Band", duration_ms=180000)
ALBUM = item("library://album/1", "album", "Hörspiel")
ALBUM_TRACKS = [
    item(f"library://track/al{i}", "album_track", f"Kapitel {i}", duration_ms=600000)
    for i in range(1, 6)
]
POD = item("library://podcast/1", "podcast", "Kinderpodcast")
EP1 = item("library://podcast_episode/1", "episode", "Folge 1", duration_ms=1200000)
EP2 = item("library://podcast_episode/2", "episode", "Folge 2", duration_ms=1200000)
BOOK = item("library://audiobook/1", "audiobook", "Buch", duration_ms=3600000)

OPTIONS = {
    "playlists": [PL1.uri],
    "audiobooks": [ALBUM.uri, BOOK.uri],
    "podcasts": [POD.uri],
}


def _fake(**kwargs) -> FakeMa:
    kwargs.setdefault(
        "library",
        {
            "playlist": [PL1],
            "album": [ALBUM],
            "audiobook": [BOOK],
            "podcast": [POD],
        },
    )
    kwargs.setdefault(
        "children",
        {
            PL1.uri: [TRACK_A, TRACK_B],
            ALBUM.uri: ALBUM_TRACKS,
            POD.uri: [EP1, EP2],
        },
    )
    kwargs.setdefault("players", {"kidshub-dev-a": PLAYER})
    return FakeMa(**kwargs)


async def _setup(hass: HomeAssistant, fake: FakeMa, options=OPTIONS):
    assert await async_setup_component(hass, DOMAIN, {})
    hass.data[DOMAIN].ma = fake
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-a",
        data={
            "entry_type": "device",
            "device_id": "dev-a",
            "child_name": "Paul",
            "token_hash": hashlib.sha256(b"tok-a").hexdigest(),
        },
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _device(hass):
    return hass.data[DOMAIN].devices["dev-a"]


async def _connect(client):
    ws = await client.ws_connect(WS_URL)
    await ws.send_json(
        {"type": "auth", "device_id": "dev-a", "token": "tok-a", "app_version": "1"}
    )
    assert (await ws.receive_json())["type"] == "auth_ok"
    assert (await ws.receive_json())["type"] == "config"
    return ws


async def _request(ws, id_, type_, **fields):
    await ws.send_json({"type": type_, "id": id_, **fields})
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), 2)
        if msg["type"] == "result" and msg["id"] == id_:
            return msg


async def _next(ws, type_):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), 2)
        if msg["type"] == type_:
            return msg


def _snap(current, *, elapsed_ms=0, index=0, playing=True, container=None):
    return QueueSnapshot(
        current=current,
        container_uri=container,
        index=index,
        elapsed_ms=elapsed_ms,
        playing=playing,
    )


def _as_track(it):
    """Album tracks come out of the MA queue as plain tracks."""
    return item(it.uri, "track", it.title, it.subtitle, duration_ms=it.duration_ms)


# -- play -------------------------------------------------------------------


async def test_play_episode_resumes(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake)
    _device(hass).store.set_progress(
        EP1.uri, position_ms=120000, duration_ms=1200000, finished=False
    )
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[EP1.uri])
    assert res["success"] is True, res
    assert res["result"] == {}
    assert fake.play_calls == [(PLAYER, [EP1.uri], 0, 120000)]
    assert fake.seek_calls == []
    await ws.close()


async def test_play_finished_episode_starts_over(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    _device(hass).store.set_progress(
        EP1.uri, position_ms=1190000, duration_ms=1200000, finished=True
    )
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[EP1.uri])
    assert res["success"] is True, res
    assert fake.play_calls == [(PLAYER, [EP1.uri], 0, 0)]
    await ws.close()


async def test_play_from_start(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake)
    store = _device(hass).store
    store.set_progress(BOOK.uri, position_ms=50000, duration_ms=3600000, finished=False)
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[BOOK.uri], from_start=True)
    assert res["success"] is True, res
    assert fake.play_calls == [(PLAYER, [BOOK.uri], 0, 0)]
    rec = store.get_progress(BOOK.uri)
    assert rec.position_ms == 0
    assert rec.finished is False
    await ws.close()


async def test_play_album_resumes_track(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    _device(hass).store.set_progress(
        ALBUM.uri,
        position_ms=30000,
        duration_ms=600000,
        finished=False,
        track_index=2,
    )
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[ALBUM.uri])
    assert res["success"] is True, res
    # the whole album is queued so that "previous" reaches earlier chapters
    assert fake.play_calls == [(PLAYER, [t.uri for t in ALBUM_TRACKS], 2, 30000)]
    assert fake.seek_calls == []
    await ws.close()


async def test_play_album_without_progress(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[ALBUM.uri], resume=False)
    assert res["success"] is True, res
    assert fake.play_calls == [(PLAYER, [t.uri for t in ALBUM_TRACKS], 0, 0)]
    await ws.close()


async def test_play_playlist_tracks_from_start_index(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    ws = await _connect(await hass_client_no_auth())
    res = await _request(
        ws, 1, "play", uris=[TRACK_A.uri, TRACK_B.uri], start_index=1
    )
    assert res["success"] is True, res
    assert fake.play_calls == [(PLAYER, [TRACK_A.uri, TRACK_B.uri], 1, 0)]
    await ws.close()


async def test_music_disabled_blocks_play(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    await _device(hass).update_settings({"music_enabled": False})
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[PL1.uri])
    assert res["success"] is False
    assert res["error"]["code"] == "not_allowed"
    res = await _request(ws, 2, "play", uris=[TRACK_A.uri])
    assert res["error"]["code"] == "not_allowed"
    assert fake.play_calls == []
    # audiobooks are not music
    res = await _request(ws, 3, "play", uris=[BOOK.uri])
    assert res["success"] is True, res
    await ws.close()


async def test_play_not_allowed(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake)
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=["library://playlist/99"])
    assert res["error"]["code"] == "not_allowed"
    assert fake.play_calls == []
    await ws.close()


async def test_play_allowed_but_missing_is_not_found(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake(library={"podcast": [POD]})  # album/audiobook gone from MA
    await _setup(hass, fake)
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[BOOK.uri])
    assert res["error"]["code"] == "not_found"
    await ws.close()


async def test_play_invalid(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass, _fake())
    ws = await _connect(await hass_client_no_auth())
    for i, fields in enumerate(
        (
            {"uris": []},
            {"uris": "x"},
            {"uris": [EP1.uri], "start_index": 1},
            {"uris": [EP1.uri], "resume": "yes"},
        ),
        1,
    ):
        res = await _request(ws, i, "play", **fields)
        assert res["error"]["code"] == "invalid", fields
    await ws.close()


async def test_play_favorites(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake)
    ws = await _connect(await hass_client_no_auth())
    assert (await _request(ws, 1, "rate", uri=TRACK_B.uri, kind="track", stars=5))[
        "success"
    ]
    assert (await _request(ws, 2, "rate", uri=TRACK_A.uri, kind="track", stars=4))[
        "success"
    ]
    res = await _request(ws, 3, "play", uris=["kidshub://favorites/track"])
    assert res["success"] is True, res
    assert fake.play_calls == [(PLAYER, [TRACK_B.uri, TRACK_A.uri], 0, 0)]
    await ws.close()


async def test_play_without_player_is_ma_unavailable(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake(players={})
    await _setup(hass, fake)
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[EP1.uri])
    assert res["error"]["code"] == "ma_unavailable"
    await ws.close()


# -- control ----------------------------------------------------------------


async def test_control(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake)
    ws = await _connect(await hass_client_no_auth())
    for i, cmd in enumerate(("pause", "resume", "next", "previous"), 1):
        assert (await _request(ws, i, "control", cmd=cmd))["success"] is True
    assert (await _request(ws, 5, "control", cmd="seek", position_ms=42000))[
        "success"
    ] is True
    assert fake.command_calls == [
        (PLAYER, "pause"),
        (PLAYER, "resume"),
        (PLAYER, "next"),
        (PLAYER, "previous"),
    ]
    assert fake.seek_calls == [(PLAYER, 42000)]
    for i, fields in enumerate(
        ({"cmd": "stop"}, {"cmd": "seek"}, {"cmd": "seek", "position_ms": -1}), 10
    ):
        res = await _request(ws, i, "control", **fields)
        assert res["error"]["code"] == "invalid", fields
    await ws.close()


# -- queue tracking -----------------------------------------------------------


async def test_queue_update_saves_progress_and_pushes_now_playing(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    device.store.set_rating(EP1.uri, "episode", 4, "Folge 1", "", None)
    ws = await _connect(await hass_client_no_auth())
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=50000))
    msg = await _next(ws, "now_playing")
    assert msg["kind"] == "episode"
    assert msg["uri"] == EP1.uri
    assert msg["title"] == "Folge 1"
    assert msg["elapsed_ms"] == 50000
    assert msg["duration_ms"] == 1200000
    assert msg["stars"] == 4
    assert msg["image_url"].startswith("/api/kidshub/img?u=")
    assert msg["image_url"].endswith("&s=512")
    assert msg["container_uri"] is None
    rec = device.store.get_progress(EP1.uri)
    assert rec.position_ms == 50000
    assert rec.finished is False
    # the playing episode is rateable
    assert device.content.now_playing.uri == EP1.uri
    await ws.close()


async def test_queue_update_throttled(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(playback_module, "monotonic", lambda: now[0])
    fake = _fake()
    await _setup(hass, fake)
    store = _device(hass).store
    ws = await _connect(await hass_client_no_auth())
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=1000))
    assert (await _next(ws, "now_playing"))["elapsed_ms"] == 1000
    now[0] += 5
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=6000))
    await hass.async_block_till_done()
    assert store.get_progress(EP1.uri).position_ms == 1000  # throttled
    now[0] += 6
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=12000))
    # the 6000 snapshot was not pushed
    assert (await _next(ws, "now_playing"))["elapsed_ms"] == 12000
    assert store.get_progress(EP1.uri).position_ms == 12000
    await ws.close()


async def test_queue_advance_marks_previous_finished(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    store = _device(hass).store
    ws = await _connect(await hass_client_no_auth())
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=500000, index=0))
    await _next(ws, "now_playing")
    fake.emit_queue(PLAYER, _snap(EP2, elapsed_ms=0, index=1))
    msg = await _next(ws, "now_playing")
    assert msg["uri"] == EP2.uri
    assert store.get_progress(EP1.uri).finished is True
    assert store.get_progress(EP2.uri).finished is False
    # near the end counts as finished
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=1180000, index=0))
    await _next(ws, "now_playing")
    assert store.get_progress(EP1.uri).finished is True
    await ws.close()


async def test_album_track_now_playing_and_progress(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    device.store.set_rating(ALBUM.uri, "album", 5, "Hörspiel", "", None)
    ws = await _connect(await hass_client_no_auth())
    assert (await _request(ws, 1, "play", uris=[ALBUM.uri], resume=False))["success"]
    fake.emit_queue(PLAYER, _snap(_as_track(ALBUM_TRACKS[2]), elapsed_ms=40000, index=2))
    msg = await _next(ws, "now_playing")
    assert msg["kind"] == "album_track"
    assert msg["uri"] == ALBUM_TRACKS[2].uri
    assert msg["container_uri"] == ALBUM.uri
    assert msg["stars"] == 5  # the container's rating
    rec = device.store.get_progress(ALBUM.uri)
    assert (rec.track_index, rec.position_ms, rec.finished) == (2, 40000, False)
    assert device.store.get_progress(ALBUM_TRACKS[2].uri) is None
    # album tracks are not rated themselves
    res = await _request(ws, 2, "rate", uri=ALBUM_TRACKS[2].uri, kind="track", stars=3)
    assert res["error"]["code"] == "invalid"
    # last track near its end finishes the album
    fake.emit_queue(
        PLAYER, _snap(_as_track(ALBUM_TRACKS[4]), elapsed_ms=590000, index=4)
    )
    await _next(ws, "now_playing")
    rec = device.store.get_progress(ALBUM.uri)
    assert (rec.track_index, rec.finished) == (4, True)
    await ws.close()


async def test_playlist_track_now_playing_no_progress(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    ws = await _connect(await hass_client_no_auth())
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=1000, container=PL1.uri))
    msg = await _next(ws, "now_playing")
    assert msg["kind"] == "track"
    assert msg["stars"] is None
    assert device.store.get_progress(TRACK_A.uri) is None
    fake.emit_queue(PLAYER, _snap(None, playing=False))
    assert (await _next(ws, "now_playing")) == {"type": "now_playing", "kind": None}
    assert device.content.now_playing is None
    await ws.close()


async def test_now_playing_sent_on_connect(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=7000))
    await hass.async_block_till_done()
    ws = await _connect(await hass_client_no_auth())
    msg = await _next(ws, "now_playing")
    assert (msg["uri"], msg["elapsed_ms"]) == (EP1.uri, 7000)
    await ws.close()


async def test_subscribes_when_player_appears(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake(players={})
    await _setup(hass, fake)
    assert not fake._queue_subs.get(PLAYER)
    fake._players["kidshub-dev-a"] = PLAYER
    ws = await _connect(await hass_client_no_auth())  # app connect triggers a retry
    for _ in range(100):
        if fake._queue_subs.get(PLAYER):
            break
        await asyncio.sleep(0.01)
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=1000))
    assert (await _next(ws, "now_playing"))["uri"] == EP1.uri
    await ws.close()


async def test_unsubscribes_on_unload(hass: HomeAssistant) -> None:
    fake = _fake()
    entry = await _setup(hass, fake)
    assert len(fake._queue_subs[PLAYER]) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert fake._queue_subs[PLAYER] == []


async def test_music_disable_stops_running_track(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=1000))
    await hass.async_block_till_done()
    await device.update_settings({"music_enabled": False})
    await hass.async_block_till_done()
    assert fake.command_calls == [(PLAYER, "stop"), (PLAYER, "clear")]


async def test_music_disable_keeps_audiobook(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    fake.emit_queue(PLAYER, _snap(BOOK, elapsed_ms=1000))
    await hass.async_block_till_done()
    await device.update_settings({"music_enabled": False})
    await hass.async_block_till_done()
    assert fake.command_calls == []


# -- wake_play ------------------------------------------------------------------


async def test_wake_play(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake, {**OPTIONS, "wake_media_uri": PL1.uri})
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "wake_play")
    assert res["success"] is True, res
    assert fake.play_calls == [(PLAYER, [PL1.uri], 0, 0)]
    await ws.close()


async def test_wake_play_missing_uri(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass, _fake())
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "wake_play")
    assert res["error"]["code"] == "invalid"
    await ws.close()


async def test_wake_play_ma_down(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake, {**OPTIONS, "wake_media_uri": PL1.uri})
    fake.available = False
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "wake_play")
    assert res["success"] is False
    assert res["error"]["code"] == "ma_unavailable"
    await ws.close()


async def test_wake_play_slow_ma(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch
) -> None:
    monkeypatch.setattr(playback_module, "WAKE_PLAY_TIMEOUT", 0.05)
    fake = _fake()
    await _setup(hass, fake, {**OPTIONS, "wake_media_uri": PL1.uri})

    async def stall(*args, **kwargs):
        await asyncio.Event().wait()

    fake.play = stall
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "wake_play")
    assert res["error"]["code"] == "ma_unavailable"
    await ws.close()


# -- options ---------------------------------------------------------------------


async def test_options_wake_media_uri(hass: HomeAssistant) -> None:
    fake = _fake()
    entry = await _setup(hass, fake)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "alarm_extras"}
    )
    assert flow["step_id"] == "alarm_extras"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"wake_media_uri": PL1.uri}
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["wake_media_uri"] == PL1.uri
    assert entry.options["playlists"] == [PL1.uri]  # preserved

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "alarm_extras"}
    )
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {})
    assert "wake_media_uri" not in entry.options


async def test_music_disable_clears_paused_track(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=1000, playing=False))
    await hass.async_block_till_done()
    await device.update_settings({"music_enabled": False})
    await hass.async_block_till_done()
    assert fake.command_calls == [(PLAYER, "stop"), (PLAYER, "clear")]


async def test_control_blocked_for_music_when_disabled(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=1000, playing=False))
    await hass.async_block_till_done()
    await device.update_settings({"music_enabled": False})
    await hass.async_block_till_done()
    fake.command_calls.clear()
    ws = await _connect(await hass_client_no_auth())
    for i, fields in enumerate(
        (
            {"cmd": "resume"},
            {"cmd": "next"},
            {"cmd": "previous"},
            {"cmd": "seek", "position_ms": 0},
        ),
        1,
    ):
        res = await _request(ws, i, "control", **fields)
        assert res["error"]["code"] == "not_allowed", fields
    assert fake.command_calls == []
    assert fake.seek_calls == []
    # pausing is always allowed
    assert (await _request(ws, 9, "control", cmd="pause"))["success"] is True
    # audiobooks are not music
    fake.emit_queue(PLAYER, _snap(BOOK, elapsed_ms=1000, playing=False))
    assert (await _request(ws, 10, "control", cmd="resume"))["success"] is True
    await ws.close()


async def test_play_now_playing_uri_still_needs_allowlist(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    foreign = item("library://track/foreign", "track", "Fremd")
    fake.emit_queue(PLAYER, _snap(foreign, elapsed_ms=1000))  # started via MA UI
    await hass.async_block_till_done()
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[foreign.uri])
    assert res["error"]["code"] == "not_allowed"
    assert fake.play_calls == []
    # it can still be rated while it plays
    res = await _request(ws, 2, "rate", uri=foreign.uri, kind="track", stars=4)
    assert res["success"] is True, res
    await ws.close()


async def test_failed_album_play_keeps_no_album_context(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)

    async def down(*args, **kwargs):
        from custom_components.kidshub.device import MaUnavailable

        raise MaUnavailable("down")

    fake.play = down
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[ALBUM.uri])
    assert res["error"]["code"] == "ma_unavailable"
    fake.emit_queue(PLAYER, _snap(_as_track(ALBUM_TRACKS[0]), elapsed_ms=1000))
    assert (await _next(ws, "now_playing"))["kind"] == "track"
    await ws.close()


async def test_ma_adapter_play_with_start_index(hass: HomeAssistant) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, call

    from homeassistant.config_entries import ConfigEntryState
    from music_assistant_models.enums import QueueOption

    from custom_components.kidshub.ma import MaAdapter

    queues = SimpleNamespace(
        **{
            name: AsyncMock()
            for name in (
                "play_media", "play_index", "clear", "stop",
                "pause", "resume", "next", "previous",
            )
        }
    )
    mass = SimpleNamespace(
        connection=SimpleNamespace(connected=True), player_queues=queues
    )
    entry = MockConfigEntry(
        domain="music_assistant",
        data={"url": "http://192.168.1.10:8095"},
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(mass=mass, listen_task=Mock())
    adapter = MaAdapter(hass)

    await adapter.play("p1", ["a", "b"])
    queues.play_media.assert_awaited_once_with(
        "p1", ["a", "b"], option=QueueOption.REPLACE
    )
    queues.play_index.assert_not_awaited()

    queues.play_media.reset_mock()
    await adapter.play("p1", ["a", "b", "c"], start_index=2, seek_ms=30500)
    queues.clear.assert_awaited_once_with("p1")
    queues.play_media.assert_awaited_once_with(
        "p1", ["a", "b", "c"], option=QueueOption.ADD
    )
    queues.play_index.assert_awaited_once_with("p1", 2, seek_position=30)

    await adapter.command("p1", "clear")
    assert queues.clear.await_args_list[-1] == call("p1")


async def test_play_containers_before_start_are_expanded(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    _device(hass).store.set_progress(
        ALBUM.uri, position_ms=1000, duration_ms=600000, finished=False, track_index=1
    )
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "play", uris=[PL1.uri, ALBUM.uri, EP1.uri], start_index=1)
    assert res["success"] is True, res
    queue = [TRACK_A.uri, TRACK_B.uri, *(t.uri for t in ALBUM_TRACKS), EP1.uri]
    assert fake.play_calls == [(PLAYER, queue, 3, 1000)]
    await ws.close()
