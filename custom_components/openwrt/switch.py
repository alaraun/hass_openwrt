from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
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

    async_add_entities(
        WirelessWpsSwitch(device, device_id, net_id)
        for net_id, info in device.data["wireless"].items()
        if "wps" in info
    )


class WirelessWpsSwitch(OpenWrtEntity, SwitchEntity):

    _attr_icon = "mdi:security"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device, device_id, interface: str) -> None:
        super().__init__(device, device_id)
        self._interface_id = interface
        self._attr_unique_id = f"{self._attr_unique_id}.{interface}.wps"
        self._attr_name = f"Wireless [{interface}] WPS toggle"

    @property
    def available(self) -> bool:
        return super().available and self._interface_id in self.data.get("wireless", {})

    @property
    def is_on(self) -> bool:
        return self.data.get("wireless", {}).get(self._interface_id, {}).get("wps", False)

    async def async_turn_on(self, **kwargs) -> None:
        await self._device.set_wps(self._interface_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._device.set_wps(self._interface_id, False)
