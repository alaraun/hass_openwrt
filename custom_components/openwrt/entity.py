"""Base entity of the OpenWrt integration."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import DeviceCoordinator


class OpenWrtEntity(CoordinatorEntity[DeviceCoordinator]):
    """An entity of one OpenWrt device.

    The entity name is a suffix of the device name `OpenWrt [<id>]`. Unique ids
    start with `sensor.openwrt.<id>` on every platform.
    """

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, device: DeviceCoordinator, device_id: str) -> None:
        super().__init__(device)
        self._device_id = device_id
        self._device = device
        self._attr_unique_id = f"sensor.openwrt.{device_id}"
        info = (device.data or {}).get("info", {})
        self._attr_device_info = DeviceInfo(
            # ("id", <id>) is the registered identifier of existing devices; keep it
            identifiers={("id", device_id)},
            name=f"OpenWrt [{device_id}]",
            model=info.get("model"),
            manufacturer=info.get("manufacturer"),
            sw_version=info.get("sw_version"),
        )

    @property
    def data(self) -> dict[str, Any]:
        return self.coordinator.data
