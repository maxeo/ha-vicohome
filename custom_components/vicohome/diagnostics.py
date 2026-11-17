"""Diagnostics: raw cloud payloads (redacted) to learn each model's fields."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .coordinator import VicoConfigEntry

TO_REDACT = {
    CONF_EMAIL, CONF_PASSWORD, "token", "accessToken", "credential", "sign", "linkToken",
    "macAddress", "ip", "networkName", "adminName", "userId", "imageUrl", "videoUrl",
    "keyShotUrl", "lastPushImageUrl", "latitude", "longitude",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: VicoConfigEntry) -> dict[str, Any]:
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "raw": async_redact_data(entry.runtime_data.raw, TO_REDACT),
        "push": {
            "active": bool(entry.runtime_data.push and entry.runtime_data.push.active),
            "last_payload": async_redact_data(
                getattr(entry.runtime_data.push, "last_payload", None) or {}, TO_REDACT
            ),
        },
    }
