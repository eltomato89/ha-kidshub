"""Persistent per-entry storage: settings, ratings and playback progress."""

from __future__ import annotations

import copy
import time
from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1
SAVE_DELAY = 5

DEFAULT_SETTINGS: dict[str, Any] = {
    "max_volume": 60,
    "alarm_max_volume": 40,
    "alarm": {
        "enabled": False,
        "time": "07:00",
        "weekdays": [0, 1, 2, 3, 4],
        "lead_minutes": 15,
        "with_light": False,
    },
    "sleep_timer": {"window_start": "19:00", "window_end": "06:00", "minutes": 30},
    "night": {"start": "19:30", "end": "06:30", "brightness": 5},
    "day_brightness": 70,
    "music_enabled": True,
    "settings_visible": False,
    "tiles": {"music": True, "audiobooks": True, "podcasts": True},
}


@dataclass(frozen=True)
class RatingRecord:
    """A star rating for a piece of content."""

    uri: str
    kind: str
    stars: int
    title: str
    subtitle: str
    image_url: str | None
    updated: float


@dataclass(frozen=True)
class ProgressRecord:
    """Playback progress for a piece of content."""

    uri: str
    position_ms: int
    duration_ms: int
    finished: bool
    track_index: int
    updated: float


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with ``patch`` recursively merged into ``base``."""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class KidStore:
    """Settings, ratings and progress of one KidsHub device entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"kidshub.{entry_id}"
        )
        self._settings: dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self._ratings: dict[str, RatingRecord] = {}
        self._progress: dict[str, ProgressRecord] = {}

    async def async_load(self) -> None:
        """Load stored data, merging saved settings over the defaults."""
        data = await self._store.async_load()
        if not data:
            return
        self._settings = _deep_merge(DEFAULT_SETTINGS, data.get("settings", {}))
        self._ratings = {
            uri: RatingRecord(**rec) for uri, rec in data.get("ratings", {}).items()
        }
        self._progress = {
            uri: ProgressRecord(**rec) for uri, rec in data.get("progress", {}).items()
        }

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "settings": self._settings,
            "ratings": {u: asdict(r) for u, r in self._ratings.items()},
            "progress": {u: asdict(p) for u, p in self._progress.items()},
        }

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    @property
    def settings(self) -> dict[str, Any]:
        """Return a deep copy of the current settings."""
        return copy.deepcopy(self._settings)

    async def async_update_settings(self, patch: dict[str, Any]) -> None:
        """Deep-merge ``patch`` into the settings and schedule a save."""
        self._settings = _deep_merge(self._settings, patch)
        self._schedule_save()

    def set_rating(
        self,
        uri: str,
        kind: str,
        stars: int,
        title: str,
        subtitle: str,
        image_url: str | None,
    ) -> None:
        """Store a 1..5 star rating."""
        if isinstance(stars, bool) or not isinstance(stars, int) or not 1 <= stars <= 5:
            raise ValueError("stars must be an integer between 1 and 5")
        self._ratings[uri] = RatingRecord(
            uri=uri,
            kind=kind,
            stars=stars,
            title=title,
            subtitle=subtitle,
            image_url=image_url,
            updated=time.time(),
        )
        self._schedule_save()

    def get_rating(self, uri: str) -> int | None:
        """Return the stars for ``uri`` or None."""
        rec = self._ratings.get(uri)
        return rec.stars if rec else None

    def ratings(self, kind: str | None = None) -> list[RatingRecord]:
        """Return ratings, most stars first, newer first on ties."""
        recs = [r for r in self._ratings.values() if kind is None or r.kind == kind]
        recs.sort(key=lambda r: (r.stars, r.updated), reverse=True)
        return recs

    def set_progress(
        self,
        uri: str,
        *,
        position_ms: int,
        duration_ms: int,
        finished: bool,
        track_index: int = 0,
    ) -> None:
        """Store playback progress."""
        self._progress[uri] = ProgressRecord(
            uri=uri,
            position_ms=position_ms,
            duration_ms=duration_ms,
            finished=finished,
            track_index=track_index,
            updated=time.time(),
        )
        self._schedule_save()

    def get_progress(self, uri: str) -> ProgressRecord | None:
        """Return stored progress for ``uri``."""
        return self._progress.get(uri)
