"""Config flow for KidsHub."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
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
from .pairing import PAIRING_TTL, PendingPairing

PAIRING_TIMEOUT: float = PAIRING_TTL

CONF_TTS_ENGINE = "tts_engine"
CONF_TTS_LANGUAGE = "tts_language"
CONF_TTS_OPTIONS = "tts_options"


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

    @callback
    def async_remove(self) -> None:
        """Cancel an unfinished pairing when the flow goes away."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._pending is not None and not self._pending.done.is_set():
            async_get_data(self.hass).pairing.cancel(self._pending.code)
