import asyncio
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util.json import json_loads

from .ubus import Ubus
from .constants import DOMAIN

import logging
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)


class DeviceCoordinator:

    def __init__(self, hass, config: dict, ubus: Ubus):
        self._config = config
        self._ubus = ubus
        self._hass = hass
        self._id = config["id"]
        self._apis = None
        self._wps = config.get("wps", False)
        self._wireless_via_uci = False  # True after network.wireless status fails (OpenWrt 25.12 bug)
        self._cached_board_info: dict | None = None
        self._last_uptime: int = 0

        self._coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="openwrt",
            update_method=self.async_update_data,
            update_interval=timedelta(seconds=config.get("interval", 30)),
        )

    @property
    def coordinator(self) -> DataUpdateCoordinator:
        return self._coordinator

    def _configured_devices(self, config_name):
        value = self._config.get(config_name, "")
        if value == "":
            return []
        return [x.strip() for x in value.split(",")]

    async def discover_wireless_uci(self) -> dict:
        result = dict(ap=[], mesh=[])
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
            for section, data in values.items():
                if data.get(".type") == "wifi-device":
                    device_name = data.get(".name")
                    disabled = data.get("disabled", "0")
                    device_disabled[device_name] = disabled in ["1", "true", True]

            # Pre-pass: generate ifnames for sections that lack an explicit 'ifname' in UCI.
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

        # optional subsystem — uci.get may not be present; catch all to keep wireless discovery alive
        except Exception as err:
            _LOGGER.warning(
                "Device [%s] doesn't support wireless (uci get) or parse failed: %s",
                self._id, err,
            )
        return result

    async def discover_wireless(self) -> dict:
        """Discover wireless interfaces using network.wireless API."""
        result = dict(ap=[], mesh=[])
        if not self.is_api_supported("network.wireless"):
            return result
        wifi_devices = self._configured_devices("wifi_devices")
        try:
            response = await self._ubus.api_call("network.wireless", "status", {})
            _LOGGER.debug("Wireless status response: %s", response)
            for radio, item in response.items():
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
                        config = iface["config"]
                        mesh_id = config.get("mesh_id")
                        if mesh_id:
                            conf["mesh_id"] = mesh_id
                        else:
                            _LOGGER.debug("mesh_id not found for %s", iface["ifname"])
                        result["mesh"].append(conf)
        except NameError as err:
            _LOGGER.warning("Device [%s] doesn't support wireless: %s", self._id, err)
        return result

    def find_mesh_peers(self, mesh_id: str):
        result = []
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            device = entry.runtime_data
            if not device:
                continue
            data = device.coordinator.data
            if not data or "mesh" not in data or not data["mesh"]:
                _LOGGER.warning("Missing or invalid 'mesh' data for device: %s", device)
                continue
            for _, mesh in data["mesh"].items():
                if mesh["id"] == mesh_id:
                    result.append(mesh["mac"])
        return result

    async def update_mesh(self, configs) -> dict:
        """Update mesh information."""
        mesh_devices = self._configured_devices("mesh_devices")
        result = dict()
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
                peers = {}
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
                for mac, assoc in zip(peer_macs, assoc_results):
                    if isinstance(assoc, Exception):
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
        except ConnectionError as err:
            _LOGGER.warning("Device [%s] doesn't support iwinfo: %s", self._id, err)
        return result

    async def update_hostapd_clients(self, interface_id: str) -> dict:
        """Update hostapd clients for a specific interface."""
        try:
            _LOGGER.debug("Updating hostapd clients for interface: %s", interface_id)
            response = await self._ubus.api_call(
                f"hostapd.{interface_id}", "get_clients", {}
            )
            _LOGGER.debug("Hostapd clients response for %s: %s", interface_id, response)

            clients = response.get("clients", {})
            if "clients" not in response:
                _LOGGER.warning(
                    "'clients' key not found in response for %s. Response: %s",
                    interface_id, response,
                )

            macs = {key: dict(signal=value.get("signal")) for key, value in clients.items()}
            result = dict(clients=len(macs), macs=macs)

            if self._wps:
                try:
                    wps_response = await self._ubus.api_call(
                        f"hostapd.{interface_id}", "wps_status", {}
                    )
                    result["wps"] = wps_response.get("pbc_status") == "Active"
                except ConnectionError as err:
                    _LOGGER.warning(
                        "Interface [%s] doesn't support WPS: %s", interface_id, err
                    )

            return result

        except NameError as e:
            _LOGGER.warning("Could not find object for interface %s: %s", interface_id, e)
            return {}
        except (ConnectionError, KeyError, ValueError, TimeoutError) as e:
            _LOGGER.error("Error updating hostapd clients for %s: %s", interface_id, e)
            return {}

    async def set_wps(self, interface_id: str, enable: bool):
        await self._ubus.api_call(
            f"hostapd.{interface_id}",
            "wps_start" if enable else "wps_cancel",
            {},
        )
        await self.coordinator.async_request_refresh()

    async def do_reboot(self):
        _LOGGER.debug("Rebooting device: %s", self._id)
        await self._ubus.api_call("system", "reboot", {})

    async def do_file_exec(self, command: str, params, env: dict, extra: dict):
        _LOGGER.debug("Executing command %s: %s with %s env=%s", self._id, command, params, env)
        result = await self._ubus.api_call(
            "file",
            "exec",
            dict(command=command, params=params, env=env) if env else dict(command=command, params=params),
        )
        _LOGGER.debug("Execute result %s: %s", self._id, result)
        self._coordinator.hass.bus.async_fire(
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
            except Exception:  # json parse fallback — non-JSON exec output is valid; silently ignore
                pass
            return data.strip().split("\n")

        return {
            "code": result.get("code", 1),
            "stdout": process_output(result.get("stdout", "")),
            "stderr": process_output(result.get("stderr", "")),
        }

    async def do_ubus_call(self, subsystem: str, method: str, params: dict):
        _LOGGER.debug("do_ubus_call(): %s / %s: %s", subsystem, method, params)
        return await self._ubus.api_call(subsystem, method, params)

    async def do_rc_init(self, name: str, action: str):
        _LOGGER.debug("Executing rc init %s: %s with %s", self._id, name, action)
        result = await self._ubus.api_call("rc", "init", dict(name=name, action=action))
        _LOGGER.debug("Execute result %s: %s", self._id, result)
        self._coordinator.hass.bus.async_fire(
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
            try:
                _LOGGER.debug("Updating AP for interface: %s", ifname)
                clients_info = await self.update_hostapd_clients(ifname)
                clients_info["ssid"] = item.get("ssid", ifname)
                result[ifname] = clients_info
            except (ConnectionError, KeyError, ValueError) as e:
                _LOGGER.error("Error updating AP for %s: %s", ifname, e)
        return result

    async def update_info(self) -> dict:
        """Get basic device information."""
        response = await self._ubus.api_call("system", "board", {})
        return {
            "model": response["model"],
            "manufacturer": response["release"]["distribution"],
            "sw_version": "%s %s" % (
                response["release"]["version"],
                response["release"]["revision"],
            ),
        }

    async def discover_mwan3(self):
        """Discover mwan3 interfaces."""
        if not self.is_api_supported("mwan3", "status"):
            return dict()
        result = dict()
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
        return result

    async def update_wan_info(self):
        result = dict()
        devices = self._configured_devices("wan_devices")
        for device_id in devices:
            response = await self._ubus.api_call(
                "network.device", "status", dict(name=device_id)
            )
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

    async def fetch_host_hints(self):
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
        # optional subsystem — luci-rpc may not be installed; swallow all errors to keep the poll alive
        except Exception as err:
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
        except ConnectionError as err:
            _LOGGER.debug(
                "Modem stats file not available on device [%s]: %s", self._id, err
            )
            return {}
        data_str = response.get("data", "")
        if not data_str:
            return {}
        try:
            return json_loads(data_str)
        except (ValueError, KeyError) as err:
            _LOGGER.warning(
                "Device [%s] modem-stats.json is malformed (partial write?): %s",
                self._id, err,
            )
            return {}

    async def update_system_info(self):
        """Get system information: uptime, memory, load."""
        try:
            response = await self._ubus.api_call("system", "info", {})
            _LOGGER.debug("System info response: %s", response)
            return {
                "uptime": response.get("uptime", 0),
                "load": response.get("load", [0, 0, 0]),
                "memory": response.get("memory", {}),
                "localtime": response.get("localtime", 0),
                "root": response.get("root", {}),
                "tmp": response.get("tmp", {}),
                "swap": response.get("swap", {}),
            }
        # semi-optional — system.info failure is non-fatal; uptime falls back to 0 (no reboot assumed)
        except Exception as err:
            _LOGGER.warning("Device [%s] failed to get system info: %s", self._id, err)
            return {}

    async def load_ubus(self):
        """Load UBUS ACLs from the session."""
        _LOGGER.debug("Calling load_ubus()")
        self._wireless_via_uci = False  # Reset on re-login; router may have been updated
        if not self._ubus.acls:
            _LOGGER.debug("ACLs not loaded yet, performing login to obtain ACLs")
            try:
                await self._ubus.login()
            # login can raise ubus auth errors, network errors, or unexpected server responses; all are non-fatal here
            except Exception as err:
                _LOGGER.error("Failed to login and load ACLs: %s", err)
                return {}

        acls_ubus = self._ubus.acls.get("ubus", {})
        _LOGGER.debug("Available APIs: %s", list(acls_ubus.keys()) if acls_ubus else "none")
        return acls_ubus

    def is_api_supported(self, name: str, method: str = None) -> bool:
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
            _LOGGER.debug("Using ubus network.wireless for wireless discovery")
            try:
                return await self.discover_wireless()
            # triggers permanent UCI fallback; must catch ubus RPC errors and OpenWrt 25.12 rpcd protocol errors
            except Exception as err:
                _LOGGER.warning(
                    "discover_wireless failed, switching permanently to UCI fallback "
                    "(known OpenWrt 25.12 rpcd bug if error is 'RPC error: 2'): %s", err
                )
                self._wireless_via_uci = True

        _LOGGER.debug("Using UCI (uci get wireless) for wireless discovery")
        try:
            return await self.discover_wireless_uci()
        # last-resort fallback — catch all discovery errors; returning empty config is safer than crashing the poll
        except Exception as err:
            _LOGGER.warning("discover_wireless_uci failed: %s", err)
        return dict(ap=[], mesh=[])

    async def _update_wireless(self):
        """Discover wireless config then fetch AP and mesh data concurrently."""
        wireless_config = await self._discover_wireless_config()
        return await asyncio.gather(
            self.update_ap(wireless_config["ap"]),
            self.update_mesh(wireless_config["mesh"]),
        )

    async def async_update_data(self):
        if self._coordinator.hass.is_stopping:
            return self._coordinator.data
        try:
            if not self._apis:
                try:
                    self._apis = await self.load_ubus()
                # startup login failure is non-fatal; coordinator continues with empty APIs and retries on next poll
                except Exception as err:
                    _LOGGER.error("Failed to load ubus APIs for device [%s]: %s", self._id, err)
                    self._apis = {}

            system_info = await self.update_system_info()
            current_uptime = system_info.get("uptime", 0) if system_info else 0

            need_board_refresh = (
                self._cached_board_info is None
                or (current_uptime > 0 and current_uptime < self._last_uptime)
            )

            if need_board_refresh:
                fetched = await self.update_info()
                if fetched:
                    self._cached_board_info = fetched

            if current_uptime > 0:
                self._last_uptime = current_uptime

            info = self._cached_board_info or {}

            (wireless, mesh), mwan3, wan, hosts, modem = await asyncio.gather(
                self._update_wireless(),
                self.discover_mwan3(),
                self.update_wan_info(),
                self.fetch_host_hints(),
                self.update_modem_stats(),
            )

            result = dict(
                info=info,
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
        except PermissionError as err:
            raise ConfigEntryAuthFailed from err
        except (TimeoutError, asyncio.CancelledError) as err:
            raise UpdateFailed(f"OpenWrt communication error: {err}")
        except Exception as err:
            _LOGGER.exception("Device [%s] async_update_data error: %s", self._id, err)
            raise UpdateFailed(f"OpenWrt communication error: {err}")


def new_ubus_client(hass, config: dict) -> Ubus:
    _LOGGER.debug("new_ubus_client(): %s", {k: v for k, v in config.items() if k != "password"})
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


def new_coordinator(hass, config: dict) -> DeviceCoordinator:
    _LOGGER.debug("new_coordinator: %s", {k: v for k, v in config.items() if k != "password"})
    connection = new_ubus_client(hass, config)
    return DeviceCoordinator(hass, config, connection)
