"""Live relay through a private go2rtc, so HA can record what is being watched.

    browser ──WebRTC──▶ go2rtc (ours) ──WebRTC (WHEP)──▶ camera
                           │
                           └─RTSP──▶ ffmpeg ──▶ archive (MP4)

The integration starts its own go2rtc (the binary bundled with Home Assistant) on
localhost with a STUN server, so it does not touch the instance HA manages. go2rtc pulls
the camera through a WHEP endpoint served by this integration: the endpoint relays the
SDP offer over the vendor signaling (CameraLink) and puts the camera's ICE candidates
into the answer, because WHEP has no trickle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
from pathlib import Path
import secrets
import shutil
import tempfile
import time

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .signaling import CameraError, CameraLink, _kind, _mid, _split_sdp

_LOGGER = logging.getLogger(__name__)

RELAY_KEY = f"{DOMAIN}_relay"
API_PORT = 11985
RTSP_PORT = 18564
WEBRTC_PORT = 18565
STUN = "stun:stun.l.google.com:19302"
# Camera candidates arrive after the answer: wait for them before answering go2rtc.
CANDIDATE_WAIT_S = 2.5
CANDIDATE_QUIET_S = 0.8

CONFIG = f"""
app:
  modules: ["api", "rtsp", "webrtc", "ws"]
api:
  listen: "127.0.0.1:{API_PORT}"
rtsp:
  listen: "127.0.0.1:{RTSP_PORT}"
webrtc:
  listen: ":{WEBRTC_PORT}"
  ice_servers:
    - urls: ["{STUN}"]
log:
  level: warn
"""


def stream_name(serial: str) -> str:
    return f"vico_{serial}"


def _add_candidates(answer: str, candidates: list[dict]) -> str:
    """Put trickled candidates into the answer (in the section of their mid, else the first)."""
    if not candidates:
        return answer
    lines = answer.replace("\r\n", "\n").rstrip("\n").split("\n")
    sections: list[list[str]] = [[]]
    for ln in lines:
        if ln.startswith("m="):
            sections.append([])
        sections[-1].append(ln)
    mids = [next((ln[6:] for ln in s if ln.startswith("a=mid:")), None) for s in sections]
    for c in candidates:
        cand = c["candidate"]
        cand = cand if cand.startswith("a=") else "a=" + (cand if cand.startswith("candidate:") else "candidate:" + cand)
        idx = mids.index(c.get("sdpMid")) if c.get("sdpMid") in mids else 1
        if idx < len(sections) and cand not in sections[idx]:
            sections[idx].append(cand)
    return "\r\n".join(ln for s in sections for ln in s) + "\r\n"


def to_camera_offer(offer: str) -> str:
    """go2rtc's offer in the shape the camera expects: audio then video, mids "0" and "1".

    The camera always answers audio as mid 0 and video as mid 1, whatever the offer says,
    and go2rtc offers video first.
    """
    head, sections = _split_sdp(offer)
    picked = [next((s for s in sections if _kind(s) == k), None) for k in ("audio", "video")]
    out: list[list[str]] = []
    for sec in picked:
        if sec is None:
            continue
        mid = str(len(out))
        out.append([f"a=mid:{mid}" if ln.startswith("a=mid:") else ln for ln in sec])
    bundle = "a=group:BUNDLE " + " ".join(str(i) for i in range(len(out)))
    head = [bundle if ln.startswith("a=group:BUNDLE") else ln for ln in head if ln != "a=cryptex"]
    return "\r\n".join(head + [ln for s in out for ln in s]) + "\r\n"


def to_go2rtc_answer(answer: str, offer: str) -> str:
    """Map the camera's answer back onto go2rtc's original offer (order, mids, directions)."""
    head, ans_sections = _split_sdp(answer)
    _, offer_sections = _split_sdp(offer)
    by_kind: dict[str, list[list[str]]] = {}
    for sec in ans_sections:
        by_kind.setdefault(_kind(sec), []).append(sec)
    ordered: list[list[str]] = []
    accepted: list[str] = []
    for osec in offer_sections:
        kind, mid = _kind(osec), _mid(osec) or str(len(ordered))
        pool = by_kind.get(kind) or []
        if not pool:
            proto, fmt = osec[0].split()[2], osec[0].split()[3]
            ordered.append([f"m={kind} 0 {proto} {fmt}", f"a=mid:{mid}", "a=inactive"])
            continue
        sec = pool.pop(0)
        offered = next((ln[2:] for ln in osec if ln in ("a=sendrecv", "a=recvonly", "a=sendonly", "a=inactive")), "sendrecv")
        fixed = []
        for ln in sec:
            if ln.startswith("a=mid:"):
                ln = f"a=mid:{mid}"
            elif offered == "recvonly" and ln in ("a=sendrecv", "a=recvonly"):
                ln = "a=sendonly" if ln == "a=sendrecv" else "a=inactive"
            fixed.append(ln)
        ordered.append(fixed)
        accepted.append(mid)
    head = [f"a=group:BUNDLE {' '.join(accepted)}" if ln.startswith("a=group:BUNDLE") else ln for ln in head]
    return "\r\n".join(head + [ln for s in ordered for ln in s]) + "\r\n"


class VicoRelay:
    """Our go2rtc process plus the camera links it pulls from (one per camera)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.key = secrets.token_urlsafe(24)
        self._proc: asyncio.subprocess.Process | None = None
        self._dir: Path | None = None
        self._session = async_get_clientsession(hass)
        self.links: dict[str, CameraLink] = {}
        self._starting = asyncio.Lock()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{API_PORT}"

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def async_ensure(self) -> bool:
        """Start go2rtc if needed; False when it cannot run here."""
        async with self._starting:
            if self.running:
                return True
            binary = shutil.which("go2rtc")
            if not binary:
                return False
            if self._dir is None:
                self._dir = Path(await self.hass.async_add_executor_job(tempfile.mkdtemp, "", "vicohome-go2rtc-"))
                await self.hass.async_add_executor_job((self._dir / "go2rtc.yaml").write_text, CONFIG, "utf-8")
            self._proc = await asyncio.create_subprocess_exec(
                binary, "-c", str(self._dir / "go2rtc.yaml"),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            self.hass.async_create_background_task(self._log_stderr(self._proc), "vicohome_go2rtc_log")
            for _ in range(50):
                try:
                    async with self._session.get(f"{self.base}/api", timeout=aiohttp.ClientTimeout(total=1)) as r:
                        if r.status == 200:
                            _LOGGER.debug("go2rtc relay started")
                            return True
                except (aiohttp.ClientError, TimeoutError):
                    pass
                await asyncio.sleep(0.2)
            _LOGGER.warning("go2rtc relay did not start")
            await self.async_stop()
            return False

    async def _log_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        async for line in proc.stderr:
            _LOGGER.debug("go2rtc: %s", line.decode(errors="replace").rstrip())

    async def async_stop(self) -> None:
        for serial in list(self.links):
            await self.close_link(serial)
        if self.running:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except TimeoutError:
                self._proc.kill()
        self._proc = None

    async def set_stream(self, serial: str, entry_id: str) -> None:
        """(Re)define the go2rtc stream of a camera: it pulls from our WHEP endpoint."""
        port = self.hass.http.server_port or 8123
        scheme = "https" if self.hass.http.ssl_certificate else "http"
        src = f"webrtc:{scheme}://127.0.0.1:{port}/api/vicohome/whep/{self.key}/{entry_id}/{serial}"
        async with self._session.put(
            f"{self.base}/api/streams", params={"name": stream_name(serial), "src": src},
        ) as r:
            r.raise_for_status()

    async def close_link(self, serial: str, only: CameraLink | None = None) -> None:
        """Hang up the camera link of `serial` (only if it is still `only`, when given)."""
        if only is not None and self.links.get(serial) is not only:
            return
        if link := self.links.pop(serial, None):
            await link.close()

    async def open_browser_session(
        self, serial: str, offer: str, on_answer: Callable[[str], None],
        on_candidate: Callable[[str], None], on_error: Callable[[str], None],
    ) -> BrowserSession:
        session = BrowserSession(self, serial, on_answer, on_candidate, on_error)
        await session.start(offer)
        return session


class BrowserSession:
    """One browser viewer on our go2rtc (its websocket API supports trickle ICE)."""

    def __init__(self, relay: VicoRelay, serial: str, on_answer, on_candidate, on_error) -> None:  # noqa: ANN001
        self.relay = relay
        self.serial = serial
        self._on_answer = on_answer
        self._on_candidate = on_candidate
        self._on_error = on_error
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self.answered = asyncio.Event()

    async def start(self, offer: str) -> None:
        self._ws = await self.relay._session.ws_connect(  # noqa: SLF001
            f"{self.relay.base}/api/ws", params={"src": stream_name(self.serial)}, heartbeat=20,
        )
        self._reader = asyncio.create_task(self._read())
        await self._ws.send_str(json.dumps({"type": "webrtc/offer", "value": offer}))

    async def add_candidate(self, candidate: str) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.send_str(json.dumps({"type": "webrtc/candidate", "value": candidate}))

    async def _read(self) -> None:
        assert self._ws
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                kind, value = data.get("type"), data.get("value")
                if kind == "webrtc/answer":
                    self.answered.set()
                    self._on_answer(value)
                elif kind == "webrtc/candidate" and value:
                    self._on_candidate(value)
                elif kind == "error":
                    self._on_error(str(value))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("go2rtc websocket ended: %r", err)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()


class LiveRecorder:
    """ffmpeg reading the relay's RTSP restream into an MP4 in the archive folder."""

    def __init__(self, serial: str, folder: Path) -> None:
        self.serial = serial
        self.started = time.time()
        self.file = folder / f"live-{serial[-6:]}-{int(self.started)}.part.mp4"
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not available")
        self._proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-v", "error", "-y", "-rtsp_transport", "tcp",
            "-i", f"rtsp://127.0.0.1:{RTSP_PORT}/{stream_name(self.serial)}",
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart", "-f", "mp4", str(self.file),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )

    async def stop(self) -> bytes | None:
        """Stop cleanly ('q' lets ffmpeg write the index); return a JPEG of the first second."""
        proc = self._proc
        if proc is None:
            return None
        if proc.returncode is None:
            try:
                proc.stdin.write(b"q")
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 20)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        err = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
        if not self.file.exists() or self.file.stat().st_size < 10_000:
            _LOGGER.debug("Live recording of %s empty: %s", self.serial[-6:], err[-300:])
            self.file.unlink(missing_ok=True)
            return None
        thumb = await asyncio.create_subprocess_exec(
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-ss", "1", "-i", str(self.file),
            "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        jpeg, _ = await thumb.communicate()
        return jpeg or None


class VicoWhepView(HomeAssistantView):
    """WHEP endpoint for our go2rtc only (secret key in the URL, localhost)."""

    url = "/api/vicohome/whep/{key}/{entry_id}/{serial}"
    name = "api:vicohome:whep"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, relay: VicoRelay) -> None:
        self.hass = hass
        self.relay = relay

    async def post(self, request: web.Request, key: str, entry_id: str, serial: str) -> web.Response:
        if not secrets.compare_digest(key, self.relay.key):
            raise web.HTTPForbidden
        entry = self.hass.config_entries.async_get_entry(entry_id)
        coord = getattr(entry, "runtime_data", None) if entry and entry.domain == DOMAIN else None
        cam = (getattr(coord, "data", None) or {}).get(serial)
        if cam is None:
            raise web.HTTPNotFound
        offer = await request.text()
        support = cam.device.get("deviceSupport") or {}
        await self.relay.close_link(serial)
        link = CameraLink(coord.api, serial, accel=(support.get("supportRtcConnectAcceleration") or 0) >= 1)
        self.relay.links[serial] = link
        candidates: list[dict] = []
        last = asyncio.Event()

        def on_candidate(c: dict) -> None:
            candidates.append(c)
            last.set()

        def on_closed(reason: str) -> None:
            _LOGGER.debug("Relay link %s closed: %s", serial[-6:], reason)
            if self.relay.links.get(serial) is link:
                self.hass.async_create_task(self.relay.close_link(serial))

        link.on_candidate = on_candidate
        link.on_closed = on_closed
        try:
            await link.connect()
            answer = to_go2rtc_answer(await link.negotiate(to_camera_offer(offer)), offer)
        except (CameraError, aiohttp.ClientError, TimeoutError) as err:
            await self.relay.close_link(serial)
            _LOGGER.debug("Relay link %s failed: %s", serial[-6:], err)
            raise web.HTTPBadGateway(text=str(err)) from err
        deadline = time.monotonic() + (0 if "a=candidate:" in answer else CANDIDATE_WAIT_S)
        while time.monotonic() < deadline:
            last.clear()
            try:
                await asyncio.wait_for(last.wait(), CANDIDATE_QUIET_S)
            except TimeoutError:
                if candidates:
                    break
        _LOGGER.debug("Relay %s: answer with %d camera candidates", serial[-6:], len(candidates))
        return web.Response(
            status=201, text=_add_candidates(answer, candidates), content_type="application/sdp",
            headers={"Location": f"/api/vicohome/whep/{key}/{entry_id}/{serial}"},
        )

    async def delete(self, request: web.Request, key: str, entry_id: str, serial: str) -> web.Response:
        if not secrets.compare_digest(key, self.relay.key):
            raise web.HTTPForbidden
        await self.relay.close_link(serial)
        return web.Response(status=200)


def offer_wants_talk(offer: str) -> bool:
    """True when the browser sends its microphone (sendrecv audio with an SSRC)."""
    in_audio = False
    direction, ssrc = None, False
    for ln in offer.replace("\r\n", "\n").split("\n"):
        if ln.startswith("m="):
            if in_audio:
                break
            in_audio = ln.startswith("m=audio")
        elif in_audio:
            if ln in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"):
                direction = ln[2:]
            elif ln.startswith("a=ssrc:"):
                ssrc = True
    return direction in ("sendrecv", "sendonly") and ssrc

