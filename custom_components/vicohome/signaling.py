"""Signaling bridge between the HA frontend (browser WebRTC) and a VicoHome camera.

Ported from averagenative/vicolocal (vico_signaling.py, MIT) to aiohttp. Home Assistant
only relays signaling: the media flows browser <-> camera directly (or through the
vendor TURN relay when they are not on the same network).

Flow:
    connect()     ticket -> wss AUTH -> JOIN_LIVE -> PEER_IN (or JOIN_LIVE_RESPONSE code 0)
    negotiate()   offer -> compact "KISS" form -> TRANSMIT SDP_OFFER -> SDP_ANSWER -> full SDP
    add_candidate()  browser ICE candidate -> TRANSMIT ICE_CANDIDATE
    on_candidate     camera ICE candidates -> callback
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import json
import logging
import os
import random
import time
from urllib.parse import urlparse

import aiohttp

from .api import VicoApi
from .vico_sdp import DIR_TO_CODE, compress_offer, decompress_answer, slim_offer

_LOGGER = logging.getLogger(__name__)


class CameraError(Exception):
    """Live view could not be started."""


# --------------------------------------------------------------------------- SDP glue
def _split_sdp(sdp: str) -> tuple[list[str], list[list[str]]]:
    lines = [ln for ln in sdp.replace("\r\n", "\n").split("\n") if ln]
    head: list[str] = []
    sections: list[list[str]] = []
    for ln in lines:
        if ln.startswith("m="):
            sections.append([ln])
        elif sections:
            sections[-1].append(ln)
        else:
            head.append(ln)
    return head, sections


def _mid(section: list[str]) -> str | None:
    for ln in section:
        if ln.startswith("a=mid:"):
            return ln[6:]
    return None


def _kind(section: list[str]) -> str:
    return section[0][2:].split()[0]


def build_camera_offer(offer_sdp: str) -> dict:
    """Compact offer for the camera.

    The HA frontend offers audio as recvonly with no SSRC unless two-way audio is on,
    but the camera only starts streaming when the viewer offers sendrecv audio with an
    SSRC (the app always does). Advertise one; the browser simply never sends on it.
    """
    compact = compress_offer(offer_sdp)
    audio = compact.get("a")
    if audio and len(audio) < 3:
        audio[1] = DIR_TO_CODE["sendrecv"]
        audio.append([random.randint(1, 2**31 - 1), f"ha{os.urandom(6).hex()}"])
    return compact


def adapt_answer(answer_sdp: str, offer_sdp: str) -> str:
    """Make the camera's answer valid for the browser's offer.

    - m-sections in the same order as the offer (decompress_answer always emits
      audio, video, application);
    - audio direction compatible with what the browser offered;
    - BUNDLE group in offer order.
    """
    _, offer_sections = _split_sdp(offer_sdp)
    head, ans_sections = _split_sdp(answer_sdp)
    by_mid = {_mid(s): s for s in ans_sections}
    by_kind = {_kind(s): s for s in ans_sections}

    ordered: list[list[str]] = []
    for osec in offer_sections:
        mid, kind = _mid(osec), _kind(osec)
        sec = by_mid.get(mid) or by_kind.get(kind)
        if sec is None:
            # Camera did not answer this m-line: reject it (port 0) keeping the mid.
            sec = [f"m={kind} 0 {osec[0].split()[2]} {osec[0].split()[3]}", f"a=mid:{mid}", "a=inactive"]
        if kind == "audio":
            offered = next(
                (ln[2:] for ln in osec if ln[2:] in ("sendrecv", "recvonly", "sendonly", "inactive")),
                "sendrecv",
            )
            if offered == "recvonly":
                sec = [
                    "a=sendonly" if ln in ("a=sendrecv", "a=sendonly")
                    else "a=inactive" if ln == "a=recvonly" else ln
                    for ln in sec
                ]
        ordered.append(sec)

    mids = " ".join(m for m in (_mid(s) for s in ordered) if m is not None)
    head = [f"a=group:BUNDLE {mids}" if ln.startswith("a=group:BUNDLE") else ln for ln in head]
    return "\r\n".join(head + [ln for s in ordered for ln in s]) + "\r\n"


# --------------------------------------------------------------------------- link
class CameraLink:
    """One live-view signaling session with a camera."""

    def __init__(self, api: VicoApi, serial: str, accel: bool = True) -> None:
        self.api = api
        self.serial = serial
        self.accel = accel
        self.ice_servers: list[dict] = []
        self.on_candidate: Callable[[dict], None] | None = None
        self.on_closed: Callable[[str], None] | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._uid: str | None = None
        self._session_id: str | None = None
        self._peer_in = asyncio.Event()
        self._answer: asyncio.Future | None = None
        self._offer_msg: dict | None = None
        self._closed = False

    def _log(self, msg: str, *args) -> None:
        _LOGGER.debug("[%s] " + msg, self.serial[-6:], *args)

    async def connect(self, wake_timeout: float = 30.0) -> None:
        ticket = await self.api.webrtc_ticket(self.serial)
        data = ticket.get("data") or {}
        if ticket.get("result") != 0 or not data.get("accessToken"):
            raise CameraError(f"ticket failed: result={ticket.get('result')} {ticket.get('msg')}")
        self._uid = data["id"]
        self._session_id = f"Android-{self._uid}-{int(time.time() * 1000)}"
        self.ice_servers = data.get("iceServer") or []
        url = (
            f"wss://{urlparse(data['signalServer']).hostname}:"
            f"{data.get('signalPort') or 443}{data['websocketPath']}"
        )
        if self._closed:
            raise CameraError("session closed")
        self._log("connecting %s", urlparse(url).hostname)
        self._ws = await self.api.session.ws_connect(url, heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=10))
        if self._closed:
            # Closed while connecting: drop the socket, or it would kick the newer session
            # (the cloud allows one connection per account id: JOIN_LIVE code 405).
            await self._ws.close()
            raise CameraError("session closed")
        self._reader = asyncio.create_task(self._read_loop())

        await self._send({
            "method": "AUTH", "accessToken": data["accessToken"], "version": "0.0.1",
            "id": self._uid, "clientType": "app", "status": "normal",
        })
        await self._send({
            "method": "JOIN_LIVE", "group": data.get("groupId") or self.serial,
            "role": data.get("role") or "viewer", "name": self._uid,
            "traceId": data.get("traceId") or f"trace-{int(time.time() * 1000)}",
            "traceparent": f"00-{os.urandom(16).hex()}-{os.urandom(8).hex()}-01",
            "recipientClientId": self.serial,
            "supportRtcConnectAcceleration": int(self.accel),
            "accessToken": data["accessToken"],
        })
        try:
            await asyncio.wait_for(self._peer_in.wait(), timeout=wake_timeout)
        except TimeoutError as err:
            raise CameraError("camera did not join in time (offline, asleep or busy)") from err
        if self._closed:
            raise CameraError("session closed")

    async def negotiate(self, offer_sdp: str, timeout: float = 15.0) -> str:
        """Send the browser's offer, return an answer the browser can apply."""
        self._answer = asyncio.get_running_loop().create_future()
        if self.accel:
            payload: dict | str = {"type": "offer", "sdp": build_camera_offer(offer_sdp)}
            version = "0.0.2"
        else:
            payload = base64.b64encode(json.dumps(
                {"type": "offer", "sdp": slim_offer(offer_sdp)}, separators=(",", ":")
            ).encode()).decode()
            version = "0.0.1"
        self._offer_msg = {
            "method": "TRANSMIT", "messageType": "SDP_OFFER", "messagePayload": payload,
            "recipientClientId": self.serial, "senderClientId": self._uid,
            "sessionId": self._session_id, "viewerType": "a4x_sdk",
            "resolution": "auto", "version": version, "sdpMode": 0,
        }
        await self._send(self._offer_msg)
        try:
            answer = await asyncio.wait_for(self._answer, timeout=timeout)
        except TimeoutError as err:
            raise CameraError("camera did not answer the offer") from err
        return adapt_answer(answer, offer_sdp)

    async def add_candidate(self, candidate: str, sdp_mid: str | None, sdp_mline_index: int | None) -> None:
        payload = base64.b64encode(json.dumps(
            {"candidate": candidate, "sdpMid": sdp_mid, "sdpMLineIndex": sdp_mline_index},
            separators=(",", ":"),
        ).encode()).decode()
        await self._send({
            "method": "TRANSMIT", "messageType": "ICE_CANDIDATE", "messagePayload": payload,
            "recipientClientId": self.serial, "senderClientId": self._uid,
            "sessionId": self._session_id, "viewerType": "a4x_sdk", "version": "0.0.1",
        })

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True
        # Wake up a connect()/negotiate() still waiting on this session.
        self._peer_in.set()
        if self._answer and not self._answer.done():
            self._answer.set_exception(CameraError("session closed"))
        if self._reader:
            self._reader.cancel()
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    # --- internals ---------------------------------------------------------
    async def _send(self, obj: dict) -> None:
        if not self._ws or self._ws.closed:
            raise CameraError("signaling socket closed")
        await self._ws.send_str(json.dumps(obj, separators=(",", ":")))

    @staticmethod
    def _payload(msg: dict):
        p = msg.get("messagePayload")
        if isinstance(p, dict):
            return p
        if isinstance(p, str) and p:
            try:
                return json.loads(base64.b64decode(p + "=" * (-len(p) % 4)))
            except Exception:  # noqa: BLE001
                try:
                    return json.loads(p)
                except Exception:  # noqa: BLE001
                    return p
        return p

    async def _read_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    self._handle(json.loads(msg.data))
                except Exception as err:  # noqa: BLE001
                    self._log("handler error: %r", err)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self._log("read loop ended: %r", err)
        if not self._closed and self.on_closed:
            self.on_closed("signal socket closed")

    def _handle(self, msg: dict) -> None:
        method, mtype = msg.get("method"), msg.get("messageType")
        if method == "AUTH_RESPONSE":
            self._log("AUTH code=%s", msg.get("code"))
        elif method == "JOIN_LIVE_RESPONSE":
            self._log("JOIN_LIVE code=%s %s", msg.get("code"), msg.get("message"))
            # Code 0: camera already awake. With connect acceleration the app offers right
            # away; newer signal servers never send PEER_IN for an awake camera.
            if msg.get("code") == 0 and self.accel:
                self._peer_in.set()
        elif method == "WEBRTC_CONFIG":
            self.ice_servers = msg.get("iceServers", self.ice_servers)
        elif method == "PEER_IN":
            self._log("camera joined")
            if self._peer_in.is_set() and self._offer_msg and self._answer and not self._answer.done():
                # A waking camera can announce itself twice; an offer sent after the first
                # PEER_IN is lost, so send it again.
                self._log("camera joined again: resending the offer")
                asyncio.get_running_loop().create_task(self._send(self._offer_msg))
            self._peer_in.set()
        elif method == "TRANSMIT" and mtype in ("SDP_ANSWER", "SDP_OFFER_RESPONSE"):
            p = self._payload(msg)
            sdp = p.get("sdp") if isinstance(p, dict) else None
            answer = decompress_answer(sdp) if isinstance(sdp, dict) else (sdp if isinstance(sdp, str) else None)
            if answer and self._answer and not self._answer.done():
                self._log("got answer (version %s)", msg.get("version"))
                self._answer.set_result(answer)
        elif method == "TRANSMIT" and mtype == "ICE_CANDIDATE":
            p = self._payload(msg)
            if isinstance(p, dict) and p.get("candidate") and self.on_candidate:
                self.on_candidate({
                    "candidate": p["candidate"],
                    "sdpMid": p.get("sdpMid"),
                    "sdpMLineIndex": p.get("sdpMLineIndex"),
                })
        elif method in ("PEER_OUT", "NEED_REAUTH"):
            self._log("%s reason=%s", method, msg.get("reason"))
            if self.on_closed:
                self.on_closed(method)
