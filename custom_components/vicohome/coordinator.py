"""Cloud polling for VicoHome: devices, cover images and recent events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VicoApi, VicoAuthError, VicoError
from .const import DOMAIN, LIBRARY_WINDOW_S, UPDATE_INTERVAL_S

_LOGGER = logging.getLogger(__name__)


def ts_seconds(value: Any) -> float | None:
    """Timestamps come as seconds or milliseconds, as int or string."""
    try:
        t = float(value)
    except (TypeError, ValueError):
        return None
    return t / 1000 if t > 1e12 else t


@dataclass
class CameraData:
    """Everything known about one camera after a poll."""

    device: dict
    cover_url: str | None = None
    cover_time: float | None = None
    events: list[dict] = field(default_factory=list)

    @property
    def last_event(self) -> dict | None:
        return self.events[0] if self.events else None

    @property
    def snapshot_url(self) -> str | None:
        """Newest still image: cover image or last event image."""
        ev = self.last_event
        ev_t = ts_seconds(ev.get("timestamp")) if ev else None
        if ev and ev.get("imageUrl") and (self.cover_time is None or (ev_t or 0) >= self.cover_time):
            return ev["imageUrl"]
        return self.cover_url or (ev.get("imageUrl") if ev else None)


type VicoConfigEntry = ConfigEntry[VicoCoordinator]


class VicoCoordinator(DataUpdateCoordinator[dict[str, CameraData]]):
    """Poll the VicoHome cloud."""

    config_entry: VicoConfigEntry

    def __init__(self, hass: HomeAssistant, entry: VicoConfigEntry, api: VicoApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_S),
        )
        self.api = api
        # Raw payloads of the last poll, for diagnostics (field names vary by model).
        self.raw: dict[str, Any] = {}
        self.push: Any = None
        self.archive: Any = None

    async def _async_update_data(self) -> dict[str, CameraData]:
        try:
            devices = await self.api.devices()
            try:
                covers = await self.api.cover_images()
            except VicoError as err:
                _LOGGER.debug("Cover images not available: %s", err)
                covers = []
            now = int(time.time())
            serials = [d["serialNumber"] for d in devices if d.get("serialNumber")]
            events = await self.api.library(serials, start=now - LIBRARY_WINDOW_S, end=now, limit=30) if serials else []
        except VicoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (VicoError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(str(err)) from err

        self.raw = {"devices": devices, "covers": covers, "events": events[:5]}
        data: dict[str, CameraData] = {}
        for dev in devices:
            sn = dev.get("serialNumber")
            if sn:
                data[sn] = CameraData(device=dev)
        for c in covers:
            cam = data.get(c.get("serialNumber"))
            if cam:
                cam.cover_url = c.get("lastPushImageUrl")
                cam.cover_time = ts_seconds(c.get("lastPushTime"))
        for ev in sorted(events, key=lambda e: ts_seconds(e.get("timestamp")) or 0, reverse=True):
            cam = data.get(ev.get("serialNumber"))
            if cam:
                cam.events.append(ev)
        return data
