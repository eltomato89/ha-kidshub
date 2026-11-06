"""HTTP views of KidsHub."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN


class PairView(HomeAssistantView):
    """Pair a device with a code (protocol 2.1)."""

    url = "/api/kidshub/pair"
    name = "api:kidshub:pair"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Complete a pending pairing."""
        invalid = self.json({"error": "invalid_code"}, HTTPStatus.FORBIDDEN)
        try:
            body: Any = await request.json()
        except ValueError:
            return invalid
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            return invalid
        app_version = body.get("app_version")
        registry = request.app["hass"].data[DOMAIN].pairing
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
            }
        )
