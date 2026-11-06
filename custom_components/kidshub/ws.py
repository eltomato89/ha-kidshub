"""Authenticated WebSocket session (protocol section 3)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import WSMsgType, web

from .device import (
    InvalidRequest,
    KidDevice,
    MaUnavailable,
    NotAllowed,
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


class WsSession:
    """One connection of the app."""

    def __init__(
        self, ws: web.WebSocketResponse, devices: dict[str, KidDevice]
    ) -> None:
        self._ws = ws
        self._devices = devices
        self.device: KidDevice | None = None

    async def send_json(self, msg: dict[str, Any]) -> None:
        """Send a message unless the socket is closing."""
        if not self._ws.closed:
            await self._ws.send_json(msg)

    async def close(self) -> None:
        """Close the socket."""
        await self._ws.close()

    async def _read_json(self, timeout: float | None = None) -> Any:
        """Return the next JSON message, or None when closed/unusable."""
        while True:
            try:
                msg = await self._ws.receive(timeout=timeout)
            except TimeoutError:
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
        msg = await self._read_json(AUTH_TIMEOUT)
        if not isinstance(msg, dict) or msg.get("type") != "auth":
            return None
        device_id = msg.get("device_id")
        token = msg.get("token")
        if not isinstance(device_id, str) or not isinstance(token, str):
            return None
        device = self._devices.get(device_id)
        if device is None or not device.verify_token(token):
            return None
        return device

    async def run(self) -> None:
        """Handshake and serve the connection until it closes."""
        device = await self._authenticate()
        if device is None:
            await self.send_json({"type": "auth_invalid"})
            await self._ws.close()
            return
        self.device = device
        try:
            await self.send_json({"type": "auth_ok"})
            await device.attach(self)
            await self.send_json(device.config_message())
            await self._loop(device)
        finally:
            device.detach(self)
            await self._ws.close()

    async def _loop(self, device: KidDevice) -> None:
        while (msg := await self._read_json()) is not None:
            if not isinstance(msg, dict):
                continue
            type_ = msg.get("type")
            if type_ == "state":
                await device.async_on_state(msg)
                continue
            id_ = msg.get("id")
            if isinstance(id_, bool) or not isinstance(id_, int):
                continue  # not a request, nothing to answer
            try:
                result = await device.handle_request(msg)
                reply: dict[str, Any] = {
                    "type": "result",
                    "id": id_,
                    "success": True,
                    "result": result,
                }
            except NotAllowed as err:
                reply = _error(id_, "not_allowed", str(err))
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
            await device.flush_deferred_push()
