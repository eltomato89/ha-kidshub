"""Tests for KidStore."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.kidshub.store import KidStore


async def _store(hass, entry_id="e1"):
    store = KidStore(hass, entry_id)
    await store.async_load()
    return store


async def test_defaults(hass):
    s = await _store(hass)
    st = s.settings
    assert st["max_volume"] == 60
    assert st["alarm"]["weekdays"] == [0, 1, 2, 3, 4]
    assert st["night"]["brightness"] == 5
    assert st["tiles"] == {"music": True, "audiobooks": True, "podcasts": True}
    assert "sendspin" not in st and "room" not in st
    assert "button_required" not in st["alarm"]
    st["alarm"]["weekdays"].append(5)
    assert s.settings["alarm"]["weekdays"] == [0, 1, 2, 3, 4]


async def test_deep_merge(hass):
    s = await _store(hass)
    await s.async_update_settings({"alarm": {"time": "06:45"}})
    assert s.settings["alarm"]["time"] == "06:45"
    assert s.settings["alarm"]["lead_minutes"] == 15


async def test_rating_bounds(hass):
    s = await _store(hass)
    for stars in (0, 6):
        with pytest.raises(ValueError):
            s.set_rating("u", "track", stars, "t", "s", None)


async def test_ratings_sorted(hass):
    s = await _store(hass)
    s.set_rating("a", "track", 5, "A", "", None)
    s.set_rating("b", "track", 3, "B", "", None)
    s.set_rating("c", "track", 4, "C", "", None)
    s.set_rating("d", "episode", 5, "D", "", None)
    assert [r.uri for r in s.ratings("track")] == ["a", "c", "b"]
    assert s.get_rating("c") == 4
    assert s.get_rating("zzz") is None
    assert len(s.ratings()) == 4


async def test_ratings_tie_newer_first(hass, monkeypatch):
    s = await _store(hass)
    times = iter([100.0, 200.0])
    monkeypatch.setattr(
        "custom_components.kidshub.store.time", SimpleNamespace(time=lambda: next(times))
    )
    s.set_rating("old", "track", 4, "", "", None)
    s.set_rating("new", "track", 4, "", "", None)
    assert [r.uri for r in s.ratings("track")] == ["new", "old"]


async def test_progress(hass):
    s = await _store(hass)
    assert s.get_progress("x") is None
    s.set_progress("x", position_ms=5, duration_ms=10, finished=False, track_index=2)
    p = s.get_progress("x")
    assert (p.position_ms, p.duration_ms, p.finished, p.track_index) == (5, 10, False, 2)


async def test_persist_roundtrip(hass, hass_storage):
    s = await _store(hass)
    await s.async_update_settings({"max_volume": 30})
    s.set_rating("a", "track", 5, "A", "sub", "img")
    s.set_progress("b", position_ms=1, duration_ms=2, finished=True)
    # Store debounces on loop time; advance it along with the wall clock.
    real_time = hass.loop.time
    with patch.object(hass.loop, "time", side_effect=lambda: real_time() + 10):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
        await hass.async_block_till_done()
    assert "kidshub.e1" in hass_storage
    s2 = await _store(hass)
    assert s2.settings["max_volume"] == 30
    assert s2.get_rating("a") == 5
    assert s2.ratings("track")[0].image_url == "img"
    assert s2.get_progress("b").finished is True
