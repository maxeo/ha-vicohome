"""VicoHome camera: cloud snapshot plus native WebRTC live view."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from webrtc_models import RTCIceCandidateInit

from homeassistant.components.camera import (
    Camera,
    CameraEntityCapabilities,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCClientConfiguration,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DATA_CHANNEL, LIVE_MAX_S, recorded_types
from .coordinator import VicoConfigEntry, VicoCoordinator
from .entity import VicoEntity
from .relay import RELAY_KEY, BrowserSession, LiveRecorder, offer_wants_talk
from .signaling import CameraError, CameraLink

_LOGGER = logging.getLogger(__name__)

HANGUP_DELAY_S = 3


async def async_setup_entry(
    hass: HomeAssistant, entry: VicoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(VicoCamera(coordinator, sn) for sn in coordinator.data)


def _supports_accel(device: dict) -> bool:
    support = device.get("deviceSupport") or {}
    return (support.get("supportRtcConnectAcceleration") or 0) >= 1


class VicoCamera(VicoEntity, Camera):
    """Camera entity. Media goes browser <-> camera; HA only relays signaling."""

    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: VicoCoordinator, serial: str) -> None:
        VicoEntity.__init__(self, coordinator, serial, "camera")
        Camera.__init__(self)
        self._links: dict[str, CameraLink] = {}
        # Sessions relayed through our go2rtc (recorded): browser session + recorder.
        self._relayed: dict[str, tuple[BrowserSession, LiveRecorder | None]] = {}
        self._pending: dict[str, list[RTCIceCandidateInit]] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        # Talk-back: the HA player sends the microphone on the sendrecv audio track the
        # camera already expects (vicolocal "talk" does the same). Doorbells have a speaker.
        support = self.cam.device.get("deviceSupport") or {}
        speaker = bool(support.get("supportVoiceVolume")) or str(
            self.cam.device.get("displayModelNo") or ""
        ).upper().startswith("DB")
        self._attr_camera_entity_capabilities = CameraEntityCapabilities(two_way_audio=speaker)
        self._image: bytes | None = None
        self._image_url: str | None = None

    @property
    def is_streaming(self) -> bool:
        return bool(self._links or self._relayed)

    # --- still image ---------------------------------------------------------
    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        url = self.cam.snapshot_url
        if not url:
            return self._image
        if url != self._image_url or self._image is None:
            try:
                async with self.coordinator.api.session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    resp.raise_for_status()
                    self._image = await resp.read()
                    self._image_url = url
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Snapshot download failed: %s", err)
        return self._image

    # --- WebRTC --------------------------------------------------------------
    @callback
    def _async_get_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
        # The camera wants a data channel in the offer (it carries light / pan-tilt commands).
        return WebRTCClientConfiguration(data_channel=DATA_CHANNEL)

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        # The cloud keeps one live connection per account: a new viewer replaces the old one.
        for old in list(self._links) + list(self._relayed):
            self.close_webrtc_session(old)
        relay = self.hass.data.get(RELAY_KEY)
        # Talking (microphone on) goes straight to the camera; plain viewing goes through
        # the relay so it can be recorded.
        record = "live" in recorded_types(self.coordinator.config_entry.options)
        if relay and record and not offer_wants_talk(offer_sdp):
            try:
                if await relay.async_ensure():
                    await self._relay_offer(relay, offer_sdp, session_id, send_message)
                    return
            except (aiohttp.ClientError, OSError) as err:
                _LOGGER.warning("Relay not available, direct live view: %s", err)
        link = CameraLink(self.coordinator.api, self.serial, accel=_supports_accel(self.cam.device))
        self._links[session_id] = link
        self._pending.setdefault(session_id, [])

        @callback
        def on_candidate(c: dict) -> None:
            send_message(WebRTCCandidate(RTCIceCandidateInit(
                c["candidate"], sdp_mid=c.get("sdpMid"), sdp_m_line_index=c.get("sdpMLineIndex"),
            )))

        link.on_candidate = on_candidate

        @callback
        def on_closed(reason: str) -> None:
            _LOGGER.debug("Live %s closed by the cloud: %s", self.serial[-6:], reason)
            self.close_webrtc_session(session_id)

        link.on_closed = on_closed
        # Battery cameras stay awake while the signaling session is open: cap it.
        self._timers[session_id] = self.hass.loop.call_later(
            LIVE_MAX_S, self.close_webrtc_session, session_id
        )
        self.async_write_ha_state()

        try:
            await link.connect()
            answer = await link.negotiate(offer_sdp)
            send_message(WebRTCAnswer(answer))
            for cand in self._pending.pop(session_id, []):
                await link.add_candidate(cand.candidate, cand.sdp_mid, cand.sdp_m_line_index)
        except (CameraError, aiohttp.ClientError, TimeoutError) as err:
            if link.closed or session_id not in self._links:
                _LOGGER.debug("Live session %s ended before it started", session_id)
                return
            _LOGGER.warning("Live view of %s failed: %s", self.name or self.serial, err)
            send_message(WebRTCError("vicohome_live_failed", str(err)))
            self.close_webrtc_session(session_id)
        except Exception as err:
            _LOGGER.exception("Unexpected live view error")
            send_message(WebRTCError("vicohome_live_failed", repr(err)))
            self.close_webrtc_session(session_id)

    async def _relay_offer(self, relay, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage) -> None:  # noqa: ANN001
        await relay.set_stream(self.serial, self.coordinator.config_entry.entry_id)
        archive = self.coordinator.archive

        @callback
        def on_answer(sdp: str) -> None:
            send_message(WebRTCAnswer(sdp))
            if archive is not None and session_id in self._relayed:
                self.hass.async_create_task(self._start_recording(session_id, archive.dir))

        @callback
        def on_candidate(cand: str) -> None:
            send_message(WebRTCCandidate(RTCIceCandidateInit(cand, sdp_m_line_index=0)))

        @callback
        def on_error(err: str) -> None:
            _LOGGER.warning("Live view of %s failed: %s", self.name or self.serial, err)
            send_message(WebRTCError("vicohome_live_failed", err))
            self.close_webrtc_session(session_id)

        session = BrowserSession(relay, self.serial, on_answer, on_candidate, on_error)
        self._relayed[session_id] = (session, None)
        self._timers[session_id] = self.hass.loop.call_later(LIVE_MAX_S, self.close_webrtc_session, session_id)
        self.async_write_ha_state()
        try:
            await session.start(offer_sdp)
        except Exception:
            self._relayed.pop(session_id, None)
            if timer := self._timers.pop(session_id, None):
                timer.cancel()
            raise

    async def _start_recording(self, session_id: str, folder) -> None:  # noqa: ANN001
        rec = LiveRecorder(self.serial, folder)
        try:
            await rec.start()
        except (RuntimeError, OSError) as err:
            _LOGGER.warning("Live recording not started: %s", err)
            return
        if session_id in self._relayed:
            self._relayed[session_id] = (self._relayed[session_id][0], rec)
            _LOGGER.debug("Recording live %s of %s", session_id, self.serial[-6:])
        else:  # closed meanwhile
            await self._finish_relay(None, rec)

    async def _finish_relay(self, session: BrowserSession | None, rec: LiveRecorder | None) -> None:
        if session:
            await session.close()
        if rec:
            jpeg = await rec.stop()
            archive = self.coordinator.archive
            if archive and await self.hass.async_add_executor_job(rec.file.exists):
                await archive.async_add_live(self.serial, rec.started, rec.file, jpeg)
                _LOGGER.debug("Live recording of %s saved", self.serial[-6:])
        relay = self.hass.data.get(RELAY_KEY)
        if relay:
            # The frontend often closes a session and opens a new one right away: hang up
            # on the camera (so it can sleep) only if nobody is watching a moment later.
            await asyncio.sleep(HANGUP_DELAY_S)
            if not self._relayed:
                await relay.close_link(self.serial)

    async def async_on_webrtc_candidate(self, session_id: str, candidate: RTCIceCandidateInit) -> None:
        if relayed := self._relayed.get(session_id):
            await relayed[0].add_candidate(candidate.candidate)
            return
        pending = self._pending.get(session_id)
        if pending is not None:  # answer not sent yet: the camera link is still starting
            pending.append(candidate)
            return
        link = self._links.get(session_id)
        if link:
            await link.add_candidate(candidate.candidate, candidate.sdp_mid, candidate.sdp_m_line_index)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        self._pending.pop(session_id, None)
        if timer := self._timers.pop(session_id, None):
            timer.cancel()
        if relayed := self._relayed.pop(session_id, None):
            _LOGGER.debug("Closing relayed live session %s of %s", session_id, self.serial[-6:])
            self.hass.async_create_task(self._finish_relay(*relayed), eager_start=True)
            self.async_write_ha_state()
        link = self._links.pop(session_id, None)
        if link:
            _LOGGER.debug("Closing live session %s of %s", session_id, self.serial[-6:])
            self.hass.async_create_task(link.close(), eager_start=True)
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        for sid in list(self._links) + list(self._relayed):
            self.close_webrtc_session(sid)
        await asyncio.sleep(0)
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict:
        ev = self.cam.last_event or {}
        return {
            "serial_number": self.serial,
            "last_event_video": ev.get("videoUrl"),
            "last_event_id": ev.get("traceId"),
        }
