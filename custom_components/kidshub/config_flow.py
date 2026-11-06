"""Config flow for KidsHub."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import async_get_data
from .const import (
    CONF_CHILD_NAME,
    CONF_DEVICE_ID,
    CONF_TOKEN_HASH,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_SETTINGS,
)
from .config_builder import DEFAULT_COLORS
from .device import InvalidRequest, MaUnavailable
from .ma import LIBRARY_TIMEOUT
from .pairing import PAIRING_TTL, PendingPairing

PAIRING_TIMEOUT: float = PAIRING_TTL

CONF_TTS_ENGINE = "tts_engine"
CONF_TTS_LANGUAGE = "tts_language"
CONF_TTS_OPTIONS = "tts_options"
WAKE_LABEL_TIMEOUT = 5.0  # seconds to fetch playlist titles for the wake music


class KidsHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for KidsHub."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending: PendingPairing | None = None
        self._task: asyncio.Task[None] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between adding a device and the global settings."""
        return self.async_show_menu(step_id="user", menu_options=["device", "settings"])

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the single settings entry."""
        if any(
            e.data.get(ENTRY_TYPE) == ENTRY_TYPE_SETTINGS
            for e in self._async_current_entries(include_ignore=False)
        ):
            return self.async_abort(reason="already_configured")
        if user_input is not None:
            options = dict(user_input)
            options.setdefault(CONF_TTS_OPTIONS, {})
            return self.async_create_entry(
                title="KidsHub",
                data={ENTRY_TYPE: ENTRY_TYPE_SETTINGS},
                options=options,
            )
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TTS_ENGINE): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="tts")
                    ),
                    vol.Required(
                        CONF_TTS_LANGUAGE, default="de-DE"
                    ): selector.TextSelector(),
                    vol.Optional(CONF_TTS_OPTIONS): selector.ObjectSelector(),
                }
            ),
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the child's name and open a pairing."""
        if user_input is None:
            return self.async_show_form(
                step_id="device",
                data_schema=vol.Schema({vol.Required(CONF_CHILD_NAME): str}),
            )
        registry = async_get_data(self.hass).pairing
        self._pending = registry.start(user_input[CONF_CHILD_NAME].strip())
        self._task = self.hass.async_create_task(self._wait(self._pending))
        return await self.async_step_wait_pairing()

    async def _wait(self, pending: PendingPairing) -> None:
        try:
            async with asyncio.timeout(PAIRING_TIMEOUT):
                await pending.done.wait()
        except TimeoutError:
            async_get_data(self.hass).pairing.cancel(pending.code)

    async def async_step_wait_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the code until the app has paired."""
        assert self._pending is not None and self._task is not None
        if not self._task.done():
            return self.async_show_progress(
                step_id="wait_pairing",
                progress_action="wait_pairing",
                progress_task=self._task,
                description_placeholders={"code": self._pending.code},
            )
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the device entry after a successful pairing."""
        pending = self._pending
        if pending is not None and pending.aborted:
            return self.async_abort(reason="too_many_attempts")
        if pending is None or not pending.done.is_set():
            return self.async_abort(reason="pairing_timeout")
        await self.async_set_unique_id(pending.device_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=pending.child_name,
            data={
                ENTRY_TYPE: ENTRY_TYPE_DEVICE,
                CONF_DEVICE_ID: pending.device_id,
                CONF_CHILD_NAME: pending.child_name,
                CONF_TOKEN_HASH: pending.token_hash,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow (device entries only; settings abort)."""
        if config_entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_DEVICE:
            return DeviceOptionsFlow()
        return SettingsOptionsFlow()

    @callback
    def async_remove(self) -> None:
        """Cancel an unfinished pairing when the flow goes away."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._pending is not None and not self._pending.done.is_set():
            async_get_data(self.hass).pairing.cancel(self._pending.code)


ROOM_DOMAINS = ["light", "switch", "cover", "input_boolean", "fan"]
STOP_BUTTON_DOMAINS = ["button", "input_button", "event", "binary_sensor"]

# Menu of the device options flow; add a step by adding its id here and an
# ``async_step_<id>`` method.
OPTIONS_MENU = ["room", "alarm_extras", "content"]

# option key -> MA library kinds offered for it
CONTENT_LIBRARIES: dict[str, tuple[str, ...]] = {
    "playlists": ("playlist",),
    "audiobooks": ("album", "audiobook"),
    "podcasts": ("podcast",),
}


def _suggested(key: str, value: Any) -> vol.Optional:
    """Optional field whose current value is prefilled but clearable."""
    if value is None:
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": value})


class SettingsOptionsFlow(OptionsFlow):
    """The settings entry has no options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="no_options")


class DeviceOptionsFlow(OptionsFlow):
    """Options of one device: room entities, names, colors, alarm extras."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self._options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._options = dict(self.config_entry.options)
        return self.async_show_menu(step_id="init", menu_options=OPTIONS_MENU)

    async def async_step_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the room entities."""
        if user_input is not None:
            self._options["room_entities"] = list(user_input["room_entities"])
            return await self.async_step_room_names()
        return self.async_show_form(
            step_id="room",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "room_entities",
                        default=self._options.get("room_entities", []),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=ROOM_DOMAINS, multiple=True
                        )
                    )
                }
            ),
        )

    async def async_step_room_names(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally name each room entity."""
        entities: list[str] = self._options.get("room_entities", [])
        old_names: dict[str, str] = self._options.get("room_names") or {}
        if user_input is not None:
            self._options["room_names"] = {
                e: str(user_input[e]).strip()
                for e in entities
                if str(user_input.get(e) or "").strip()
            }
            return await self.async_step_colors()
        return self.async_show_form(
            step_id="room_names",
            data_schema=vol.Schema(
                {
                    _suggested(e, old_names.get(e)): selector.TextSelector()
                    for e in entities
                }
            ),
        )

    async def async_step_colors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the six light colors."""
        current = self._options.get("colors") or DEFAULT_COLORS
        keys = [f"color_{i + 1}" for i in range(len(DEFAULT_COLORS))]
        if user_input is not None:
            self._options["colors"] = [
                [int(c) for c in user_input[k]] for k in keys
            ]
            return self._save()
        return self.async_show_form(
            step_id="colors",
            data_schema=vol.Schema(
                {
                    vol.Required(k, default=list(current[i])): selector.ColorRGBSelector()
                    for i, k in enumerate(keys)
                }
            ),
        )

    async def _playlist_labels(self, uris: list[str]) -> dict[str, str]:
        """Return MA titles of ``uris`` (falls back to the URI without MA)."""
        labels = {u: u for u in uris}
        ma = async_get_data(self.hass).ma
        if ma is None or not uris:
            return labels
        try:
            async with asyncio.timeout(WAKE_LABEL_TIMEOUT):
                playlists = await ma.library("playlist")
        except (MaUnavailable, InvalidRequest, TimeoutError):
            return labels
        for item in playlists:
            if item.uri in labels and item.title:
                labels[item.uri] = item.title
        return labels

    async def async_step_alarm_extras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the wake lamp, the stop button and the wake-up music."""
        if user_input is not None:
            for key in ("wake_lamp_entity", "stop_button_entity", "wake_media_uri"):
                if user_input.get(key):
                    self._options[key] = user_input[key]
                else:
                    self._options.pop(key, None)
            return self._save()
        schema: dict[Any, Any] = {
            _suggested(
                "wake_lamp_entity", self._options.get("wake_lamp_entity")
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="light")),
            _suggested(
                "stop_button_entity", self._options.get("stop_button_entity")
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=STOP_BUTTON_DOMAINS)
            ),
        }
        playlists = list(self._options.get("playlists") or [])
        if playlists:
            labels = await self._playlist_labels(playlists)
            current = self._options.get("wake_media_uri")
            schema[
                _suggested("wake_media_uri", current if current in labels else None)
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[{"value": u, "label": t} for u, t in labels.items()],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        return self.async_show_form(step_id="alarm_extras", data_schema=vol.Schema(schema))

    async def async_step_content(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the allowed playlists, albums/audiobooks and podcasts."""
        if user_input is not None and user_input:
            for key in CONTENT_LIBRARIES:
                self._options[key] = list(user_input.get(key) or [])
            return self._save()
        ma = async_get_data(self.hass).ma
        try:
            if ma is None or not ma.available:
                raise MaUnavailable("Music Assistant is not available")
            async with asyncio.timeout(LIBRARY_TIMEOUT):
                libraries = {
                    key: [
                        item
                        for lib in await asyncio.gather(*(ma.library(k) for k in kinds))
                        for item in lib
                    ]
                    for key, kinds in CONTENT_LIBRARIES.items()
                }
        except (MaUnavailable, TimeoutError):
            return self.async_show_form(
                step_id="content",
                data_schema=vol.Schema({}),
                errors={"base": "ma_unavailable"},
            )
        schema: dict[Any, Any] = {}
        for key, items in libraries.items():
            current = list(self._options.get(key) or [])
            options = [{"value": i.uri, "label": i.title or i.uri} for i in items]
            known = {i.uri for i in items}
            # keep selected items that MA no longer lists, so saving keeps them
            options += [{"value": u, "label": u} for u in current if u not in known]
            schema[vol.Optional(key, default=current)] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        return self.async_show_form(step_id="content", data_schema=vol.Schema(schema))
