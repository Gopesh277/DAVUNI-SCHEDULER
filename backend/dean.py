"""
Dean's Office portal API (read-only, all departments).

The dean can load any department's last generated timetable and check
whether a group of faculty -- possibly from several departments -- is free
for a meeting, and where each busy person is right now.

Data comes from the timetable snapshot every department saves whenever its
timetable is generated or edited (see store.save_snapshot), so this module
never touches a department's live state.

Faculty are matched across departments by name (case-insensitive, titles
such as "Dr." / "Prof." ignored), so one person teaching in two departments
is a single person here and a clash in either department counts as busy.

Times are compared as clock times, not period numbers, so departments may
define their own period timings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from timetable_scheduler.config import PERIODS as _DEFAULT_PERIODS

from . import departments
from . import store as store_mod

router = APIRouter(prefix="/api/dean")

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------------------------------------------------------------- helpers
_TITLE_RE = re.compile(r"^(dr|prof|professor|mr|mrs|ms|miss|er|sh|shri|smt)\b\.?\s*", re.I)


def normalize_name(name: str) -> str:
    """'Dr. Rahul  Hans' / 'rahul hans' / 'Prof.Rahul Hans' -> 'rahul hans'."""
    s = re.sub(r"\s+", " ", (name or "").replace(".", ". ")).strip()
    while True:
        m = _TITLE_RE.match(s)
        if not m:
            break
        s = s[m.end():]
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


_RANGE_RE = re.compile(
    r"(\d{1,2})[:.](\d{2})\s*([ap]\.?m\.?)?\s*(?:-|\u2013|\u2014|to)\s*(\d{1,2})[:.](\d{2})\s*([ap]\.?m\.?)?", re.I)


def _clock(h: int, m: int, ampm: Optional[str]) -> int:
    if ampm:
        pm = ampm.lower().startswith("p")
        h = h % 12 + (12 if pm else 0)
    elif 1 <= h <= 7:  # a bare "1:30" in a college timetable means 1:30 pm
        h += 12
    return h * 60 + m


def parse_label(label: str) -> Optional[tuple[int, int]]:
    """'9:30-10:30' -> (570, 630) minutes since midnight; '12:30-1:30' -> (750, 810)."""
    m = _RANGE_RE.search(label or "")
    if not m:
        return None
    h1, m1, a1, h2, m2, a2 = m.groups()
    s, e = _clock(int(h1), int(m1), a1), _clock(int(h2), int(m2), a2)
    if e <= s and not a2:
        e += 12 * 60
    return (s, e) if 0 <= s < e <= 24 * 60 else None


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_hhmm(text: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise HTTPException(400, f"Invalid time {text!r}; use HH:MM (24-hour).")
    return int(m.group(1)) * 60 + int(m.group(2))


def _period_windows(config: dict) -> dict[int, tuple[int, int]]:
    """{period index: (start_min, end_min)} for one department's config."""
    out: dict[int, tuple[int, int]] = {}
    for p in config.get("periods", []):
        iv = parse_label(p.get("label", ""))
        if iv is None:  # unparseable custom label -> fall back to the default timing of that period number
            d = next((dp for dp in _DEFAULT_PERIODS if dp.index == p.get("index")), None)
            iv = parse_label(d.label) if d else None
        if iv:
            out[p["index"]] = iv
    return out


# ------------------------------------------------------------------ model
@dataclass
class Block:
    dept_id: str
    dept_short: str
    dept_display: str
    day: str            # lower-case weekday name
    day_label: str
    start: int
    end: int
    room: str
    course_code: str
    course_name: str
    kind: str
    section: str


@dataclass
class Person:
    key: str
    name: str
    depts: dict = field(default_factory=dict)      # dept id -> short label
    blocks: list = field(default_factory=list)


def load_all() -> tuple[dict[str, Person], list[tuple[int, int]], list[str]]:
    """Read every department's snapshot -> (people by key, sorted unique period
    windows, working days in weekday order)."""
    people: dict[str, Person] = {}
    windows: set[tuple[int, int]] = set()
    days: list[str] = []
    for d in departments.list_departments():
        snap = store_mod.read_snapshot(d["id"])
        if not snap:
            continue
        cfg = snap.get("config", {})
        pw = _period_windows(cfg)
        windows.update(pw.values())
        for day in cfg.get("days", []):
            if day not in days:
                days.append(day)
        for p in snap.get("placed", []):
            span = [pw.get(i) for i in range(p["start_period"], p["start_period"] + p["duration"])]
            if not span or any(x is None for x in span):
                continue
            key = normalize_name(p["teacher"])
            if not key:
                continue
            person = people.setdefault(key, Person(key=key, name=p["teacher"]))
            person.depts[d["id"]] = d["short"]
            person.blocks.append(Block(
                d["id"], d["short"], d["display"], p["day_label"].strip().lower(), p["day_label"],
                min(s for s, _ in span), max(e for _, e in span), p["room"],
                p["course_code"], p["course_name"], p["kind"], p["section"]))
    days.sort(key=lambda x: _WEEKDAYS.index(x.lower()) if x.lower() in _WEEKDAYS else 99)
    return people, sorted(windows), days


def _block_json(b: Block) -> dict:
    return {
        "dept_id": b.dept_id, "dept": b.dept_short, "dept_display": b.dept_display,
        "room": b.room, "course_code": b.course_code, "course_name": b.course_name,
        "kind": b.kind, "section": b.section, "day": b.day_label,
        "start": hhmm(b.start), "end": hhmm(b.end),
    }


def _overlaps(b: Block, start: int, end: int) -> bool:
    return b.start < end and b.end > start


# -------------------------------------------------------------- endpoints
@router.get("/overview")
def overview():
    out = []
    for d in departments.list_departments():
        snap = store_mod.read_snapshot(d["id"])
        st = (snap or {}).get("stats", {})
        out.append({
            **d,
            "has_timetable": bool(snap),
            "generated_at": (snap or {}).get("generated_at"),
            "status": (snap or {}).get("status"),
            "placed": st.get("placed", 0), "unplaced": st.get("unplaced", 0),
            "faculty": st.get("faculty", 0), "rooms": st.get("rooms", 0),
            "completion_percentage": st.get("completion_percentage"),
        })
    return {"departments": out}


@router.get("/timetable/{dept_id}")
def timetable(dept_id: str):
    dept = departments.get(dept_id)
    if dept is None:
        raise HTTPException(404, "Unknown department.")
    snap = store_mod.read_snapshot(dept["id"])
    if not snap:
        raise HTTPException(404, f"{dept['display']} has not generated a timetable yet.")
    return snap


@router.get("/faculty")
def faculty():
    people, _, _ = load_all()
    rows = [{
        "key": p.key, "name": p.name,
        "departments": [{"id": i, "short": s} for i, s in sorted(p.depts.items())],
        "sessions": len(p.blocks),
    } for p in people.values()]
    rows.sort(key=lambda r: r["name"].lower())
    return {"faculty": rows}


class AvailabilityIn(BaseModel):
    faculty: list[str] = Field(..., min_length=1, max_length=60)
    day: Optional[str] = None      # e.g. "Tuesday"
    start: Optional[str] = None    # "HH:MM" 24-hour
    end: Optional[str] = None      # optional; defaults to start + 1 minute ("right now")


@router.post("/availability")
def availability(req: AvailabilityIn):
    people, windows, days = load_all()
    chosen = []
    for k in dict.fromkeys(req.faculty):  # de-dupe, keep order
        if k not in people:
            raise HTTPException(404, "Unknown faculty member (their department may have regenerated its timetable). Reload the list.")
        chosen.append(people[k])

    # ---- 1) the specific time asked about ("is everyone free then / where are they?")
    at_time = None
    if req.start:
        if not req.day:
            raise HTTPException(400, "Choose a day as well as a time.")
        start = parse_hhmm(req.start)
        end = parse_hhmm(req.end) if req.end else min(start + 1, 24 * 60)
        if end <= start:
            raise HTTPException(400, "End time must be after the start time.")
        day = req.day.strip().lower()
        rows = []
        for p in chosen:
            today = sorted((b for b in p.blocks if b.day == day), key=lambda b: b.start)
            busy = [b for b in today if _overlaps(b, start, end)]
            row = {"key": p.key, "name": p.name,
                   "departments": [{"id": i, "short": s} for i, s in sorted(p.depts.items())],
                   "free": not busy, "busy": [_block_json(b) for b in busy]}
            if busy:
                t = max(b.end for b in busy)
                changed = True
                while changed:  # chain back-to-back classes to find when they are truly free
                    changed = False
                    for b in today:
                        if b.start <= t < b.end:
                            t, changed = b.end, True
                row["free_from"] = hhmm(t)
            else:
                nxt = next((b for b in today if b.start >= end), None)
                row["next_class"] = _block_json(nxt) if nxt else None
            rows.append(row)
        at_time = {
            "day": req.day.strip(), "start": hhmm(start), "end": hhmm(end),
            "all_free": all(r["free"] for r in rows),
            "busy_count": sum(1 for r in rows if not r["free"]),
            "people": rows,
        }

    # ---- 2) the whole week: when is everybody free together?
    grid, suggested = {}, {}
    for day_label in days:
        d = day_label.lower()
        cells, free_runs = [], []
        for (s, e) in windows:
            busy = []
            for p in chosen:
                hits = [b for b in p.blocks if b.day == d and _overlaps(b, s, e)]
                if hits:
                    busy.append({"name": p.name, **_block_json(hits[0])})
            cells.append({"start": hhmm(s), "end": hhmm(e), "all_free": not busy, "busy": busy})
            if not busy:
                if free_runs and free_runs[-1][1] >= s:
                    free_runs[-1][1] = e
                else:
                    free_runs.append([s, e])
        grid[day_label] = cells
        suggested[day_label] = [[hhmm(a), hhmm(b)] for a, b in free_runs]

    return {
        "at_time": at_time,
        "slots": [{"start": hhmm(s), "end": hhmm(e)} for s, e in windows],
        "days": days,
        "grid": grid,
        "suggested": suggested,
        "has_data": bool(people),
    }
