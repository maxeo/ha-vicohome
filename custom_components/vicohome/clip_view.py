"""Serve cloud recordings as MP4.

The cloud gives HLS whose first segments carry an AAC track the browser player
cannot parse (ffprobe: sample_rate=0), so hls.js fails with "Error with media stream
contents". ffmpeg remuxes the whole clip (no re-encoding) into a regular MP4.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import shutil
import tempfile

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .api import VicoError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL = "/api/vicohome/clip/{entry_id}/{trace_id}.mp4"
CACHE_DIR = Path(tempfile.gettempdir()) / "vicohome_clips"
CACHE_MAX = 30
_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def clip_url(entry_id: str, trace_id: str) -> str:
    return f"/api/vicohome/clip/{entry_id}/{trace_id}.mp4"


def archive_url(entry_id: str, name: str) -> str:
    return f"/api/vicohome/archive/{entry_id}/{name}"


_SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.(mp4|jpg)$")


class VicoArchiveView(HomeAssistantView):
    """Files of the local archive (signed URLs from the media source)."""

    url = "/api/vicohome/archive/{entry_id}/{name}"
    name = "api:vicohome:archive"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str, name: str) -> web.StreamResponse:
        if not _SAFE.match(entry_id) or not _SAFE_FILE.match(name) or ".." in name:
            raise web.HTTPBadRequest
        entry = self.hass.config_entries.async_get_entry(entry_id)
        archive = getattr(getattr(entry, "runtime_data", None), "archive", None)
        if entry is None or entry.domain != DOMAIN or archive is None:
            raise web.HTTPNotFound
        path = archive.path(name)
        if not await self.hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound
        ctype = "video/mp4" if name.endswith(".mp4") else "image/jpeg"
        return web.FileResponse(path, headers={"Content-Type": ctype, "Cache-Control": "private, max-age=86400"})


def _prune() -> None:
    files = sorted(CACHE_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[CACHE_MAX:]:
        old.unlink(missing_ok=True)


class VicoClipView(HomeAssistantView):
    """Authenticated (signed URL from media_source) MP4 of one recording."""

    url = URL
    name = "api:vicohome:clip"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, request: web.Request, entry_id: str, trace_id: str) -> web.StreamResponse:
        if not _SAFE.match(entry_id) or not _SAFE.match(trace_id):
            raise web.HTTPBadRequest
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
            raise web.HTTPNotFound
        target = CACHE_DIR / f"{trace_id}.mp4"
        lock = self._locks.setdefault(trace_id, asyncio.Lock())
        async with lock:
            if not target.exists():
                await self._remux(entry.runtime_data.api, trace_id, target)
        return web.FileResponse(target, headers={"Content-Type": "video/mp4", "Cache-Control": "private, max-age=3600"})

    async def _remux(self, api, trace_id: str, target: Path) -> None:
        try:
            ev = await api.event(trace_id)
        except VicoError as err:
            raise web.HTTPBadGateway(text=str(err)) from err
        url = ev.get("videoUrl")
        if not url:
            raise web.HTTPNotFound(text="no video")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise web.HTTPNotImplemented(text="ffmpeg not available")
        await self.hass.async_add_executor_job(lambda: CACHE_DIR.mkdir(parents=True, exist_ok=True))
        tmp = target.with_suffix(".part.mp4")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-v", "error", "-y", "-i", url, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        except TimeoutError:
            proc.kill()
            raise web.HTTPGatewayTimeout from None
        if proc.returncode != 0 or not tmp.exists():
            _LOGGER.warning("Remux of %s failed: %s", trace_id, err.decode(errors="replace")[-300:])
            tmp.unlink(missing_ok=True)
            raise web.HTTPBadGateway(text="remux failed")
        await self.hass.async_add_executor_job(lambda: (tmp.replace(target), _prune()))
