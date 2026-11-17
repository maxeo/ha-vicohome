"""Instant notifications: receive the same FCM pushes as the phone app.

HA registers itself with Firebase Cloud Messaging using the Android app's public FCM
identity (as the Ring integration does), then hands the FCM token to the VicoHome cloud
with /user/setusertoken (msgType "101", like the app's push manager). Every push makes
the coordinator re-read the event library right away instead of waiting for the poll.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from firebase_messaging import FcmPushClient, FcmRegisterConfig

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .api import VicoError
from .const import (
    DOMAIN,
    FCM_API_KEY,
    FCM_APP_ID,
    FCM_BUNDLE_ID,
    FCM_PROJECT_ID,
    FCM_SENDER_ID,
    PUSH_MSG_TYPE_FCM,
)
from .coordinator import VicoCoordinator

_LOGGER = logging.getLogger(__name__)

# The library item can appear a moment after the push: re-read a few times.
REFRESH_DELAYS_S = (0.5, 3, 8)


def _param(header: str, name: str) -> str:
    """Value of `name=` in a header like "dh=AAA;p256ecdsa=BBB", base64url-padded."""
    for part in header.replace(",", ";").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and value:
            return value + "=" * (-len(value) % 4)
    return ""


class VicoFcmClient(FcmPushClient):
    """FcmPushClient that copes with VicoHome's data messages.

    The stock client takes crypto-key[3:] / encryption[5:] verbatim; VicoHome sends
    "dh=...;p256ecdsa=..." and unpadded values, so decoding fails ("Incorrect padding")
    and the client shuts down. Messages without encryption headers are passed as-is.
    """

    def _handle_data_message(self, msg) -> None:  # noqa: ANN001
        app_data = {x.key: x.value for x in msg.app_data}
        if app_data.get("message_type") == "deleted_messages":
            return
        crypto_key = _param(app_data.get("crypto-key", ""), "dh")
        salt = _param(app_data.get("encryption", ""), "salt")
        payload: Any
        if crypto_key and salt and self.credentials:
            try:
                raw = self._decrypt_raw_data(self.credentials, crypto_key, salt, msg.raw_data)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except ValueError:
                    payload = {"message": raw.decode("utf-8", "replace")}
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Push %s not decrypted (%s), using app data", msg.persistent_id, err)
                payload = {"app_data": app_data}
        else:
            payload = {"app_data": app_data}
            if msg.raw_data:
                try:
                    payload["raw"] = json.loads(msg.raw_data.decode("utf-8"))
                except ValueError:
                    payload["raw"] = msg.raw_data.decode("utf-8", "replace")
        if not isinstance(payload, dict):
            payload = {"message": payload}
        try:
            self.callback(payload, msg.persistent_id, self.callback_context)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error in push callback")


def push_signal(serial: str) -> str:
    return f"{DOMAIN}_push_{serial}"


class VicoPush:
    """FCM listener for one config entry."""

    def __init__(self, hass: HomeAssistant, coordinator: VicoCoordinator, entry_id: str) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{entry_id}.fcm")
        self._client: FcmPushClient | None = None
        self._tasks: set[asyncio.Task] = set()
        self.last_payload: dict | None = None

    @property
    def active(self) -> bool:
        return bool(self._client and self._client.is_started())

    async def async_run(self) -> None:
        """Start, retrying with backoff: GCM registration fails now and then
        (PHONE_REGISTRATION_ERROR). Polling covers the gap."""
        delay = 30
        while True:
            try:
                await self.async_start()
                # Watchdog: the client stops itself after some errors; start it again.
                # It reports "started" only after the MCS login, so check every few minutes.
                while True:
                    await asyncio.sleep(300)
                    if not self.active:
                        break
                _LOGGER.warning("Push listener stopped, restarting in %s s", delay)
                await self._stop_client()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1800)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Push not available yet (%s), retrying in %s s", err, delay)
                await self._stop_client()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1800)

    async def _stop_client(self) -> None:
        client, self._client = self._client, None
        if client:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        config = FcmRegisterConfig(
            FCM_PROJECT_ID, FCM_APP_ID, FCM_API_KEY, FCM_SENDER_ID, bundle_id=FCM_BUNDLE_ID,
        )
        self._client = VicoFcmClient(
            self._on_push,
            config,
            stored.get("credentials"),
            self._on_credentials,
            http_client_session=self.coordinator.api.session,
        )
        token = await self._client.checkin_or_register()
        if not token:
            raise RuntimeError("FCM registration failed")
        await self._client.start()
        if token != stored.get("registered_token"):
            await self._register_token(token)
            await self._store.async_save({**(await self._store.async_load() or {}), "registered_token": token})
        _LOGGER.debug("FCM push listener started")

    async def _register_token(self, token: str) -> None:
        api = self.coordinator.api
        try:
            await api.post("/user/setusertoken", {
                **api._ctx(),  # noqa: SLF001
                "tenantId": "vicoo",
                "msgType": PUSH_MSG_TYPE_FCM,
                "msgToken": token,
            })
        except VicoError as err:
            _LOGGER.warning("Could not register push token with VicoHome: %s", err)
            raise
        _LOGGER.info("Push token registered with VicoHome")

    async def async_stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self._stop_client()

    def _on_credentials(self, credentials: dict) -> None:
        # Called synchronously by firebase_messaging: save in the background.
        async def save() -> None:
            data = await self._store.async_load() or {}
            await self._store.async_save({**data, "credentials": credentials})

        self.hass.async_create_task(save())

    def _on_push(self, notification: dict, persistent_id: str, context: Any = None) -> None:
        # Called from the FCM client task on the event loop.
        self.last_payload = notification
        _LOGGER.debug("Push %s: %s", persistent_id, notification)
        self.hass.loop.call_soon_threadsafe(self._dispatch, notification)
        self.hass.loop.call_soon_threadsafe(self._schedule_refresh)

    @callback
    def _dispatch(self, notification: dict) -> None:
        """Tell the camera's event entity right away (same traceId as the library item)."""
        data = notification.get("data") or notification.get("app_data") or {}
        serial, trace = data.get("serialNumber"), data.get("traceId")
        if serial and trace:
            async_dispatcher_send(self.hass, push_signal(serial), data)

    @callback
    def _schedule_refresh(self) -> None:
        async def refresh() -> None:
            for delay in REFRESH_DELAYS_S:
                await asyncio.sleep(delay)
                await self.coordinator.async_refresh()

        task = self.hass.async_create_background_task(refresh(), f"{DOMAIN}_push_refresh")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
