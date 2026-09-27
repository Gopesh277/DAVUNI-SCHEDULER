"""
Holds the application's mutable state: the current teaching-load
register, the department config (rooms/days/periods), solver settings,
and the most recently generated schedule.

Courses + config + solver settings are persisted to a JSON file on disk
so they survive a server restart. The generated schedule itself is not
persisted -- it's cheap to regenerate and large table is derived data.

The store starts empty on a genuinely fresh install -- it does not seed
itself from any bundled sample register. The department's own register
is loaded by uploading a file (or entering rows by hand) in "Manage
data"; nothing is pre-populated on the user's behalf.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from timetable_scheduler import SchedulerConfig, CourseOffering, ScheduleResult
from timetable_scheduler.config import DEFAULT_TIME_LIMIT_SECONDS, DEFAULT_NUM_WORKERS

DATA_DIR = Path(__file__).parent / "data"
STATE_FILE = DATA_DIR / "app_state.json"


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
                return
            except Exception:
                pass  # fall through to a clean empty state below
        # first run, or corrupt/empty state file: start with no courses at
        # all -- the register is only ever populated by an explicit upload
        # or hand-entered rows, never auto-seeded.
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


store = Store()
