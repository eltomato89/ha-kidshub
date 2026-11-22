"""Runtime of one paired KidsHub device."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .config_builder import build_config
from .const import CONF_CHILD_NAME, CONF_DEVICE_ID, CONF_TOKEN_HASH, DOMAIN
from .store import KidStore

if TYPE_CHECKING:
    from .content import ContentService
    from .ws import WsSession

_LOGGER = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

TTS_MAX_CHARS = 200
ANNOUNCE_MAX_CHARS = 500  # announcements from HA may be longer than speak buttons
ANNOUNCE_MIN_APP = (0, 1, 3)  # older apps drop the ``announce`` push silently
_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ALARM_KEYS = {"enabled", "time", "weekdays", "with_light"}


def tts_text(text: object) -> str | None:
    """Return what a speak button sends for ``text`` (``text[:200]``), None if blank.

    The app truncates a title to 200 characters before its ``tts`` request; the
    pre-warming uses the same normalisation so the cache keys match.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    return text[:TTS_MAX_CHARS]


class PushConfig(dict):  # type: ignore[type-arg]
    """Handler result that asks the session to push the config after the result."""


class NotAllowed(Exception):
    """The requested action or content is not permitted (code ``not_allowed``)."""


class MaUnavailable(Exception):
    """Music Assistant is not reachable (code ``ma_unavailable``)."""


class NotFound(Exception):
    """Allowed content no longer exists in Music Assistant (code ``not_found``)."""


class TtsFailed(Exception):
    """Speech could not be generated (code ``tts_failed``)."""


class InvalidRequest(Exception):
    """The request is malformed (code ``invalid``)."""


def parse_version(value: object) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` of an app version, None if unknown."""
    match = _VERSION_RE.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def signal_name(device_id: str) -> str:
    """Return the dispatcher signal fired when a device changes."""
    return f"kidshub_{device_id}_updated"


def _parse_alarm_patch(msg: dict[str, Any]) -> dict[str, Any]:
    """Validate an ``alarm_update`` request and return the alarm patch."""
    patch = {k: v for k, v in msg.items() if k not in ("type", "id")}
    if not patch or not set(patch) <= _ALARM_KEYS:
        raise InvalidRequest("unsupported alarm fields")
    for key in ("enabled", "with_light"):
        if key in patch and not isinstance(patch[key], bool):
            raise InvalidRequest(f"{key} must be a boolean")
    if "time" in patch and not (
        isinstance(patch["time"], str) and _TIME_RE.match(patch["time"])
    ):
        raise InvalidRequest("time must be HH:MM")
    if "weekdays" in patch:
        days = patch["weekdays"]
        if (
            not isinstance(days, list)
            or any(isinstance(d, bool) or not isinstance(d, int) for d in days)
            or any(not 0 <= d <= 6 for d in days)
            or len(set(days)) != len(days)
        ):
            raise InvalidRequest("weekdays must be unique ints 0..6")
        patch["weekdays"] = sorted(days)
    return patch


class KidDevice:
    """State, settings and the live WebSocket session of one device."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.device_id: str = entry.data[CONF_DEVICE_ID]
        self.store = KidStore(hass, entry.entry_id)
        self.config_version = 1
        self.last_state: dict[str, Any] | None = None
        self.app_version: str | None = None
        self._session: WsSession | None = None
        # content requests and allowlist; set by the entry setup
        self.content: ContentService | None = None
        self._handlers: dict[str, Handler] = {}
        self._connected_callbacks: list[Callable[[], Awaitable[None]]] = []
        self.register_handler("alarm_update", self._handle_alarm_update)
        self.register_handler("tts", self._handle_tts)

    @property
    def connected(self) -> bool:
        """Return whether a session is attached."""
        return self._session is not None

    @property
    def signal(self) -> str:
        """Return the dispatcher signal of this device."""
        return signal_name(self.device_id)

    async def async_load(self) -> None:
        """Load persisted data."""
        await self.store.async_load()

    async def async_close(self) -> None:
        """Close the session and persist data (entry unload)."""
        session, self._session = self._session, None
        if session is not None:
            await session.close()
        await self.store.async_flush()

    def verify_token(self, token: str) -> bool:
        """Check a device token against the stored sha256 hash."""
        if not isinstance(token, str):
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        stored = str(self.entry.data.get(CONF_TOKEN_HASH, ""))
        return hmac.compare_digest(digest, stored)

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    def notify(self) -> None:
        """Tell the entities that device data (e.g. ratings) changed."""
        self._notify()

    async def attach(self, session: WsSession) -> None:
        """Make ``session`` the active one, closing any previous session."""
        old, self._session = self._session, session
        if old is not None and old is not session:
            await old.close()
        self._notify()

    def detach(self, session: WsSession) -> None:
        """Detach ``session`` if it is still the active one."""
        if self._session is session:
            self._session = None
            self._notify()

    async def push(self, msg: dict[str, Any]) -> None:
        """Send ``msg`` to the app; no-op while offline."""
        session = self._session
        if session is not None:
            await session.send_json(msg)

    def config_message(self) -> dict[str, Any]:
        """Build the current ``config`` message."""
        child_name = self.entry.data[CONF_CHILD_NAME]
        data = self.hass.data.get(DOMAIN)
        ma = getattr(data, "ma", None)
        sendspin = ma.sendspin_info(self.device_id, child_name) if ma else None
        return build_config(
            version=self.config_version,
            child_name=child_name,
            settings=self.store.settings,
            options=self.entry.options,
            hass_states=self.hass.states.get,
            sendspin=sendspin,
            image_url=self.content.image_url if self.content is not None else None,
        )

    async def async_config_message(self) -> dict[str, Any]:
        """Look up a fallback Sendspin address if needed, then build ``config``."""
        ma = getattr(self.hass.data.get(DOMAIN), "ma", None)
        if ma is not None:
            await ma.async_refresh_sendspin()
        config = self.config_message()
        self.prewarm_tts(r.get("name") for r in config.get("room") or [])
        return config

    async def push_config(self) -> None:
        """Bump the config version and push the new config."""
        self.config_version += 1
        if self.connected:
            await self.push(await self.async_config_message())

    async def update_settings(self, patch: dict[str, Any]) -> None:
        """Merge ``patch`` into the settings, notify and push the config."""
        await self._apply_settings(patch)
        await self.push_config()

    async def apply_settings(self, patch: dict[str, Any]) -> None:
        """Merge ``patch`` and notify, without pushing (a handler returns PushConfig)."""
        await self._apply_settings(patch)

    async def _apply_settings(self, patch: dict[str, Any]) -> None:
        await self.store.async_update_settings(copy.deepcopy(patch))
        self._notify()

    def prewarm_tts(
        self,
        texts: Iterable[object],
        *,
        priority: bool = True,
        limit: int | None = None,
    ) -> None:
        """Generate speech for ``texts`` in the background (never blocks or raises)."""
        prewarm = getattr(self.hass.data.get(DOMAIN), "prewarm", None)
        if prewarm is not None:
            prewarm.schedule(texts, priority=priority, limit=limit)

    def register_handler(self, type_: str, handler: Handler) -> None:
        """Register the handler of an app request type."""
        self._handlers[type_] = handler

    def register_connected_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """Register a coroutine to run right after ``config`` was sent on connect."""
        self._connected_callbacks.append(callback)

    async def on_connected(self) -> None:
        """Run the connected callbacks (called by the session after ``config``).

        A failing callback is logged; it must not end the session.
        """
        for callback in list(self._connected_callbacks):
            try:
                await callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Connected callback %s failed", callback)

    async def handle_request(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a request to its handler."""
        handler = self._handlers.get(msg.get("type"))  # type: ignore[arg-type]
        if handler is None:
            raise InvalidRequest("unknown request type")
        return await handler(msg)

    async def async_on_state(self, msg: dict[str, Any]) -> None:
        """Store the last ``state`` message of the app and notify entities."""
        self.last_state = msg
        self._notify()

    async def announce(self, message: object, volume: int | None = None) -> None:
        """Speak ``message`` on the device (protocol ``announce``).

        Works regardless of ``speak_enabled`` and at night; ``volume`` is capped
        at ``max_volume``. Raises ``HomeAssistantError`` while offline.
        """
        text = message.strip() if isinstance(message, str) else ""
        if not text or len(text) > ANNOUNCE_MAX_CHARS:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_message",
                translation_placeholders={"max": str(ANNOUNCE_MAX_CHARS)},
            )
        name = self.entry.data[CONF_CHILD_NAME]
        if not self.connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_connected",
                translation_placeholders={"name": name},
            )
        version = parse_version(self.app_version)
        if version is not None and version < ANNOUNCE_MIN_APP:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="app_too_old",
                translation_placeholders={
                    "name": name,
                    "version": str(self.app_version),
                    "min": ".".join(map(str, ANNOUNCE_MIN_APP)),
                },
            )
        try:
            url = await self.hass.data[DOMAIN].tts.get_url(text, announcement=True)
        except TtsFailed as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="tts_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        msg: dict[str, Any] = {
            "type": "announce",
            "id": str(uuid.uuid4()),
            "text": text,
            "url": url,
        }
        if volume is not None:
            cap = self.store.settings["max_volume"]
            msg["volume"] = max(0, min(int(volume), cap))
        await self.push(msg)

    async def _handle_alarm_update(self, msg: dict[str, Any]) -> dict[str, Any]:
        patch = _parse_alarm_patch(msg)
        await self._apply_settings({"alarm": patch})
        return PushConfig()

    async def _handle_tts(self, msg: dict[str, Any]) -> dict[str, Any]:
        if not self.store.settings["speak_enabled"]:
            raise NotAllowed("speak buttons are disabled")
        text = msg.get("text")
        say = tts_text(text)
        if say is None or say != text:
            raise InvalidRequest("text must be 1..200 characters")
        url = await self.hass.data[DOMAIN].tts.get_url(say)
        return {"url": url}
