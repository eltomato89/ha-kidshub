"""Tests for TTS pre-warming of item titles and room names (0.1.3)."""

import asyncio
import hashlib
import tempfile
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kidshub.const import DOMAIN

from .fake_ma import FakeMa, item

LONG = "Ein sehr langer Titel " * 15  # > 200 characters
PL1 = item("library://playlist/1", "playlist", "Lieder")
PL2 = item("library://playlist/2", "playlist", "Gute Nacht")
TRACKS = [item(f"library://track/{i}", "track", f"Lied {i}") for i in range(6)]
TRACK_LONG = item("library://track/long", "track", LONG)


class Engine:
    """Fake TTS engine: records texts, can be gated, tracks concurrency."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.active = 0
        self.max_active = 0

    async def audio(self, hass, media_source_id):
        self.texts.append(media_source_id)
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
        children={PL1.uri: [*TRACKS, TRACK_LONG], PL2.uri: TRACKS[:2]},
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
        await hass.async_block_till_done()
    assert sorted(eng.texts) == ["Gute Nacht", "Lieder"]


async def test_prewarm_key_matches_app_text(hass: HomeAssistant) -> None:
    """Long titles are pre-warmed as ``title[:200]``, which the app sends."""
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass)
        await _browse(device, section="music", parent_uri=PL1.uri)
        await hass.async_block_till_done()
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
        await hass.async_block_till_done()
        eng.texts.clear()
        await device.handle_request({"type": "favorites", "id": 2, "kind": "track"})
        await hass.async_block_till_done()
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
        await hass.async_block_till_done()
        await _browse(device, section="music")  # everything cached now
        await hass.async_block_till_done()
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
        await hass.async_block_till_done()
    assert len(eng.texts) == 7
    assert eng.max_active == 2


async def test_no_engine_no_prewarm(hass: HomeAssistant) -> None:
    eng = Engine()
    p1, p2 = eng.patches()
    with p1, p2:
        _, device = await _setup(hass, engine=False)
        res = await _browse(device, section="music", parent_uri=PL1.uri)
        await hass.async_block_till_done()
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
        await hass.async_block_till_done()
    assert res["url"].startswith("/api/kidshub/tts/")
    assert eng.texts.count("Lieder") == 1


async def test_queue_bounded_drops_oldest(hass: HomeAssistant, monkeypatch) -> None:
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
        await hass.async_block_till_done()
    assert sorted(eng.texts) == ["T7", "T8", "T9"]  # only the newest 3 were kept


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
        await hass.async_block_till_done()
    assert len(res["items"]) == 2
    hass.data[DOMAIN].prewarm.schedule([None, 5, "", "   "])  # ignored
    await hass.async_block_till_done()


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
        await hass.async_block_till_done()
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
        await hass.async_block_till_done()
    assert [r["name"] for r in config["room"]] == ["Deckenlicht", "Nachtlicht"]
    assert sorted(eng.texts) == ["Deckenlicht", "Nachtlicht"]  # not the child name
