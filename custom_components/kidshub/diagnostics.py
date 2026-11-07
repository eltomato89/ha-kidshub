"""Config entry diagnostics of KidsHub (Sendspin address, versions)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CONF_CHILD_NAME, CONF_DEVICE_ID, CONF_TOKEN_HASH, DOMAIN

TO_REDACT = {CONF_TOKEN_HASH, "token", "access_token", "api_key", "password", "secret"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics of a device or the settings entry."""
    data = hass.data.get(DOMAIN)
    ma = getattr(data, "ma", None)
    ma_diag = ma.diagnostics() if ma is not None else None
    server_info = (ma_diag or {}).get("server_info") or {}
    integration = await async_get_integration(hass, DOMAIN)
    result: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "versions": {
            "integration": str(integration.version),
            "home_assistant": HA_VERSION,
            "ma_server": server_info.get("server_version"),
        },
    }
    device_id = entry.data.get(CONF_DEVICE_ID)
    if device_id is None:
        result["sendspin"] = {
            "url": (ma_diag or {}).get("sendspin_url"),
            "source": (ma_diag or {}).get("source"),
            "ma": ma_diag,
        }
        return result
    device = getattr(data, "devices", {}).get(device_id)
    result["versions"]["app"] = getattr(device, "app_version", None)
    result["device"] = {
        "loaded": device is not None,
        "connected": bool(getattr(device, "connected", False)),
        "config_version": getattr(device, "config_version", None),
    }
    result["sendspin"] = {
        "info": ma.sendspin_info(device_id, entry.data.get(CONF_CHILD_NAME, ""))
        if ma is not None
        else None,
        "source": (ma_diag or {}).get("source"),
        "ma": ma_diag,
    }
    return result
