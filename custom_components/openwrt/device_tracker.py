"""One tracker per Wi-Fi client: connected to any of the routers, or not."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OpenWrtConfigEntry
from .clients import (
    UNIQUE_ID_PREFIX,
    ClientEntity,
    async_setup_client_entities,
    connected_clients,
    coordinators,
    host_hint,
)


def tracker_unique_id(mac: str) -> str:
    return f"{UNIQUE_ID_PREFIX}{mac}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenWrtConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_setup_client_entities(
        hass, entry, "device_tracker", tracker_unique_id, OpenWrtClientTracker, async_add_entities
    )


class OpenWrtClientTracker(ClientEntity, ScannerEntity):
    """Home while the client is associated to an access point of any router.

    Created disabled, and without a device of its own (one per client would fill
    the device list). Attributes name the router and SSID it is on.

    Routers poll at different times, so a client that roams leaves one list before
    it appears in the next. When the client leaves the router that had it, every
    other router polls once before the tracker shows `not_home`.
    """

    _attr_device_info = DeviceInfo()

    def __init__(self, hass: HomeAssistant, mac: str) -> None:
        ClientEntity.__init__(self, hass, mac)
        self._attr_mac_address = mac
        self._attr_hostname = self._host_name
        # router id -> (its coordinator, polls it had started when the client left)
        self._confirming: dict[str, tuple[Any, int]] | None = None

    @property
    def unique_id(self) -> str:
        return tracker_unique_id(self._mac)

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return self._attr_hostname or self._mac

    @property
    def is_connected(self) -> bool:
        return self._seen is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._seen is None:
            return {}
        return {"router": self._seen.router, "ssid": self._seen.ssid}

    @callback
    def _gone_confirmed(self, seen) -> bool:
        """False while a confirming poll of another router is outstanding.

        A router confirms with a poll started after the client left; one whose last
        poll failed, or that was reloaded (a new coordinator), is not waited for. A
        router with no poll started since then is asked again (the debouncer drops a
        request made while a poll runs).
        """
        if seen is not None:
            self._confirming = None
            return True
        current = {c.device_id: c for c in coordinators(self.hass)}
        if self._confirming is None:
            if self._seen is None:
                return True
            self._confirming = {
                rid: (c, c.polls_started)
                for rid, c in current.items()
                if rid != self._seen.router
            }
        pending = False
        for rid, (c, started) in self._confirming.items():
            if current.get(rid) is not c or not c.last_update_success or c.data_poll > started:
                continue
            pending = True
            if c.polls_started <= started:
                # not eager: every tracker of this dispatch takes its snapshot first
                c.config_entry.async_create_background_task(
                    self.hass, c.async_request_refresh(), "openwrt confirm", eager_start=False
                )
        if pending:
            return False
        self._confirming = None
        return True

    @callback
    def _async_refresh(self) -> bool:
        seen = connected_clients(self.hass).get(self._mac)
        if not self._gone_confirmed(seen):
            return False
        ip, name = host_hint(self.hass, self._mac)
        old = (self._seen and (self._seen.router, self._seen.ssid), self._attr_ip_address, self._attr_hostname)
        self._seen = seen
        if ip:
            self._attr_ip_address = ip
        if name:
            self._attr_hostname = name
        new = (seen and (seen.router, seen.ssid), self._attr_ip_address, self._attr_hostname)
        return new != old
