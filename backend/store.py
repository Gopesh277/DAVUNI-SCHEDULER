"""
Holds the application's mutable state, one independent copy per department.

Each department has its own teaching-load register, room/day/period config,
solver settings and last generated schedule, persisted to its own JSON file
(backend/data/dept_<id>.json) so departments never see each other's data
and everything survives a server restart. The generated schedule itself is
not persisted -- it is cheap to regenerate.

CSE starts with the department's real rooms (see timetable_scheduler.config).
Every other department starts with NO rooms: the web UI asks for them on
first sign-in, because only the department knows its own rooms.

The module-level `store` object is a thin proxy for "the current
department's store". The auth middleware sets the current department from
the session on every request, so route handlers keep writing `store.courses`
etc. and always get the signed-in department's data.
"""

from __future__ import annotations

import json
import os
import threading
from contextvars import ContextVar
from pathlib import Path

from timetable_scheduler import SchedulerConfig, CourseOffering, ScheduleResult
from timetable_scheduler.config import (
    DEFAULT_TIME_LIMIT_SECONDS, DEFAULT_NUM_WORKERS, DAYS, PERIODS,
    DAY_BREAK_AFTER_PERIOD, PRACTICAL_BLOCK_SIZE, DEFAULT_MAX_CONSECUTIVE_PERIODS,
    DEFAULT_POSITION_DAILY_CAPS, DEFAULT_POSITION_DAILY_CAP,
)

from . import departments

DATA_DIR = Path(__file__).parent / "data"
LEGACY_STATE_FILE = DATA_DIR / "app_state.json"  # pre-multi-department single-file state (CSE)
PRELOADED_ROOMS_DEPT = "cse"  # the only department that ships with rooms


def snapshot_path(dept_id: str) -> Path:
    return DATA_DIR / f"schedule_{dept_id}.json"


def read_snapshot(dept_id: str) -> dict | None:
    """The department's last generated timetable as saved JSON (or None).

    Read straight from disk -- it never creates a department's store -- so
    the dean portal can look at every department without touching their
    live state. Written by DeptStore.save_snapshot."""
    if not departments.exists(dept_id):
        return None
    f = snapshot_path(dept_id.strip().lower())
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except Exception:
        return None


def _empty_config() -> SchedulerConfig:
    """Standard days/periods/rules, but no rooms -- the department adds them."""
    return SchedulerConfig(
        rooms=[], lab_rooms=[], days=list(DAYS), periods=list(PERIODS),
        day_break_after_period=DAY_BREAK_AFTER_PERIOD,
        practical_block_size=PRACTICAL_BLOCK_SIZE,
        max_consecutive_periods=DEFAULT_MAX_CONSECUTIVE_PERIODS,
        position_daily_caps=dict(DEFAULT_POSITION_DAILY_CAPS),
        default_position_daily_cap=DEFAULT_POSITION_DAILY_CAP,
    )


class DeptStore:
    def __init__(self, dept_id: str):
        self.dept_id = dept_id
        self._lock = threading.RLock()
        self.courses: list[CourseOffering] = []
        self.config: SchedulerConfig = (
            SchedulerConfig.default() if dept_id == PRELOADED_ROOMS_DEPT else _empty_config()
        )
        self.solver_settings: dict = {
            "time_limit": DEFAULT_TIME_LIMIT_SECONDS,
            "workers": DEFAULT_NUM_WORKERS,
            "balance": True,
        }
        self.last_result: ScheduleResult | None = None
        # bumped whenever the register or rooms/days/periods/rules change, so a solve that
        # started on older data can be recognised as out of date
        self.data_version = 0
        self._load()

    @property
    def state_file(self) -> Path:
        return DATA_DIR / f"dept_{self.dept_id}.json"

    @property
    def rooms_configured(self) -> bool:
        return bool(self.config.rooms)

    # ---------------- persistence -----------------------------------
    def _load(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        candidates = [self.state_file]
        if self.dept_id == PRELOADED_ROOMS_DEPT:
            candidates.append(LEGACY_STATE_FILE)  # carry over an existing single-department install
        for f in candidates:
            if not f.exists():
                continue
            try:
                d = json.loads(f.read_text())
                self.courses = [CourseOffering(**c) for c in d.get("courses", [])]
                if d.get("config"):
                    self.config = SchedulerConfig.from_dict(d["config"])
                if d.get("solver_settings"):
                    self.solver_settings.update(d["solver_settings"])
                if f is not self.state_file:
                    self._persist()  # migrate legacy file into the per-department file
                return
            except Exception:
                continue  # fall through to a clean empty state
        self.courses = []
        self._persist()

    def _persist(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        d = {
            "courses": [c.to_dict() for c in self.courses],
            "config": self.config.to_dict(),
            "solver_settings": self.solver_settings,
        }
        self.state_file.write_text(json.dumps(d, indent=2, default=str))

    # ---------------- published timetable (for the dean portal) -------
    def save_snapshot(self, snapshot: dict):
        """Persist the latest timetable so it survives restarts and can be
        read by the dean portal. Atomic: write a temp file, then rename."""
        with self._lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            f = snapshot_path(self.dept_id)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, default=str), encoding="utf-8")
            os.replace(tmp, f)

    def clear_snapshot(self):
        with self._lock:
            try:
                snapshot_path(self.dept_id).unlink()
            except FileNotFoundError:
                pass

    # ---------------- mutators -----------------------------------------
    def set_courses(self, courses: list[CourseOffering]):
        with self._lock:
            self.courses = courses
            self.data_version += 1
            self._persist()

    def set_config(self, config: SchedulerConfig):
        with self._lock:
            self.config = config
            self.data_version += 1
            self._persist()

    def set_solver_settings(self, **kwargs):
        with self._lock:
            self.solver_settings.update({k: v for k, v in kwargs.items() if v is not None})
            self._persist()


class _Registry:
    """Lazily creates and caches one DeptStore per known department."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stores: dict[str, DeptStore] = {}

    def get(self, dept_id: str) -> DeptStore:
        dept_id = (dept_id or "").strip().lower()
        if not departments.exists(dept_id):
            raise KeyError(f"Unknown department: {dept_id!r}")
        with self._lock:
            if dept_id not in self._stores:
                self._stores[dept_id] = DeptStore(dept_id)
            return self._stores[dept_id]


registry = _Registry()

_current_dept: ContextVar[str | None] = ContextVar("current_department", default=None)


def set_current_department(dept_id: str | None):
    return _current_dept.set(dept_id)


def current_department_id() -> str | None:
    return _current_dept.get()


class _CurrentStore:
    """Proxy: attribute reads/writes go to the signed-in department's store."""

    def _target(self) -> DeptStore:
        dept = _current_dept.get()
        if not dept:
            raise RuntimeError("No department selected for this request.")
        return registry.get(dept)

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        setattr(self._target(), name, value)


store = _CurrentStore()
