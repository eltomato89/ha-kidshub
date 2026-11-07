"""Tests for TTS pre-warming of item titles and room names (0.1.3)."""

import asyncio
import hashlib
import tempfile

import pytest
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kidshub.const import DOMAIN
from custom_components.kidshub.device import TtsFailed

from .fake_ma import FakeMa, item

LONG = "Ein sehr langer Titel " * 15  # > 200 characters
PL1 = item("library://playlist/1", "playlist", "Lieder")
PL2 = item("library://playlist/2", "playlist", "Gute Nacht")
TRACKS = [item(f"library://track/{i}", "track", f"Lied {i}") for i in range(6)]
TRACK_LONG = item("library://track/long", "track", LONG)
BIG = [item(f"library://track/big{i}", "track", f"Big {i}") for i in range(300)]


class Engine:
    """Fake TTS engine: records texts, can be gated, tracks concurrency."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.active = 0
        self.max_active = 0
        self.fail = False

    async def audio(self, hass, media_source_id):
        self.texts.append(media_source_id)
        if self.fail:
            raise ValueError("engine down")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self.gate.wait()
        finally:
            self.active -= 1
        return ("mp3", b"ID3" + media_source_id.encode())

    def patches(self):
        return (
            patch("homeassistant.components.tts.async_get_media_source_audio", self.audio),
            patch(
                "homeassistant.components.tts.generate_media_source_id",
                lambda hass, text, engine, language, options: text,
            ),
        )


async def _setup(hass: HomeAssistant, *, engine: bool = True, options=None):
    hass.config.config_dir = tempfile.mkdtemp(prefix="kidshub-test-")
    if engine:
        MockConfigEntry(
            domain=DOMAIN,
            unique_id="settings",
            data={"entry_type": "settings"},
            options={"tts_engine": "tts.fake", "tts_language": "de-DE"},
        ).add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    hass.data[DOMAIN].ma = FakeMa(
        library={"playlist": [PL1, PL2]},
        children={PL1.uri: [*TRACKS, TRACK_LONG], PL2.uri: BIG},
    )
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
        or {"playlists": [PL1.uri, PL2.uri], "audiobooks": [], "podcasts": []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN].devices["dev-a"]


async def _wait(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def _idle(hass: HomeAssistant) -> None:
    """Wait until the prewarm queue and all generations are done."""
    await _wait(lambda: not hass.data[DOMAIN].prewarm.busy)
    await hass.async_block_till_done()


async def _browse(device, **fields):
    return await device.handle_request({"type": "browse", "id": 1, **fields})


async def test_browse_prewarms_titles_without_delay(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        eng.gate.clear()  # generation hangs: the browse answer must not wait
        res = await _browse(device, section="music")
        assert [i["title"] for i in res["items"]] == ["Lieder", "Gute Nacht"]
        await _wait(lambda: len(eng.texts) == 2)
        eng.gate.set()
        await _idle(hass)
    assert sorted(eng.texts) == ["Gute Nacht", "Lieder"]


async def test_prewarm_key_matches_app_text(hass: HomeAssistant) -> None:
    """Long titles are pre-warmed as ``title[:200]``, which the app sends."""
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        await _browse(device, section="music", parent_uri=PL1.uri)
        await _idle(hass)
        assert LONG[:200] in eng.texts
        assert len(eng.texts) == 7
        res = await device.handle_request({"type": "tts", "id": 2, "text": LONG[:200]})
        await device.handle_request({"type": "tts", "id": 3, "text": "Lied 3"})
    assert res["url"].startswith("/api/kidshub/tts/")
    assert len(eng.texts) == 7  # both answered from the cache


async def test_favorites_prewarm(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        await device.handle_request(
            {"type": "rate", "id": 1, "uri": TRACKS[0].uri, "kind": "track", "stars": 5}
        )
        await _idle(hass)
        eng.texts.clear()
        await device.handle_request({"type": "favorites", "id": 2, "kind": "track"})
        await _idle(hass)
    assert eng.texts == ["Lied 0"]


async def test_prewarm_skips_cached_and_inflight(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        await device.handle_request({"type": "tts", "id": 1, "text": "Lieder"})
        assert eng.texts == ["Lieder"]
        eng.gate.clear()
        await _browse(device, section="music")
        await _browse(device, section="music")  # same titles while in flight
        await _wait(lambda: len(eng.texts) == 2)
        await asyncio.sleep(0.05)
        eng.gate.set()
        await _idle(hass)
        await _browse(device, section="music")  # everything cached now
        await _idle(hass)
    assert eng.texts == ["Lieder", "Gute Nacht"]


async def test_prewarm_concurrency_limit(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        eng.gate.clear()
        await _browse(device, section="music", parent_uri=PL1.uri)
        await _wait(lambda: len(eng.texts) == 2)
        await asyncio.sleep(0.05)
        assert len(eng.texts) == 2
        eng.gate.set()
        await _idle(hass)
    assert len(eng.texts) == 7
    assert eng.max_active == 2


async def test_no_engine_no_prewarm(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass, engine=False)
        res = await _browse(device, section="music", parent_uri=PL1.uri)
        await _idle(hass)
    assert len(res["items"]) == 7
    assert eng.texts == []


async def test_interactive_tts_joins_prewarm(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        eng.gate.clear()
        await _browse(device, section="music")
        await _wait(lambda: "Lieder" in eng.texts)
        tts = hass.async_create_task(
            device.handle_request({"type": "tts", "id": 2, "text": "Lieder"})
        )
        await asyncio.sleep(0.05)
        assert not tts.done()
        eng.gate.set()
        res = await tts
        await _idle(hass)
    assert res["url"].startswith("/api/kidshub/tts/")
    assert eng.texts.count("Lieder") == 1


async def test_queue_bounded_drops_newest_low(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.kidshub import tts_cache

    monkeypatch.setattr(tts_cache, "PREWARM_QUEUE_SIZE", 3)
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        await _setup(hass)
        eng.gate.clear()
        hass.data[DOMAIN].prewarm.schedule([f"T{i}" for i in range(10)])
        await _wait(lambda: len(eng.texts) == 2)
        eng.gate.set()
        await _idle(hass)
    assert sorted(eng.texts) == ["T0", "T1", "T2"]  # newest low-priority dropped


async def test_prewarm_never_raises(hass: HomeAssistant) -> None:
    async def boom(hass, media_source_id):
        raise ValueError("engine down")

    with (
        patch("homeassistant.components.tts.async_get_media_source_audio", boom),
        patch(
            "homeassistant.components.tts.generate_media_source_id",
            lambda hass, text, engine, language, options: text,
        ),
    ):
        _, device = await _setup(hass)
        res = await _browse(device, section="music")
        await _idle(hass)
    assert len(res["items"]) == 2
    hass.data[DOMAIN].prewarm.schedule([None, 5, "", "   "])  # ignored
    await _idle(hass)


async def test_unload_cancels_prewarm(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        entry, device = await _setup(hass)
        eng.gate.clear()
        await _browse(device, section="music", parent_uri=PL1.uri)
        await _wait(lambda: len(eng.texts) == 2)
        prewarm = hass.data[DOMAIN].prewarm
        assert prewarm.busy
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not prewarm.busy
        eng.gate.set()
        await _idle(hass)
    assert len(eng.texts) == 2  # queued titles were dropped


async def test_room_names_prewarmed_on_config(hass: HomeAssistant) -> None:
    hass.states.async_set("light.decke", "on", {"friendly_name": "Deckenlicht"})
    hass.states.async_set("switch.lampe", "off", {"friendly_name": "Lampe"})
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(
            hass,
            options={
                "room_entities": ["light.decke", "switch.lampe"],
                "room_names": {"switch.lampe": "Nachtlicht"},
            },
        )
        config = await device.async_config_message()
        await _idle(hass)
    assert [r["name"] for r in config["room"]] == ["Deckenlicht", "Nachtlicht"]
    assert sorted(eng.texts) == ["Deckenlicht", "Nachtlicht"]  # not the child name


# -- fix round 1: cap, priority, circuit breaker, negative cache -------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_browse_prewarms_at_most_30(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        res = await _browse(device, section="music", parent_uri=PL2.uri)
        await _idle(hass)
    assert len(res["items"]) == 300
    assert sorted(eng.texts) == sorted(f"Big {i}" for i in range(30))


async def test_root_titles_survive_flood(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        eng.gate.clear()
        prewarm = hass.data[DOMAIN].prewarm
        prewarm.schedule([f"F{i}" for i in range(600)])  # fills the queue (500)
        await _wait(lambda: len(eng.texts) == 2)
        await _browse(device, section="music")  # root: high priority, queue full
        eng.gate.set()
        await _idle(hass)
    assert set(eng.texts[2:4]) == {"Lieder", "Gute Nacht"}  # served first
    assert len(eng.texts) == 500 + 2 - 2 + 2  # 2 newest low items evicted
    assert "F499" in eng.texts and "F501" not in eng.texts


async def test_breaker_pauses_after_3_failures(hass: HomeAssistant, caplog) -> None:
    eng = Engine()
    eng.fail = True
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        prewarm = hass.data[DOMAIN].prewarm
        clock = prewarm._clock = Clock()
        caplog.clear()
        await _browse(device, section="music", parent_uri=PL1.uri)
        await _idle(hass)
        failed = len(eng.texts)
        assert 3 <= failed <= 4  # 2 workers: at most one more already started
        assert not prewarm.busy  # low-priority queue cleared
        await _browse(device, section="music")
        await _idle(hass)
        assert len(eng.texts) == failed  # paused
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1 and "paus" in warnings[0].getMessage()
        clock.now += 301
        eng.fail = False
        await _browse(device, section="music")
        await _idle(hass)
    assert eng.texts[failed:] in (["Lieder", "Gute Nacht"], ["Gute Nacht", "Lieder"])


async def test_interactive_tts_works_while_paused(hass: HomeAssistant) -> None:
    eng = Engine()
    eng.fail = True
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        await _browse(device, section="music", parent_uri=PL1.uri)
        await _idle(hass)
        failed = len(eng.texts)
        eng.fail = False
        res = await device.handle_request({"type": "tts", "id": 2, "text": "Lied 1"})
    assert res["url"].startswith("/api/kidshub/tts/")
    assert len(eng.texts) == failed + 1


async def test_negative_cache_ttl(hass: HomeAssistant) -> None:
    eng = Engine()
    eng.fail = True
    p1, p2 = eng.patches()
    with p1, p2:
        await _setup(hass)
        prewarm = hass.data[DOMAIN].prewarm
        clock = prewarm._clock = Clock()
        prewarm.schedule(["X"])
        await _idle(hass)
        assert eng.texts == ["X"]
        eng.fail = False
        clock.now += 599
        prewarm.schedule(["X"])
        await _idle(hass)
        assert eng.texts == ["X"]  # still in the negative cache
        clock.now += 2
        prewarm.schedule(["X"])
        await _idle(hass)
    assert eng.texts == ["X", "X"]


async def test_failed_generation_is_retried(hass: HomeAssistant) -> None:
    """A failed generation must not stay registered as in flight."""
    eng = Engine()
    eng.fail = True
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        try:
            await device.handle_request({"type": "tts", "id": 1, "text": "Hallo"})
        except Exception:  # noqa: BLE001
            pass
        await hass.async_block_till_done()
        eng.fail = False
        res = await device.handle_request({"type": "tts", "id": 2, "text": "Hallo"})
    assert res["url"].startswith("/api/kidshub/tts/")
    assert eng.texts == ["Hallo", "Hallo"]


# --- 0.1.4: prewarm option and quota detection ---------------------------------


QUOTA_DELAY = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric per day "
    "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '81307s'}"
)
QUOTA_NO_DELAY = "429 RESOURCE_EXHAUSTED. You exceeded your current quota"


def _settings_entry(hass: HomeAssistant):
    return hass.config_entries.async_entries(DOMAIN)[0]


def _quota_warnings(caplog):
    return [r for r in caplog.records if r.levelname == "WARNING"]


async def test_option_off_no_engine_calls(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        hass.config_entries.async_update_entry(
            _settings_entry(hass),
            options={
                "tts_engine": "tts.fake",
                "tts_language": "de-DE",
                "tts_prewarm": False,
            },
        )
        await hass.async_block_till_done()
        await _browse(device, section="music")
        await _browse(device, section="music", parent_uri=PL1.uri)
        await _idle(hass)
        assert eng.texts == []
        # interactive tts is unaffected
        res = await device.handle_request({"type": "tts", "id": 2, "text": "Hallo"})
        assert res["url"].startswith("/api/kidshub/tts/")
        assert eng.texts == ["Hallo"]
        # switching back on takes effect without reload
        hass.config_entries.async_update_entry(
            _settings_entry(hass),
            options={"tts_engine": "tts.fake", "tts_language": "de-DE"},
        )
        await _browse(device, section="music")
        await _idle(hass)
    assert "Lieder" in eng.texts


async def test_quota_with_retry_delay_pauses(hass: HomeAssistant, caplog) -> None:
    eng = Engine()
    eng.fail = True
    p1, p2 = eng.patches()

    async def boom(hass_, media_source_id):
        eng.texts.append(media_source_id)
        raise RuntimeError(QUOTA_DELAY)

    with p1, p2, patch(
        "homeassistant.components.tts.async_get_media_source_audio", boom
    ):
        _, device = await _setup(hass)
        prewarm = hass.data[DOMAIN].prewarm
        clock = prewarm._clock = Clock()
        caplog.clear()
        await _browse(device, section="music", parent_uri=PL1.uri)
        await _idle(hass)
        assert len(eng.texts) <= 2  # no 3-failure ramp: first quota error pauses
        assert not prewarm.busy
        assert prewarm._paused_until == clock.now + 81307
        warnings = _quota_warnings(caplog)
        assert len(warnings) == 1
        assert "Kontingent" in warnings[0].getMessage()
        before = len(eng.texts)
        clock.now += 81000
        await _browse(device, section="music")
        await _idle(hass)
        assert len(eng.texts) == before  # still paused
        clock.now += 400
        await _browse(device, section="music")
        await _idle(hass)
        assert len(eng.texts) > before  # resumed (engine fails again, fine)


async def test_quota_without_delay_pauses_until_midnight(
    hass: HomeAssistant, caplog
) -> None:
    from datetime import datetime, timedelta

    from homeassistant.util import dt as dt_util

    eng = Engine()
    p1, p2 = eng.patches()

    async def boom(hass_, media_source_id):
        eng.texts.append(media_source_id)
        raise RuntimeError(QUOTA_NO_DELAY)

    now = dt_util.now().replace(hour=22, minute=30, second=0, microsecond=0)
    with p1, p2, patch(
        "homeassistant.components.tts.async_get_media_source_audio", boom
    ), patch("custom_components.kidshub.tts_cache.dt_util.now", lambda: now):
        _, device = await _setup(hass)
        prewarm = hass.data[DOMAIN].prewarm
        clock = prewarm._clock = Clock()
        await _browse(device, section="music", parent_uri=PL1.uri)
        await _idle(hass)
        assert prewarm._paused_until == clock.now + 90 * 60  # 22:30 -> 00:00
        diag = prewarm.status()
        assert diag["paused_until"].endswith("00:00:00" ) or "T00:00" in diag["paused_until"]
        assert diag["reason"] == "quota"


async def test_quota_delay_capped_to_24h(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()

    async def boom(hass_, media_source_id):
        raise RuntimeError("429 quota exceeded, retry in 999999 seconds")

    with p1, p2, patch(
        "homeassistant.components.tts.async_get_media_source_audio", boom
    ):
        _, device = await _setup(hass)
        prewarm = hass.data[DOMAIN].prewarm
        clock = prewarm._clock = Clock()
        await _browse(device, section="music", parent_uri=PL1.uri)
        await _idle(hass)
        assert prewarm._paused_until == clock.now + 86400


async def test_interactive_tts_works_during_quota_pause(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    calls = []

    async def boom(hass_, media_source_id):
        calls.append(media_source_id)
        raise RuntimeError(QUOTA_DELAY)

    with p1, p2:
        _, device = await _setup(hass)
        with patch("homeassistant.components.tts.async_get_media_source_audio", boom):
            await _browse(device, section="music")
            await _idle(hass)
        assert hass.data[DOMAIN].prewarm.status()["paused_until"] is not None
        res = await device.handle_request({"type": "tts", "id": 2, "text": "Jetzt"})
    assert res["url"].startswith("/api/kidshub/tts/")
    assert "Jetzt" in eng.texts


async def test_prewarm_status_fields(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        await _setup(hass)
        status = hass.data[DOMAIN].prewarm.status()
    assert status == {
        "enabled": True,
        "paused_until": None,
        "reason": None,
        "queue": 0,
    }


# --- follow-ups: interactive quota error, real error text, tight matching ------

LIVE_ERROR = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
    "current quota, please check your plan and billing details. ... Quota exceeded "
    "for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
    "limit: 10, model: gemini-3.8-flash-tts, description: Generate content requests "
    "per day limit exceeded on Free Tier. ...', 'status': 'RESOURCE_EXHAUSTED', "
    "'details': [{'@type': 'type.googleapis.com/google.rpc.Help'}, "
    "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '81307s'}]}}"
)


async def test_interactive_quota_error_pauses_prewarm(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()

    async def boom(hass_, media_source_id):
        raise RuntimeError(LIVE_ERROR)

    with p1, p2:
        _, device = await _setup(hass)
        prewarm = hass.data[DOMAIN].prewarm
        clock = prewarm._clock = Clock()
        with patch("homeassistant.components.tts.async_get_media_source_audio", boom):
            with pytest.raises(TtsFailed):
                await device.handle_request({"type": "tts", "id": 2, "text": "Hi"})
        assert prewarm._paused_until == clock.now + 81307
        await _browse(device, section="music")
        await _idle(hass)
    assert eng.texts == []  # prewarm paused


@pytest.mark.parametrize(
    "message",
    [
        "engine down at http://host:14290/tts",
        "error code 14290",
        "HTTP 429 from proxy",  # no quota/rate context
        "timeout after 4290 ms",
    ],
)
async def test_bare_429_does_not_pause(hass: HomeAssistant, message) -> None:
    eng = Engine()
    p1, p2 = eng.patches()

    async def boom(hass_, media_source_id):
        raise RuntimeError(message)

    with p1, p2, patch(
        "homeassistant.components.tts.async_get_media_source_audio", boom
    ):
        _, device = await _setup(hass)
        prewarm = hass.data[DOMAIN].prewarm
        prewarm._clock = Clock()
        await _browse(device, section="music")
        await _idle(hass)
        assert prewarm.status()["reason"] != "quota"
