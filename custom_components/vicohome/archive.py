"""Local archive: keep a copy of every cloud recording on the HA host for a few days.

After each poll the archive looks at the library items whose recording is over and
saves them as MP4 (ffmpeg remux of the cloud HLS, no re-encoding) plus the JPEG
thumbnail, under <config>/vicohome/<entry_id>/. The index survives the cloud deleting
the event, so the media browser can still play it. Live views recorded by HA are
stored here too (kind "live").
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import json
import logging
from pathlib import Path
import shutil
import time

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN
from .coordinator import ts_seconds
from .events import event_tags, event_type

_LOGGER = logging.getLogger(__name__)

# Wait this long after the end of a recording before fetching it (the cloud finishes
# uploading the last segments a little later).
SETTLE_S = 60
# Without a duration the event is still being recorded; give up waiting after this.
UNKNOWN_PERIOD_WAIT_S = 240
MAX_ATTEMPTS = 3
FFMPEG_TIMEOUT_S = 180


@dataclass
class ArchivedClip:
    trace_id: str
    serial: str
    ts: float
    kind: str  # ring, person, ... or "live"
    tags: list[str] = field(default_factory=list)
    period: float | None = None
    video: str | None = None  # file name inside the archive folder
    image: str | None = None


class VicoArchive:
    """Archive of one config entry."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, session: aiohttp.ClientSession,
        days: int, archive_events: bool,
    ) -> None:
        self.hass = hass
        self.days = days
        self.archive_events = archive_events
        self.dir = Path(hass.config.path("vicohome", entry_id))
        self._session = session
        self._index_file = self.dir / "index.json"
        self.clips: dict[str, ArchivedClip] = {}
        self._attempts: dict[str, int] = {}
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._queued: set[str] = set()
        self._worker: asyncio.Task | None = None

    # --- lifecycle -------------------------------------------------------------
    async def async_load(self) -> None:
        def load() -> dict[str, ArchivedClip]:
            self.dir.mkdir(parents=True, exist_ok=True)
            if not self._index_file.exists():
                return {}
            raw = json.loads(self._index_file.read_text("utf-8"))
            return {k: ArchivedClip(**v) for k, v in raw.items()}

        try:
            self.clips = await self.hass.async_add_executor_job(load)
        except (OSError, ValueError, TypeError) as err:
            _LOGGER.warning("Archive index unreadable, starting empty: %s", err)
            self.clips = {}
        await self.async_prune()

    def start(self, create_task) -> None:  # noqa: ANN001
        self._worker = create_task(self._run(), "vicohome_archive")

    async def async_stop(self) -> None:
        if self._worker:
            self._worker.cancel()

    # --- public ----------------------------------------------------------------
    def path(self, name: str) -> Path:
        return self.dir / name

    def for_camera(self, serial: str) -> list[ArchivedClip]:
        clips = [c for c in self.clips.values() if c.serial == serial and c.video]
        return sorted(clips, key=lambda c: c.ts, reverse=True)

    @callback
    def async_sync(self, events: list[dict]) -> None:
        """Queue the finished recordings that are not archived yet."""
        if not self.archive_events:
            return
        now = time.time()
        for ev in events:
            trace = ev.get("traceId")
            if not trace or trace in self.clips or trace in self._queued or not ev.get("videoUrl"):
                continue
            if self._attempts.get(trace, 0) >= MAX_ATTEMPTS:
                continue
            start = ts_seconds(ev.get("timestamp")) or now
            if now - start > self.days * 86400:
                continue
            period = ev.get("period")
            if isinstance(period, (int, float)) and period > 0:
                ready = start + period + SETTLE_S
            else:
                ready = start + UNKNOWN_PERIOD_WAIT_S
            if now < ready:
                continue
            self._queued.add(trace)
            self._queue.put_nowait(ev)

    async def async_add_live(self, serial: str, started: float, tmp_file: Path, image: bytes | None) -> None:
        """Register a recorded live view (file already written in the archive folder)."""
        trace = f"live-{serial[-6:]}-{int(started)}"
        name = f"{_stamp(started)}_{serial[-6:]}_live.mp4"
        img_name = None

        def move() -> None:
            tmp_file.replace(self.dir / name)
            if image:
                (self.dir / f"{trace}.jpg").write_bytes(image)

        await self.hass.async_add_executor_job(move)
        if image:
            img_name = f"{trace}.jpg"
        self.clips[trace] = ArchivedClip(
            trace, serial, started, "live", period=round(time.time() - started), video=name, image=img_name,
        )
        async_dispatcher_send(self.hass, live_signal(serial), asdict(self.clips[trace]))
        await self._save_index()

    async def async_prune(self) -> None:
        limit = time.time() - self.days * 86400
        old = [c for c in self.clips.values() if c.ts < limit]

        def remove() -> None:
            for clip in old:
                for name in (clip.video, clip.image):
                    if name:
                        (self.dir / name).unlink(missing_ok=True)
            # Leftovers of interrupted downloads.
            for part in self.dir.glob("*.part.mp4"):
                if part.stat().st_mtime < time.time() - 3600:
                    part.unlink(missing_ok=True)

        for clip in old:
            self.clips.pop(clip.trace_id, None)
        await self.hass.async_add_executor_job(remove)
        if old:
            _LOGGER.debug("Archive: removed %d clips older than %d days", len(old), self.days)
            await self._save_index()

    # --- worker ----------------------------------------------------------------
    async def _run(self) -> None:
        last_prune = time.time()
        while True:
            ev = await self._queue.get()
            trace = ev["traceId"]
            try:
                await self._archive(ev)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self._attempts[trace] = self._attempts.get(trace, 0) + 1
                _LOGGER.warning("Archive of %s failed (attempt %d): %s", trace, self._attempts[trace], err)
            finally:
                self._queued.discard(trace)
            if time.time() - last_prune > 3600:
                last_prune = time.time()
                await self.async_prune()

    async def _archive(self, ev: dict) -> None:
        trace, serial = ev["traceId"], ev.get("serialNumber") or ""
        start = ts_seconds(ev.get("timestamp")) or time.time()
        kind = event_type(ev)
        base = f"{_stamp(start)}_{serial[-6:]}_{kind}_{trace[-8:]}"
        video = f"{base}.mp4"
        await self._remux(ev["videoUrl"], self.dir / video)
        image = None
        if ev.get("imageUrl"):
            try:
                async with self._session.get(ev["imageUrl"], timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
                await self.hass.async_add_executor_job((self.dir / f"{base}.jpg").write_bytes, data)
                image = f"{base}.jpg"
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                _LOGGER.debug("Archive: no thumbnail for %s: %s", trace, err)
        period = ev.get("period")
        self.clips[trace] = ArchivedClip(
            trace, serial, start, kind, event_tags(ev),
            float(period) if isinstance(period, (int, float)) else None, video, image,
        )
        await self._save_index()
        _LOGGER.debug("Archived %s as %s", trace, video)

    async def _remux(self, url: str, target: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not available")
        tmp = target.with_suffix(".part.mp4")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-v", "error", "-y", "-i", url, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            raise RuntimeError("ffmpeg timeout") from None
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg: {err.decode(errors='replace')[-200:]}")
        await self.hass.async_add_executor_job(tmp.replace, target)

    async def _save_index(self) -> None:
        data = {k: asdict(v) for k, v in self.clips.items()}

        def write() -> None:
            tmp = self._index_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            tmp.replace(self._index_file)

        await self.hass.async_add_executor_job(write)


def live_signal(serial: str) -> str:
    """Dispatcher signal sent when a live view of `serial` has been recorded."""
    return f"{DOMAIN}_live_{serial}"


def _stamp(ts: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
