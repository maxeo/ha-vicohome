"""Base entity for VicoHome cameras."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CameraData, VicoCoordinator


class VicoEntity(CoordinatorEntity[VicoCoordinator]):
    """One entity bound to one camera serial."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VicoCoordinator, serial: str, key: str) -> None:
        super().__init__(coordinator)
        self.serial = serial
        self._attr_unique_id = f"{serial}_{key}"
        dev = self.cam.device
        info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="VicoHome",
            model=dev.get("displayModelNo") or dev.get("modelNo"),
            model_id=dev.get("modelNo"),
            name=dev.get("deviceName") or serial,
            serial_number=dev.get("userSn") or serial,
            sw_version=dev.get("firmwareId") or dev.get("firmwareVersion"),
            suggested_area=dev.get("locationName"),
        )
        if dev.get("macAddress"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, dev["macAddress"].lower())}
        self._attr_device_info = info

    @property
    def cam(self) -> CameraData:
        return self.coordinator.data[self.serial]

    @property
    def available(self) -> bool:
        return super().available and self.serial in self.coordinator.data
