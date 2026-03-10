from __future__ import annotations
from .constants import DOMAIN, PLATFORMS

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType

import homeassistant.helpers.config_validation as cv

import voluptuous as vol
import logging

from .coordinator import new_coordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
    }),
}, extra=vol.ALLOW_EXTRA)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    device = new_coordinator(hass, entry.data)

    entry.runtime_data = device

    await device.coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    entry.runtime_data = None

    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:

    async def async_reboot(call):
        for entry_id in await service.async_extract_config_entry_ids(call):
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry and entry.runtime_data:
                await entry.runtime_data.do_reboot()

    async def async_exec(call):
        parts = call.data["command"].split(" ")
        ids = await service.async_extract_config_entry_ids(call)
        response = {}
        for entry_id in ids:
            entry = hass.config_entries.async_get_entry(entry_id)
            coordinator = entry.runtime_data if entry else None
            if coordinator and coordinator.is_api_supported("file"):
                args = parts[1:]
                if "arguments" in call.data:
                    args = call.data["arguments"].strip().split("\n")
                response[entry_id] = await coordinator.do_file_exec(
                    parts[0],
                    args,
                    call.data.get("environment", {}),
                    call.data.get("extra", {}),
                )
        if len(ids) == 1:
            return response.get(list(ids)[0]) or {}
        return response

    async def async_init(call):
        parts = call.data["name"].split(" ")
        for entry_id in await service.async_extract_config_entry_ids(call):
            entry = hass.config_entries.async_get_entry(entry_id)
            device = entry.runtime_data if entry else None
            if device and device.is_api_supported("rc"):
                await device.do_rc_init(parts[0], call.data.get("action", {}))

    async def async_ubus(call):
        response = {}
        ids = await service.async_extract_config_entry_ids(call)
        for entry_id in ids:
            entry = hass.config_entries.async_get_entry(entry_id)
            coordinator = entry.runtime_data if entry else None
            if coordinator:
                response[entry_id] = await coordinator.do_ubus_call(
                    call.data.get("subsystem"),
                    call.data.get("method"),
                    call.data.get("parameters", {}),
                )
        if len(ids) == 1:
            return response.get(list(ids)[0]) or {}
        return response

    hass.services.async_register(DOMAIN, "reboot", async_reboot)
    hass.services.async_register(DOMAIN, "exec", async_exec, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "init", async_init)
    hass.services.async_register(DOMAIN, "ubus", async_ubus, supports_response=SupportsResponse.ONLY)

    return True


from homeassistant.helpers.update_coordinator import CoordinatorEntity


class OpenWrtEntity(CoordinatorEntity):
    def __init__(self, device, device_id: str):
        super().__init__(device.coordinator)
        self._device_id = device_id
        self._device = device

    @property
    def device_info(self):
        return {
            "identifiers": {("id", self._device_id)},
            "name": f"OpenWrt [{self._device_id}]",
            "model": self.data["info"]["model"],
            "manufacturer": self.data["info"]["manufacturer"],
            "sw_version": self.data["info"]["sw_version"],
        }

    @property
    def name(self):
        return "OpenWrt [%s]" % self._device_id

    @property
    def unique_id(self):
        return "sensor.openwrt.%s" % self._device_id

    @property
    def data(self) -> dict:
        return self.coordinator.data
