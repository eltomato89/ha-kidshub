"""The ``kidshub.announce`` service: speak a message on one or more devices."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import ATTR_MESSAGE, ATTR_VOLUME, DOMAIN, SERVICE_ANNOUNCE
from .device import ANNOUNCE_MAX_CHARS

if TYPE_CHECKING:
    from .device import KidDevice

ANNOUNCE_SCHEMA = vol.Schema(
    {
        **cv.ENTITY_SERVICE_FIELDS,
        vol.Required(ATTR_MESSAGE): vol.All(
            cv.string, vol.Length(min=1, max=ANNOUNCE_MAX_CHARS)
        ),
        vol.Optional(ATTR_VOLUME): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    }
)


def _ids(call: ServiceCall, key: str) -> list[str]:
    value = call.data.get(key)
    if value is None or value == "none":
        return []
    return [value] if isinstance(value, str) else list(value)


def _target_devices(hass: HomeAssistant, call: ServiceCall) -> list[KidDevice]:
    """Resolve the target (devices, areas, entities) to KidsHub devices."""
    devices: dict[str, KidDevice] = hass.data[DOMAIN].devices
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    registry_ids = set(_ids(call, ATTR_DEVICE_ID))
    for area_id in _ids(call, ATTR_AREA_ID):
        registry_ids.update(d.id for d in dr.async_entries_for_area(dev_reg, area_id))
    for entity_id in _ids(call, ATTR_ENTITY_ID):
        ent = ent_reg.async_get(entity_id)
        if ent is None or ent.platform != DOMAIN or ent.device_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_target",
                translation_placeholders={"target": entity_id},
            )
        registry_ids.add(ent.device_id)
    found: dict[str, KidDevice] = {}
    for registry_id in registry_ids:
        entry = dev_reg.async_get(registry_id)
        kid_ids = [i[1] for i in entry.identifiers if i[0] == DOMAIN] if entry else []
        for kid_id in kid_ids:
            if kid_id in devices:
                found[kid_id] = devices[kid_id]
    if not found:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_target"
        )
    return list(found.values())


async def _async_announce(call: ServiceCall) -> None:
    targets = _target_devices(call.hass, call)
    volume: int | None = call.data.get(ATTR_VOLUME)
    results: list[Any] = await asyncio.gather(
        *(device.announce(call.data[ATTR_MESSAGE], volume) for device in targets),
        return_exceptions=True,
    )
    # the connected devices got the announcement; report the first failure
    for result in results:
        if isinstance(result, BaseException):
            raise result


def async_register_services(hass: HomeAssistant) -> None:
    """Register the KidsHub services (once)."""
    if not hass.services.has_service(DOMAIN, SERVICE_ANNOUNCE):
        hass.services.async_register(
            DOMAIN, SERVICE_ANNOUNCE, _async_announce, schema=ANNOUNCE_SCHEMA
        )
