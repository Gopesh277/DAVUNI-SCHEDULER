"""Changing the time limit (or the data) while a solve is running must take
effect: the old solve is stopped and the new request runs with the new values.
Previously the new request silently joined the old solve and got a result
computed with the OLD time limit."""

import threading
import time

from backend import app as app_module
from timetable_scheduler import CourseOffering, ScheduleResult, SolveCancelled, model
from timetable_scheduler.config import SchedulerConfig
from timetable_scheduler.sessions import build_sessions

ROW = {"faculty": "A", "nature": "Regular", "course_code": "X1", "course_name": "X", "programme": "B.Tech CSE A",
       "semester": "1", "theory_hrs": 1, "tutorial_hrs": 0, "practical_hrs": 0}


def _install_fake_solver(monkeypatch, duration=4.0):
    """A stand-in for the real solver that 'solves' for `duration` seconds or until cancelled."""
    started, finished = [], []

    def fake(sessions, config, time_limit_seconds, num_workers, balance_load, cancel_event=None, **kw):
        started.append(time_limit_seconds)
        t0 = time.monotonic()
        while time.monotonic() - t0 < duration:
            if cancel_event is not None and cancel_event.is_set():
                raise SolveCancelled()
            time.sleep(0.02)
        finished.append(time_limit_seconds)
        return ScheduleResult(status="OPTIMAL", placed=[], unplaced=[], wall_time_seconds=duration, config=config)
    monkeypatch.setattr(app_module, "build_and_solve", fake)
    return started, finished


def _post_in_thread(client, out, key):
    def go():
        out[key] = client.post("/api/generate", json={})
    t = threading.Thread(target=go)
    t.start()
    return t


def _wait_for(cond, timeout=3.0):
    t0 = time.monotonic()
    while not cond() and time.monotonic() - t0 < timeout:
        time.sleep(0.02)
    assert cond()


def test_raising_the_time_limit_stops_the_old_solve_and_uses_the_new_limit(client_factory, monkeypatch):
    started, finished = _install_fake_solver(monkeypatch)
    a, _ = client_factory("cse")        # e.g. the page's automatic generate after a restart (150 s)
    b, _ = client_factory("cse")        # the same user saving new settings
    a.put("/api/courses", json=[ROW])
    out = {}
    t0 = time.monotonic()
    ta = _post_in_thread(a, out, "old")
    _wait_for(lambda: started == [150])

    assert b.put("/api/solver-settings", json={"time_limit": 300}).status_code == 200
    out["new"] = b.post("/api/generate", json={})          # "Save settings & regenerate"
    ta.join()

    assert started == [150, 300]                           # the new request ran its OWN solve with 300 s
    assert finished == [300]                               # the old one never completed
    assert out["old"].status_code == 409                   # and its caller was told it was replaced
    assert out["new"].status_code == 200
    assert time.monotonic() - t0 < 4.0 + 2.5               # the old solve was stopped, not waited for


def test_same_settings_still_share_one_solve(client_factory, monkeypatch):
    started, finished = _install_fake_solver(monkeypatch, duration=1.0)
    a, _ = client_factory("cse")
    b, _ = client_factory("cse")
    a.put("/api/courses", json=[ROW])
    out = {}
    ta = _post_in_thread(a, out, "a")
    _wait_for(lambda: len(started) == 1)
    out["b"] = b.post("/api/generate", json={})
    ta.join()
    assert started == [150] and finished == [150]          # one solve, shared
    assert out["a"].status_code == out["b"].status_code == 200


def test_editing_the_register_stops_the_stale_solve(client_factory, monkeypatch):
    started, finished = _install_fake_solver(monkeypatch)
    a, _ = client_factory("cse")
    b, _ = client_factory("cse")
    a.put("/api/courses", json=[ROW])
    out = {}
    ta = _post_in_thread(a, out, "old")
    _wait_for(lambda: len(started) == 1)
    b.put("/api/courses", json=[ROW, {**ROW, "course_code": "X2"}])    # data changed -> running solve is stale
    out["new"] = b.post("/api/generate", json={})
    ta.join()
    assert len(started) == 2 and out["old"].status_code == 409 and out["new"].status_code == 200


def test_other_departments_are_never_cancelled(client_factory, monkeypatch):
    started, finished = _install_fake_solver(monkeypatch, duration=1.5)
    cse, _ = client_factory("cse")
    civil, _ = client_factory("civil")
    cse.put("/api/courses", json=[ROW])
    cfg = civil.get("/api/config").json()["config"]; cfg["rooms"], cfg["lab_rooms"] = ["C1"], ["C1"]
    civil.put("/api/config", json=cfg)
    civil.put("/api/courses", json=[{**ROW, "programme": "B.Tech Civil A"}])
    out = {}
    ta = _post_in_thread(cse, out, "cse")
    _wait_for(lambda: len(started) == 1)
    civil.put("/api/solver-settings", json={"time_limit": 99})
    out["civil"] = civil.post("/api/generate", json={})
    ta.join()
    assert out["cse"].status_code == 200 and out["civil"].status_code == 200 and len(finished) == 2


# ------------------------------------------------------- the real CP-SAT solver really stops
def _heavy_sessions():
    cs = [CourseOffering(faculty=f"T{i % 12}", nature="Regular", course_code=f"C{i}", course_name="x", course_type="Theory",
                         programme=f"Sec{i % 6}", semester="1", theory_hrs=4, tutorial_hrs=1,
                         practical_hrs=2 if i % 3 == 0 else 0) for i in range(30)]
    cfg = SchedulerConfig.default()
    return build_sessions(cs, config=cfg), cfg


def test_real_solver_stops_promptly_when_cancelled():
    sessions, cfg = _heavy_sessions()
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    t0 = time.monotonic()
    try:
        model.build_and_solve(sessions, config=cfg, time_limit_seconds=60, num_workers=2, balance_load=True, cancel_event=cancel)
        raised = False
    except SolveCancelled:
        raised = True
    elapsed = time.monotonic() - t0
    assert raised and elapsed < 6, (raised, elapsed)       # a 60 s solve stopped about 1.5 s in


def test_already_cancelled_solve_does_not_start():
    sessions, cfg = _heavy_sessions()
    cancel = threading.Event(); cancel.set()
    t0 = time.monotonic()
    try:
        model.build_and_solve(sessions, config=cfg, time_limit_seconds=60, num_workers=2, cancel_event=cancel)
        assert False, "should have raised"
    except SolveCancelled:
        pass
    assert time.monotonic() - t0 < 3
