"""Helpers to read VicoHome library events."""

from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util

from .coordinator import ts_seconds

# Cloud tag -> event type exposed by the event entity.
TAG_TO_TYPE = {
    "DOORBELL_PRESS": "ring",
    "PERSON": "person",
    "VEHICLE": "vehicle",
    "PET": "pet",
    "PACKAGE": "package",
}
# Push "type" -> event type. 3 = doorbell press (body "有人按门铃"); others unknown yet.
PUSH_TYPE_TO_EVENT = {"3": "ring"}
EVENT_TYPES = ["ring", "person", "vehicle", "pet", "package", "motion", "live"]
# Labels for the texts built here (media browser, events card); entity states use translations/*.json.
TYPE_LABELS = {
    "it": {
        "ring": "Campanello", "person": "Persona", "vehicle": "Veicolo", "pet": "Animale",
        "package": "Pacco", "motion": "Movimento", "live": "Camera avviata",
    },
    "en": {
        "ring": "Doorbell", "person": "Person", "vehicle": "Vehicle", "pet": "Pet",
        "package": "Package", "motion": "Motion", "live": "Camera started",
    },
    "de": {
        "ring": "Türklingel", "person": "Person", "vehicle": "Fahrzeug", "pet": "Haustier",
        "package": "Paket", "motion": "Bewegung", "live": "Kamera gestartet",
    },
}


def ui_language(language: str | None) -> str:
    """it, en or de from a Home Assistant language code (it, de-CH, en-GB…); English otherwise."""
    code = (language or "en").split("-")[0].lower()
    return code if code in TYPE_LABELS else "en"


def type_label(kind: str, language: str | None) -> str:
    return TYPE_LABELS[ui_language(language)].get(kind, kind)


def event_tags(ev: dict) -> list[str]:
    """Tags of an event, e.g. ["DOORBELL_PRESS"] or ["PERSON", "VEHICLE"]."""
    raw = ",".join(str(ev.get(k) or "") for k in ("tags", "doorbellTags", "aiEdgeTags"))
    seen: list[str] = []
    for tag in (t.strip().upper() for t in raw.split(",")):
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def event_type(ev: dict) -> str:
    """Main event type: ring wins, then the first known AI tag, else motion."""
    tags = event_tags(ev)
    if "DOORBELL_PRESS" in tags:
        return "ring"
    for tag in tags:
        if tag in TAG_TO_TYPE:
            return TAG_TO_TYPE[tag]
    return "motion"


def event_time(ev: dict) -> datetime | None:
    t = ts_seconds(ev.get("timestamp"))
    return dt_util.utc_from_timestamp(t) if t else None

