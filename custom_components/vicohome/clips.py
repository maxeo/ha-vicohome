"""One list of recordings per camera: local archive plus cloud items not downloaded yet."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import time

from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant

from .api import VicoError
from .clip_view import archive_url, clip_url
from .coordinator import VicoCoordinator, ts_seconds
from .events import TYPE_LABEL_IT, event_type

CLOUD_LIMIT = 60
SIGN_TTL = timedelta(hours=1)


@dataclass
class Clip:
    id: str
    ts: float
    kind: str
    duration: float | None
    archived: bool
    playable: bool
    thumbnail: str | None  # ready to use in <img> (signed, or the cloud's own URL)
    video_path: str | None  # relative path of the MP4 view, not signed yet

    @property
    def label(self) -> str:
        return TYPE_LABEL_IT.get(self.kind, self.kind)


async def async_list_clips(hass: HomeAssistant, coord: VicoCoordinator, entry_id: str, serial: str) -> list[Clip]:
    """Newest first. The cloud is read for the archive period (at most 30 days)."""
    archive = coord.archive
    days = archive.days if archive else 7
    cloud_days = min(days or 30, 30)  # the cloud keeps far less than a long local archive
    clips: dict[str, Clip] = {}
    now = int(time.time())
    try:
        events = await coord.api.library([serial], start=now - cloud_days * 86400, end=now, limit=CLOUD_LIMIT)
    except VicoError:
        events = []  # cloud down: the local archive is still there
    for ev in events:
        trace = ev.get("traceId")
        if not trace:
            continue
        period = ev.get("period")
        clips[trace] = Clip(
            id=trace,
            ts=ts_seconds(ev.get("timestamp")) or 0,
            kind=event_type(ev),
            duration=float(period) if isinstance(period, (int, float)) else None,
            archived=False,
            playable=bool(ev.get("videoUrl")),
            thumbnail=ev.get("imageUrl"),
            video_path=clip_url(entry_id, trace) if ev.get("videoUrl") else None,
        )
    for item in archive.for_camera(serial) if archive else []:
        clips[item.trace_id] = Clip(
            id=item.trace_id,
            ts=item.ts,
            kind=item.kind,
            duration=item.period,
            archived=True,
            playable=True,
            thumbnail=async_sign_path(hass, archive_url(entry_id, item.image), SIGN_TTL) if item.image else None,
            video_path=archive_url(entry_id, item.video) if item.video else None,
        )
    return sorted(clips.values(), key=lambda c: c.ts, reverse=True)
