"""Event entity: fires on new cloud events (ring, person, motion...)."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import VicoConfigEntry, VicoCoordinator
from .entity import VicoEntity
from .events import EVENT_TYPES, PUSH_TYPE_TO_EVENT, event_tags, event_type
from .archive import live_signal
from .push import push_signal


async def async_setup_entry(
    hass: HomeAssistant, entry: VicoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(VicoEvent(coordinator, sn) for sn in coordinator.data)


class VicoEvent(VicoEntity, EventEntity):
    """Triggers once per new library item (detected by cloud polling, up to ~30 s late)."""

    _attr_translation_key = "activity"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: VicoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "event")
        dev = self.cam.device
        is_doorbell = str(dev.get("displayModelNo") or "").upper().startswith("DB") or any(
            "DOORBELL" in str(ev.get("doorbellTags") or "").upper() for ev in self.cam.events
        )
        self._attr_device_class = EventDeviceClass.DOORBELL if is_doorbell else EventDeviceClass.MOTION
        # Events already in the cloud at startup are history, not new.
        self._seen: set[str] = {ev.get("traceId") for ev in self.cam.events if ev.get("traceId")}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, push_signal(self.serial), self._handle_push))
        self.async_on_remove(async_dispatcher_connect(self.hass, live_signal(self.serial), self._handle_live))

    @callback
    def _handle_live(self, clip: dict) -> None:
        """A live view was recorded: list it with the other activity."""
        self._trigger_event("live", {
            "trace_id": clip.get("trace_id"),
            "source": "live",
            "started": dt_util.utc_from_timestamp(clip["ts"]).isoformat() if clip.get("ts") else None,
            "duration_s": clip.get("period"),
        })
        self.async_write_ha_state()

    @callback
    def _handle_push(self, data: dict) -> None:
        """Instant trigger from the FCM push; the library item comes later with the same traceId."""
        trace = data.get("traceId")
        if not trace or trace in self._seen:
            return
        self._seen.add(trace)
        self._trigger_event(PUSH_TYPE_TO_EVENT.get(str(data.get("type")), "motion"), {
            "trace_id": trace,
            "source": "push",
            "push_type": data.get("type"),
        })
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        new = [ev for ev in self.cam.events if ev.get("traceId") and ev["traceId"] not in self._seen]
        for ev in reversed(new):  # oldest first
            self._seen.add(ev["traceId"])
            self._trigger_event(event_type(ev), {
                "tags": event_tags(ev),
                "trace_id": ev.get("traceId"),
                "image_url": ev.get("imageUrl"),
                "video_url": ev.get("videoUrl"),
                "duration_s": ev.get("period"),
            })
        super()._handle_coordinator_update()
