"""Wi-Fi clients across every OpenWrt router of this Home Assistant instance.

A client has one tracker (and one signal sensor) for all routers: it is owned by
the config entry that first saw it, and its state merges the client lists of every
loaded entry. A router whose poll fails keeps its last client list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.start import async_at_started

from .constants import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import DeviceCoordinator

SIGNAL_CLIENTS_UPDATED = f"{DOMAIN}_clients_updated"
UNIQUE_ID_PREFIX = "openwrt.client."
_CLAIMS = f"{DOMAIN}_client_claims"


@dataclass(frozen=True, slots=True)
class ClientSeen:
    router: str
    interface: str
    ssid: str | None
    signal: int | None


def coordinators(hass: HomeAssistant) -> list[DeviceCoordinator]:
    from .coordinator import DeviceCoordinator

    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if isinstance(getattr(entry, "runtime_data", None), DeviceCoordinator)
    ]


@callback
def connected_clients(hass: HomeAssistant) -> dict[str, ClientSeen]:
    """Connected clients by formatted MAC; the strongest signal wins a duplicate."""
    result: dict[str, ClientSeen] = {}
    for device in coordinators(hass):
        for ifname, iface in (device.data or {}).get("wireless", {}).items():
            for mac, info in iface.get("macs", {}).items():
                seen = ClientSeen(device.device_id, ifname, iface.get("ssid"), info.get("signal"))
                mac = format_mac(mac)
                old = result.get(mac)
                if old is None or (seen.signal or -999) > (old.signal or -999):
                    result[mac] = seen
    return result


@callback
def host_hint(hass: HomeAssistant, mac: str) -> tuple[str | None, str | None]:
    """(IP, host name) of `mac` from any router's host hints."""
    ip = name = None
    for device in coordinators(hass):
        for hint_mac, hint in (device.data or {}).get("hosts", {}).items():
            if format_mac(hint_mac) != mac:
                continue
            ip = ip or hint.get("ip") or None
            name = name or hint.get("name") or None
    return ip, name


@callback
def owned_macs(hass: HomeAssistant, entry: ConfigEntry, domain: str) -> set[str]:
    """MACs of this entry's registered client entities of `domain`; claims them."""
    claims = hass.data.setdefault(_CLAIMS, {})
    macs = set()
    for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if reg.domain == domain and reg.unique_id.startswith(UNIQUE_ID_PREFIX):
            mac = reg.unique_id.removeprefix(UNIQUE_ID_PREFIX).split(".")[0]
            macs.add(mac)
            claims[(domain, mac)] = entry.entry_id
    return macs


@callback
def claim(hass: HomeAssistant, entry: ConfigEntry, domain: str, mac: str, unique_id: str) -> bool:
    """Claim a client entity for `entry` unless another entry owns it."""
    claims = hass.data.setdefault(_CLAIMS, {})
    owner = claims.get((domain, mac))
    # an owner deleted while not loaded released nothing: its claim is stale
    if owner is not None and hass.config_entries.async_get_entry(owner) is not None:
        return owner == entry.entry_id
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    if entity_id is not None:
        reg = er.async_get(hass).async_get(entity_id)
        if reg is not None and reg.config_entry_id != entry.entry_id:
            claims[(domain, mac)] = reg.config_entry_id
            return False
    claims[(domain, mac)] = entry.entry_id
    return True


@callback
def release_claims(hass: HomeAssistant, entry: ConfigEntry) -> None:
    claims = hass.data.get(_CLAIMS, {})
    for key in [k for k, owner in claims.items() if owner == entry.entry_id]:
        del claims[key]


class ClientEntity(Entity):
    """An entity of one Wi-Fi client, updated from every router's poll.

    Unavailable until Home Assistant has started, so that a client seen by a router
    whose entry loads later does not show as gone in between.
    """

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, mac: str) -> None:
        self._mac = mac
        self._seen: ClientSeen | None = None
        # known before registration, so the entity id is made from the host name
        self._host_name = host_hint(hass, mac)[1]

    @property
    def available(self) -> bool:
        return self.hass.state is CoreState.running

    @callback
    def _async_refresh(self) -> bool:
        """Read the merged client data; return True if the shown values changed."""
        raise NotImplementedError

    @callback
    def _async_handle_update(self) -> None:
        if self._async_refresh():
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._async_refresh()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_CLIENTS_UPDATED, self._async_handle_update)
        )
        if self.hass.state is not CoreState.running:
            self.async_on_remove(async_at_started(self.hass, self._async_write_on_start))

    @callback
    def _async_write_on_start(self, _hass: HomeAssistant) -> None:
        self._async_refresh()
        self.async_write_ha_state()


@callback
def async_setup_client_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    domain: str,
    unique_id: Callable[[str], str],
    factory: Callable[[HomeAssistant, str], Entity],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add this entry's client entities: registered ones, then each new client
    connected to this entry's router that no other entry owns."""
    device: DeviceCoordinator = entry.runtime_data
    known = owned_macs(hass, entry, domain)
    async_add_entities(factory(hass, mac) for mac in sorted(known))

    @callback
    def _add_new() -> None:
        new = []
        for iface in (device.data or {}).get("wireless", {}).values():
            for raw_mac in iface.get("macs", {}):
                mac = format_mac(raw_mac)
                # a client another entry owns is tried again on every poll: that
                # entry may be deleted
                if mac in known or not claim(hass, entry, domain, mac, unique_id(mac)):
                    continue
                known.add(mac)
                new.append(factory(hass, mac))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_CLIENTS_UPDATED, _add_new))
