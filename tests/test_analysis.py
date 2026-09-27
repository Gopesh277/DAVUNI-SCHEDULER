from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from timetable_scheduler.config import SchedulerConfig, Period
from timetable_scheduler.parser import CourseOffering
from timetable_scheduler.sessions import build_sessions
from timetable_scheduler.analysis import validate_offerings, detect_duplicate_offerings, preflight_capacity


def _off(**kw):
    base = dict(faculty="Dr. A", nature="Regular", course_code="C1", course_name="Course",
                course_type="Th", programme="4A", semester="1st", theory_hrs=2)
    base.update(kw)
    return CourseOffering(**base)


def small_config(**overrides):
    base = dict(
        rooms=["R1", "R2"], lab_rooms=["R2"],
        days=["Monday", "Tuesday"],
        periods=[Period(1, "P1"), Period(2, "P2"), Period(3, "P3"), Period(4, "P4")],
        day_break_after_period=2, practical_block_size=2, max_consecutive_periods=3,
        position_daily_caps={"Regular": 3}, default_position_daily_cap=3,
    )
    base.update(overrides)
    return SchedulerConfig(**base)


# ---- validation ------------------------------------------------------
def test_missing_faculty_is_an_error():
    issues = validate_offerings([_off(faculty="")])
    assert any(i["severity"] == "error" and i["field"] == "faculty" for i in issues)


def test_negative_hours_is_an_error():
    issues = validate_offerings([_off(theory_hrs=-1)])
    assert any(i["severity"] == "error" and i["field"] == "theory_hrs" for i in issues)


def test_missing_course_code_is_a_warning_not_dropped():
    off = _off(course_code="-")
    issues = validate_offerings([off])
    assert any(i["field"] == "course_code" for i in issues)
    # the row itself is never removed by validation -- caller decides
    assert off.course_code == "-"


def test_valid_row_has_no_issues():
    issues = validate_offerings([_off()])
    assert issues == []


def test_zero_hours_row_flagged():
    issues = validate_offerings([_off(theory_hrs=0, tutorial_hrs=0, practical_hrs=0)])
    assert any(i["field"] == "hours" for i in issues)


# ---- duplicate detection ----------------------------------------------
def test_exact_duplicate_rows_reported_not_removed():
    offs = [_off(), _off()]
    dups = detect_duplicate_offerings(offs)
    assert len(dups) == 1
    assert dups[0]["rows"] == [0, 1]
    assert len(offs) == 2  # nothing removed


def test_legitimately_different_rows_not_flagged():
    offs = [_off(course_code="C1"), _off(course_code="C2")]
    assert detect_duplicate_offerings(offs) == []


def test_different_hours_not_a_duplicate():
    offs = [_off(theory_hrs=2), _off(theory_hrs=3)]
    assert detect_duplicate_offerings(offs) == []


# ---- capacity pre-flight -----------------------------------------------
def test_capacity_ok_for_light_load():
    cfg = small_config()
    offs = [_off(theory_hrs=2, faculty="Dr. Light")]
    sessions = build_sessions(offs, config=cfg)
    cap = preflight_capacity(sessions, cfg)
    assert cap["warnings"] == []
    assert not cap["likely_infeasible"]


def test_capacity_flags_faculty_overload():
    cfg = small_config()  # daily cap 3, 2 days -> weekly capacity 6
    offs = [_off(theory_hrs=8, faculty="Dr. Heavy")]
    sessions = build_sessions(offs, config=cfg)
    cap = preflight_capacity(sessions, cfg)
    assert cap["likely_infeasible"]
    assert any(f["faculty"] == "Dr. Heavy" and f["overloaded"] for f in cap["faculty_load"])
    assert any("Dr. Heavy" in w for w in cap["warnings"])


def test_capacity_flags_section_overload():
    cfg = small_config()  # weekly slots = 2 days x 4 periods = 8
    offs = [
        _off(theory_hrs=6, faculty="Dr. A", programme="4X"),
        _off(theory_hrs=6, faculty="Dr. B", programme="4X", course_code="C2"),
    ]
    sessions = build_sessions(offs, config=cfg)
    cap = preflight_capacity(sessions, cfg)
    assert any(s["over_capacity"] for s in cap["section_load"])


def test_capacity_flags_lab_shortage():
    cfg = small_config(rooms=["R1", "R2"], lab_rooms=["R2"])  # 1 lab x 8 slots = 8 lab-periods
    offs = [_off(course_type="Pr", practical_hrs=12, faculty="Dr. Lab")]
    sessions = build_sessions(offs, config=cfg)
    cap = preflight_capacity(sessions, cfg)
    assert cap["practical_required_periods"] > cap["lab_capacity_periods"]
    assert any("lab" in w.lower() for w in cap["warnings"])


def test_capacity_reports_resource_types_separately():
    cfg = small_config()
    offs = [_off()]
    sessions = build_sessions(offs, config=cfg)
    cap = preflight_capacity(sessions, cfg)
    for key in ("room_capacity_periods", "lab_capacity_periods", "faculty_load", "section_load"):
        assert key in cap
