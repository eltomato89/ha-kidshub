"""HTTP views of KidsHub."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .ws import HEARTBEAT, MAX_MSG_SIZE, WsSession

NO_STORE = {"Cache-Control": "no-store"}


class PairView(HomeAssistantView):
    """Pair a device with a code (protocol 2.1)."""

    url = "/api/kidshub/pair"
    name = "api:kidshub:pair"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Complete a pending pairing."""
        registry = request.app["hass"].data[DOMAIN].pairing
        invalid = self.json(
            {"error": "invalid_code"},
            HTTPStatus.FORBIDDEN,
            headers=NO_STORE,
        )
        try:
            body: Any = await request.json()
        except ValueError:
            registry.register_failure()
            return invalid
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            registry.register_failure()
            return invalid
        app_version = body.get("app_version")
        result = await registry.complete(
            body["code"], app_version if isinstance(app_version, str) else ""
        )
        if result is None:
            return invalid
        pending, token = result
        return self.json(
            {
                "device_id": pending.device_id,
                "token": token,
                "child_name": pending.child_name,
            },
            headers=NO_STORE,
        )


class WsView(HomeAssistantView):
    """Authenticated WebSocket of a device (protocol 3)."""

    url = "/api/kidshub/ws"
    name = "api:kidshub:ws"
    requires_auth = False

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        """Upgrade to a WebSocket and serve the session."""
        ws = web.WebSocketResponse(heartbeat=HEARTBEAT, max_msg_size=MAX_MSG_SIZE)
        await ws.prepare(request)
        devices = request.app["hass"].data[DOMAIN].devices
        await WsSession(ws, devices).run()
        return ws
