"""
The list of DAV University departments that can sign in.

Departments live in backend/departments.json so they can be added,
renamed or removed without touching code. Each entry needs:
  id     short lowercase key (letters/digits/underscore) -- used for the
         per-department data file and optional per-department credentials
  name   the department's name WITHOUT the "Department of" prefix
  short  a short label used in the page footer / download file name

Only "cse" ships with pre-loaded rooms (see store.py); every other
department is asked for its rooms the first time it signs in.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_FILE = Path(__file__).parent / "departments.json"
_ID_RE = re.compile(r"^[a-z0-9_]+$")

_FALLBACK = [{"id": "cse", "name": "Computer Science & Engineering", "short": "CSE"}]


def _load() -> dict[str, dict]:
    try:
        raw = json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        raw = _FALLBACK
    out: dict[str, dict] = {}
    for d in raw:
        did = str(d.get("id", "")).strip().lower()
        name = str(d.get("name", "")).strip()
        if not did or not name or not _ID_RE.match(did) or did in out:
            continue
        out[did] = {"id": did, "name": name, "short": str(d.get("short") or name).strip()}
    return out or {d["id"]: d for d in _FALLBACK}


_DEPARTMENTS = _load()


def _public(d: dict) -> dict:
    return {**d, "display": f"Department of {d['name']}"}


def list_departments() -> list[dict]:
    return [_public(d) for d in _DEPARTMENTS.values()]


def get(dept_id: str | None) -> dict | None:
    d = _DEPARTMENTS.get((dept_id or "").strip().lower())
    return _public(d) if d else None


def exists(dept_id: str | None) -> bool:
    return (dept_id or "").strip().lower() in _DEPARTMENTS
