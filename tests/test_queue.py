"""Tests for the waiting queue (``queue_add``/``queue_remove``, protocol 3.5)."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.kidshub.ma import _queue_ended

from .fake_ma import item
from .test_playback import (
    ALBUM,
    BOOK,
    EP1,
    EP2,
    OPTIONS,
    PLAYER,
    TRACK_A,
    TRACK_B,
    _connect,
    _device,
    _fake,
    _next,
    _request,
    _setup,
    _snap,
)

RADIO = item("library://radio/1", "radio", "Radio")
Q_OPTIONS = {**OPTIONS, "radios": [RADIO.uri]}


async def _playing(hass, fake, current=TRACK_A):
    """Let the device play ``current`` (started by the child)."""
    await _device(hass).handle_request({"type": "play", "id": 0, "uris": [current.uri]})
    fake.emit_queue(PLAYER, _snap(current, elapsed_ms=1000))
    await hass.async_block_till_done()


async def _req(ws, id_, type_, **fields):
    """Send a request; return its result and the ``queue`` pushes before it."""
    await ws.send_json({"type": type_, "id": id_, **fields})
    pushes = []
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), 2)
        if msg["type"] == "queue":
            pushes.append(msg)
        elif msg["type"] == "result" and msg["id"] == id_:
            return msg, pushes


def _uris(msg):
    return [i["uri"] for i in msg["items"]]


async def test_queue_pushed_after_now_playing(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    ws = await (await hass_client_no_auth()).ws_connect("/api/kidshub/ws")
    await ws.send_json(
        {"type": "auth", "device_id": "dev-a", "token": "tok-a", "app_version": "1"}
    )
    types = [(await ws.receive_json())["type"] for _ in range(4)]
    assert types == ["auth_ok", "config", "now_playing", "queue"]
    await ws.close()


async def test_queue_add_remove_full(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    await device.update_settings({"queue": {"max_length": 2}})
    ws = await _connect(await hass_client_no_auth())
    await _playing(hass, fake)
    res, (msg,) = await _req(ws, 1, "queue_add", uri=EP1.uri)
    assert res["success"] is True and res["result"] == {}, res
    assert _uris(msg) == [EP1.uri] and msg["max"] == 2
    assert msg["items"][0]["kind"] == "episode"
    assert msg["items"][0]["title"] == "Folge 1"
    assert "progress" in msg["items"][0]
    res, (msg,) = await _req(ws, 2, "queue_add", uri=ALBUM.uri)
    assert _uris(msg) == [EP1.uri, ALBUM.uri]
    res = await _request(ws, 3, "queue_add", uri=BOOK.uri)
    assert res["error"]["code"] == "invalid"
    assert res["error"]["message"] == "queue_full"
    qid = msg["items"][0]["qid"]
    assert isinstance(qid, str) and qid != msg["items"][1]["qid"]
    for id_, bad in ((4, None), (5, 0), (6, "")):
        res = await _request(ws, id_, "queue_remove", qid=bad)
        assert res["error"]["code"] == "invalid", bad
    res, (msg,) = await _req(ws, 8, "queue_remove", qid=qid)
    assert res["success"] and _uris(msg) == [ALBUM.uri]
    # idempotent: an unknown qid is no error and pushes the list again
    res, (msg,) = await _req(ws, 9, "queue_remove", qid=qid)
    assert res["result"] == {} and _uris(msg) == [ALBUM.uri]
    assert [e["uri"] for e in device.store.queue] == [ALBUM.uri]
    assert len(fake.play_calls) == 1  # only the initial play
    await ws.close()


async def test_queue_add_rules(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    fake._library["radio"] = [RADIO]
    await _setup(hass, fake, Q_OPTIONS)
    device = _device(hass)
    ws = await _connect(await hass_client_no_auth())
    await _playing(hass, fake, EP2)
    for id_, uri, code in (
        (1, "library://track/unknown", "not_allowed"),
        (2, RADIO.uri, "invalid"),
        (3, "kidshub://favorites/track", "invalid"),
        (4, "library://podcast/1", "invalid"),
        (5, None, "invalid"),
    ):
        res = await _request(ws, id_, "queue_add", uri=uri)
        assert res["error"]["code"] == code, (uri, res)
    await device.update_settings({"music_enabled": False})
    res = await _request(ws, 6, "queue_add", uri=TRACK_A.uri)
    assert res["error"]["code"] == "not_allowed"
    assert device.store.queue == []
    await ws.close()


async def test_queue_add_when_idle_starts(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    device.store.set_progress(EP1.uri, position_ms=5000, duration_ms=1200000, finished=False)
    ws = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "queue_add", uri=EP1.uri)
    assert res["result"] == {"started": True}, res
    assert fake.play_calls == [(PLAYER, [EP1.uri], 0, 5000)]
    assert device.store.queue == []
    # an ended queue counts as idle too
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=1000))
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=1200000, playing=False, ended=True))
    await hass.async_block_till_done()
    res = await _request(ws, 2, "queue_add", uri=EP2.uri)
    assert res["result"] == {"started": True}, res
    await ws.close()


async def test_queue_advances_on_natural_end(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    device.store.set_progress(EP1.uri, position_ms=7000, duration_ms=1200000, finished=False)
    ws = await _connect(await hass_client_no_auth())
    await _playing(hass, fake)
    assert (await _request(ws, 1, "queue_add", uri=EP1.uri))["success"]
    assert (await _request(ws, 2, "queue_add", uri=EP2.uri))["success"]
    # play keeps the queue
    assert (await _request(ws, 3, "play", uris=[TRACK_B.uri]))["success"]
    assert len(device.store.queue) == 2
    fake.emit_queue(PLAYER, _snap(TRACK_B, elapsed_ms=1000))
    fake.emit_queue(PLAYER, _snap(TRACK_B, elapsed_ms=180000, playing=False, ended=True))
    msg = await _next(ws, "queue")
    assert _uris(msg) == [EP2.uri]
    assert fake.play_calls[-1] == (PLAYER, [EP1.uri], 0, 7000)  # resumed
    # a stale ended snapshot of the old queue does not advance again
    fake.emit_queue(PLAYER, _snap(TRACK_B, elapsed_ms=180000, playing=False, ended=True))
    await hass.async_block_till_done()
    assert len(fake.play_calls) == 3
    # the queued episode plays and ends: the next entry follows
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=1000))
    fake.emit_queue(PLAYER, _snap(EP1, elapsed_ms=1200000, playing=False, ended=True))
    assert _uris(await _next(ws, "queue")) == []
    assert fake.play_calls[-1][1] == [EP2.uri]
    await ws.close()


async def test_no_advance_after_wake_play_or_pause(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake, {**OPTIONS, "wake_media_uri": OPTIONS["playlists"][0]})
    device = _device(hass)
    ws = await _connect(await hass_client_no_auth())
    await _playing(hass, fake)
    assert (await _request(ws, 1, "queue_add", uri=EP1.uri))["success"]
    # paused (also the sleep timer): no advance
    assert (await _request(ws, 2, "control", cmd="pause"))["success"]
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=180000, playing=False, ended=True))
    await hass.async_block_till_done()
    assert len(fake.play_calls) == 1
    # wake-up music that ends: no advance either
    assert (await _request(ws, 3, "wake_play"))["success"]
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=1000))
    fake.emit_queue(PLAYER, _snap(TRACK_B, elapsed_ms=180000, playing=False, ended=True))
    await hass.async_block_till_done()
    assert len(fake.play_calls) == 2
    assert [e["uri"] for e in device.store.queue] == [EP1.uri]
    await ws.close()


async def test_no_advance_after_music_lock_stop(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    ws = await _connect(await hass_client_no_auth())
    await _playing(hass, fake)
    assert (await _request(ws, 1, "queue_add", uri=EP1.uri))["success"]
    await device.update_settings({"music_enabled": False})
    await hass.async_block_till_done()
    assert (PLAYER, "stop") in fake.command_calls
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=180000, playing=False, ended=True))
    await hass.async_block_till_done()
    assert len(fake.play_calls) == 1
    await ws.close()


async def test_advance_skips_entries_no_longer_allowed(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    fake = _fake()
    entry = await _setup(hass, fake)
    device = _device(hass)
    ws = await _connect(await hass_client_no_auth())
    await _playing(hass, fake, EP2)
    for id_, uri in ((1, TRACK_A.uri), (2, ALBUM.uri), (3, EP1.uri)):
        assert (await _request(ws, id_, "queue_add", uri=uri))["success"]
    # music locked (the episode keeps playing) and the album removed
    await device.update_settings({"music_enabled": False})
    hass.config_entries.async_update_entry(
        entry, options={**OPTIONS, "audiobooks": [BOOK.uri]}
    )
    await hass.async_block_till_done()
    fake.emit_queue(PLAYER, _snap(EP2, elapsed_ms=1200000, playing=False, ended=True))
    msg = await _next(ws, "queue")
    while _uris(msg):
        msg = await _next(ws, "queue")
    assert fake.play_calls[-1][1] == [EP1.uri]
    assert device.store.queue == []
    await ws.close()


def _local(hour: int, minute: int, days: int = 0) -> datetime:
    now = dt_util.now()
    return (now + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


async def test_queue_cleared_at_night_end(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    freezer.move_to(_local(20, 0))
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    ws = await _connect(await hass_client_no_auth())
    await _playing(hass, fake)
    assert (await _request(ws, 1, "queue_add", uri=EP1.uri))["success"]
    morning = _local(6, 30, days=1)
    freezer.move_to(morning)
    async_fire_time_changed(hass, morning)
    msg = await _next(ws, "queue")
    assert msg["items"] == []
    assert device.store.queue_cleared_on == morning.date().isoformat()
    await ws.close()


async def test_queue_clear_follows_night_end(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_local(20, 0))
    await _setup(hass, _fake())
    device = _device(hass)
    await device.update_settings({"night": {"end": "07:15"}})
    device.store.set_queue([{"uri": EP1.uri, "kind": "episode"}])
    at = _local(6, 30, days=1)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done()
    assert len(device.store.queue) == 1
    at = _local(7, 15, days=1)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done()
    assert device.store.queue == []


async def test_queue_clear_unsubscribed_on_unload(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_local(20, 0))
    entry = await _setup(hass, _fake())
    store = _device(hass).store
    store.set_queue([{"uri": EP1.uri, "kind": "episode"}])
    assert await hass.config_entries.async_unload(entry.entry_id)
    at = _local(6, 30, days=1)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done()
    assert len(store.queue) == 1


def _stored(queue, cleared_on):
    return {
        "version": 1,
        "minor_version": 1,
        "key": "kidshub.x",
        "data": {
            "settings": {},
            "ratings": {},
            "progress": {},
            "queue": queue,
            "queue_cleared_on": cleared_on,
        },
    }


async def test_queue_catch_up_clear_at_startup(
    hass: HomeAssistant, hass_storage, freezer
) -> None:
    freezer.move_to(_local(9, 0))
    yesterday = (dt_util.now() - timedelta(days=1)).date().isoformat()
    fake = _fake()
    # the entry id is only known in _setup: seed the storage when the store loads
    from custom_components.kidshub import store as store_module

    real_load = store_module.KidStore.async_load

    async def load(self):
        hass_storage[self._store.key] = _stored(
            [{"uri": EP1.uri, "kind": "episode"}], yesterday
        )
        await real_load(self)

    store_module.KidStore.async_load = load
    try:
        await _setup(hass, fake)
    finally:
        store_module.KidStore.async_load = real_load
    store = _device(hass).store
    assert store.queue == []
    assert store.queue_cleared_on == dt_util.now().date().isoformat()


async def test_queue_kept_before_night_end_at_startup(
    hass: HomeAssistant, hass_storage, freezer
) -> None:
    freezer.move_to(_local(5, 0))
    yesterday = (dt_util.now() - timedelta(days=1)).date().isoformat()
    from custom_components.kidshub import store as store_module

    real_load = store_module.KidStore.async_load

    async def load(self):
        hass_storage[self._store.key] = _stored(
            [{"uri": EP1.uri, "kind": "episode"}], yesterday
        )
        await real_load(self)

    store_module.KidStore.async_load = load
    try:
        await _setup(hass, _fake())
    finally:
        store_module.KidStore.async_load = real_load
    store = _device(hass).store
    assert [e["uri"] for e in store.queue] == [EP1.uri]
    assert store.queue[0]["qid"]  # old entries get a qid
    assert store.queue_cleared_on == yesterday


def test_queue_ended_detection() -> None:
    from music_assistant_models.enums import PlaybackState

    def queue(**kw):
        base = dict(
            state=PlaybackState.IDLE,
            current_item=SimpleNamespace(duration=180),
            current_index=2,
            items=3,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    assert _queue_ended(queue(ended=True), 0) is True
    assert _queue_ended(queue(ended=False), 180) is False
    # older models without ``ended``: idle on the last item at its end
    assert _queue_ended(queue(), 178) is True
    assert _queue_ended(queue(), 60) is False
    assert _queue_ended(queue(current_index=1), 180) is False
    assert _queue_ended(queue(state=PlaybackState.PAUSED), 180) is False
    assert _queue_ended(queue(current_item=None), 180) is False


# -- starting the queue when nothing will end by itself ----------------------


async def _add(hass, uri):
    return await _device(hass).handle_request({"type": "queue_add", "id": 1, "uri": uri})


async def test_queue_add_starts_after_wake_music(hass: HomeAssistant) -> None:
    fake = _fake()
    await _setup(hass, fake, {**OPTIONS, "wake_media_uri": OPTIONS["playlists"][0]})
    await _device(hass).handle_request({"type": "wake_play", "id": 0})
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=1000))
    assert await _add(hass, EP1.uri) == {"started": True}
    assert fake.play_calls[-1][1] == [EP1.uri]


async def test_queue_add_starts_for_outside_or_radio(hass: HomeAssistant) -> None:
    fake = _fake()
    fake._library["radio"] = [RADIO]
    await _setup(hass, fake, Q_OPTIONS)
    # started outside KidsHub (or before an HA restart): no auto-advance
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=1000))
    assert await _add(hass, EP1.uri) == {"started": True}
    # radio never ends
    await _playing(hass, fake, RADIO)
    assert await _add(hass, EP2.uri) == {"started": True}
    assert [c[1] for c in fake.play_calls] == [[EP1.uri], [RADIO.uri], [EP2.uri]]


async def test_queue_add_starts_when_stopped_from_outside(hass: HomeAssistant) -> None:
    fake = _fake()
    await _setup(hass, fake)
    await _playing(hass, fake)
    # stopped in the MA UI: idle on the item, not ended
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=5000, playing=False))
    assert await _add(hass, EP1.uri) == {"started": True}


async def test_queue_add_waits_while_paused(hass: HomeAssistant) -> None:
    fake = _fake()
    await _setup(hass, fake)
    await _playing(hass, fake)
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=5000, playing=False, paused=True))
    assert await _add(hass, EP1.uri) == {}
    assert len(fake.play_calls) == 1


async def test_queue_add_plays_waiting_entry_first(hass: HomeAssistant) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    device.store.set_queue([{"uri": EP1.uri, "kind": "episode", "title": "Folge 1"}])
    assert await _add(hass, EP2.uri) == {"started": True}
    assert fake.play_calls[-1][1] == [EP1.uri]
    assert [e["uri"] for e in device.store.queue] == [EP2.uri]


async def test_concurrent_queue_add_starts_once(hass: HomeAssistant) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    results = await asyncio.gather(
        device.handle_request({"type": "queue_add", "id": 1, "uri": EP1.uri}),
        device.handle_request({"type": "queue_add", "id": 2, "uri": EP2.uri}),
    )
    assert sorted(map(str, results)) == ["{'started': True}", "{}"]
    assert len(fake.play_calls) == 1
    assert len(device.store.queue) == 1


async def test_queue_add_during_play_is_appended(hass: HomeAssistant) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    gate = asyncio.Event()
    real_play = fake.play

    async def slow_play(*args, **kwargs):
        await gate.wait()
        await real_play(*args, **kwargs)

    fake.play = slow_play
    play = hass.async_create_task(
        device.handle_request({"type": "play", "id": 1, "uris": [TRACK_A.uri]})
    )
    await asyncio.sleep(0)
    add = hass.async_create_task(_add(hass, EP1.uri))
    await asyncio.sleep(0.05)
    gate.set()
    assert await play == {}
    assert await add == {}
    assert [c[1] for c in fake.play_calls] == [[TRACK_A.uri]]
    assert [e["uri"] for e in device.store.queue] == [EP1.uri]


async def test_external_resume_clears_pause(hass: HomeAssistant) -> None:
    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    await _playing(hass, fake)
    assert await _add(hass, EP1.uri) == {}
    await device.handle_request({"type": "control", "id": 2, "cmd": "pause"})
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=5000, playing=False, paused=True))
    # resumed in the MA UI, then it ends by itself
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=6000))
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=180000, playing=False, ended=True))
    await hass.async_block_till_done()
    assert fake.play_calls[-1][1] == [EP1.uri]


async def test_unreported_start_counts_as_ended(hass: HomeAssistant) -> None:
    """MA never reports a queued entry that failed: it is dropped after a timeout."""
    from custom_components.kidshub.playback import FRESH_TIMEOUT

    fake = _fake()
    await _setup(hass, fake)
    device = _device(hass)
    await _playing(hass, fake)
    assert await _add(hass, EP1.uri) == {}
    assert await _add(hass, EP2.uri) == {}
    fake.emit_queue(PLAYER, _snap(TRACK_A, elapsed_ms=180000, playing=False, ended=True))
    await hass.async_block_till_done()
    assert fake.play_calls[-1][1] == [EP1.uri]
    # MA reports nothing for EP1 (it failed right away)
    for step in (1, 2, 3):
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=step * (FRESH_TIMEOUT + 1))
        )
        await hass.async_block_till_done()
    assert [c[1] for c in fake.play_calls[-2:]] == [[EP1.uri], [EP2.uri]]
    assert device.store.queue == []
    assert len(fake.play_calls) == 3  # each entry tried once, no loop
