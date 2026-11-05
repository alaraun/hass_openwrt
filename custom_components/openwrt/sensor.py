from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import MATCH_ALL, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OpenWrtConfigEntry
from .entity import OpenWrtEntity

_LOGGER = logging.getLogger(__name__)

# A boot time computed from a later poll may differ by the poll's latency and the
# whole-second uptime; a reboot is recognised by the uptime going down.
BOOT_TIME_TOLERANCE = timedelta(seconds=60)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenWrtConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = entry.runtime_data
    device_id = entry.data["id"]
    data = device.data

    entities: list[SensorEntity] = []

    entities.append(HostsSensor(device, device_id))

    wireless = []
    for net_id in data["wireless"]:
        sensor = WirelessClientsSensor(device, device_id, net_id)
        wireless.append(sensor)
        entities.append(sensor)
    if wireless:
        entities.append(WirelessTotalClientsSensor(device, device_id, wireless))

    for net_id in data["mesh"]:
        entities.append(MeshSignalSensor(device, device_id, net_id))
        entities.append(MeshPeersSensor(device, device_id, net_id))

    mwan3_data = data.get("mwan3", {})
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
            except (TypeError, ValueError) as err:
                _LOGGER.warning(
                    "Skipping mwan3 entry '%s' for device %s: invalid data (%s)",
                    net_id, device_id, err,
                )
                continue
            entities.append(Mwan3OnlineSensor(device, device_id, net_id))

    for net_id in data["wan"]:
        entities.append(WanRxTxSensor(device, device_id, net_id, "rx"))
        entities.append(WanRxTxSensor(device, device_id, net_id, "tx"))

    if system_info := data.get("system_info"):
        entities.append(SystemUptimeSensor(device, device_id))
        entities.append(SystemLoadSensor(device, device_id))
        entities.append(SystemMemorySensor(device, device_id))
        for disk in ("swap", "root", "tmp"):
            if system_info.get(disk, {}).get("total", 0) > 0:
                entities.append(SystemDiskSensor(device, device_id, disk))

    if data.get("modem"):
        entities.append(ModemModeSensor(device, device_id))
        entities.append(ModemSignalSensor(device, device_id, "lte_rsrp", "LTE RSRP", "dBm", SensorDeviceClass.SIGNAL_STRENGTH))
        entities.append(ModemSignalSensor(device, device_id, "lte_rsrq", "LTE RSRQ", "dB", None))
        entities.append(ModemSignalSensor(device, device_id, "lte_rssi", "LTE RSSI", "dBm", SensorDeviceClass.SIGNAL_STRENGTH))
        entities.append(ModemSignalSensor(device, device_id, "lte_sinr", "LTE SINR", "dB", None))
        entities.append(ModemSignalSensor(device, device_id, "nr_rsrp", "NR RSRP", "dBm", SensorDeviceClass.SIGNAL_STRENGTH))
        entities.append(ModemSignalSensor(device, device_id, "nr_rsrq", "NR RSRQ", "dB", None))
        entities.append(ModemSignalSensor(device, device_id, "nr_sinr", "NR SINR", "dB", None))
        entities.append(ModemSignalSensor(device, device_id, "temp", "Modem temp", "°C", SensorDeviceClass.TEMPERATURE))
        entities.append(ModemTrafficSensor(device, device_id, "rx_bytes", "RX bytes", "B", SensorDeviceClass.DATA_SIZE))
        entities.append(ModemTrafficSensor(device, device_id, "tx_bytes", "TX bytes", "B", SensorDeviceClass.DATA_SIZE))
        entities.append(ModemTrafficSensor(device, device_id, "rx_packets", "RX packets", "packets", None))
        entities.append(ModemTrafficSensor(device, device_id, "tx_packets", "TX packets", "packets", None))
        entities.append(ModemTimestampSensor(device, device_id))

    async_add_entities(entities)

    # Wireless and mesh interfaces that appear later get their sensors on the next
    # poll; vanished ones turn their sensors unavailable (see `available`).
    known_wireless: set[str] = set(data["wireless"])
    known_mesh: set[str] = set(data["mesh"])

    @callback
    def _on_coordinator_update() -> None:
        current = device.data or {}
        new_entities: list[SensorEntity] = []
        for net_id in set(current.get("wireless", {})) - known_wireless:
            sensor = WirelessClientsSensor(device, device_id, net_id)
            # the total sensor sums this list
            wireless.append(sensor)
            new_entities.append(sensor)
            known_wireless.add(net_id)
        for net_id in set(current.get("mesh", {})) - known_mesh:
            new_entities.append(MeshSignalSensor(device, device_id, net_id))
            new_entities.append(MeshPeersSensor(device, device_id, net_id))
            known_mesh.add(net_id)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(device.async_add_listener(_on_coordinator_update))


class OpenWrtSensor(OpenWrtEntity, SensorEntity):
    """Base class for OpenWrt sensors."""

    _attr_state_class = SensorStateClass.MEASUREMENT


class WirelessClientsSensor(OpenWrtSensor):
    """Number of clients on a wireless interface."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # per-client signal, IP and name: live only, they change on most polls
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, device, device_id: str, interface: str) -> None:
        super().__init__(device, device_id)
        self._interface_id = interface
        self._attr_unique_id = f"{self._attr_unique_id}.{interface}.clients"

    @property
    def interface_id(self) -> str:
        return self._interface_id

    @property
    def _iface(self) -> dict:
        return self.data.get("wireless", {}).get(self._interface_id, {})

    @property
    def available(self) -> bool:
        return super().available and self._interface_id in self.data.get("wireless", {})

    @property
    def name(self):
        return "Wireless [%s] clients" % self._iface.get("ssid", self._interface_id)

    @property
    def native_value(self) -> int:
        return self._iface.get("clients", 0)

    @property
    def icon(self):
        return "mdi:wifi-off" if self.native_value == 0 else "mdi:wifi"

    @property
    def extra_state_attributes(self):
        result = dict()
        data = self._iface
        hosts = {mac.lower(): info for mac, info in self.data.get("hosts", {}).items()}
        for mac, value in data.get("macs", {}).items():
            host = hosts.get(mac.lower(), {})
            client_info = f"{value.get('signal', 0)} dBm"
            if host.get("ip"):
                client_info += f" | IP: {host['ip']}"
            if host.get("name"):
                client_info += f" | Name: {host['name']}"
            result[mac.upper()] = client_info
        if "ssid" in data:
            result["ssid"] = data["ssid"]
        return result


class MeshSignalSensor(OpenWrtSensor):
    """Signal strength of a mesh interface."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"

    def __init__(self, device, device_id: str, interface: str) -> None:
        super().__init__(device, device_id)
        self._interface_id = interface
        self._attr_unique_id = f"{self._attr_unique_id}.{interface}.mesh_signal"
        self._attr_name = f"Mesh [{interface}] signal"

    @property
    def available(self) -> bool:
        return super().available and self._interface_id in self.data.get("mesh", {})

    @property
    def native_value(self):
        return self.data.get("mesh", {}).get(self._interface_id, {}).get("signal", None)

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
        value = self.native_value
        if value is None:
            return icons[-1]
        for idx, level in enumerate([-50, -60, -67, -70, -80]):
            if value >= level:
                return icons[idx]
        return icons[-1]


class MeshPeersSensor(OpenWrtSensor):
    """Number of active mesh peers on a mesh interface."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, device, device_id: str, interface: str) -> None:
        super().__init__(device, device_id)
        self._interface_id = interface
        self._attr_unique_id = f"{self._attr_unique_id}.{interface}.mesh_peers"
        self._attr_name = f"Mesh [{interface}] peers"

    @property
    def _peers(self) -> dict:
        return self.data.get("mesh", {}).get(self._interface_id, {}).get("peers", {})

    @property
    def available(self) -> bool:
        return super().available and self._interface_id in self.data.get("mesh", {})

    @property
    def native_value(self) -> int:
        return len([p for p in self._peers.values() if p.get("active")])

    @property
    def icon(self):
        return "mdi:server-network" if self.native_value > 0 else "mdi:server-network-off"

    @property
    def extra_state_attributes(self):
        return {
            mac.upper(): f"{value.get('signal', 0)} dBm"
            for mac, value in self._peers.items()
        }


class WirelessTotalClientsSensor(OpenWrtSensor):
    """Total number of clients across all wireless interfaces."""

    _unrecorded_attributes = frozenset({MATCH_ALL})
    _attr_name = "Wireless total clients"

    def __init__(self, device, device_id: str, sensors: list[WirelessClientsSensor]) -> None:
        super().__init__(device, device_id)
        self._sensors = sensors
        self._attr_unique_id = f"{self._attr_unique_id}.total_clients"

    @property
    def native_value(self) -> int:
        return sum(s.native_value for s in self._sensors if s.available)

    @property
    def icon(self):
        return "mdi:wifi-off" if self.native_value == 0 else "mdi:wifi"

    @property
    def extra_state_attributes(self):
        result = {}
        for sensor in self._sensors:
            if not sensor.available:
                continue
            ssid = sensor.data["wireless"][sensor.interface_id].get(
                "ssid", sensor.interface_id
            )
            result[ssid] = sensor.native_value
        return result


class Mwan3OnlineSensor(OpenWrtSensor):
    """Online ratio of a WAN interface managed by mwan3."""

    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:router-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device, device_id: str, interface: str) -> None:
        super().__init__(device, device_id)
        self._interface_id = interface
        self._attr_unique_id = f"{self._attr_unique_id}.{interface}.mwan3_online_ratio"
        self._attr_name = f"Mwan3 [{interface}] online ratio"

    @property
    def available(self) -> bool:
        return super().available and self._interface_id in self.data.get("mwan3", {})

    @property
    def native_value(self):
        data = self.data["mwan3"].get(self._interface_id, {})
        uptime = data.get("uptime_sec")
        online = data.get("online_sec")
        return round(online / uptime * 100, 1) if uptime else 100


class WanRxTxSensor(OpenWrtSensor):
    """RX or TX byte counter for a WAN interface."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = "B"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, device, device_id: str, interface: str, code: str) -> None:
        super().__init__(device, device_id)
        self._interface = interface
        self._code = code
        self._attr_icon = "mdi:download-network" if code == "rx" else "mdi:upload-network"
        self._attr_unique_id = f"{self._attr_unique_id}.{interface}.wan_{code}_bytes"
        self._attr_name = f"Wan [{interface}] {code.capitalize()} bytes"

    @property
    def _data(self) -> dict:
        return self.data.get("wan", {}).get(self._interface, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._data.get("up"))

    @property
    def native_value(self):
        return self._data.get(f"{self._code}_bytes")

    @property
    def extra_state_attributes(self):
        return dict(mac=self._data.get("mac"), speed=self._data.get("speed"))


class HostsSensor(OpenWrtSensor):
    """Number of known hosts in the router."""

    _attr_icon = "mdi:devices"
    _attr_name = "Known Hosts"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, device, device_id: str) -> None:
        super().__init__(device, device_id)
        self._attr_unique_id = f"{self._attr_unique_id}.hosts"

    @property
    def native_value(self) -> int:
        return len(self.data.get("hosts", {}))

    @property
    def extra_state_attributes(self):
        ip_to_host = {}
        for mac, host_data in self.data.get("hosts", {}).items():
            if ip := host_data.get("ip", ""):
                ip_to_host[ip] = {"name": host_data.get("name", ""), "mac": mac}
        return {
            ip: f"{ip_to_host[ip]['name'] or 'Unknown'} : {ip_to_host[ip]['mac']}"
            for ip in sorted(ip_to_host, key=self._sort_ip)
        }

    @staticmethod
    def _sort_ip(ip):
        try:
            return [int(n) for n in ip.split(".")]
        except (ValueError, AttributeError):
            return [999, 999, 999, 999]


class _SystemInfoSensor(OpenWrtSensor):
    """A sensor of the `system info` data; unavailable while it could not be read."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def _system_info(self) -> dict:
        return self.data.get("system_info", {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._system_info)


class SystemUptimeSensor(_SystemInfoSensor):
    """Boot time of the device (state changes only on a reboot)."""

    _attr_icon = "mdi:clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_state_class = None
    _attr_name = "System uptime"

    def __init__(self, device, device_id: str) -> None:
        super().__init__(device, device_id)
        self._attr_unique_id = f"{self._attr_unique_id}.system_uptime"
        self._boot_time: datetime | None = None
        self._last_uptime = 0
        self._update_boot_time()

    def _update_boot_time(self) -> None:
        uptime = self._system_info.get("uptime", 0)
        if not uptime:
            return
        boot_time = dt_util.utcnow().replace(microsecond=0) - timedelta(seconds=uptime)
        if (
            self._boot_time is None
            or uptime < self._last_uptime
            or abs(boot_time - self._boot_time) > BOOT_TIME_TOLERANCE
        ):
            self._boot_time = boot_time
        self._last_uptime = uptime

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_boot_time()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> datetime | None:
        return self._boot_time


class SystemMemorySensor(_SystemInfoSensor):
    """Free system memory sensor."""

    _attr_icon = "mdi:memory"
    _attr_native_unit_of_measurement = "MB"
    _attr_name = "System memory free"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, device, device_id: str) -> None:
        super().__init__(device, device_id)
        self._attr_unique_id = f"{self._attr_unique_id}.system_memory_free"

    @property
    def native_value(self):
        memory = self._system_info.get("memory", {})
        return round(memory.get("free", 0) / (1024 * 1024), 1)

    @property
    def extra_state_attributes(self):
        memory = self._system_info.get("memory", {})
        return {
            "total_mb": round(memory.get("total", 0) / (1024 * 1024), 1),
            "free_mb": round(memory.get("free", 0) / (1024 * 1024), 1),
            "shared_mb": round(memory.get("shared", 0) / (1024 * 1024), 1),
            "cached_mb": round(memory.get("cached", 0) / (1024 * 1024), 1),
            "available_mb": round(memory.get("available", 0) / (1024 * 1024), 1),
            "used_percent": round(
                (1 - memory.get("free", 0) / (memory.get("total") or 1)) * 100, 1
            ),
        }


class SystemLoadSensor(_SystemInfoSensor):
    """System 1-minute load average sensor."""

    _attr_icon = "mdi:cpu-64-bit"
    _attr_name = "System load"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, device, device_id: str) -> None:
        super().__init__(device, device_id)
        self._attr_unique_id = f"{self._attr_unique_id}.system_load"

    @property
    def _load(self) -> list:
        return self._system_info.get("load") or [0, 0, 0]

    @property
    def native_value(self):
        # system.info reports the load averages in 1/65536 units
        return round(self._load[0] / 65536, 2)

    @property
    def extra_state_attributes(self):
        load = self._load
        if len(load) >= 3:
            return {
                "load_1min": round(load[0] / 65536, 2),
                "load_5min": round(load[1] / 65536, 2),
                "load_15min": round(load[2] / 65536, 2),
            }
        return {}


class SystemDiskSensor(_SystemInfoSensor):
    """Disk usage sensor (swap, root, tmp)."""

    _attr_icon = "mdi:harddisk"
    _attr_native_unit_of_measurement = "%"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, device, device_id: str, disk_type: str) -> None:
        super().__init__(device, device_id)
        self._disk_type = disk_type
        self._attr_unique_id = f"{self._attr_unique_id}.{disk_type}_usage"
        self._attr_name = f"{disk_type.capitalize()} usage"

    @property
    def native_value(self):
        disk_info = self._system_info.get(self._disk_type, {})
        total = disk_info.get("total", 0)
        free = disk_info.get("free", 0)
        if total > 0:
            return round((1 - free / total) * 100, 1)
        return 0

    @property
    def extra_state_attributes(self):
        disk_info = self._system_info.get(self._disk_type, {})
        return {
            "total_kb": disk_info.get("total", 0),
            "free_kb": disk_info.get("free", 0),
            "used_kb": disk_info.get("used", 0),
            "avail_kb": disk_info.get("avail", 0),
        }


class _ModemSensor(OpenWrtSensor):
    """A sensor of the modem stats file; unavailable while the file is missing."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def _modem(self) -> dict:
        return self.data.get("modem", {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._modem)


class ModemModeSensor(_ModemSensor):
    """Current connection mode (LTE / 5G NSA / 5G SA) with the modem's details as attributes."""

    _attr_icon = "mdi:signal-5g"
    _attr_state_class = None
    _attr_name = "Modem mode"

    def __init__(self, device, device_id: str) -> None:
        super().__init__(device, device_id)
        self._attr_unique_id = f"{self._attr_unique_id}.modem_mode"

    @property
    def native_value(self):
        return self._modem.get("mode")

    @property
    def extra_state_attributes(self):
        # The collection time is the "Modem last update" sensor, not an attribute
        # here, so the attributes change only with the cell or the registration.
        m = self._modem
        return {
            key: m.get(key)
            for key in (
                "operator", "mcc_mnc", "bands", "pcc_band", "nr_band", "registered",
                "tac_hex", "tac_dec", "cell_id_hex", "cell_id_dec", "lte_pci",
                "lte_earfcn", "nr_pci", "nr_earfcn", "firmware", "model",
                "manufacturer", "imei", "iccid", "sim_slot", "proto", "iface",
            )
        }


class ModemSignalSensor(_ModemSensor):
    """Single numeric modem signal metric (RSRP, RSRQ, SINR, temp, ...)."""

    def __init__(self, device, device_id: str, key: str, label: str, unit: str, device_class) -> None:
        super().__init__(device, device_id)
        self._key = key
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_unique_id = f"{self._attr_unique_id}.modem_{key}"
        self._attr_name = f"Modem {label}"

    @property
    def available(self) -> bool:
        return super().available and self._modem.get(self._key) is not None

    @property
    def icon(self):
        if "temp" in self._key:
            return "mdi:thermometer"
        val = self.native_value
        if val is None:
            return "mdi:signal-cellular-outline"
        if "rsrp" in self._key:
            # RSRP: strong >= -80, medium >= -100, low < -100 dBm
            level = 3 if val >= -80 else 2 if val >= -100 else 1
        elif "rsrq" in self._key:
            # RSRQ: strong >= -10, medium >= -15, low < -15 dB
            level = 3 if val >= -10 else 2 if val >= -15 else 1
        elif "rssi" in self._key:
            # RSSI: strong >= -65, medium >= -95, low < -95 dBm
            level = 3 if val >= -65 else 2 if val >= -95 else 1
        else:
            # SINR: strong >= 10, medium >= 3, low < 3 dB
            level = 3 if val >= 10 else 2 if val >= 3 else 1
        return f"mdi:signal-cellular-{level}"

    @property
    def native_value(self):
        val = self._modem.get(self._key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None


class ModemTrafficSensor(_ModemSensor):
    """Cumulative RX/TX byte or packet counter for the modem interface."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, device, device_id: str, key: str, label: str, unit: str, device_class) -> None:
        super().__init__(device, device_id)
        self._key = key
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_icon = "mdi:download-network" if key.startswith("rx") else "mdi:upload-network"
        self._attr_unique_id = f"{self._attr_unique_id}.modem_{key}"
        self._attr_name = f"Modem {label}"

    @property
    def available(self) -> bool:
        return super().available and self._modem.get(self._key) is not None

    @property
    def native_value(self):
        val = self._modem.get(self._key)
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None


class ModemTimestampSensor(_ModemSensor):
    """Timestamp of the last modem stats collection run."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"
    _attr_state_class = None
    _attr_name = "Modem last update"

    def __init__(self, device, device_id: str) -> None:
        super().__init__(device, device_id)
        self._attr_unique_id = f"{self._attr_unique_id}.modem_timestamp"

    @property
    def native_value(self):
        ts = self._modem.get("timestamp")
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(int(ts), tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
