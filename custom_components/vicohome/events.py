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
TYPE_LABEL_IT = {
    "ring": "Campanello",
    "person": "Persona",
    "vehicle": "Veicolo",
    "pet": "Animale",
    "package": "Pacco",
    "motion": "Movimento",
    "live": "Camera avviata",
}


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


def event_title(ev: dict) -> str:
    when = event_time(ev)
    label = TYPE_LABEL_IT[event_type(ev)]
    stamp = dt_util.as_local(when).strftime("%d/%m %H:%M:%S") if when else "?"
    period = ev.get("period")
    secs = f" ({float(period):.0f} s)" if isinstance(period, (int, float)) else ""
    return f"{stamp} · {label}{secs}"
