"""Media source: recordings of each camera, from the local archive and the cloud."""

from __future__ import annotations

from datetime import timedelta
import time

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import VicoError
from .clip_view import archive_url, clip_url
from .const import DOMAIN
from .coordinator import VicoCoordinator
from .events import TYPE_LABEL_IT, event_title

BROWSE_LIMIT = 60
THUMB_TTL = timedelta(hours=1)


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    return VicoMediaSource(hass)


class VicoMediaSource(MediaSource):
    """Identifiers: "<entry_id>/<serial>" (camera) and "<entry_id>/<serial>/<traceId>" (clip)."""

    name = "VicoHome"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _coordinators(self) -> dict[str, VicoCoordinator]:
        return {e.entry_id: e.runtime_data for e in self.hass.config_entries.async_loaded_entries(DOMAIN)}

    def _coordinator(self, entry_id: str) -> VicoCoordinator:
        coord = self._coordinators().get(entry_id)
        if coord is None:
            raise Unresolvable("VicoHome account not loaded")
        return coord

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/")
        if len(parts) != 3:
            raise Unresolvable("Not a clip")
        coord = self._coordinator(parts[0])
        archive = coord.archive
        clip = archive.clips.get(parts[2]) if archive else None
        if clip and clip.video:
            return PlayMedia(archive_url(parts[0], clip.video), "video/mp4")
        # Not archived yet: the clip view remuxes the cloud HLS to MP4.
        return PlayMedia(clip_url(parts[0], parts[2]), "video/mp4")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        parts = [p for p in (item.identifier or "").split("/") if p]
        if not parts:
            return self._root()
        if len(parts) == 2:
            return await self._camera(parts[0], parts[1])
        raise Unresolvable("Unknown item")

    def _root(self) -> BrowseMediaSource:
        children = []
        for entry_id, coord in self._coordinators().items():
            for serial, cam in (coord.data or {}).items():
                children.append(BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{entry_id}/{serial}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.VIDEO,
                    title=cam.device.get("deviceName") or serial,
                    can_play=False,
                    can_expand=True,
                    thumbnail=cam.snapshot_url,
                ))
        return BrowseMediaSource(
            domain=DOMAIN, identifier=None, media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO, title="VicoHome", can_play=False, can_expand=True,
            children=children, children_media_class=MediaClass.DIRECTORY,
        )

    async def _camera(self, entry_id: str, serial: str) -> BrowseMediaSource:
        coord = self._coordinator(entry_id)
        cam = (coord.data or {}).get(serial)
        archive = coord.archive
        days = archive.days if archive else 7
        cloud_days = min(days or 30, 30)  # the cloud keeps far less than a long local archive
        items: dict[str, tuple[float, BrowseMediaSource]] = {}
        now = int(time.time())
        try:
            events = await coord.api.library([serial], start=now - cloud_days * 86400, end=now, limit=BROWSE_LIMIT)
        except VicoError:
            events = []  # cloud down: the local archive is still there
        for ev in events:
            if not ev.get("traceId"):
                continue
            items[ev["traceId"]] = (ev.get("timestamp") and float(ev["timestamp"]) or 0, BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{entry_id}/{serial}/{ev['traceId']}",
                media_class=MediaClass.VIDEO,
                media_content_type=MediaType.VIDEO,
                title=event_title(ev) + ("" if archive and ev["traceId"] in archive.clips else " ☁"),
                can_play=bool(ev.get("videoUrl")),
                can_expand=False,
                thumbnail=ev.get("imageUrl"),
            ))
        for clip in archive.for_camera(serial) if archive else []:
            thumb = None
            if clip.image:
                thumb = async_sign_path(self.hass, archive_url(entry_id, clip.image), THUMB_TTL)
            when = dt_util.as_local(dt_util.utc_from_timestamp(clip.ts)).strftime("%d/%m %H:%M:%S")
            label = "Live registrata" if clip.kind == "live" else TYPE_LABEL_IT.get(clip.kind, clip.kind)
            secs = f" ({clip.period:.0f} s)" if clip.period else ""
            items[clip.trace_id] = (clip.ts, BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{entry_id}/{serial}/{clip.trace_id}",
                media_class=MediaClass.VIDEO,
                media_content_type=MediaType.VIDEO,
                title=f"{when} · {label}{secs}",
                can_play=True,
                can_expand=False,
                thumbnail=thumb,
            ))
        # Cloud timestamps may be in ms: normalise before sorting.
        children = [b for _, b in sorted(items.values(), key=lambda t: t[0] / 1000 if t[0] > 1e12 else t[0], reverse=True)]
        if not children and not events and archive is None:
            raise Unresolvable("No recordings")
        name = cam.device.get("deviceName") if cam else serial
        return BrowseMediaSource(
            domain=DOMAIN, identifier=f"{entry_id}/{serial}", media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=f"{name} (ultimi {days} giorni)" if days else f"{name} (archivio completo)",
            can_play=False, can_expand=True, children=children, children_media_class=MediaClass.VIDEO,
        )
