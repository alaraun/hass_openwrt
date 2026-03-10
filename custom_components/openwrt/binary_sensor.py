from homeassistant.config_entries import ConfigEntry
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant

import logging

from . import OpenWrtEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    device = entry.runtime_data
    device_id = entry.data["id"]

    entities = [OpenWrtBinarySensor(device, device_id)]

    for net_id in device.coordinator.data["mwan3"]:
        entities.append(Mwan3OnlineBinarySensor(device, device_id, net_id))

    if device.coordinator.data.get("modem"):
        entities.append(ModemRegisteredBinarySensor(device, device_id))

    async_add_entities(entities)
    return True


class OpenWrtBinarySensor(OpenWrtEntity, BinarySensorEntity):

    def __init__(self, device, device_id: str):
        super().__init__(device, device_id)

    @property
    def is_on(self):
        return True

    @property
    def device_class(self):
        return "connectivity"


class Mwan3OnlineBinarySensor(OpenWrtEntity, BinarySensorEntity):

    def __init__(self, device, device_id: str, interface: str):
        super().__init__(device, device_id)
        self._interface_id = interface

    @property
    def available(self):
        return self._interface_id in self.data["mwan3"]

    @property
    def unique_id(self):
        return "%s.%s.mwan3_online" % (super().unique_id, self._interface_id)

    @property
    def name(self):
        return f"{super().name} Mwan3 [{self._interface_id}] online"

    @property
    def is_on(self):
        data = self.data["mwan3"].get(self._interface_id, {})
        return data.get("online", False)

    @property
    def device_class(self):
        return "connectivity"

    @property
    def icon(self):
        return "mdi:access-point-network" if self.is_on else "mdi:access-point-network-off"


class ModemRegisteredBinarySensor(OpenWrtEntity, BinarySensorEntity):
    """Whether the modem is registered on the mobile network."""

    def __init__(self, device, device_id: str):
        super().__init__(device, device_id)

    @property
    def _modem(self) -> dict:
        return self.data.get("modem", {})

    @property
    def available(self):
        return bool(self._modem)

    @property
    def unique_id(self):
        return f"{super().unique_id}.modem_registered"

    @property
    def name(self):
        return f"{super().name} Modem registered"

    @property
    def is_on(self):
        return bool(self._modem.get("registered", False))

    @property
    def device_class(self):
        return "connectivity"

    @property
    def icon(self):
        return "mdi:signal-cellular-3" if self.is_on else "mdi:signal-cellular-off"
