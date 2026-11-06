"""
Bath timetable from an iCal feed, used to lay lectures out on the calendar.

Set TIMETABLE_ICS_URL in .env to your timetable's iCal link (the same link you
subscribed Google Calendar to; comma-separate several), or drop a file named
timetable.ics next to this script.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent
LONDON = ZoneInfo("Europe/London")
LOCAL_FILE = ROOT / "timetable.ics"
CACHE_FILE = ROOT / ".timetable-cache.ics"
CACHE_TTL = 6 * 3600

KINDS = [
    ("lab", r"\blab"), ("tutorial", r"tutorial|\btut\b"), ("seminar", r"seminar"),
    ("workshop", r"workshop"), ("exam", r"\bexam"), ("lecture", r"lecture|\blec\b"),
]


def _urls() -> list[str]:
    raw = os.getenv("TIMETABLE_ICS_URL", "")
    return [u.strip().replace("webcal://", "https://") for u in raw.split(",") if u.strip()]


def _fetch(force: bool = False) -> list[bytes]:
    urls = _urls()
    if not urls:
        return [LOCAL_FILE.read_bytes()] if LOCAL_FILE.exists() else []
    if not force and CACHE_FILE.exists() and time.time() - CACHE_FILE.stat().st_mtime < CACHE_TTL:
        return CACHE_FILE.read_bytes().split(b"\n%%NEXT%%\n")
    blobs = []
    for u in urls:
        r = httpx.get(u, timeout=30, follow_redirects=True)
        r.raise_for_status()
        blobs.append(r.content)
    CACHE_FILE.write_bytes(b"\n%%NEXT%%\n".join(blobs))
    return blobs


def configured() -> bool:
    return bool(_urls()) or LOCAL_FILE.exists()


def _aware(dt) -> datetime | None:
    if not isinstance(dt, datetime):
        return None  # all-day events
    return dt.replace(tzinfo=LONDON) if dt.tzinfo is None else dt


def classify(text: str) -> str:
    t = text.lower()
    for kind, pat in KINDS:
        if re.search(pat, t):
            return kind
    return "lecture"


def events(start: datetime, end: datetime, detect_module, force: bool = False) -> list[dict]:
    import icalendar
    import recurring_ical_events

    out = []
    for blob in _fetch(force):
        cal = icalendar.Calendar.from_ical(blob)
        for ev in recurring_ical_events.of(cal).between(start, end):
            s = _aware(ev.get("DTSTART").dt)
            if s is None:
                continue
            e = _aware(ev.get("DTEND").dt) if ev.get("DTEND") else s + timedelta(hours=1)
            title = str(ev.get("SUMMARY", "")).strip()
            desc = str(ev.get("DESCRIPTION", "")).strip()
            code = detect_module(title, desc)
            if not code:
                continue
            uid = hashlib.sha1(f"{ev.get('UID')}|{s.isoformat()}".encode()).hexdigest()[:16]
            out.append({
                "uid": uid, "start": s.astimezone(LONDON), "end": e.astimezone(LONDON),
                "title": title, "location": str(ev.get("LOCATION", "")).strip(),
                "module_code": code, "kind": classify(f"{title} {desc}"),
            })
    out.sort(key=lambda x: x["start"])
    return out


def link(evts: list[dict], sessions: list[dict], tolerance_min: int = 50, max_shift_min: int = 360) -> dict[str, str]:
    """Pair Panopto sessions with timetable events. Returns {event_uid: delivery_id}."""
    pairs = []
    for s in sessions:
        if not s.get("start"):
            continue
        for e in evts:
            if s.get("module_code") and e["module_code"] != s["module_code"]:
                continue
            delta = abs((s["start"] - e["start"]).total_seconds()) / 60
            if delta <= tolerance_min:
                pairs.append((delta, e["uid"], s["delivery_id"]))
    pairs.sort()
    used_e, used_s, out = set(), set(), {}
    for _, eu, sid in pairs:
        if eu in used_e or sid in used_s:
            continue
        used_e.add(eu)
        used_s.add(sid)
        out[eu] = sid
    # Second pass: recordings started well off the timetabled time (uploaded after the slot, or
    # recorded late) pair with the unmatched slot of the same module on the same day.
    late = []
    for s in sessions:
        if s["delivery_id"] in used_s or not s.get("start") or not s.get("module_code"):
            continue
        day = s["start"].astimezone(LONDON).date()
        for e in evts:
            if e["uid"] in used_e or e["module_code"] != s["module_code"] or e["start"].date() != day:
                continue
            delta = abs((s["start"] - e["start"]).total_seconds()) / 60
            if delta <= max_shift_min:
                late.append((delta, e["uid"], s["delivery_id"]))
    for _, eu, sid in sorted(late):
        if eu in used_e or sid in used_s:
            continue
        used_e.add(eu)
        used_s.add(sid)
        out[eu] = sid
    return out
