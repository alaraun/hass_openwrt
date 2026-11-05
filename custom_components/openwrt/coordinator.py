"""Polling coordinator for one OpenWrt device."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.json import json_loads

from .constants import DOMAIN
from .ubus import (
    Ubus,
    UbusAuthError,
    UbusConnectionError,
    UbusRPCError,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Errors of an optional data source: the call was answered, but with an error,
# or with data of an unexpected shape. Connection and login errors fail the poll.
OPTIONAL_ERRORS = (UbusRPCError, KeyError, TypeError, ValueError, AttributeError, IndexError)


class DeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches all data of one device per poll and runs its commands."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ubus: Ubus) -> None:
        config = entry.data
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"openwrt {config['id']}",
            update_interval=timedelta(seconds=config.get("interval", 30)),
        )
        self._config = config
        self._ubus = ubus
        self._id: str = config["id"]
        self._apis: dict[str, Any] | None = None
        self._wps = config.get("wps", False)
        # True after network.wireless status failed (rpcd on OpenWrt 25.12 answers status 2)
        self._wireless_via_uci = False
        self._cached_board_info: dict | None = None
        self._last_uptime: int = 0
        # completed polls, failed ones too (client trackers wait for one)
        self.poll_count = 0

    @property
    def device_id(self) -> str:
        return self._id

    @property
    def ubus(self) -> Ubus:
        return self._ubus

    @property
    def wireless_via_uci(self) -> bool:
        return self._wireless_via_uci

    def _configured_devices(self, config_name: str) -> list[str]:
        value = self._config.get(config_name, "")
        if value == "":
            return []
        return [x.strip() for x in value.split(",")]

    async def discover_wireless_uci(self) -> dict:
        result: dict[str, list] = dict(ap=[], mesh=[])
        wifi_devices = self._configured_devices("wifi_devices")
        if not self.is_api_supported("uci", "get"):
            _LOGGER.debug("Device [%s] doesn't support uci.get", self._id)
            return result

        try:
            response = await self._ubus.api_call("uci", "get", dict(config="wireless"))
            _LOGGER.debug("UCI wireless config response: %s", response)
            values = response.get("values", {})

            # Build a mapping of device -> disabled to filter radios
            device_disabled = {}
            for data in values.values():
                if data.get(".type") == "wifi-device":
                    device_name = data.get(".name")
                    disabled = data.get("disabled", "0")
                    device_disabled[device_name] = disabled in ["1", "true", True]

            # Generate ifnames for sections without an explicit 'ifname'.
            # OpenWrt 25.12+ with ucode wifi-scripts no longer stores 'option ifname';
            # the ifname is generated at runtime as phy{N}-{mode}{idx} (e.g. phy0-ap0).
            generated_ifnames: dict = {}
            device_mode_count: dict = {}
            for _section, _data in sorted(
                values.items(), key=lambda x: x[1].get(".index", 999)
            ):
                if _data.get(".type") != "wifi-iface" or _data.get("ifname"):
                    continue
                _device = _data.get("device", "")
                _mode = _data.get("mode", "ap")
                radio_num = "".join(c for c in _device if c.isdigit()) or "0"
                counters = device_mode_count.setdefault(_device, {})
                idx = counters.get(_mode, 0)
                counters[_mode] = idx + 1
                generated_ifnames[_section] = f"phy{radio_num}-{_mode}{idx}"

            for section, data in values.items():
                if data.get(".type") != "wifi-iface":
                    continue

                device = data.get("device")
                if not device:
                    _LOGGER.debug("wifi-iface %s has no device", section)
                    continue

                if device_disabled.get(device, False):
                    _LOGGER.debug("Device %s is disabled, skipping interface %s", device, section)
                    continue

                ifname = data.get("ifname") or generated_ifnames.get(section)
                if not ifname:
                    _LOGGER.debug("iface %s has no ifname and could not generate one", section)
                    continue

                network = data.get("network")
                if isinstance(network, list):
                    network = network[0] if network else ""

                conf = dict(ifname=ifname, network=network, device=device)
                mode = data.get("mode")

                if mode == "ap":
                    ssid = data.get("ssid")
                    if ssid:
                        conf["ssid"] = ssid
                    else:
                        _LOGGER.debug("SSID of %s not found", ifname)

                    if len(wifi_devices) and ifname not in wifi_devices:
                        _LOGGER.debug("Interface %s is not in wifi_devices, skipping", ifname)
                        continue

                    result["ap"].append(conf)

                elif mode == "mesh":
                    mesh_id = (
                        data.get("mesh_id") or data.get("mesh") or data.get("ssid") or None
                    )
                    if mesh_id:
                        conf["mesh_id"] = mesh_id
                    else:
                        _LOGGER.debug("mesh_id not found for %s", ifname)
                    result["mesh"].append(conf)

        except OPTIONAL_ERRORS as err:
            _LOGGER.warning(
                "Device [%s] doesn't support wireless (uci get) or parse failed: %s",
                self._id, err,
            )
        return result

    async def discover_wireless(self) -> dict:
        """Discover wireless interfaces using the network.wireless API."""
        result: dict[str, list] = dict(ap=[], mesh=[])
        if not self.is_api_supported("network.wireless"):
            return result
        wifi_devices = self._configured_devices("wifi_devices")
        response = await self._ubus.api_call("network.wireless", "status", {})
        _LOGGER.debug("Wireless status response: %s", response)
        for item in response.values():
            if item.get("disabled", False):
                continue
            for iface in item["interfaces"]:
                if "ifname" not in iface:
                    _LOGGER.debug("iface %s has no ifname", iface)
                    continue
                network_list = iface["config"].get("network", [])
                network = network_list[0] if network_list else ""
                conf = dict(
                    ifname=iface["ifname"],
                    network=network,
                )
                if iface["config"]["mode"] == "ap":
                    ssid = iface["config"].get("ssid")
                    if ssid:
                        conf["ssid"] = ssid
                    else:
                        _LOGGER.debug("SSID of %s not found", iface["ifname"])
                    if len(wifi_devices) and iface["ifname"] not in wifi_devices:
                        _LOGGER.debug(
                            "Interface %s not in wifi_devices, skipping", iface["ifname"]
                        )
                        continue
                    result["ap"].append(conf)
                if iface["config"]["mode"] == "mesh":
                    mesh_id = iface["config"].get("mesh_id")
                    if mesh_id:
                        conf["mesh_id"] = mesh_id
                    else:
                        _LOGGER.debug("mesh_id not found for %s", iface["ifname"])
                    result["mesh"].append(conf)
        return result

    def find_mesh_peers(self, mesh_id: str) -> list[str]:
        result = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            device = getattr(entry, "runtime_data", None)
            if not isinstance(device, DeviceCoordinator):
                continue
            data = device.data
            if not data or not data.get("mesh"):
                continue
            for mesh in data["mesh"].values():
                if mesh["id"] == mesh_id:
                    result.append(mesh["mac"])
        return result

    async def update_mesh(self, configs) -> dict:
        """Update mesh information."""
        mesh_devices = self._configured_devices("mesh_devices")
        result: dict[str, Any] = dict()
        if not (
            self.is_api_supported("iwinfo", "info")
            and self.is_api_supported("iwinfo", "assoclist")
        ):
            return result
        try:
            for conf in configs:
                if len(mesh_devices) and conf["ifname"] not in mesh_devices:
                    continue
                info = await self._ubus.api_call(
                    "iwinfo", "info", dict(device=conf["ifname"])
                )
                peers: dict[str, Any] = {}
                result[conf["ifname"]] = dict(
                    mac=info["bssid"].lower(),
                    signal=info.get("signal", -100),
                    id=conf["mesh_id"],
                    noise=info.get("noise", 0),
                    bitrate=info.get("bitrate", -1),
                    peers=peers,
                )
                peer_macs = self.find_mesh_peers(conf["mesh_id"])
                tasks = [
                    self._ubus.api_call(
                        "iwinfo", "assoclist", dict(device=conf["ifname"], mac=mac)
                    )
                    for mac in peer_macs
                ]
                assoc_results = await asyncio.gather(*tasks, return_exceptions=True)
                for mac, assoc in zip(peer_macs, assoc_results, strict=True):
                    if isinstance(assoc, (UbusConnectionError, UbusAuthError)):
                        raise assoc
                    if isinstance(assoc, BaseException):
                        _LOGGER.warning(
                            "Failed to get assoclist for peer %s on %s: %s",
                            mac, conf["ifname"], assoc,
                        )
                        continue
                    peers[mac] = dict(
                        active=assoc.get("mesh plink") == "ESTAB",
                        signal=assoc.get("signal", -100),
                        noise=assoc.get("noise", 0),
                    )
        except OPTIONAL_ERRORS as err:
            _LOGGER.warning("Device [%s] doesn't support iwinfo or parse failed: %s", self._id, err)
        return result

    async def update_hostapd_clients(self, interface_id: str) -> dict:
        """Update hostapd clients for a specific interface."""
        try:
            response = await self._ubus.api_call(
                f"hostapd.{interface_id}", "get_clients", {}
            )
            _LOGGER.debug("Hostapd clients response for %s: %s", interface_id, response)
            if not response:
                # no hostapd object: the interface is down or not an AP
                return {}
            if "clients" not in response:
                _LOGGER.warning(
                    "'clients' key not found in response for %s. Response: %s",
                    interface_id, response,
                )
            clients = response.get("clients", {})

            macs = {key: dict(signal=value.get("signal")) for key, value in clients.items()}
            result: dict[str, Any] = dict(clients=len(macs), macs=macs)

            if self._wps:
                try:
                    wps_response = await self._ubus.api_call(
                        f"hostapd.{interface_id}", "wps_status", {}
                    )
                    result["wps"] = wps_response.get("pbc_status") == "Active"
                except OPTIONAL_ERRORS as err:
                    _LOGGER.warning(
                        "Interface [%s] doesn't support WPS or parse failed: %s", interface_id, err
                    )
            return result
        except OPTIONAL_ERRORS as err:
            _LOGGER.warning("Error updating hostapd clients for %s: %s", interface_id, err)
            return {}

    async def set_wps(self, interface_id: str, enable: bool) -> None:
        await self._ubus.api_call(
            f"hostapd.{interface_id}",
            "wps_start" if enable else "wps_cancel",
            {},
        )
        await self.async_request_refresh()

    async def do_reboot(self) -> None:
        _LOGGER.debug("Rebooting device: %s", self._id)
        await self._ubus.api_call("system", "reboot", {})

    async def do_file_exec(self, command: str, params, env: dict, extra: dict) -> dict:
        _LOGGER.debug("Executing command %s: %s with %s env=%s", self._id, command, params, env)
        result = await self._ubus.api_call(
            "file",
            "exec",
            dict(command=command, params=params, env=env) if env else dict(command=command, params=params),
        )
        _LOGGER.debug("Execute result %s: %s", self._id, result)
        self.hass.bus.async_fire(
            "openwrt_exec_result",
            {
                "address": self._config.get("address"),
                "id": self._config.get("id"),
                "command": command,
                "code": result.get("code", 1),
                "stdout": result.get("stdout", ""),
                **extra,
            },
        )

        def process_output(data: str):
            try:
                parsed = json_loads(data)
                if isinstance(parsed, (list, dict)):
                    return parsed
            except ValueError:
                pass  # non-JSON output is returned as lines
            return data.strip().split("\n")

        return {
            "code": result.get("code", 1),
            "stdout": process_output(result.get("stdout", "")),
            "stderr": process_output(result.get("stderr", "")),
        }

    async def do_ubus_call(self, subsystem: str, method: str, params: dict) -> dict:
        _LOGGER.debug("do_ubus_call(): %s / %s: %s", subsystem, method, params)
        return await self._ubus.api_call(subsystem, method, params)

    async def do_rc_init(self, name: str, action: str) -> None:
        _LOGGER.debug("Executing rc init %s: %s with %s", self._id, name, action)
        result = await self._ubus.api_call("rc", "init", dict(name=name, action=action))
        _LOGGER.debug("Execute result %s: %s", self._id, result)
        self.hass.bus.async_fire(
            "openwrt_init_result",
            {
                "address": self._config.get("address"),
                "id": self._config.get("id"),
                "name": name,
                "code": result.get("code", 1),
                "stdout": result.get("stdout", ""),
            },
        )

    async def update_ap(self, configs) -> dict:
        result = dict()
        for item in configs:
            if "ifname" not in item:
                _LOGGER.warning("Missing 'ifname' in AP config: %s", item)
                continue
            ifname = item["ifname"]
            clients_info = await self.update_hostapd_clients(ifname)
            if not clients_info:
                continue
            clients_info["ssid"] = item.get("ssid", ifname)
            result[ifname] = clients_info
        return result

    async def update_info(self) -> dict:
        """Get basic device information (system.board)."""
        try:
            response = await self._ubus.api_call("system", "board", {})
            release = response.get("release", {})
            return {
                "model": response.get("model"),
                "manufacturer": release.get("distribution"),
                "sw_version": f"{release.get('version', '')} {release.get('revision', '')}".strip(),
            }
        except OPTIONAL_ERRORS as err:
            _LOGGER.warning("Device [%s] failed to get board info: %s", self._id, err)
            return {}

    async def discover_mwan3(self) -> dict:
        """Read the status of enabled mwan3 interfaces."""
        if not self.is_api_supported("mwan3", "status"):
            return dict()
        result = dict()
        try:
            response = await self._ubus.api_call(
                "mwan3", "status", dict(section="interfaces")
            )
            for key, iface in response.get("interfaces", {}).items():
                if not iface.get("enabled", False):
                    continue
                result[key] = {
                    "offline_sec": iface.get("offline", 0),
                    "online_sec": iface.get("online", 0),
                    "uptime_sec": iface.get("uptime", 0),
                    "online": iface.get("status") == "online",
                    "status": iface.get("status"),
                    "up": iface.get("up"),
                }
        except OPTIONAL_ERRORS as err:
            _LOGGER.warning("Device [%s] failed to get mwan3 status: %s", self._id, err)
        return result

    async def update_wan_info(self) -> dict:
        result = dict()
        devices = self._configured_devices("wan_devices")
        for device_id in devices:
            try:
                response = await self._ubus.api_call(
                    "network.device", "status", dict(name=device_id)
                )
            except OPTIONAL_ERRORS as err:
                _LOGGER.warning(
                    "Device [%s] failed to get status of %s: %s", self._id, device_id, err
                )
                continue
            stats = response.get("statistics", {})
            _LOGGER.debug("WAN info: %s", response)
            result[device_id] = {
                "up": response.get("up", False),
                "rx_bytes": stats.get("rx_bytes", 0),
                "tx_bytes": stats.get("tx_bytes", 0),
                "speed": response.get("speed"),
                "mac": response.get("macaddr"),
            }
        return result

    async def fetch_host_hints(self) -> dict:
        """Fetch host hints from luci-rpc."""
        if not self.is_api_supported("luci-rpc", "getHostHints"):
            _LOGGER.debug("Device [%s] doesn't support luci-rpc.getHostHints", self._id)
            return {}
        try:
            response = await self._ubus.api_call("luci-rpc", "getHostHints", {})
            _LOGGER.debug("Host hints response: %s", response)
            hosts = {}
            for mac, data in response.items():
                ip_addresses = data.get("ipaddrs", [])
                hosts[mac] = {
                    "ip": ip_addresses[0] if ip_addresses else "",
                    "name": data.get("name", ""),
                    "mac": mac,
                }
            return hosts
        except OPTIONAL_ERRORS as err:
            _LOGGER.warning("Failed to get host hints for device [%s]: %s", self._id, err)
            return {}

    async def update_modem_stats(self) -> dict:
        """Read modem stats from /tmp/modem-stats.json via the file ubus object."""
        if not self.is_api_supported("file", "read"):
            return {}
        try:
            response = await self._ubus.api_call(
                "file", "read", {"path": "/tmp/modem-stats.json"}
            )
        except UbusRPCError as err:
            _LOGGER.debug(
                "Modem stats file not available on device [%s]: %s", self._id, err
            )
            return {}
        data_str = response.get("data", "")
        if not data_str:
            return {}
        try:
            parsed = json_loads(data_str)
        except ValueError as err:
            _LOGGER.warning(
                "Device [%s] modem-stats.json is malformed (partial write?): %s",
                self._id, err,
            )
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def update_system_info(self) -> dict:
        """Get system information: uptime, memory, load. Empty if not readable."""
        try:
            response = await self._ubus.api_call("system", "info", {})
        except UbusRPCError as err:
            _LOGGER.warning("Device [%s] failed to get system info: %s", self._id, err)
            return {}
        _LOGGER.debug("System info response: %s", response)
        if not response:
            return {}
        return {
            "uptime": response.get("uptime", 0),
            "load": response.get("load", [0, 0, 0]),
            "memory": response.get("memory", {}),
            "localtime": response.get("localtime", 0),
            "root": response.get("root", {}),
            "tmp": response.get("tmp", {}),
            "swap": response.get("swap", {}),
        }

    async def load_ubus(self) -> dict:
        """Log in if needed and return the session's ubus ACLs."""
        self._wireless_via_uci = False  # re-checked after each login; the firmware may have changed
        if not self._ubus.acls:
            await self._ubus.login()
        acls_ubus = self._ubus.acls.get("ubus", {})
        _LOGGER.debug("Available APIs: %s", list(acls_ubus.keys()) if acls_ubus else "none")
        return acls_ubus

    def is_api_supported(self, name: str, method: str | None = None) -> bool:
        """Check if an API (and optionally a method) is permitted by ACLs."""
        if not self._apis:
            return False
        if name not in self._apis:
            return False
        if method is None:
            return True
        return method in self._apis[name]

    async def _discover_wireless_config(self) -> dict:
        """Return wireless config using network.wireless or UCI fallback."""
        if self.is_api_supported("network.wireless") and not self._wireless_via_uci:
            try:
                return await self.discover_wireless()
            except OPTIONAL_ERRORS as err:
                # rpcd on OpenWrt 25.12 answers network.wireless status with ubus status 2
                _LOGGER.debug(
                    "Device [%s]: network.wireless status failed, using uci from now on: %s",
                    self._id, err,
                )
                self._wireless_via_uci = True
        return await self.discover_wireless_uci()

    async def _update_wireless(self):
        """Discover wireless config then fetch AP and mesh data concurrently."""
        wireless_config = await self._discover_wireless_config()
        return await asyncio.gather(
            self.update_ap(wireless_config["ap"]),
            self.update_mesh(wireless_config["mesh"]),
        )

    @callback
    def async_update_listeners(self) -> None:
        self.poll_count += 1
        super().async_update_listeners()

    async def _async_update_data(self) -> dict[str, Any]:
        if self.data is not None and self.hass.state is not CoreState.running:
            # Polls wait for Home Assistant to finish starting: on a busy host its
            # start delays the replies past the timeout. The first refresh runs.
            return self.data
        try:
            if not self._apis:
                self._apis = await self.load_ubus()

            system_info = await self.update_system_info()
            current_uptime = system_info.get("uptime", 0)

            if self._cached_board_info is None or (
                0 < current_uptime < self._last_uptime
            ):
                if fetched := await self.update_info():
                    self._cached_board_info = fetched

            if current_uptime > 0:
                self._last_uptime = current_uptime

            (wireless, mesh), mwan3, wan, hosts, modem = await asyncio.gather(
                self._update_wireless(),
                self.discover_mwan3(),
                self.update_wan_info(),
                self.fetch_host_hints(),
                self.update_modem_stats(),
            )
        except UbusAuthError as err:
            raise ConfigEntryAuthFailed(f"{self._id}: {err}") from err
        except UbusConnectionError as err:
            raise UpdateFailed(f"{self._id}: {err}") from err

        result = dict(
            info=self._cached_board_info or {},
            wireless=wireless,
            mesh=mesh,
            mwan3=mwan3,
            wan=wan,
            hosts=hosts,
            system_info=system_info,
            modem=modem,
        )
        _LOGGER.debug("Full update [%s]: %s", self._id, result)
        return result


def new_ubus_client(hass: HomeAssistant, config) -> Ubus:
    schema = "https" if config["https"] else "http"
    port = ":%d" % config["port"] if config["port"] > 0 else ""
    url = "%s://%s%s%s" % (schema, config["address"], port, config["path"])
    return Ubus(
        hass,
        url,
        config["username"],
        config.get("password", ""),
        verify=config.get("verify_cert", True),
    )


def new_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> DeviceCoordinator:
    return DeviceCoordinator(hass, entry, new_ubus_client(hass, entry.data))
