from homeassistant import config_entries
import homeassistant.helpers.config_validation as cv
from .constants import DOMAIN
from .coordinator import new_ubus_client

import logging
import voluptuous as vol

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


class OpenWrtConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    @staticmethod
    def _build_reconfigure_schema(data):
        """Return a vol.Schema pre-filled from entry data for the reconfigure step.

        Address and id are intentionally excluded — they are preserved verbatim
        from the existing entry so the router unique_id and device title stay stable.
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

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()

        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=self._build_reconfigure_schema(entry.data),
                description_placeholders={"address": entry.data.get("address", "")},
            )

        errors = {}
        merged_config = {**entry.data, **user_input}

        try:
            ubus = new_ubus_client(self.hass, merged_config)
            await ubus.login()
        except Exception as err:
            _LOGGER.error("Failed to connect to OpenWrt device during reconfigure: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=self._build_reconfigure_schema(entry.data),
                description_placeholders={"address": entry.data.get("address", "")},
                errors=errors,
            )

        self.hass.config_entries.async_update_entry(entry, data=merged_config)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reconfigure_successful")

    async def async_step_reauth(self, user_input):
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        _LOGGER.debug("Config flow input: address=%s", user_input.get("address"))
        errors = {}

        try:
            ubus = new_ubus_client(self.hass, user_input)
            await ubus.login()
        except Exception as err:
            _LOGGER.error("Failed to connect to OpenWrt device: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_DATA_SCHEMA,
                errors=errors,
            )

        await self.async_set_unique_id(user_input["address"])
        self._abort_if_unique_id_configured()

        title = "%s - %s" % (user_input["id"], user_input["address"])
        return self.async_create_entry(title=title, data=user_input)
