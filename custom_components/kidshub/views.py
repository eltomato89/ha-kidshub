"""HTTP views of KidsHub."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from . import images
from .const import DOMAIN
from .tts_cache import CONTENT_TYPES, NAME_RE
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


def _authorized(request: web.Request) -> bool:
    """Check the Bearer token against all paired devices."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    devices = request.app["hass"].data[DOMAIN].devices
    return any([d.verify_token(token) for d in list(devices.values())])


def _unauthorized() -> web.Response:
    return web.Response(status=HTTPStatus.UNAUTHORIZED, headers=NO_STORE)


class ImageView(HomeAssistantView):
    """Scaled proxy of emitted cover images (protocol 2)."""

    url = "/api/kidshub/img"
    name = "api:kidshub:img"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the image ``u`` scaled to ``s`` pixels as JPEG."""
        if not _authorized(request):
            return _unauthorized()
        hass = request.app["hass"]
        url = request.query.get("u", "")
        try:
            size = int(request.query.get("s", "256"))
        except ValueError:
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        # SSRF guard: only URLs that ContentService handed out are fetched.
        if url not in hass.data[DOMAIN].images or not images.is_http_url(url):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            body = await images.fetch_scaled(hass, url, size)
        except images.ImageError:
            return web.Response(status=HTTPStatus.BAD_GATEWAY)
        return web.Response(
            body=body,
            content_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )


class TtsView(HomeAssistantView):
    """Cached TTS audio files (protocol 2)."""

    url = "/api/kidshub/tts/{name}"
    name = "api:kidshub:tts"
    requires_auth = False

    async def get(self, request: web.Request, name: str) -> web.Response:
        """Serve a cached audio file."""
        if not _authorized(request):
            return _unauthorized()
        if not NAME_RE.fullmatch(name):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        body = await request.app["hass"].data[DOMAIN].tts.read(name)
        if body is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(
            body=body,
            content_type=CONTENT_TYPES[name.rsplit(".", 1)[1]],
            headers={"Cache-Control": "private, max-age=86400"},
        )
