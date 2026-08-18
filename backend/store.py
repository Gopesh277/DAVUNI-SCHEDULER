"""
Holds the application's mutable state: the current teaching-load
register, the department config (rooms/days/periods), solver settings,
and the most recently generated schedule.

Courses + config + solver settings are persisted to a JSON file on disk
so they survive a server restart. The generated schedule itself is not
persisted -- it's cheap to regenerate and large table is derived data.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from timetable_scheduler import (
    SchedulerConfig, CourseOffering, ScheduleResult,
    parse_teaching_load,
)
from timetable_scheduler.config import DEFAULT_TIME_LIMIT_SECONDS, DEFAULT_NUM_WORKERS

DATA_DIR = Path(__file__).parent / "data"
STATE_FILE = DATA_DIR / "app_state.json"
SAMPLE_FILE = Path(__file__).parent.parent / "sample_data" / "teaching_load24252updated.xlsx"


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self.courses: list[CourseOffering] = []
        self.config: SchedulerConfig = SchedulerConfig.default()
        self.solver_settings: dict = {
            "time_limit": DEFAULT_TIME_LIMIT_SECONDS,
            "workers": DEFAULT_NUM_WORKERS,
            "balance": True,
        }
        self.last_result: ScheduleResult | None = None
        self._load()

    # ---------------- persistence -----------------------------------
    def _load(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text())
                self.courses = [CourseOffering(**c) for c in d.get("courses", [])]
                if d.get("config"):
                    self.config = SchedulerConfig.from_dict(d["config"])
                if d.get("solver_settings"):
                    self.solver_settings.update(d["solver_settings"])
                if self.courses:
                    return
            except Exception:
                pass  # fall through to sample load below
        # first run, or corrupt/empty state: seed from the bundled sample register
        if SAMPLE_FILE.exists():
            try:
                self.courses = parse_teaching_load(SAMPLE_FILE)
            except Exception:
                self.courses = []
        self._persist()

    def _persist(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        d = {
            "courses": [c.to_dict() for c in self.courses],
            "config": self.config.to_dict(),
            "solver_settings": self.solver_settings,
        }
        STATE_FILE.write_text(json.dumps(d, indent=2, default=str))

    # ---------------- mutators -----------------------------------------
    def set_courses(self, courses: list[CourseOffering]):
        with self._lock:
            self.courses = courses
            self._persist()

    def set_config(self, config: SchedulerConfig):
        with self._lock:
            self.config = config
            self._persist()

    def set_solver_settings(self, **kwargs):
        with self._lock:
            self.solver_settings.update({k: v for k, v in kwargs.items() if v is not None})
            self._persist()

    def reset_to_sample(self):
        with self._lock:
            self.courses = parse_teaching_load(SAMPLE_FILE)
            self._persist()


store = Store()
