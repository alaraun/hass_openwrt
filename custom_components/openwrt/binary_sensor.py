from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OpenWrtConfigEntry
from .entity import OpenWrtEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenWrtConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = entry.runtime_data
    device_id = entry.data["id"]

    entities: list[BinarySensorEntity] = [OpenWrtBinarySensor(device, device_id)]

    for net_id in device.data["mwan3"]:
        entities.append(Mwan3OnlineBinarySensor(device, device_id, net_id))

    if device.data.get("modem"):
        entities.append(ModemRegisteredBinarySensor(device, device_id))

    async_add_entities(entities)


class OpenWrtBinarySensor(OpenWrtEntity, BinarySensorEntity):
    """On while the device answers; unavailable while a poll fails."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_is_on = True


class Mwan3OnlineBinarySensor(OpenWrtEntity, BinarySensorEntity):

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, device, device_id: str, interface: str) -> None:
        super().__init__(device, device_id)
        self._interface_id = interface
        self._attr_unique_id = f"{self._attr_unique_id}.{interface}.mwan3_online"
        self._attr_name = f"Mwan3 [{interface}] online"

    @property
    def available(self) -> bool:
        return super().available and self._interface_id in self.data.get("mwan3", {})

    @property
    def is_on(self) -> bool:
        return self.data["mwan3"].get(self._interface_id, {}).get("online", False)

    @property
    def icon(self):
        return "mdi:access-point-network" if self.is_on else "mdi:access-point-network-off"


class ModemRegisteredBinarySensor(OpenWrtEntity, BinarySensorEntity):
    """Whether the modem is registered on the mobile network."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Modem registered"

    def __init__(self, device, device_id: str) -> None:
        super().__init__(device, device_id)
        self._attr_unique_id = f"{self._attr_unique_id}.modem_registered"

    @property
    def _modem(self) -> dict:
        return self.data.get("modem", {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._modem)

    @property
    def is_on(self) -> bool:
        return bool(self._modem.get("registered", False))

    @property
    def icon(self):
        return "mdi:signal-cellular-3" if self.is_on else "mdi:signal-cellular-off"
