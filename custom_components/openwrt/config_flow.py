from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

from .constants import DOMAIN
from .coordinator import new_ubus_client
from .ubus import UbusAuthError, UbusError

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema({
    vol.Required("id"): cv.string,
    vol.Required("address"): cv.string,
    vol.Required("username"): cv.string,
    vol.Optional("password"): cv.string,
    vol.Required("https", default=False): cv.boolean,
    vol.Required("verify_cert", default=False): cv.boolean,
    vol.Optional("port", default=0): cv.positive_int,
    vol.Optional("path", default="/ubus"): cv.string,
    vol.Required("interval", default=30): cv.positive_int,
    vol.Required("wps", default=False): cv.boolean,
    vol.Optional("wan_devices"): cv.string,
    vol.Optional("wifi_devices"): cv.string,
    vol.Optional("mesh_devices"): cv.string,
})

STEP_REAUTH_DATA_SCHEMA = vol.Schema({
    vol.Required("username"): cv.string,
    vol.Optional("password"): cv.string,
})


class OpenWrtConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    async def _async_try_login(self, config: Mapping[str, Any]) -> str | None:
        """Log in with `config`; return an error key, or None on success."""
        try:
            await new_ubus_client(self.hass, config).login()
        except UbusAuthError as err:
            _LOGGER.warning("Login to %s rejected: %s", config.get("address"), err)
            return "invalid_auth"
        except UbusError as err:
            _LOGGER.warning("Failed to connect to %s: %s", config.get("address"), err)
            return "cannot_connect"
        return None

    @staticmethod
    def _build_reconfigure_schema(data):
        """Return a vol.Schema pre-filled from entry data for the reconfigure step.

        Address and id are excluded: they keep the entry's unique id and device title.
        """
        return vol.Schema({
            vol.Required("username", default=data.get("username", "")): cv.string,
            vol.Optional("password"): cv.string,
            vol.Required("https", default=data.get("https", False)): cv.boolean,
            vol.Required("verify_cert", default=data.get("verify_cert", False)): cv.boolean,
            vol.Optional("port", default=data.get("port", 0)): cv.positive_int,
            vol.Optional("path", default=data.get("path", "/ubus")): cv.string,
            vol.Required("interval", default=data.get("interval", 30)): cv.positive_int,
            vol.Required("wps", default=data.get("wps", False)): cv.boolean,
            vol.Optional("wan_devices"): cv.string,
            vol.Optional("wifi_devices"): cv.string,
            vol.Optional("mesh_devices"): cv.string,
        })

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            merged_config = {**entry.data, **user_input}
            if not (error := await self._async_try_login(merged_config)):
                return self.async_update_reload_and_abort(entry, data=merged_config)
            errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._build_reconfigure_schema(entry.data),
            description_placeholders={"address": entry.data.get("address", "")},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            merged_config = {**entry.data, **user_input}
            if not (error := await self._async_try_login(merged_config)):
                return self.async_update_reload_and_abort(entry, data=merged_config)
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_DATA_SCHEMA, {"username": entry.data.get("username", "")}
            ),
            description_placeholders={"address": entry.data.get("address", "")},
            errors=errors,
        )

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input["address"])
            self._abort_if_unique_id_configured()
            if not (error := await self._async_try_login(user_input)):
                title = "%s - %s" % (user_input["id"], user_input["address"])
                return self.async_create_entry(title=title, data=user_input)
            errors["base"] = error

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )
