"""The solver time limit is whatever the user saved in Settings -- no hidden
defaults override it, and the solve never runs past it."""

import pytest
from ortools.sat.python import cp_model

from backend import app as app_module
from timetable_scheduler import CourseOffering, ScheduleResult, model
from timetable_scheduler.config import SchedulerConfig
from timetable_scheduler.sessions import build_sessions


def _courses():
    return [CourseOffering(faculty=f"T{i}", nature="Regular", course_code=f"C{i}", course_name=f"Course {i}", course_type="Theory",
                           programme=f"Sec{i % 3}", semester="1", theory_hrs=3, tutorial_hrs=0, practical_hrs=0)
            for i in range(6)]


# ---------------------------------------------------- saved setting reaches the solver
def test_saved_time_limit_is_what_generate_uses(client_factory, monkeypatch):
    c, _ = client_factory("cse")
    assert c.get("/api/config").json()["solver_settings"]["time_limit"] == 150   # only the starting value

    seen = {}

    def fake_build_and_solve(sessions, config, time_limit_seconds, num_workers, balance_load, **kw):
        seen.update(time_limit=time_limit_seconds, workers=num_workers, balance=balance_load)
        return ScheduleResult(status="OPTIMAL", placed=[], unplaced=[], wall_time_seconds=0.0, config=config)
    monkeypatch.setattr(app_module, "build_and_solve", fake_build_and_solve)

    row = {"faculty": "A", "nature": "Regular", "course_code": "X1", "course_name": "X", "programme": "B.Tech CSE A",
           "semester": "1", "theory_hrs": 1, "tutorial_hrs": 0, "practical_hrs": 0}
    c.put("/api/courses", json=[row])

    assert c.put("/api/solver-settings", json={"time_limit": 37, "workers": 3, "balance": False}).status_code == 200
    assert c.post("/api/generate", json={}).status_code == 200      # what the UI sends: nothing overrides the saved value
    assert seen == {"time_limit": 37, "workers": 3, "balance": False}

    # saving only one field keeps the others (no hidden 60 / 8 defaults)
    assert c.put("/api/solver-settings", json={"workers": 5}).status_code == 200
    s = c.get("/api/config").json()["solver_settings"]
    assert s == {"time_limit": 37, "workers": 5, "balance": False}

    # a per-call override still works, and bad values are rejected
    c.post("/api/generate", json={"time_limit": 12})
    assert seen["time_limit"] == 12
    assert c.put("/api/solver-settings", json={"time_limit": 0}).status_code == 422
    assert c.put("/api/solver-settings", json={"time_limit": 601}).status_code == 422


def test_each_department_keeps_its_own_time_limit(client_factory):
    a, _ = client_factory("cse")
    b, _ = client_factory("civil")
    a.put("/api/solver-settings", json={"time_limit": 20})
    b.put("/api/solver-settings", json={"time_limit": 90})
    assert a.get("/api/config").json()["solver_settings"]["time_limit"] == 20
    assert b.get("/api/config").json()["solver_settings"]["time_limit"] == 90


# ------------------------------------------------ the solve never runs past the limit
class _FakeSolver:
    def __init__(self, wall): self._wall = wall
    def WallTime(self): return self._wall


@pytest.mark.parametrize("limit", [3, 5, 10, 30, 150, 600])
@pytest.mark.parametrize("balance", [True, False])
def test_worst_case_never_exceeds_limit(monkeypatch, limit, balance):
    """Worst case: every solver stage uses all the time it is given and finds nothing."""
    clock = [0.0]
    monkeypatch.setattr(model.time, "monotonic", lambda: clock[0])
    budgets = []

    def slow_solve(ctx, time_limit, workers, log, cancel=None):
        budgets.append(time_limit)
        clock[0] += time_limit            # the stage uses its whole budget
        return _FakeSolver(time_limit), cp_model.UNKNOWN
    monkeypatch.setattr(model, "_solve", slow_solve)

    cfg = SchedulerConfig.default()
    model.build_and_solve(build_sessions(_courses(), config=cfg), config=cfg,
                          time_limit_seconds=limit, num_workers=2, balance_load=balance)
    assert clock[0] <= max(limit, model.MIN_SOLVE_SECONDS * 2) + 1e-6, (limit, balance, budgets)
    assert budgets and all(b >= model.MIN_SOLVE_SECONDS for b in budgets)


def test_real_solve_respects_a_short_limit():
    import time
    cfg = SchedulerConfig.default()
    t0 = time.monotonic()
    res = model.build_and_solve(build_sessions(_courses(), config=cfg), config=cfg,
                                time_limit_seconds=4, num_workers=2, balance_load=True)
    assert res.status in ("OPTIMAL", "FEASIBLE") and not res.unplaced
    assert time.monotonic() - t0 < 4 + 2
