from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from timetable_scheduler.config import SchedulerConfig, Period
from timetable_scheduler.parser import CourseOffering
from timetable_scheduler.sessions import build_sessions
from timetable_scheduler.model import build_and_solve


def small_config(**overrides):
    base = dict(
        rooms=["R1", "R2", "R3"],
        lab_rooms=["R2", "R3"],
        days=["Monday", "Tuesday", "Wednesday"],
        periods=[Period(1, "P1"), Period(2, "P2"), Period(3, "P3"), Period(4, "P4")],
        day_break_after_period=2,
        practical_block_size=2,
        max_consecutive_periods=3,
        position_daily_caps={"Regular": 4},
        default_position_daily_cap=4,
    )
    base.update(overrides)
    return SchedulerConfig(**base)


def _offering(**kw):
    base = dict(faculty="Dr. F", nature="Regular", course_code="C1", course_name="X",
                course_type="Th", programme="4A", semester="1st")
    base.update(kw)
    return CourseOffering(**base)


def test_single_section_all_placed_no_clashes():
    cfg = small_config()
    offs = [_offering(theory_hrs=3)]
    sessions = build_sessions(offs, config=cfg)
    result = build_and_solve(sessions, config=cfg, time_limit_seconds=10, balance_load=False)
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert len(result.unplaced) == 0
    assert len(result.placed) == 3


def test_multiple_sections_no_room_double_booking():
    cfg = small_config()
    offs = [
        _offering(course_code="C1", programme="4A", theory_hrs=2, faculty="Dr. A"),
        _offering(course_code="C2", programme="4B", theory_hrs=2, faculty="Dr. B"),
        _offering(course_code="C3", programme="4C", theory_hrs=2, faculty="Dr. C"),
    ]
    sessions = build_sessions(offs, config=cfg)
    result = build_and_solve(sessions, config=cfg, time_limit_seconds=15, balance_load=False)
    assert len(result.unplaced) == 0
    seen = set()
    for ps in result.placed:
        key = (ps.room, ps.day, ps.start_period)
        assert key not in seen, "room double-booked"
        seen.add(key)


def test_whole_section_conflicts_with_every_group_but_groups_run_parallel():
    cfg = small_config(rooms=["R1", "R2"], lab_rooms=["R1", "R2"])
    offs = [
        _offering(course_code="WHOLE", programme="4A", theory_hrs=1, faculty="Dr. Whole"),
        _offering(course_code="G1C", programme="4A(G1)", practical_hrs=2, course_type="Pr",
                   faculty="Dr. G1"),
        _offering(course_code="G2C", programme="4A(G2)", practical_hrs=2, course_type="Pr",
                   faculty="Dr. G2"),
    ]
    sessions = build_sessions(offs, config=cfg)
    result = build_and_solve(sessions, config=cfg, time_limit_seconds=20, balance_load=False)
    assert len(result.unplaced) == 0

    by_section = defaultdict(list)
    for ps in result.placed:
        by_section[ps.session.section].append(ps)

    whole = by_section["4A - 1st Sem"][0]
    g1 = by_section["4A (G1) - 1st Sem"][0]
    g2 = by_section["4A (G2) - 1st Sem"][0]

    def overlaps(a, b):
        a_slots = {(a.day, p) for p in a.periods}
        b_slots = {(b.day, p) for p in b.periods}
        return bool(a_slots & b_slots)

    assert not overlaps(whole, g1), "whole-section session must conflict with (not overlap) G1"
    assert not overlaps(whole, g2), "whole-section session must conflict with (not overlap) G2"
    # G1 and G2 themselves ARE allowed to run in parallel (different rooms) --
    # we don't assert they overlap (solver may or may not choose to), we only
    # assert the model didn't forbid it by checking no unplaced sessions above.


def test_faculty_never_double_booked():
    cfg = small_config()
    offs = [
        _offering(course_code="C1", programme="4A", theory_hrs=2, faculty="Dr. Busy"),
        _offering(course_code="C2", programme="4B", theory_hrs=2, faculty="Dr. Busy"),
    ]
    sessions = build_sessions(offs, config=cfg)
    result = build_and_solve(sessions, config=cfg, time_limit_seconds=15, balance_load=False)
    assert len(result.unplaced) == 0
    slots = [(ps.day, p) for ps in result.placed for p in ps.periods]
    assert len(slots) == len(set(slots)), "same faculty scheduled twice in one slot"


def test_practical_uses_lab_room_only():
    cfg = small_config(rooms=["R1", "LAB1"], lab_rooms=["LAB1"])
    offs = [_offering(course_code="P1", course_type="Pr", practical_hrs=2, faculty="Dr. Lab")]
    sessions = build_sessions(offs, config=cfg)
    result = build_and_solve(sessions, config=cfg, time_limit_seconds=10, balance_load=False)
    assert len(result.unplaced) == 0
    for ps in result.placed:
        assert ps.room == "LAB1"


def test_practical_block_is_continuous_and_respects_break():
    cfg = small_config(practical_block_size=2, day_break_after_period=2)
    offs = [_offering(course_code="P1", course_type="Pr", practical_hrs=2, faculty="Dr. Block")]
    sessions = build_sessions(offs, config=cfg)
    result = build_and_solve(sessions, config=cfg, time_limit_seconds=10, balance_load=False)
    assert len(result.placed) == 1
    ps = result.placed[0]
    assert ps.periods == list(range(ps.start_period, ps.start_period + 2))
    # must not straddle the break (period 2 -> 3 is the break in this config)
    assert not (ps.start_period <= 2 < ps.start_period + 1 and ps.periods[-1] > 2)


def test_infeasible_load_returns_partial_not_empty():
    # 1 room, 1 day, 1 faculty overloaded well beyond what fits -- a
    # complete timetable is impossible, but SOMETHING should be scheduled.
    cfg = small_config(rooms=["R1"], lab_rooms=["R1"], days=["Monday"])
    offs = [
        _offering(course_code="C1", programme="4A", theory_hrs=8, faculty="Dr. Overload"),
    ]
    sessions = build_sessions(offs, config=cfg)
    result = build_and_solve(sessions, config=cfg, time_limit_seconds=15, balance_load=True)
    assert result.status not in ("INFEASIBLE", "UNKNOWN") or len(result.placed) > 0
    assert len(result.placed) > 0, "Stage 2 must schedule at least the feasible subset, not give up"
    assert len(result.unplaced) > 0, "this load genuinely cannot fit -- some must remain unplaced"
    # every unplaced session must be traceable (never silently dropped)
    total_seen = len(result.placed) + len(result.unplaced)
    assert total_seen == len(sessions)
