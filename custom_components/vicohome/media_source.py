"""Media source: recordings of each camera, from the local archive and the cloud."""

from __future__ import annotations

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .clip_view import archive_url, clip_url
from .clips import async_list_clips
from .const import DOMAIN
from .coordinator import VicoCoordinator
from .events import ui_language

async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    return VicoMediaSource(hass)


# Title of a camera folder: archive of N days, or kept forever.
FOLDER_TITLE = {
    "it": ("ultimi {days} giorni", "archivio completo"),
    "en": ("last {days} days", "full archive"),
    "de": ("letzte {days} Tage", "gesamtes Archiv"),
}

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
        days = coord.archive.days if coord.archive else 7
        lang = ui_language(self.hass.config.language)
        children = []
        for clip in await async_list_clips(self.hass, coord, entry_id, serial):
            when = dt_util.as_local(dt_util.utc_from_timestamp(clip.ts)).strftime("%d/%m %H:%M:%S") if clip.ts else "?"
            secs = f" ({clip.duration:.0f} s)" if clip.duration else ""
            children.append(BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{entry_id}/{serial}/{clip.id}",
                media_class=MediaClass.VIDEO,
                media_content_type=MediaType.VIDEO,
                title=f"{when} · {clip.label(lang)}{secs}" + ("" if clip.archived else " ☁"),
                can_play=clip.playable,
                can_expand=False,
                thumbnail=clip.thumbnail,
            ))
        name = cam.device.get("deviceName") if cam else serial
        return BrowseMediaSource(
            domain=DOMAIN, identifier=f"{entry_id}/{serial}", media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=f"{name} ({FOLDER_TITLE[lang][0].format(days=days)})" if days else f"{name} ({FOLDER_TITLE[lang][1]})",
            can_play=False, can_expand=True, children=children, children_media_class=MediaClass.VIDEO,
        )
