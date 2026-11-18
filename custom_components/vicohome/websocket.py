"""Websocket API for the events card: recordings with photo and video URLs."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .clips import SIGN_TTL, async_list_clips
from .const import DOMAIN


@callback
def async_setup_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_events)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/events",
    vol.Optional("serial"): str,
    vol.Optional("limit", default=40): vol.All(int, vol.Range(min=1, max=500)),
    # Filters, all in Unix seconds: [start, end) for the calendar, before for "load more".
    vol.Optional("start"): vol.Coerce(float),
    vol.Optional("end"): vol.Coerce(float),
    vol.Optional("before"): vol.Coerce(float),
    vol.Optional("kinds"): [str],
    vol.Optional("language"): str,
})
@websocket_api.async_response
async def ws_events(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Recordings of one camera (or of all cameras), newest first."""
    start, end, before = msg.get("start"), msg.get("end"), msg.get("before")
    kinds = set(msg["kinds"]) if msg.get("kinds") else None
    # The user's language when set in their profile, else the server's.
    language = msg.get("language") or hass.config.language
    items: list[dict] = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        coord = entry.runtime_data
        for serial, cam in (coord.data or {}).items():
            if msg.get("serial") and msg["serial"] != serial:
                continue
            for clip in await async_list_clips(hass, coord, entry.entry_id, serial):
                if (start is not None and clip.ts < start) or (end is not None and clip.ts >= end):
                    continue
                if (before is not None and clip.ts >= before) or (kinds and clip.kind not in kinds):
                    continue
                items.append({
                    "id": clip.id,
                    "camera": cam.device.get("deviceName") or serial,
                    "time": dt_util.utc_from_timestamp(clip.ts).isoformat() if clip.ts else None,
                    "ts": clip.ts,
                    "kind": clip.kind,
                    "label": clip.label(language),
                    "duration": clip.duration,
                    "archived": clip.archived,
                    "thumbnail": clip.thumbnail,
                    "video": async_sign_path(hass, clip.video_path, SIGN_TTL) if clip.video_path else None,
                })
    items.sort(key=lambda i: i["ts"], reverse=True)
    connection.send_result(msg["id"], items[: msg["limit"]])
