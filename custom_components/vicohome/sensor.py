"""VicoHome sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CameraData, VicoConfigEntry, ts_seconds
from .entity import VicoEntity
from .events import event_tags, event_type


def _event_time(cam: CameraData) -> datetime | None:
    ev = cam.last_event
    t = ts_seconds(ev.get("timestamp")) if ev else None
    return datetime.fromtimestamp(t, UTC) if t else None


@dataclass(frozen=True, kw_only=True)
class VicoSensorDescription(SensorEntityDescription):
    value_fn: Callable[[CameraData], Any]
    exists_fn: Callable[[CameraData], bool] = lambda cam: True


SENSORS: tuple[VicoSensorDescription, ...] = (
    VicoSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda cam: cam.device.get("batteryLevel"),
        exists_fn=lambda cam: cam.device.get("batteryLevel") is not None,
    ),
    VicoSensorDescription(
        key="signal",
        translation_key="signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda cam: cam.device.get("signalStrength"),
        exists_fn=lambda cam: cam.device.get("signalStrength") is not None,
    ),
    VicoSensorDescription(
        key="ip",
        translation_key="ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda cam: cam.device.get("ip"),
        exists_fn=lambda cam: "ip" in cam.device,
    ),
    VicoSensorDescription(
        key="wifi",
        translation_key="wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda cam: cam.device.get("networkName"),
        exists_fn=lambda cam: "networkName" in cam.device,
    ),
    VicoSensorDescription(
        key="last_event",
        translation_key="last_event",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_event_time,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: VicoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        VicoSensor(coordinator, sn, desc)
        for sn, cam in coordinator.data.items()
        for desc in SENSORS
        if desc.exists_fn(cam)
    )


class VicoSensor(VicoEntity, SensorEntity):
    entity_description: VicoSensorDescription

    def __init__(self, coordinator, serial: str, description: VicoSensorDescription) -> None:
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.cam)

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.entity_description.key != "last_event":
            return None
        ev = self.cam.last_event
        if not ev:
            return None
        return {
            "type": event_type(ev),
            "tags": event_tags(ev),
            "image_url": ev.get("imageUrl"),
            "video_url": ev.get("videoUrl"),
            "duration_s": ev.get("period"),
            "trace_id": ev.get("traceId"),
        }
