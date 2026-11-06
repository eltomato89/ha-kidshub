"""Authenticated WebSocket session (protocol section 3)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from aiohttp import WSMsgType, web
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_ID, CONF_TOKEN_HASH, DOMAIN
from .device import (
    InvalidRequest,
    KidDevice,
    MaUnavailable,
    NotAllowed,
    NotFound,
    PushConfig,
    TtsFailed,
)

_LOGGER = logging.getLogger(__name__)

AUTH_TIMEOUT = 10.0
HEARTBEAT = 30
MAX_MSG_SIZE = 64 * 1024


def _error(id_: int, code: str, message: str) -> dict[str, Any]:
    return {
        "type": "result",
        "id": id_,
        "success": False,
        "error": {"code": code, "message": message},
    }


def _paired_with_token(hass: HomeAssistant, device_id: str, token: str) -> bool:
    """Return whether a config entry (loaded or not) pairs ``device_id`` with ``token``."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_DEVICE_ID) == device_id:
            stored = str(entry.data.get(CONF_TOKEN_HASH, ""))
            return hmac.compare_digest(digest, stored)
    return False


class _NotLoaded(Exception):
    """Paired device whose entry is not loaded right now (reload, HA start)."""


class _AuthInvalid(Exception):
    """Unknown device or wrong token: the app must pair again (protocol 3.1)."""


class WsSession:
    """One connection of the app."""

    def __init__(
        self,
        ws: web.WebSocketResponse,
        devices: dict[str, KidDevice],
        hass: HomeAssistant,
    ) -> None:
        self._ws = ws
        self._devices = devices
        self._hass = hass
        self.device: KidDevice | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()

    async def send_json(self, msg: dict[str, Any]) -> None:
        """Send a message; a gone transport counts as offline (never raises)."""
        if self._ws.closed:
            return
        try:
            async with self._send_lock:
                await self._ws.send_json(msg)
        except (ConnectionError, RuntimeError) as err:
            _LOGGER.debug("Send failed, treating device as offline: %s", err)

    async def close(self) -> None:
        """Close the socket."""
        try:
            await self._ws.close()
        except (ConnectionError, RuntimeError) as err:
            _LOGGER.debug("Close failed: %s", err)

    async def _read_json(self) -> Any:
        """Return the next JSON message, or None when closed/unusable."""
        while True:
            try:
                msg = await self._ws.receive()
            except (ConnectionError, RuntimeError):
                return None
            if msg.type is WSMsgType.TEXT:
                try:
                    return json.loads(msg.data)
                except ValueError:
                    return {}
            if msg.type in (
                WSMsgType.CLOSE,
                WSMsgType.CLOSING,
                WSMsgType.CLOSED,
                WSMsgType.ERROR,
            ):
                return None
            # ignore binary and other frames

    async def _authenticate(self) -> KidDevice | None:
        """Return the device; ``None`` for a timeout or a malformed handshake.

        Raises ``_AuthInvalid`` (unknown device, wrong token) or ``_NotLoaded``.
        """
        try:
            # Absolute deadline for the whole handshake, regardless of
            # pings or binary frames sent in between.
            async with asyncio.timeout(AUTH_TIMEOUT):
                msg = await self._read_json()
        except TimeoutError:
            return None
        if not isinstance(msg, dict) or msg.get("type") != "auth":
            return None
        device_id = msg.get("device_id")
        token = msg.get("token")
        if not isinstance(device_id, str) or not isinstance(token, str):
            return None
        device = self._devices.get(device_id)
        if device is None:
            # Protocol 3.1: auth_invalid makes the app give up for good, so
            # only send it when the device is really unknown or the token is
            # wrong; a known but unloaded device just gets the socket closed.
            if _paired_with_token(self._hass, device_id, token):
                raise _NotLoaded
            raise _AuthInvalid
        if not device.verify_token(token):
            raise _AuthInvalid
        version = msg.get("app_version")
        device.app_version = version if isinstance(version, str) else None
        return device

    async def run(self) -> None:
        """Handshake and serve the connection until it closes."""
        try:
            device = await self._authenticate()
        except _AuthInvalid:
            await self.send_json({"type": "auth_invalid"})
            await self.close()
            return
        except _NotLoaded:
            device = None
        if device is None:
            # timeout, malformed handshake or entry not loaded: no auth_invalid,
            # the app retries with backoff (auth_invalid makes it unpair)
            await self.close()
            return
        self.device = device
        try:
            await self.send_json({"type": "auth_ok"})
            await device.attach(self)
            if self._devices.get(device.device_id) is not device:
                return  # entry unloaded meanwhile
            await self.send_json(device.config_message())
            await device.on_connected()
            await self._loop(device)
        finally:
            device.detach(self)
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.close()

    async def _loop(self, device: KidDevice) -> None:
        while (msg := await self._read_json()) is not None:
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "state":
                await device.async_on_state(msg)
                continue
            id_ = msg.get("id")
            if isinstance(id_, bool) or not isinstance(id_, int):
                continue  # not a request, nothing to answer
            task = asyncio.create_task(self._run_request(device, msg, id_))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_request(
        self, device: KidDevice, msg: dict[str, Any], id_: int
    ) -> None:
        type_ = msg.get("type")
        push_config = False
        try:
            result = await device.handle_request(msg)
            push_config = isinstance(result, PushConfig)
            reply: dict[str, Any] = {
                "type": "result",
                "id": id_,
                "success": True,
                "result": dict(result),
            }
        except NotAllowed as err:
            reply = _error(id_, "not_allowed", str(err))
        except NotFound as err:
            reply = _error(id_, "not_found", str(err))
        except TtsFailed as err:
            reply = _error(id_, "tts_failed", str(err))
        except MaUnavailable as err:
            reply = _error(id_, "ma_unavailable", str(err))
        except InvalidRequest as err:
            reply = _error(id_, "invalid", str(err))
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Request %s failed", type_)
            reply = _error(id_, "invalid", "request failed")
        await self.send_json(reply)
        if push_config:
            await device.push_config()
