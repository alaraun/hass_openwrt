"""Diagnostics of an OpenWrt config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import OpenWrtConfigEntry

# credentials, modem identities, and the LAN's clients (diagnostics are often posted)
TO_REDACT = {"password", "username", "imei", "iccid", "hosts", "macs"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OpenWrtConfigEntry
) -> dict[str, Any]:
    device = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "apis": device.ubus.acls.get("ubus", {}),
        "wireless_via_uci": device.wireless_via_uci,
        "last_update_success": device.last_update_success,
        "data": async_redact_data(device.data or {}, TO_REDACT),
    }
