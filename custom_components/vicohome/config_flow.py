"""Config flow for VicoHome."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import VicoApi, VicoAuthError, VicoError
from .const import (
    CONF_ARCHIVE_EVENTS,
    CONF_RECORD_LIVE,
    CONF_RETENTION_DAYS,
    DEFAULT_ARCHIVE_EVENTS,
    DEFAULT_RECORD_LIVE,
    DEFAULT_RETENTION_DAYS,
    MAX_RETENTION_DAYS,
    CONF_COUNTRY,
    CONF_LANGUAGE,
    CONF_REGION,
    DEFAULT_COUNTRY,
    DEFAULT_LANGUAGE,
    DEFAULT_REGION,
    DOMAIN,
    REGIONS,
)

USER_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
    vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    vol.Required(CONF_REGION, default=DEFAULT_REGION): SelectSelector(SelectSelectorConfig(options=REGIONS)),
    vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): str,
    vol.Required(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): str,
})


class VicoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with a VicoHome account (preferably a secondary one with shared cameras)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> VicoOptionsFlow:
        return VicoOptionsFlow()

    async def _try_login(self, data: dict) -> tuple[dict[str, str], int]:
        api = VicoApi(
            async_get_clientsession(self.hass),
            data[CONF_EMAIL],
            data[CONF_PASSWORD],
            data.get(CONF_REGION, DEFAULT_REGION),
            data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
            data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        )
        try:
            await api.login()
            devices = await api.devices()
        except VicoAuthError:
            return {"base": "invalid_auth"}, 0
        except (VicoError, aiohttp.ClientError, TimeoutError):
            return {"base": "cannot_connect"}, 0
        return {}, len(devices)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()
            errors, count = await self._try_login(user_input)
            if not errors and count == 0:
                errors = {"base": "no_devices"}
            if not errors:
                return self.async_create_entry(title=user_input[CONF_EMAIL], data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            errors, _ = await self._try_login(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


OPTIONS_SCHEMA = vol.Schema({
    vol.Required(CONF_ARCHIVE_EVENTS, default=DEFAULT_ARCHIVE_EVENTS): BooleanSelector(),
    vol.Required(CONF_RECORD_LIVE, default=DEFAULT_RECORD_LIVE): BooleanSelector(),
    vol.Required(CONF_RETENTION_DAYS, default=DEFAULT_RETENTION_DAYS): NumberSelector(NumberSelectorConfig(
        min=1, max=MAX_RETENTION_DAYS, step=1, mode=NumberSelectorMode.BOX,
    )),
})


class VicoOptionsFlow(OptionsFlowWithReload):
    """Recording options: what to keep on the HA host and for how long."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_RETENTION_DAYS] = int(user_input[CONF_RETENTION_DAYS])
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, self.config_entry.options),
        )
