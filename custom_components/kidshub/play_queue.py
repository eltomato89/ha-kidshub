"""Waiting queue of one device: ``queue_add``, ``queue_remove`` and auto-advance.

The list lives in the ``KidStore`` (protocol 3.5), not in the MA queue: every
``play``/``wake_play`` replaces the MA queue, and resume positions need the KidsHub
play path. When the current playback ends by itself, the first entry is played
like a ``play`` with ``resume`` and removed. The list is cleared every morning at
``night.end``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, time as dt_time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .content import FAVORITES_PREFIX
from .device import InvalidRequest, KidDevice, MaUnavailable, NotAllowed, NotFound
from .ma import MaItem
from .playback import MUSIC_KINDS

if TYPE_CHECKING:
    from .content import ContentService
    from .playback import PlaybackService

_LOGGER = logging.getLogger(__name__)

QUEUE_KINDS = {"track", "playlist", "album", "audiobook", "episode", "album_track"}
DEFAULT_CLEAR_TIME = dt_time(6, 30)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_time(value: Any) -> dt_time:
    """Parse ``HH:MM`` (falls back to 06:30)."""
    try:
        hour, minute = (int(p) for p in str(value).split(":"))
        return dt_time(hour, minute)
    except ValueError:
        return DEFAULT_CLEAR_TIME


def _entry(it: MaItem) -> dict[str, Any]:
    """Return the stored form of a queue entry."""
    return {
        "uri": it.uri,
        "kind": it.kind,
        "title": it.title,
        "subtitle": it.subtitle,
        "image": it.image,
        "duration_ms": it.duration_ms,
        "added": time.time(),
        "qid": uuid.uuid4().hex,
    }


def _item(entry: dict[str, Any]) -> MaItem:
    """Return the ``MaItem`` of a stored queue entry."""
    duration = entry.get("duration_ms")
    return MaItem(
        uri=entry["uri"],
        kind=str(entry.get("kind") or "track"),
        title=str(entry.get("title") or ""),
        subtitle=str(entry.get("subtitle") or ""),
        image=entry.get("image") if isinstance(entry.get("image"), str) else None,
        duration_ms=duration if _is_int(duration) else 0,
        published=None,
    )


class QueueService:
    """The waiting list of one child, played one entry after another."""

    def __init__(self, device: KidDevice, playback: PlaybackService) -> None:
        self.device = device
        self.hass = device.hass
        self.playback = playback
        self._lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._unsub_clear: Callable[[], None] | None = None
        self._clear_time: dt_time | None = None
        device.register_handler("queue_add", self._handle_add)
        device.register_handler("queue_remove", self._handle_remove)
        # registered right after playback: ``queue`` follows ``now_playing``
        device.register_connected_callback(self.push)
        playback.on_natural_end = self._on_natural_end

    @property
    def content(self) -> ContentService:
        """Return the content service (allowlist) of the device."""
        assert self.device.content is not None
        return self.device.content

    @property
    def max_length(self) -> int:
        """Return the configured maximum queue length."""
        return int(self.device.store.settings["queue"]["max_length"])

    # -- lifecycle ----------------------------------------------------------

    def start(self, entry: ConfigEntry) -> None:
        """Arm the morning clear (and catch up on a missed one); ``stop`` on unload."""
        self._arm_clear()
        self._unsubs.append(
            async_dispatcher_connect(self.hass, self.device.signal, self._arm_clear)
        )
        self._catch_up()

    def stop(self) -> None:
        """Drop the timer and the settings subscription."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._unsub_clear is not None:
            self._unsub_clear()
            self._unsub_clear = None
        self._clear_time = None

    @callback
    def _arm_clear(self) -> None:
        """(Re-)arm the daily clear at ``night.end`` if that time changed."""
        at = _parse_time(self.device.store.settings["night"]["end"])
        if at == self._clear_time and self._unsub_clear is not None:
            return
        if self._unsub_clear is not None:
            self._unsub_clear()
        self._clear_time = at
        self._unsub_clear = async_track_time_change(
            self.hass, self._on_clear_time, hour=at.hour, minute=at.minute, second=0
        )

    @callback
    def _on_clear_time(self, now: datetime) -> None:
        self._clear(dt_util.as_local(now).date().isoformat())

    def _catch_up(self) -> None:
        """Clear now if this morning's clear was missed (HA was not running)."""
        now = dt_util.now()
        today = now.date().isoformat()
        cleared = self.device.store.queue_cleared_on
        at = self._clear_time or DEFAULT_CLEAR_TIME
        if (cleared is None or cleared < today) and now.time() >= at:
            self._clear(today)

    def _clear(self, today: str) -> None:
        store = self.device.store
        store.set_queue_cleared_on(today)
        if store.queue:
            store.set_queue([])
            self._schedule_push()

    # -- messages -----------------------------------------------------------

    def message(self) -> dict[str, Any]:
        """Return the protocol ``queue`` message."""
        return {
            "type": "queue",
            "items": [
                {**self.content.to_item(_item(e)), "qid": e["qid"]}
                for e in self.device.store.queue
            ],
            "max": self.max_length,
        }

    async def push(self) -> None:
        """Send the ``queue`` message (no-op while offline)."""
        await self.device.push(self.message())

    def _schedule_push(self) -> None:
        self.hass.async_create_task(
            self.push(), f"kidshub queue {self.device.device_id}"
        )

    # -- handlers -----------------------------------------------------------

    async def _handle_add(self, msg: dict[str, Any]) -> dict[str, Any]:
        uri = msg.get("uri")
        if not isinstance(uri, str) or not uri or uri.startswith(FAVORITES_PREFIX):
            raise InvalidRequest("uri must be the uri of an item")
        found = await self.content.find_allowed_item(uri)
        if found is None:
            if uri in self.content.allowed():
                raise NotFound("content no longer exists in Music Assistant")
            raise NotAllowed("content not allowed")
        if found.kind not in QUEUE_KINDS:
            raise InvalidRequest(f"{found.kind} cannot be queued")
        if found.kind in MUSIC_KINDS and not self.device.store.settings["music_enabled"]:
            raise NotAllowed("music is disabled")
        # check and start under the lock: requests run concurrently
        async with self._lock:
            start = self.playback.should_start_queue()
            queue = self.device.store.queue
            if start and not queue:
                await self.playback.play_queued(uri)
                return {"started": True}
            if len(queue) >= self.max_length:
                raise InvalidRequest("queue_full")
            queue.append(_entry(found))
            self.device.store.set_queue(queue)
            if not start:
                await self.push()
                return {}
            # nothing will end by itself: the first waiting entry plays now
            try:
                await self._advance_locked()
            finally:
                await self.push()
            return {"started": True}

    async def _handle_remove(self, msg: dict[str, Any]) -> dict[str, Any]:
        qid = msg.get("qid")
        if not isinstance(qid, str) or not qid:
            raise InvalidRequest("qid must be a string")
        async with self._lock:
            queue = self.device.store.queue
            self.device.store.set_queue([e for e in queue if e.get("qid") != qid])
        await self.push()  # also for an unknown qid (idempotent)
        return {}

    # -- auto-advance -------------------------------------------------------

    @callback
    def _on_natural_end(self) -> None:
        if self.device.store.queue:
            self.hass.async_create_task(
                self.advance(), f"kidshub queue advance {self.device.device_id}"
            )

    async def advance(self) -> None:
        """Start the next entry unless something else started in the meantime."""
        async with self._lock:
            if not self.device.store.queue or not self.playback.should_start_queue():
                return
            try:
                await self._advance_locked()
            except MaUnavailable as err:
                _LOGGER.warning("Could not play the next queue entry: %s", err)
            finally:
                await self.push()

    async def _advance_locked(self) -> None:
        """Play the first entry that is still allowed; drop it and the skipped ones.

        Each entry is tried once. ``MaUnavailable`` keeps the entry and is raised.
        """
        while queue := self.device.store.queue:
            entry = queue[0]
            try:
                await self.playback.play_queued(entry["uri"])
            except (NotAllowed, NotFound, InvalidRequest) as err:
                _LOGGER.debug("Skipping queue entry %s: %s", entry["uri"], err)
                self._drop(entry)
                continue
            self._drop(entry)
            return

    def _drop(self, entry: dict[str, Any]) -> None:
        """Remove ``entry`` by its qid (the list may have changed while playing)."""
        queue = self.device.store.queue
        self.device.store.set_queue([e for e in queue if e.get("qid") != entry.get("qid")])
