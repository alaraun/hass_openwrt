from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType

from .constants import DOMAIN, PLATFORMS
from .coordinator import DeviceCoordinator, new_coordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type OpenWrtConfigEntry = ConfigEntry[DeviceCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: OpenWrtConfigEntry) -> bool:
    device = new_coordinator(hass, entry)
    await device.async_config_entry_first_refresh()
    entry.runtime_data = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenWrtConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _devices(hass: HomeAssistant, entry_ids) -> dict[str, DeviceCoordinator]:
    """Loaded coordinators of the targeted config entries."""
    result = {}
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == DOMAIN and isinstance(
            getattr(entry, "runtime_data", None), DeviceCoordinator
        ):
            result[entry_id] = entry.runtime_data
    return result


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:

    async def async_reboot(call: ServiceCall) -> None:
        ids = await service.async_extract_config_entry_ids(call)
        for device in _devices(hass, ids).values():
            await device.do_reboot()

    async def async_exec(call: ServiceCall):
        parts = call.data["command"].split(" ")
        ids = await service.async_extract_config_entry_ids(call)
        response = {}
        for entry_id, device in _devices(hass, ids).items():
            if device.is_api_supported("file"):
                args = parts[1:]
                if "arguments" in call.data:
                    args = call.data["arguments"].strip().split("\n")
                response[entry_id] = await device.do_file_exec(
                    parts[0],
                    args,
                    call.data.get("environment", {}),
                    call.data.get("extra", {}),
                )
        if len(ids) == 1:
            return response.get(list(ids)[0]) or {}
        return response

    async def async_init(call: ServiceCall) -> None:
        parts = call.data["name"].split(" ")
        ids = await service.async_extract_config_entry_ids(call)
        for device in _devices(hass, ids).values():
            if device.is_api_supported("rc"):
                await device.do_rc_init(parts[0], call.data.get("action", {}))

    async def async_ubus(call: ServiceCall):
        response = {}
        ids = await service.async_extract_config_entry_ids(call)
        for entry_id, device in _devices(hass, ids).items():
            response[entry_id] = await device.do_ubus_call(
                call.data["subsystem"],
                call.data["method"],
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
