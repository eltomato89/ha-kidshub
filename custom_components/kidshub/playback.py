"""Playback of one device: ``play``, ``control``, ``wake_play`` and queue tracking.

Playback runs on the Sendspin player of the device in Music Assistant (protocol 3.3).
The MA queue of that player is followed to push ``now_playing`` (protocol 3.2) and to
save the resume position of episodes, audiobooks and albums.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import CONF_CHILD_NAME
from .content import FAVORITES_PREFIX
from .device import InvalidRequest, KidDevice, MaUnavailable, NotAllowed, NotFound
from .ma import MA_DOMAIN

if TYPE_CHECKING:
    from .content import ContentService
    from .ma import MaAdapter, MaItem, QueueSnapshot

_LOGGER = logging.getLogger(__name__)

WAKE_PLAY_TIMEOUT = 7.0  # the app falls back to its own alarm sound after 8 s
PUSH_INTERVAL = 10.0  # seconds between now_playing pushes of the same item
SAVE_INTERVAL = 10.0  # seconds between progress saves of the same item
FINISHED_MARGIN_MS = 30000
RETRY_DELAYS = (1, 2, 5, 10, 30, 60)  # seconds, queue subscription backoff
FROM_START_SEEK_MS = 1000  # MA treats seek 0 as "resume at MA's own position"
FROM_START_KINDS = {"album", "episode", "audiobook"}
WAKE_FROM_START_TYPES = {"audiobook", "podcast_episode", "podcast"}  # MA media types

MUSIC_KINDS = {"track", "playlist"}
CONTAINER_KINDS = {"playlist", "album", "podcast"}
NOW_PLAYING_KINDS = {"track", "episode", "audiobook"}
COMMANDS = {"pause", "resume", "next", "previous"}


_CONTAINER_KINDS = {"album", "audiobook", "playlist", "podcast"}


def _container_kind(cur: "_Current") -> str | None:
    """Return the protocol ``container_kind`` of what the current item plays in."""
    if cur.kind == "album_track":
        return "album"
    if cur.container_uri is None:
        return None
    media_type = cur.container_uri.partition("://")[2].partition("/")[0]
    return media_type if media_type in _CONTAINER_KINDS else None


@dataclass(frozen=True)
class _Album:
    """An album played by ``play`` (its queue holds the album's tracks)."""

    uri: str
    tracks: tuple[str, ...]


@dataclass(frozen=True)
class _Current:
    """The current queue item as KidsHub sees it."""

    item: MaItem
    kind: str  # protocol now_playing kind
    container_uri: str | None
    progress_uri: str | None  # where the resume position is saved
    track_index: int
    is_last: bool  # last track of its album
    index: int  # queue index
    elapsed_ms: int
    playing: bool


def _media_type(uri: str) -> str:
    """Return the media type of an MA URI (``{provider}://{media_type}/{id}``)."""
    return uri.partition("://")[2].partition("/")[0]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_play(msg: dict[str, Any]) -> tuple[list[str], int, bool, bool]:
    """Validate a ``play`` request; return ``(uris, start_index, resume, from_start)``."""
    uris = msg.get("uris")
    if (
        not isinstance(uris, list)
        or not uris
        or not all(isinstance(u, str) and u for u in uris)
    ):
        raise InvalidRequest("uris must be a non-empty list of strings")
    start = msg.get("start_index", 0)
    if not _is_int(start) or not 0 <= start < len(uris):
        raise InvalidRequest("start_index out of range")
    resume = msg.get("resume", True)
    from_start = msg.get("from_start", False)
    if not isinstance(resume, bool) or not isinstance(from_start, bool):
        raise InvalidRequest("resume and from_start must be booleans")
    return uris, start, resume, from_start


class PlaybackService:
    """Play allowed content on the device's MA player and follow its queue."""

    def __init__(self, device: KidDevice, ma: MaAdapter) -> None:
        self.device = device
        self.hass = device.hass
        self.ma = ma
        self.client_id = f"kidshub-{device.device_id}"
        self.player_name = f"KidsHub {device.entry.data[CONF_CHILD_NAME]}"
        self._player_id: str | None = None  # subscribed player
        self._unsub_queue: Callable[[], None] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._retry = asyncio.Event()
        self._play_lock = asyncio.Lock()
        # Persisted so that the album's tracks are still recognized as a
        # Hörspiel after an HA restart (the queue holds plain track URIs).
        ctx = device.store.album_context
        self._album: _Album | None = _Album(*ctx) if ctx else None
        self._current: _Current | None = None
        self._last_push = float("-inf")
        self._last_save = float("-inf")
        self._music_enabled: bool = device.store.settings["music_enabled"]
        device.register_handler("play", self._handle_play)
        device.register_handler("control", self._handle_control)
        device.register_handler("wake_play", self._handle_wake_play)
        device.register_connected_callback(self._on_connected)

    @property
    def content(self) -> ContentService:
        """Return the content service (allowlist) of the device."""
        assert self.device.content is not None
        return self.device.content

    # -- lifecycle ----------------------------------------------------------

    def start(self, entry: ConfigEntry) -> None:
        """Start following the queue; call ``stop`` on unload."""
        self._unsubs.append(
            async_dispatcher_connect(self.hass, self.device.signal, self._on_device_update)
        )
        self._unsubs.append(
            async_dispatcher_connect(
                self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._on_entry_changed
            )
        )
        self._task = entry.async_create_background_task(
            self.hass,
            self._subscription_loop(),
            f"kidshub playback {self.device.device_id}",
        )

    def stop(self) -> None:
        """Stop following the queue."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._drop_subscription()

    def _drop_subscription(self) -> None:
        unsub, self._unsub_queue = self._unsub_queue, None
        self._player_id = None
        if unsub is not None:
            with suppress(ValueError, KeyError):  # already gone with the old client
                unsub()

    async def _subscription_loop(self) -> None:
        """Subscribe to the player's queue; resubscribe on demand, back off on errors."""
        attempt = 0
        try:
            while True:
                self._retry.clear()
                try:
                    player_id = await self._resolve_player()
                    if player_id is not None:
                        self._unsub_queue = self.ma.subscribe_queue(
                            player_id, self._on_snapshot
                        )
                        self._player_id = player_id
                except MaUnavailable as err:
                    _LOGGER.debug("Queue subscription failed: %s", err)
                if self._unsub_queue is not None:
                    attempt = 0
                    await self._retry.wait()
                    self._drop_subscription()
                    continue
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                attempt += 1
                with suppress(TimeoutError):
                    async with asyncio.timeout(delay):
                        await self._retry.wait()
        finally:
            self._drop_subscription()

    def _request_resubscribe(self) -> None:
        self._retry.set()

    @callback
    def _on_entry_changed(self, change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.domain == MA_DOMAIN:
            self._request_resubscribe()
            # config.sendspin depends on the MA entry (added/removed/reloaded)
            self.hass.async_create_task(
                self.device.push_config(), f"kidshub ma config {self.device.device_id}"
            )

    async def _on_connected(self) -> None:
        if self._unsub_queue is None:
            # the Sendspin player appears in MA once the app has connected
            self._request_resubscribe()
        await self.device.push(self._now_playing_message())

    # -- player ---------------------------------------------------------------

    async def _resolve_player(self) -> str | None:
        return await self.ma.player_id_for(self.client_id, self.player_name)

    async def _player(self) -> str:
        """Return the MA player of this device or raise ``MaUnavailable``."""
        player_id = await self._resolve_player()
        if player_id is None:
            raise MaUnavailable("the player of this device is not known to Music Assistant")
        if player_id != self._player_id:
            self._request_resubscribe()
        return player_id

    # -- handlers -------------------------------------------------------------

    async def _handle_play(self, msg: dict[str, Any]) -> dict[str, Any]:
        uris, start, resume, from_start = _parse_play(msg)
        groups = [await self._resolve_uri(uri) for uri in uris]
        if not groups[start]:
            raise NotFound("nothing to play")
        if not self.device.store.settings["music_enabled"] and any(
            i.kind in MUSIC_KINDS for group in groups for i in group
        ):
            raise NotAllowed("music is disabled")
        # Containers before the start item are expanded so that the queue index
        # of the start item is known (MA expands them in the queue as well).
        queue: list[str] = []
        for group in groups[:start]:
            for it in group:
                if it.kind in CONTAINER_KINDS:
                    queue.extend(c.uri for c in await self.content.children(it.uri))
                else:
                    queue.append(it.uri)
        index = len(queue)
        first, *rest = [i for group in groups[start:] for i in group]
        album: _Album | None = None
        seek_ms = 0
        if first.kind == "album":
            tracks = await self.content.children(first.uri, fresh=True)
            if not tracks:
                raise NotFound("album has no tracks")
            track_index, seek_ms = self._resume_point(
                first, resume, from_start, len(tracks)
            )
            album = _Album(first.uri, tuple(t.uri for t in tracks))
            queue.extend(album.tracks)
            index += track_index
        else:
            if first.kind in ("episode", "audiobook"):
                _, seek_ms = self._resume_point(first, resume, from_start, 1)
            queue.append(first.uri)
        if first.kind in FROM_START_KINDS and seek_ms <= 0:
            # Ruling R24: at seek 0 MA resumes audiobooks/episodes at its own
            # saved position (play_index), so "from the beginning" is 1 s.
            seek_ms = FROM_START_SEEK_MS
        queue.extend(i.uri for i in rest)
        await self._play(queue, index, album, seek_ms)
        return {}

    def _resume_point(
        self, item: MaItem, resume: bool, from_start: bool, tracks: int
    ) -> tuple[int, int]:
        """Return ``(track_index, position_ms)`` to start ``item`` at."""
        store = self.device.store
        rec = store.get_progress(item.uri)
        if from_start:
            store.set_progress(
                item.uri,
                position_ms=0,
                duration_ms=rec.duration_ms if rec else item.duration_ms,
                finished=False,
                track_index=0,
            )
            return 0, 0
        if not resume or rec is None or rec.finished:
            return 0, 0
        if not 0 <= rec.track_index < tracks:
            return 0, 0
        return rec.track_index, max(0, rec.position_ms)

    async def _resolve_uri(self, uri: str) -> list[MaItem]:
        """Check ``uri`` against the allowlist; a favorites tile gives its items."""
        if uri.startswith(FAVORITES_PREFIX):
            items: list[MaItem] = []
            for fav in self.content.favorites(uri.removeprefix(FAVORITES_PREFIX)):
                found = await self.content.find_allowed_item(fav["uri"])
                if found is not None:  # skip favorites no longer allowed
                    items.append(found)
            return items
        found = await self.content.find_allowed_item(uri)
        if found is None:
            if uri in self.content.allowed():
                raise NotFound("content no longer exists in Music Assistant")
            raise NotAllowed("content not allowed")
        return [found]

    async def _play(
        self, queue: list[str], index: int, album: _Album | None, seek_ms: int
    ) -> None:
        async with self._play_lock:
            try:
                player_id = await self._player()
            except MaUnavailable:
                self._request_resubscribe()
                raise
            self._flush_current()
            # Set before playing: queue events of the new queue may arrive while
            # ``play`` is still awaited. Restored if MA does not take the queue.
            previous, self._album = self._album, album
            try:
                await self.ma.play(player_id, queue, index, seek_ms)
            except MaUnavailable:
                self._album = previous
                self._request_resubscribe()
                raise
            except asyncio.CancelledError:
                # e.g. the wake_play timeout: MA may still take the new queue,
                # so the old album context must not survive a restart
                self._store_album_context(album)
                raise
            self._store_album_context(album)

    def _store_album_context(self, album: _Album | None) -> None:
        if album is not None:
            self.device.store.set_album_context(album.uri, album.tracks)
        else:
            self.device.store.set_album_context(None)

    def _music_blocked(self) -> bool:
        """Return whether music is disabled and the queue holds music."""
        cur = self._current
        return (
            not self.device.store.settings["music_enabled"]
            and cur is not None
            and cur.kind == "track"
        )

    async def _handle_control(self, msg: dict[str, Any]) -> dict[str, Any]:
        cmd = msg.get("cmd")
        position = msg.get("position_ms")
        if cmd == "seek":
            if not _is_int(position) or position < 0:
                raise InvalidRequest("position_ms must be a non-negative integer")
        elif cmd not in COMMANDS:
            raise InvalidRequest("unknown cmd")
        if cmd != "pause" and self._music_blocked():
            raise NotAllowed("music is disabled")
        try:
            player_id = await self._player()
            if cmd == "seek":
                await self.ma.seek(player_id, position)
            else:
                await self.ma.command(player_id, cmd)
        except MaUnavailable:
            self._request_resubscribe()
            raise
        return {}

    async def _handle_wake_play(self, msg: dict[str, Any]) -> dict[str, Any]:
        uri = self.device.entry.options.get("wake_media_uri")
        if not uri:
            raise InvalidRequest("no wake-up music configured")
        if uri not in self.content.allowed():
            raise NotAllowed("wake-up music is not allowed")
        # R24: MA resumes audiobooks/episodes at its own position at seek 0
        seek_ms = FROM_START_SEEK_MS if _media_type(uri) in WAKE_FROM_START_TYPES else 0
        try:
            async with asyncio.timeout(WAKE_PLAY_TIMEOUT):
                await self._play([uri], 0, None, seek_ms)
        except TimeoutError as err:
            self._request_resubscribe()
            raise MaUnavailable("Music Assistant did not start in time") from err
        return {}

    # -- queue tracking -------------------------------------------------------

    def _describe(self, snap: QueueSnapshot) -> _Current | None:
        it = snap.current
        if it is None:
            return None
        kind = it.kind if it.kind in NOW_PLAYING_KINDS else "track"
        container = snap.container_uri
        progress_uri: str | None = None
        track_index = 0
        is_last = False
        album = self._album
        if album is not None and not self._allowed_album(album.uri):
            album = None  # the album was removed from the Hörspiel allowlist
        if kind == "track" and album is not None and it.uri in album.tracks:
            kind, container, progress_uri = "album_track", album.uri, album.uri
            track_index = album.tracks.index(it.uri)
            is_last = track_index == len(album.tracks) - 1
        elif kind == "track" and self._allowed_album(container):
            # album started elsewhere (e.g. MA UI): queue index = album index
            kind, progress_uri, track_index = "album_track", container, snap.index
        elif (
            kind == "track" and container is None and self._allowed_album(it.album_uri)
        ):
            # no album context and no container (e.g. a single track started
            # elsewhere): the track's own album decides; its album index is
            # unknown, the queue index is the best guess. Never for a track of
            # a playlist, whose progress would overwrite the Hörspiel's.
            container = it.album_uri
            kind, progress_uri, track_index = "album_track", container, snap.index
        elif kind in ("episode", "audiobook"):
            progress_uri = it.uri
        return _Current(
            item=it,
            kind=kind,
            container_uri=container,
            progress_uri=progress_uri,
            track_index=track_index,
            is_last=is_last,
            index=snap.index,
            elapsed_ms=max(0, snap.elapsed_ms),
            playing=snap.playing,
        )

    def _allowed_album(self, uri: str | None) -> bool:
        """Return whether ``uri`` is an album on the Hörspiel allowlist."""
        return (
            uri is not None
            and "://album/" in uri
            and uri in self.content.allowed("audiobooks")
        )

    @callback
    def _on_snapshot(self, snap: QueueSnapshot) -> None:
        """Handle a queue change of the device's player."""
        now = monotonic()
        prev = self._current
        cur = self._describe(snap)
        item_changed = (prev is None) != (cur is None) or (
            prev is not None and cur is not None and prev.item.uri != cur.item.uri
        )
        state_changed = (
            prev is not None and cur is not None and prev.playing != cur.playing
        )
        if item_changed and prev is not None:
            advanced = cur is not None and cur.index == prev.index + 1
            self._save(prev, advanced=advanced)
        self._current = cur
        self.content.now_playing = (
            dataclasses.replace(cur.item, kind=cur.kind) if cur is not None else None
        )
        if cur is not None and cur.progress_uri is not None:
            if item_changed or state_changed or now - self._last_save >= SAVE_INTERVAL:
                self._save(cur)
                self._last_save = now
        playing = cur is not None and cur.playing
        if item_changed or state_changed or (
            playing and now - self._last_push >= PUSH_INTERVAL
        ):
            self._last_push = now
            self.hass.async_create_task(
                self.device.push(self._now_playing_message()),
                f"kidshub now_playing {self.device.device_id}",
            )

    def _save(self, cur: _Current, *, advanced: bool = False) -> None:
        """Save the resume position of ``cur`` (no-op for music tracks)."""
        if cur.progress_uri is None:
            return
        duration = cur.item.duration_ms
        done = advanced or (
            duration > 0 and cur.elapsed_ms >= duration - FINISHED_MARGIN_MS
        )
        if cur.kind == "album_track":
            done = done and cur.is_last  # the album is finished after its last track
        self.device.store.set_progress(
            cur.progress_uri,
            position_ms=cur.elapsed_ms,
            duration_ms=duration,
            finished=done,
            track_index=cur.track_index,
        )

    def _flush_current(self) -> None:
        """Save the last known position before the queue is replaced."""
        cur, self._current = self._current, None
        if cur is not None:
            self._save(cur)

    def _now_playing_message(self) -> dict[str, Any]:
        cur = self._current
        if cur is None:
            return {"type": "now_playing", "kind": None}
        it = cur.item
        rated = cur.container_uri if cur.kind == "album_track" else it.uri
        return {
            "type": "now_playing",
            "kind": cur.kind,
            "uri": it.uri,
            "title": it.title,
            "subtitle": it.subtitle,
            "image_url": self.content.image_url(it.image, 512),
            "duration_ms": it.duration_ms,
            "elapsed_ms": cur.elapsed_ms,
            "stars": self.device.store.get_rating(rated) if rated else None,
            "container_uri": cur.container_uri,
            "container_kind": _container_kind(cur),
        }

    # -- settings -------------------------------------------------------------

    @callback
    def _on_device_update(self) -> None:
        """Stop and clear queued music when ``music_enabled`` is switched off."""
        enabled = self.device.store.settings["music_enabled"]
        was, self._music_enabled = self._music_enabled, enabled
        if was and not enabled and self._music_blocked():
            self.hass.async_create_task(
                self._stop_music(), f"kidshub stop music {self.device.device_id}"
            )

    async def _stop_music(self) -> None:
        """Stop the music and clear the queue so it cannot be resumed."""
        try:
            player_id = await self._player()
            await self.ma.command(player_id, "stop")
            await self.ma.command(player_id, "clear")
        except MaUnavailable as err:
            _LOGGER.warning("Could not stop music after it was disabled: %s", err)
