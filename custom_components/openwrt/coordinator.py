from unittest import result
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

    def __init__(self, hass, config: dict, ubus: Ubus, all_devices: dict):
        self._config = config
        self._ubus = ubus
        self._all_devices = all_devices
        self._id = config["id"]
        self._apis = None
        self._wps = config.get("wps", False)
        self._wireless_via_uci = False  # True after network.wireless status fails (OpenWrt 25.12 bug)

        self._coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name='openwrt',
            update_method=self.make_async_update_data(),
            update_interval=timedelta(seconds=config.get("interval", 30))
        )

    @property
    def coordinator(self) -> DataUpdateCoordinator:
        return self._coordinator

    def _configured_devices(self, config_name):
        value = self._config.get(config_name, "")
        if value == "":
            return []
        return list([x.strip() for x in value.split(",")])

    async def discover_wireless_uci(self) -> dict:
        result = dict(ap=[], mesh=[])
        wifi_devices = self._configured_devices("wifi_devices")
        # Check if UCI get method is supported
        if not self.is_api_supported("uci", "get"):
            _LOGGER.debug(f"Device [{self._id}] doesn't support uci.get")
            return result
        
        try:
            response = await self._ubus.api_call('uci', 'get', dict(config='wireless'))
            _LOGGER.debug(f"UCI wireless config response: {response}")
            values = response.get('values', {})
            
            # First, build a mapping of device -> disabled to filter radios
            device_disabled = {}
            for section, data in values.items():
                if data.get('.type') == 'wifi-device':
                    device_name = data.get('.name')
                    disabled = data.get('disabled', '0')
                    # UCI returns strings, convert to boolean / UCI devuelve strings, convertir a boolean
                    device_disabled[device_name] = disabled in ['1', 'true', True]

            # Pre-pass: generate ifnames for sections that lack an explicit 'ifname' in UCI.
            # OpenWrt 25.12+ with ucode wifi-scripts no longer stores 'option ifname' in UCI;
            # the ifname is generated at runtime as phy{N}-{mode}{idx} (e.g. phy0-ap0).
            # Old OpenWrt versions do have 'option ifname', so those take priority below.
            generated_ifnames: dict = {}
            device_mode_count: dict = {}
            for _section, _data in sorted(values.items(), key=lambda x: x[1].get('.index', 999)):
                if _data.get('.type') != 'wifi-iface' or _data.get('ifname'):
                    continue
                _device = _data.get('device', '')
                _mode = _data.get('mode', 'ap')
                radio_num = ''.join(c for c in _device if c.isdigit()) or '0'
                counters = device_mode_count.setdefault(_device, {})
                idx = counters.get(_mode, 0)
                counters[_mode] = idx + 1
                generated_ifnames[_section] = f"phy{radio_num}-{_mode}{idx}"

            # Now process wifi-iface interfaces
            for section, data in values.items():
                if data.get('.type') != 'wifi-iface':
                    continue

                device = data.get('device')
                if not device:
                    _LOGGER.debug(f"wifi-iface {section} has no device")
                    continue

                # Skip if radio is disabled
                if device_disabled.get(device, False):
                    _LOGGER.debug(f"Device {device} is disabled, skipping interface {section}")
                    continue

                # Prefer explicit UCI ifname (old OpenWrt), fall back to generated name (25.12+)
                ifname = data.get('ifname') or generated_ifnames.get(section)
                if not ifname:
                    _LOGGER.debug(f"iface {section} has no ifname and could not generate one")
                    continue

                # network can be string or list in UCI
                network = data.get('network')
                if isinstance(network, list):
                    network = network[0] if network else ""

                conf = dict(ifname=ifname, network=network, device=device)

                mode = data.get('mode')
                if mode == 'ap':
                    ssid = data.get('ssid')
                    if ssid:
                        conf['ssid'] = ssid
                    else:
                        _LOGGER.debug(f"SSID of {ifname} not found")

                    if len(wifi_devices) and ifname not in wifi_devices:
                        _LOGGER.debug(f"Interface {ifname} is not in wifi_devices, skipping")
                        continue

                    result['ap'].append(conf)

                elif mode == 'mesh':
                    # El campo mesh_id puede variar; intentar varias claves comunes
                    mesh_id = data.get('mesh_id') or data.get('mesh') or data.get('ssid') or None
                    if mesh_id:
                        conf['mesh_id'] = mesh_id
                    else:
                        _LOGGER.debug(f"mesh_id not found for {ifname}")
                    result['mesh'].append(conf)

        except Exception as err:
            _LOGGER.warning(f"Device [{self._id}] doesn't support wireless (uci get) or parse failed: {err}")
        return result

    async def discover_wireless(self) -> dict:
        """Discover wireless interfaces using network.wireless API."""
        result = dict(ap=[], mesh=[])
        if not self.is_api_supported("network.wireless"):
            return result
        wifi_devices = self._configured_devices("wifi_devices")
        try:
            response = await self._ubus.api_call('network.wireless', 'status', {})
            _LOGGER.debug(f"Wireless status response: {response}")
            for radio, item in response.items():
                if item.get('disabled', False):
                    continue
                for iface in item['interfaces']:
                    if 'ifname' not in iface:
                        _LOGGER.debug(f"iface {iface} no tiene ifname")
                        continue
                    conf = dict(ifname=iface['ifname'],
                                network=iface['config']['network'][0])
                    if iface['config']['mode'] == 'ap':
                        # This is where we extract the SSID  
                        ssid = iface['config'].get('ssid')  # We use .get() to avoid errors if 'ssid' does not exist  
                        if ssid:
                            conf['ssid'] = ssid # We add the SSID to the conf dictionary.
                        else:
                            _LOGGER.debug(f"SSID of {iface['ifname']} not found")
                        if len(wifi_devices) and iface['ifname'] not in wifi_devices:
                            _LOGGER.debug(f"Interface {iface['ifname']} is not in wifi_devices, skipping")
                            continue
                        result['ap'].append(conf)
                    if iface['config']['mode'] == 'mesh':
                        conf['mesh_id'] = iface['config']['mesh_id']
                        result['mesh'].append(conf)
        except NameError as err:
            _LOGGER.warning(f"Device [{self._id}] doesn't support wireless: {err}")
        return result

    def find_mesh_peers(self, mesh_id: str):
        result = []
        for _, device in self._all_devices.items():
            data = device.coordinator.data
            if not data or 'mesh' not in data or not data['mesh']:
                _LOGGER.warning(f"Missing or invalid 'mesh' data for device: {device}")
                continue
            for _, mesh in data['mesh'].items():
                if mesh['id'] == mesh_id:
                    result.append(mesh['mac'])
        return result

    async def update_mesh(self, configs) -> dict:
        """Update mesh information."""
        mesh_devices = self._configured_devices("mesh_devices")
        result = dict()
#        if not self.is_api_supported("iwinfo"):
#            return result
        # Check both iwinfo methods needed
        if not (self.is_api_supported("iwinfo", "info") and 
                self.is_api_supported("iwinfo", "assoclist")):
            return result
        try:
            for conf in configs:
                if len(mesh_devices) and conf['ifname'] not in mesh_devices:
                    continue
                info = await self._ubus.api_call(
                    'iwinfo',
                    'info',
                    dict(device=conf['ifname'])
                )
                peers = {}
                result[conf['ifname']] = dict(
                    mac=info['bssid'].lower(),
                    signal=info.get("signal", -100),
                    id=conf['mesh_id'],
                    noise=info.get("noise", 0),
                    bitrate=info.get("bitrate", -1),
                    peers=peers,
                )
                for mac in self.find_mesh_peers(conf['mesh_id']):
                    try:
                        assoc = await self._ubus.api_call(
                            'iwinfo',
                            'assoclist',
                            dict(device=conf['ifname'], mac=mac)
                        )
                        peers[mac] = dict(
                            active=assoc.get("mesh plink") == "ESTAB",
                            signal=assoc.get("signal", -100),
                            noise=assoc.get("noise", 0)
                        )
                    except ConnectionError:
                        _LOGGER.warning(f"Failed to get assoclist for {mac} on device {conf['ifname']}")
                        pass
        except ConnectionError as err:
            _LOGGER.warning(f"Device [{self._id}] doesn't support iwinfo: {err}")
        return result

    async def update_hostapd_clients(self, interface_id: str) -> dict:
        """Update hostapd clients for a specific interface."""
        try:
            _LOGGER.debug(f"Updating hostapd clients for interface: {interface_id}")
            response = await self._ubus.api_call(
                f"hostapd.{interface_id}",
                'get_clients',
                dict()
            )
            _LOGGER.debug(f"Hostapd clients response for {interface_id}: {response}")

            if 'clients' in response:
                clients = response['clients']
            else:
                _LOGGER.warning(f"'clients' key not found in response for interface {interface_id}. Response: {response}")
                clients = {}

            macs = dict()
            for key, value in clients.items():
                macs[key] = dict(signal=value.get("signal"))

            result = dict(
                clients=len(macs),
                macs=macs,
            )

            if self._wps:
                try:
                    response = await self._ubus.api_call(
                        f"hostapd.{interface_id}",
                        'wps_status',
                        dict()
                    )
                    result["wps"] = response.get("pbc_status") == "Active"
                except ConnectionError as err:
                    _LOGGER.warning(f"Interface [{interface_id}] doesn't support WPS: {err}")

            return result

        except NameError as e:
            _LOGGER.warning(f"Could not find object for interface {interface_id}: {e}")
            return {}
        except Exception as e:
            _LOGGER.error(f"Error while updating hostapd clients for {interface_id}: {e}")
            return {}

    async def set_wps(self, interface_id: str, enable: bool):
        await self._ubus.api_call(
            f"hostapd.{interface_id}",
            "wps_start" if enable else "wps_cancel",
            dict()
        )
        await self.coordinator.async_request_refresh()

    async def do_reboot(self):
        _LOGGER.debug(f"Rebooting device: {self._id}")
        await self._ubus.api_call(
            "system",
            "reboot",
            dict()
        )

    async def do_file_exec(self, command: str, params, env: dict, extra: dict):
        _LOGGER.debug(f"Executing command: {self._id}: {command} with {params} env={env}")
        result = await self._ubus.api_call(
            "file",
            "exec",
            dict(command=command, params=params, env=env) if len(env) else dict(command=command, params=params)
        )
        _LOGGER.debug(f"Execute result: {self._id}: {result}")
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
                json = json_loads(data)
                if isinstance(json, (list, dict)):
                    return json
            except Exception as e:
                _LOGGER.debug(f"Failed to parse JSON output: {e}")
                pass
            return data.strip().split("\n")

        return {
            "code": result.get("code", 1),
            "stdout": process_output(result.get("stdout", "")),
            "stderr": process_output(result.get("stderr", "")),
        }

    async def do_ubus_call(self, subsystem: str, method: str, params: dict):
        _LOGGER.debug(f"do_ubus_call(): {subsystem} / {method}: {params}")
        return await self._ubus.api_call(subsystem, method, params)

    async def do_rc_init(self, name: str, action: str):
        _LOGGER.debug(f"Executing name: {self._id}: {name} with {action}")
        result = await self._ubus.api_call(
            "rc",
            "init",
            dict(name=name, action=action)
        )
        _LOGGER.debug(f"Execute result: {self._id}: {result}")
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
            if 'ifname' in item:
                ifname = item['ifname']
                try:
                    _LOGGER.debug(f"Updating AP for interface: {ifname}")
                    clients_info = await self.update_hostapd_clients(ifname)
                    # Add the SSID to the AP information if available 
                    if 'ssid' in item:
                        clients_info['ssid'] = item['ssid']
                    else:
                        clients_info['ssid'] = ifname 
                    result[ifname] = clients_info
                except Exception as e:
                    _LOGGER.error(f"Error updating AP for {ifname}: {e}")
                    continue  # Continue with the next item
            else:
                _LOGGER.warning(f"Missing 'ifname' in AP config: {item}")
        return result

    async def update_info(self) -> dict:
        """Get basic device information."""
        result = dict()
        response = await self._ubus.api_call("system", "board", {})
        return {
            "model": response["model"],
            "manufacturer": response["release"]["distribution"],
            "sw_version": "%s %s" % (
                response["release"]["version"],
                response["release"]["revision"]
            ),
        }

    async def discover_mwan3(self):
#        if not self.is_api_supported("mwan3"):
#            return dict()
        """Discover mwan3 interfaces."""
        if not self.is_api_supported("mwan3", "status"):
            return dict()
        result = dict()
        response = await self._ubus.api_call(
            "mwan3",
            "status",
            dict(section="interfaces")
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
                "up": iface.get("up")
            }
        return result

    async def update_wan_info(self):
        result = dict()
        devices = self._configured_devices("wan_devices")
        for device_id in devices:
            response = await self._ubus.api_call(
                "network.device",
                "status",
                dict(name=device_id)
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
#        """Fetch host hints from luci-rpc."""
#        if not self.is_api_supported("luci-rpc"):
#            _LOGGER.debug(f"Device [{self._id}] doesn't support luci-rpc")
#            return {}
        """Fetch host hints from luci-rpc."""
        if not self.is_api_supported("luci-rpc", "getHostHints"):
            _LOGGER.debug(f"Device [{self._id}] doesn't support luci-rpc.getHostHints")
            return {}          
        try:
            response = await self._ubus.api_call(
                "luci-rpc",
                "getHostHints",
                {}
            )
            _LOGGER.debug(f"Host hints response: {response}")
            
            # Process the response to extract mac, ip, and name
            hosts = {}
            for mac, data in response.items():
                ip_addresses = data.get("ipaddrs", [])
                name = data.get("name", "")
                hosts[mac] = {
                    "ip": ip_addresses[0] if ip_addresses else "",
                    "name": name,
                    "mac": mac
                }
            
            return hosts
        except Exception as err:
            _LOGGER.warning(f"Failed to get host hints for device [{self._id}]: {err}")
            return {}

    async def update_system_info(self):
        """Get system information like uptime, memory, load"""
        try:
            response = await self._ubus.api_call(
                "system",
                "info",
                {}
            )
            _LOGGER.debug(f"System info response: {response}")
            return {
                "uptime": response.get("uptime", 0),
                "load": response.get("load", [0, 0, 0]),
                "memory": response.get("memory", {}),
                "localtime": response.get("localtime", 0),
                "root": response.get("root", {}),
                "tmp": response.get("tmp", {}),
                "swap": response.get("swap", {})
            }
        except Exception as err:
            _LOGGER.warning(f"Device [{self._id}] failed to get system info: {err}")
            return {}
## OK
#    async def load_ubus(self):
#        _LOGGER.debug("Calling load_ubus()")
#        result = await self._ubus.api_list()
#        _LOGGER.debug(f"Result of load_ubus: {result}")
#        _LOGGER.debug("Available APIs: %s", list(result.keys()) if isinstance(result, dict) else "Not a dict")
#        return result

    async def load_ubus(self):
        """Load UBUS ACLs from the session."""
        _LOGGER.debug("Calling load_ubus()")
        self._wireless_via_uci = False  # Reset on re-login; router may have been updated
        # If ACLs are not loaded yet, we need to login first
        if not self._ubus.acls:
            _LOGGER.debug("ACLs not loaded yet, performing login to obtain ACLs")
            try:
                await self._ubus._login()
            except Exception as err:
                _LOGGER.error(f"Failed to login and load ACLs: {err}")
                return {}
        
        # Get only the 'ubus' section from ACLs
        acls_ubus = self._ubus.acls.get("ubus", {})
        _LOGGER.debug("Result of load_ubus (ACLs): %s", acls_ubus)
        _LOGGER.debug("Available APIs: %s", list(acls_ubus.keys()) if acls_ubus else "No APIs available")
        return acls_ubus

    def is_api_supported(self, name: str, method: str = None) -> bool:
        """
        Check if an API is supported based on ACLs.
        
        Args:
            name: The subsystem name (e.g., 'iwinfo', 'system', 'network.wireless')
            method: Optional method name to check specific permissions
            
        Returns:
            True if the API (and optionally the method) is supported
        """
        _LOGGER.debug(f"Checking if API '{name}' is supported" + (f" with method '{method}'" if method else ""))
        
        if not self._apis:
            _LOGGER.debug(f"No APIs loaded yet, API '{name}' is NOT supported")
            return False
        
        # Check if the exact subsystem exists in ACLs
        if name in self._apis:
            # If no method specified, just check if subsystem exists
            if method is None:
                _LOGGER.debug(f"API '{name}' is supported")
                return True
            
            # If method specified, check if it's in the allowed methods list
            allowed_methods = self._apis[name]
            if method in allowed_methods:
                _LOGGER.debug(f"API '{name}' with method '{method}' is supported")
                return True
            else:
                _LOGGER.debug(f"API '{name}' exists but method '{method}' is NOT in allowed methods: {allowed_methods}")
                return False
        
        _LOGGER.debug(f"API '{name}' is NOT supported")
        return False

    def is_api_supported_ori(self, name: str) -> bool:
        _LOGGER.debug(f"Checking if the API '{name}' is supported")
        if self._apis and name in self._apis:
            _LOGGER.debug(f"The API '{name}' is supported")
            return True
        _LOGGER.debug(f"The API '{name}' is NOT supported")
        return False

    def make_async_update_data(self):
        async def async_update_data():
            try:
                # Los ACLs ya se cargaron en __init__, pero por si acaso recargamos
                if not self._apis:
                    try:
                        self._apis = await self.load_ubus()
                    except Exception as err:
                        _LOGGER.error("Failed to load ubus APIs for device [%s]: %s", self._id, err)
                        self._apis = {}
#                if not self._apis:
#                    self._apis = await self.load_ubus()
                               
                result = dict()
                result["info"] = await self.update_info()

                # Prefer ubus "network.wireless" if available, otherwise fall back to UCI.
                # OpenWrt 25.12 bug: network.wireless status returns [2] via rpcd HTTP even when
                # the ACL is correct. After the first failure we permanently use UCI for this
                # session to avoid a warning log on every 30-second update.
                wireless_config = dict(ap=[], mesh=[])
                if self.is_api_supported("network.wireless") and not self._wireless_via_uci:
                    _LOGGER.debug("Using ubus network.wireless for wireless discovery")
                    try:
                        wireless_config = await self.discover_wireless()
                    except Exception as err:
                        _LOGGER.warning(
                            "discover_wireless failed, switching permanently to UCI fallback "
                            "(known OpenWrt 25.12 rpcd bug if error is 'RPC error: 2'): %s", err
                        )
                        self._wireless_via_uci = True
                        try:
                            wireless_config = await self.discover_wireless_uci()
                        except Exception as err2:
                            _LOGGER.warning("discover_wireless_uci fallback also failed: %s", err2)
                else:
                    _LOGGER.debug("Using UCI (uci get wireless) for wireless discovery")
                    try:
                        wireless_config = await self.discover_wireless_uci()
                    except Exception as err:
                        _LOGGER.warning("discover_wireless_uci failed, using empty result: %s", err)

                result['wireless'] = await self.update_ap(wireless_config['ap'])
                result['mesh'] = await self.update_mesh(wireless_config['mesh'])
                result["mwan3"] = await self.discover_mwan3()
                result["wan"] = await self.update_wan_info()
                # Add the host hints data and system info to the result
                result["hosts"] = await self.fetch_host_hints()
                result["system_info"] = await self.update_system_info()

                _LOGGER.debug(f"Full update [{self._id}]: {result}")
                return result
            except PermissionError as err:
                raise ConfigEntryAuthFailed from err
            except Exception as err:
                _LOGGER.exception(f"Device [{self._id}] async_update_data error: {err}")
                raise UpdateFailed(f"OpenWrt communication error: {err}")
        return async_update_data

def new_ubus_client(hass, config: dict) -> Ubus:
    _LOGGER.debug(f"new_ubus_client(): {config}")
    schema = "https" if config["https"] else "http"
    port = ":%d" % (config["port"]) if config["port"] > 0 else ''
    url = "%s://%s%s%s" % (schema, config["address"], port, config["path"])
    return Ubus(
        hass.async_add_executor_job,
        url,
        config["username"],
        config.get("password", ""),
        verify=config.get("verify_cert", True)
    )

def new_coordinator(hass, config: dict, all_devices: dict) -> DeviceCoordinator:
    _LOGGER.debug(f"new_coordinator: {config}, {all_devices}")
    connection = new_ubus_client(hass, config)
    device = DeviceCoordinator(hass, config, connection, all_devices)
    return device
