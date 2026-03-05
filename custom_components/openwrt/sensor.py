from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

import logging

from . import OpenWrtEntity
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    device = entry.runtime_data
    device_id = entry.data["id"]

    entities = []

    entities.append(HostsSensor(device, device_id))

    wireless = []
    for net_id in device.coordinator.data["wireless"]:
        sensor = WirelessClientsSensor(device, device_id, net_id)
        wireless.append(sensor)
        entities.append(sensor)
    if wireless:
        entities.append(WirelessTotalClientsSensor(device, device_id, wireless))

    for net_id in device.coordinator.data["mesh"]:
        entities.append(MeshSignalSensor(device, device_id, net_id))
        entities.append(MeshPeersSensor(device, device_id, net_id))

    mwan3_data = device.coordinator.data.get("mwan3", {})
    if isinstance(mwan3_data, dict):
        for net_id, net_data in mwan3_data.items():
            if not isinstance(net_data, dict):
                _LOGGER.warning(
                    "Skipping mwan3 entry '%s' for device %s: data is not a dict",
                    net_id, device_id,
                )
                continue
            try:
                uptime = net_data.get("uptime_sec")
                online = net_data.get("online_sec")
                if uptime is None or online is None:
                    raise ValueError("missing uptime_sec or online_sec")
                int(uptime)
                int(online)
            except Exception as err:
                _LOGGER.warning(
                    "Skipping mwan3 entry '%s' for device %s: invalid data (%s)",
                    net_id, device_id, err,
                )
                continue
            entities.append(Mwan3OnlineSensor(device, device_id, net_id))
    else:
        _LOGGER.debug("No valid 'mwan3' data available for device %s", device_id)

    for net_id in device.coordinator.data["wan"]:
        entities.append(WanRxTxSensor(device, device_id, net_id, "rx"))
        entities.append(WanRxTxSensor(device, device_id, net_id, "tx"))

    if "system_info" in device.coordinator.data:
        entities.append(SystemUptimeSensor(device, device_id))
        entities.append(SystemLoadSensor(device, device_id))
        entities.append(SystemMemorySensor(device, device_id))
        if device.coordinator.data["system_info"].get("swap", {}).get("total", 0) > 0:
            entities.append(SystemDiskSensor(device, device_id, "swap"))

    async_add_entities(entities)
    return True


class OpenWrtSensor(OpenWrtEntity, SensorEntity):
    """Base class for OpenWrt sensors."""

    def __init__(self, coordinator, device: str):
        super().__init__(coordinator, device)

    @property
    def state_class(self):
        return "measurement"


class WirelessClientsSensor(OpenWrtSensor):
    """Number of clients on a wireless interface."""

    def __init__(self, device, device_id: str, interface: str):
        super().__init__(device, device_id)
        self._interface_id = interface

    @property
    def unique_id(self):
        return "%s.%s.clients" % (super().unique_id, self._interface_id)

    @property
    def name(self):
        ssid = self.data["wireless"][self._interface_id].get("ssid", self._interface_id)
        return "%s Wireless [%s] clients" % (super().name, ssid)

    @property
    def state(self):
        return self.data["wireless"][self._interface_id]["clients"]

    @property
    def icon(self):
        return "mdi:wifi-off" if self.state == 0 else "mdi:wifi"

    @property
    def extra_state_attributes(self):
        result = dict()
        data = self.data["wireless"][self._interface_id]
        _LOGGER.debug("Generando atributos para %s con datos: %s", self._interface_id, data)

        hosts_data = self.data.get("hosts", {})
        mac_to_ip = {}
        mac_to_name = {}
        for mac, host_info in hosts_data.items():
            mac_lower = mac.lower()
            mac_to_ip[mac_lower] = host_info.get("ip", "")
            mac_to_name[mac_lower] = host_info.get("name", "")

        for mac, value in data.get("macs", {}).items():
            mac_lower = mac.lower()
            signal = value.get("signal", 0)
            client_info = f"{signal} dBm"
            if mac_lower in mac_to_ip and mac_to_ip[mac_lower]:
                client_info += f" | IP: {mac_to_ip[mac_lower]}"
            if mac_lower in mac_to_name and mac_to_name[mac_lower]:
                client_info += f" | Nombre: {mac_to_name[mac_lower]}"
            result[mac.upper()] = client_info

        if "ssid" in data:
            result["ssid"] = data["ssid"]
        return result

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC


class MeshSignalSensor(OpenWrtSensor):
    """Signal strength of a mesh interface."""

    def __init__(self, device, device_id: str, interface: str):
        super().__init__(device, device_id)
        self._interface_id = interface

    @property
    def unique_id(self):
        return "%s.%s.mesh_signal" % (super().unique_id, self._interface_id)

    @property
    def name(self):
        return f"{super().name} Mesh [{self._interface_id}] signal"

    @property
    def native_value(self):
        return self.data["mesh"][self._interface_id]["signal"]

    @property
    def native_unit_of_measurement(self):
        return "dBm"

    @property
    def device_class(self):
        return SensorDeviceClass.SIGNAL_STRENGTH

    @property
    def _signal_level(self):
        value = self.native_value
        levels = [-50, -60, -67, -70, -80]
        for idx, level in enumerate(levels):
            if value >= level:
                return idx
        return len(levels)

    @property
    def icon(self):
        icons = [
            "mdi:network-strength-4",
            "mdi:network-strength-3",
            "mdi:network-strength-2",
            "mdi:network-strength-1",
            "mdi:network-strength-outline",
            "mdi:network-strength-off-outline",
        ]
        return icons[self._signal_level]

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC


class MeshPeersSensor(OpenWrtSensor):
    """Number of active mesh peers on a mesh interface."""

    def __init__(self, device, device_id: str, interface: str):
        super().__init__(device, device_id)
        self._interface_id = interface

    @property
    def unique_id(self):
        return "%s.%s.mesh_peers" % (super().unique_id, self._interface_id)

    @property
    def name(self):
        return f"{super().name} Mesh [{self._interface_id}] peers"

    @property
    def state(self):
        peers = self.data["mesh"][self._interface_id]["peers"]
        return len([p for p in peers.values() if p["active"]])

    @property
    def icon(self):
        return "mdi:server-network" if self.state > 0 else "mdi:server-network-off"

    @property
    def extra_state_attributes(self):
        result = dict()
        data = self.data["mesh"][self._interface_id]
        for key, value in data.get("peers", {}).items():
            signal = value.get("signal", 0)
            result[key.upper()] = f"{signal} dBm"
        return result

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC


class WirelessTotalClientsSensor(OpenWrtSensor):
    """Total number of clients across all wireless interfaces."""

    def __init__(self, device, device_id: str, sensors):
        super().__init__(device, device_id)
        self._sensors = sensors

    @property
    def unique_id(self):
        return "%s.total_clients" % super().unique_id

    @property
    def name(self):
        return "%s Wireless total clients" % super().name

    @property
    def state(self):
        return sum(s.state for s in self._sensors)

    @property
    def icon(self):
        return "mdi:wifi-off" if self.state == 0 else "mdi:wifi"

    @property
    def extra_state_attributes(self):
        result = {}
        for sensor in self._sensors:
            ssid = sensor.data["wireless"][sensor._interface_id].get(
                "ssid", sensor._interface_id
            )
            result[ssid] = sensor.state
        return result


class Mwan3OnlineSensor(OpenWrtSensor):
    """Online ratio of a WAN interface managed by mwan3."""

    def __init__(self, device, device_id: str, interface: str):
        super().__init__(device, device_id)
        self._interface_id = interface
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon = "mdi:router-network"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self):
        return self._interface_id in self.data["mwan3"]

    @property
    def unique_id(self):
        return "%s.%s.mwan3_online_ratio" % (super().unique_id, self._interface_id)

    @property
    def name(self):
        return f"{super().name} Mwan3 [{self._interface_id}] online ratio"

    @property
    def native_value(self):
        data = self.data["mwan3"].get(self._interface_id, {})
        uptime = data.get("uptime_sec")
        online = data.get("online_sec")
        return round(online / uptime * 100, 1) if uptime else 100


class WanRxTxSensor(OpenWrtSensor):
    """RX or TX byte counter for a WAN interface."""

    def __init__(self, device, device_id: str, interface: str, code: str):
        super().__init__(device, device_id)
        self._interface = interface
        self._code = code
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:download-network" if code == "rx" else "mdi:upload-network"
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_native_unit_of_measurement = "B"

    @property
    def _data(self):
        return self.data["wan"].get(self._interface)

    @property
    def available(self):
        return self._interface in self.data["wan"] and self._data.get("up")

    @property
    def unique_id(self):
        return "%s.%s.wan_%s_bytes" % (super().unique_id, self._interface, self._code)

    @property
    def name(self):
        return f"{super().name} Wan [{self._interface}] {self._code.capitalize()} bytes"

    @property
    def native_value(self):
        return self._data.get(f"{self._code}_bytes")

    @property
    def extra_state_attributes(self):
        return dict(mac=self._data.get("mac"), speed=self._data.get("speed"))

    @property
    def state_class(self):
        return "total_increasing"


class HostsSensor(OpenWrtSensor):
    """Number of known hosts in the router."""

    def __init__(self, device, device_id: str):
        super().__init__(device, device_id)
        self._attr_icon = "mdi:devices"

    @property
    def unique_id(self):
        return "%s.hosts" % super().unique_id

    @property
    def name(self):
        return f"{super().name} Known Hosts"

    @property
    def state(self):
        return len(self.data.get("hosts", {}))

    @property
    def extra_state_attributes(self):
        hosts = self.data.get("hosts", {})
        ip_to_host = {}
        for mac, host_data in hosts.items():
            ip = host_data.get("ip", "")
            if ip:
                ip_to_host[ip] = {"name": host_data.get("name", ""), "mac": mac}

        result = {}
        for ip in sorted(ip_to_host.keys(), key=self._sort_ip):
            host_info = ip_to_host[ip]
            name = host_info["name"] if host_info["name"] else "Desconocido"
            result[ip] = f"{name} : {host_info['mac']}"
        return result

    def _sort_ip(self, ip):
        try:
            return [int(n) for n in ip.split(".")]
        except (ValueError, AttributeError):
            return [999, 999, 999, 999]


class SystemUptimeSensor(OpenWrtSensor):
    """System uptime sensor."""

    def __init__(self, device, device_id: str):
        super().__init__(device, device_id)
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:clock-outline"

    @property
    def state_class(self):
        return None  # uptime is formatted as a string, not a numeric measurement

    @property
    def unique_id(self):
        return "%s.system_uptime" % super().unique_id

    @property
    def name(self):
        return f"{super().name} System uptime"

    @property
    def state(self):
        seconds = self.data.get("system_info", {}).get("uptime", 0)
        delta = timedelta(seconds=seconds)
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    @property
    def extra_state_attributes(self):
        return {"seconds": self.data.get("system_info", {}).get("uptime", 0)}


class SystemMemorySensor(OpenWrtSensor):
    """Free system memory sensor."""

    def __init__(self, device, device_id: str):
        super().__init__(device, device_id)
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:memory"
        self._attr_native_unit_of_measurement = "MB"

    @property
    def unique_id(self):
        return "%s.system_memory_free" % super().unique_id

    @property
    def name(self):
        return f"{super().name} System memory free"

    @property
    def native_value(self):
        memory = self.data.get("system_info", {}).get("memory", {})
        return round(memory.get("free", 0) / (1024 * 1024), 1)

    @property
    def extra_state_attributes(self):
        memory = self.data.get("system_info", {}).get("memory", {})
        return {
            "total_mb": round(memory.get("total", 0) / (1024 * 1024), 1),
            "free_mb": round(memory.get("free", 0) / (1024 * 1024), 1),
            "shared_mb": round(memory.get("shared", 0) / (1024 * 1024), 1),
            "cached_mb": round(memory.get("cached", 0) / (1024 * 1024), 1),
            "available_mb": round(memory.get("available", 0) / (1024 * 1024), 1),
            "used_percent": round(
                (1 - memory.get("free", 0) / memory.get("total", 1)) * 100, 1
            ),
        }


class SystemLoadSensor(OpenWrtSensor):
    """System 1-minute load average sensor."""

    def __init__(self, device, device_id: str):
        super().__init__(device, device_id)
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:cpu-64-bit"

    @property
    def unique_id(self):
        return "%s.system_load" % super().unique_id

    @property
    def name(self):
        return f"{super().name} System load"

    @property
    def native_value(self):
        load = self.data.get("system_info", {}).get("load", [0, 0, 0])
        if load and len(load) >= 1:
            return round(load[0] / 65536, 2)
        return 0

    @property
    def extra_state_attributes(self):
        load = self.data.get("system_info", {}).get("load", [0, 0, 0])
        if load and len(load) >= 3:
            return {
                "load_1min": round(load[0] / 65536, 2),
                "load_5min": round(load[1] / 65536, 2),
                "load_15min": round(load[2] / 65536, 2),
            }
        return {}


class SystemDiskSensor(OpenWrtSensor):
    """Disk usage sensor (swap, root, tmp)."""

    def __init__(self, device, device_id: str, disk_type: str):
        super().__init__(device, device_id)
        self._disk_type = disk_type
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:harddisk"
        self._attr_native_unit_of_measurement = "%"

    @property
    def unique_id(self):
        return f"{super().unique_id}.{self._disk_type}_usage"

    @property
    def name(self):
        return f"{super().name} {self._disk_type.capitalize()} usage"

    @property
    def native_value(self):
        disk_info = self.data.get("system_info", {}).get(self._disk_type, {})
        total = disk_info.get("total", 0)
        free = disk_info.get("free", 0)
        if total > 0:
            return round((1 - free / total) * 100, 1)
        return 0

    @property
    def extra_state_attributes(self):
        disk_info = self.data.get("system_info", {}).get(self._disk_type, {})
        return {
            "total_kb": disk_info.get("total", 0),
            "free_kb": disk_info.get("free", 0),
            "used_kb": disk_info.get("used", 0),
            "avail_kb": disk_info.get("avail", 0),
        }
