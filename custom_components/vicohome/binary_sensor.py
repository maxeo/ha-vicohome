"""VicoHome binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import RECENT_EVENT_S
from .coordinator import CameraData, VicoConfigEntry, ts_seconds
from .entity import VicoEntity

ONLINE_KEYS = ("online", "onlineStatus", "deviceStatus")


def _online(cam: CameraData) -> bool | None:
    for key in ONLINE_KEYS:
        if key in cam.device:
            v = cam.device[key]
            return v in (1, True, "1", "online", "ONLINE")
    return None


def _recent(cam: CameraData) -> bool:
    ev = cam.last_event
    t = ts_seconds(ev.get("timestamp")) if ev else None
    return bool(t and time.time() - t < RECENT_EVENT_S)


@dataclass(frozen=True, kw_only=True)
class VicoBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[CameraData], bool | None]
    exists_fn: Callable[[CameraData], bool] = lambda cam: True


BINARY_SENSORS: tuple[VicoBinaryDescription, ...] = (
    VicoBinaryDescription(
        key="recent_event",
        translation_key="recent_event",
        device_class=BinarySensorDeviceClass.MOTION,
        value_fn=_recent,
    ),
    VicoBinaryDescription(
        key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda cam: bool(cam.device.get("isCharging")),
        exists_fn=lambda cam: "isCharging" in cam.device,
    ),
    VicoBinaryDescription(
        key="awake",
        translation_key="awake",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda cam: bool(cam.device.get("awake")),
        exists_fn=lambda cam: "awake" in cam.device,
    ),
    VicoBinaryDescription(
        key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_online,
        exists_fn=lambda cam: any(k in cam.device for k in ONLINE_KEYS),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: VicoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        VicoBinarySensor(coordinator, sn, desc)
        for sn, cam in coordinator.data.items()
        for desc in BINARY_SENSORS
        if desc.exists_fn(cam)
    )


class VicoBinarySensor(VicoEntity, BinarySensorEntity):
    entity_description: VicoBinaryDescription

    def __init__(self, coordinator, serial: str, description: VicoBinaryDescription) -> None:
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.cam)
