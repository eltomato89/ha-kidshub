"""Runtime of one paired KidsHub device."""

from __future__ import annotations

import copy
import hashlib
import hmac
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .config_builder import build_config
from .const import CONF_CHILD_NAME, CONF_DEVICE_ID, CONF_TOKEN_HASH
from .store import KidStore

if TYPE_CHECKING:
    from .ws import WsSession

_LOGGER = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ALARM_KEYS = {"enabled", "time", "weekdays", "with_light"}


class NotAllowed(Exception):
    """The requested action or content is not permitted (code ``not_allowed``)."""


class MaUnavailable(Exception):
    """Music Assistant is not reachable (code ``ma_unavailable``)."""


class InvalidRequest(Exception):
    """The request is malformed (code ``invalid``)."""


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
        self._session: WsSession | None = None
        self._push_pending = False
        self._handlers: dict[str, Handler] = {}
        self.register_handler("alarm_update", self._handle_alarm_update)

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
        """Build the current ``config`` message (sendspin wired in a later task)."""
        return build_config(
            version=self.config_version,
            child_name=self.entry.data[CONF_CHILD_NAME],
            settings=self.store.settings,
            options=self.entry.options,
            hass_states=self.hass.states.get,
            sendspin=None,
        )

    async def push_config(self) -> None:
        """Bump the config version and push the new config."""
        self.config_version += 1
        self._push_pending = False
        await self.push(self.config_message())

    async def flush_deferred_push(self) -> None:
        """Push a config whose push was deferred until after a result."""
        if self._push_pending:
            await self.push_config()

    async def update_settings(
        self, patch: dict[str, Any], *, defer_push: bool = False
    ) -> None:
        """Merge ``patch`` into the settings, notify and push the config.

        With ``defer_push`` the config push waits for ``flush_deferred_push``
        so a request's ``result`` can be sent first.
        """
        await self.store.async_update_settings(copy.deepcopy(patch))
        self._notify()
        if defer_push:
            self._push_pending = True
        else:
            await self.push_config()

    def register_handler(self, type_: str, handler: Handler) -> None:
        """Register the handler of an app request type."""
        self._handlers[type_] = handler

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

    async def _handle_alarm_update(self, msg: dict[str, Any]) -> dict[str, Any]:
        patch = _parse_alarm_patch(msg)
        await self.update_settings({"alarm": patch}, defer_push=True)
        return {}
