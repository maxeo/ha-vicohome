"""Async client for the VicoHome cloud API (A4x/Addx platform).

Endpoints come from dydx/vico-cli and averagenative/vicolocal (both MIT).
Every call is a JSON POST; the session token goes in the Authorization header.
Responses are {"result": int, "msg": str, "data": ...}; result 0 means success.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Result codes that mean "log in again": -1024 kicked by another login,
# -1025/-1026/-1027 token missing/invalid/expired.
AUTH_ERRORS = {-1024, -1025, -1026, -1027}
LOGIN_FAILED = {-1001, -1002, -1003, -1004, -1005}

# What the Android app sends as `app` (A4xServiceContext.AppInfo), as recovered by vicolocal.
APP_INFO = {
    "appName": "VicoHome",
    "appType": "Android",
    "bundle": "com.smartaddx.vicohome",
    "channelId": 1000,
    "tenantId": "vicoo",
    "version": 406000877,
    "versionName": "4.6.0",
}


class VicoError(Exception):
    """API call returned a non-zero result."""

    def __init__(self, result: Any, msg: str = "", path: str = "") -> None:
        super().__init__(f"{path}: result={result} msg={msg}")
        self.result = result
        self.msg = msg


class VicoAuthError(VicoError):
    """Login rejected (wrong credentials)."""


class VicoApi:
    """Minimal async VicoHome client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        region: str = "eu",
        country: str = "IT",
        language: str = "it",
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self.base = f"https://api-{region}.vicohome.io"
        self.country = country
        self.language = language
        self._token: str | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        """The shared aiohttp session (also used for the signaling websocket)."""
        return self._session

    # --- transport ---------------------------------------------------------
    async def login(self) -> None:
        """Log in and keep the token. Each login kicks other sessions of the account."""
        resp = await self._request(
            "/account/login",
            {"email": self._email, "password": self._password, "loginType": 0},
            auth=False,
        )
        result = resp.get("result")
        if result != 0:
            raise VicoAuthError(result, resp.get("msg", ""), "/account/login")
        self._token = resp["data"]["token"]["token"]

    async def _request(self, path: str, body: dict, auth: bool = True) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            headers["Authorization"] = self._token or ""
        async with self._session.post(
            self.base + path,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def post_raw(self, path: str, body: dict) -> dict:
        """POST with automatic (re)login; returns the whole envelope."""
        if not self._token:
            await self.login()
        resp = await self._request(path, body)
        if resp.get("result") in AUTH_ERRORS:
            _LOGGER.debug("Token rejected on %s (%s), logging in again", path, resp.get("result"))
            await self.login()
            resp = await self._request(path, body)
        return resp

    async def post(self, path: str, body: dict) -> Any:
        """POST and return `data`, raising VicoError on a non-zero result."""
        resp = await self.post_raw(path, body)
        if resp.get("result") != 0:
            raise VicoError(resp.get("result"), resp.get("msg", ""), path)
        return resp.get("data")

    def _ctx(self) -> dict:
        return {"app": APP_INFO, "countryNo": self.country, "language": self.language}

    @staticmethod
    def _as_list(data: Any) -> list:
        if isinstance(data, dict):
            return data.get("list") or []
        return data or []

    # --- reads -------------------------------------------------------------
    async def devices(self) -> list[dict]:
        """All cameras visible to the account (owned and shared)."""
        return self._as_list(await self.post("/device/listuserdevices", self._ctx()))

    async def device(self, serial: str) -> dict:
        """Details of one camera."""
        return await self.post("/device/selectsingledevice", {**self._ctx(), "serialNumber": serial}) or {}

    async def cover_images(self) -> list[dict]:
        """Latest cover image per camera, without waking it.

        Items: {serialNumber, lastPushImageUrl, lastPushTime}.
        """
        body = {**self._ctx(), "tenantId": APP_INFO["tenantId"]}
        return self._as_list(await self.post("/device/devicePushImage", body))

    async def library(
        self,
        serials: list[str] | None = None,
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[dict]:
        """Cloud event recordings, newest first.

        Items carry imageUrl, videoUrl (HLS), timestamp, period, traceId, serialNumber,
        deviceName and deviceAiEventList.
        """
        now = int(time.time())
        body = {
            **self._ctx(),
            "serialNumbers": serials or [],
            "startTimestamp": start if start is not None else now - 7 * 86400,
            "endTimestamp": end if end is not None else now,
            "from": offset,
            "to": offset + limit,
        }
        return self._as_list(await self.post("/library/newselectlibrary", body))

    async def event(self, trace_id: str) -> dict:
        """One library item by traceId, with freshly signed image/video URLs."""
        data = await self.post("/library/newselectsinglelibrary", {**self._ctx(), "traceId": trace_id})
        if isinstance(data, dict) and "list" in data:
            items = data.get("list") or []
            return items[0] if items else {}
        return data or {}

    async def webrtc_ticket(self, serial: str) -> dict:
        """Live-view ticket: accessToken, signal server, TURN servers (whole envelope)."""
        body = {
            **self._ctx(),
            "serialNumber": serial,
            "supportUnlimitedWebsocket": True,
            "verifyDormancyStatus": True,
            "verify4GVIPStatus": True,
            "needWakeupDevice": False,
        }
        return await self.post_raw("/device/getWebrtcTicket", body)
