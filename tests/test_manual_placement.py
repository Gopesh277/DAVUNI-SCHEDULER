from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from timetable_scheduler.config import SchedulerConfig
from timetable_scheduler.model import PlacedSession, ScheduleResult
from timetable_scheduler.sessions import Session
from timetable_scheduler.manual import (
    PlacementError, place_session, unplace_session, move_session,
)


def _session(id, **kw):
    base = dict(
        offering_index=id, course_code=f"CST{id}", course_name="Course",
        teacher="Dr. A", section="4A - 4th Sem", base_section="4A - 4th Sem",
        group=None, nature="Regular", kind="Theory", duration=1,
    )
    base.update(kw)
    return Session(id=id, **base)


def _result(placed=None, unplaced=None, config=None):
    return ScheduleResult(
        status="PARTIAL", placed=placed or [], unplaced=unplaced or [],
        wall_time_seconds=1.0, config=config or SchedulerConfig.default(),
    )


def test_place_moves_session_from_unplaced_to_placed():
    cfg = SchedulerConfig.default()
    s = _session(1)
    result = _result(unplaced=[s], config=cfg)
    new_result = place_session(result, cfg, session_id=1, day=0, start_period=1, room=cfg.rooms[0])
    assert new_result.unplaced == []
    assert len(new_result.placed) == 1
    assert new_result.placed[0].session.id == 1
    assert new_result.placed[0].room == cfg.rooms[0]
    assert new_result.placed[0].periods == [1]
    # original result is untouched
    assert result.unplaced == [s]
    assert result.placed == []


def test_place_unknown_session_id_raises():
    cfg = SchedulerConfig.default()
    result = _result(unplaced=[_session(1)], config=cfg)
    with pytest.raises(PlacementError):
        place_session(result, cfg, session_id=999, day=0, start_period=1, room=cfg.rooms[0])


def test_place_rejects_teacher_clash():
    cfg = SchedulerConfig.default()
    existing = PlacedSession(
        session=_session(1, teacher="Dr. A"), day=0, start_period=1, periods=[1], room=cfg.rooms[0],
    )
    candidate = _session(2, teacher="Dr. A", course_code="CST2")
    result = _result(placed=[existing], unplaced=[candidate], config=cfg)
    with pytest.raises(PlacementError, match="already teaching"):
        place_session(result, cfg, session_id=2, day=0, start_period=1, room=cfg.rooms[1])


def test_place_rejects_room_clash():
    cfg = SchedulerConfig.default()
    existing = PlacedSession(
        session=_session(1, teacher="Dr. A"), day=0, start_period=1, periods=[1], room=cfg.rooms[0],
    )
    candidate = _session(2, teacher="Dr. B", course_code="CST2")
    result = _result(placed=[existing], unplaced=[candidate], config=cfg)
    with pytest.raises(PlacementError, match="already in use"):
        place_session(result, cfg, session_id=2, day=0, start_period=1, room=cfg.rooms[0])


def test_place_rejects_same_section_clash():
    cfg = SchedulerConfig.default()
    existing = PlacedSession(
        session=_session(1, teacher="Dr. A", section="4A - 4th Sem", base_section="4A - 4th Sem"),
        day=0, start_period=1, periods=[1], room=cfg.rooms[0],
    )
    candidate = _session(2, teacher="Dr. B", course_code="CST2",
                          section="4A - 4th Sem", base_section="4A - 4th Sem")
    result = _result(placed=[existing], unplaced=[candidate], config=cfg)
    with pytest.raises(PlacementError, match="already has"):
        place_session(result, cfg, session_id=2, day=0, start_period=1, room=cfg.rooms[1])


def test_place_allows_sibling_groups_at_same_time():
    """G1 and G2 of the same base section are DIFFERENT students -- they
    may run simultaneously in different rooms."""
    cfg = SchedulerConfig.default()
    existing = PlacedSession(
        session=_session(1, teacher="Dr. A", section="4A - 4th Sem (G1)",
                          base_section="4A - 4th Sem", group="G1"),
        day=0, start_period=1, periods=[1], room=cfg.rooms[0],
    )
    candidate = _session(2, teacher="Dr. B", course_code="CST2",
                          section="4A - 4th Sem (G2)", base_section="4A - 4th Sem", group="G2")
    result = _result(placed=[existing], unplaced=[candidate], config=cfg)
    new_result = place_session(result, cfg, session_id=2, day=0, start_period=1, room=cfg.rooms[1])
    assert len(new_result.placed) == 2


def test_place_rejects_shared_session_clashing_with_any_group():
    """A shared (group=None) session of a base_section clashes with EVERY
    sub-group of that base_section -- same physical students."""
    cfg = SchedulerConfig.default()
    existing = PlacedSession(
        session=_session(1, teacher="Dr. A", section="4A - 4th Sem (G1)",
                          base_section="4A - 4th Sem", group="G1"),
        day=0, start_period=1, periods=[1], room=cfg.rooms[0],
    )
    shared_candidate = _session(2, teacher="Dr. B", course_code="CST2",
                                 section="4A - 4th Sem", base_section="4A - 4th Sem", group=None)
    result = _result(placed=[existing], unplaced=[shared_candidate], config=cfg)
    with pytest.raises(PlacementError, match="already has"):
        place_session(result, cfg, session_id=2, day=0, start_period=1, room=cfg.rooms[1])


def test_place_rejects_practical_in_non_lab_room():
    cfg = SchedulerConfig.default()
    non_lab_room = next(r for r in cfg.rooms if r not in cfg.lab_rooms)
    candidate = _session(1, kind="Practical", duration=2)
    result = _result(unplaced=[candidate], config=cfg)
    with pytest.raises(PlacementError, match="lab room"):
        place_session(result, cfg, session_id=1, day=0, start_period=1, room=non_lab_room)


def test_place_allows_practical_in_lab_room():
    cfg = SchedulerConfig.default()
    candidate = _session(1, kind="Practical", duration=2)
    result = _result(unplaced=[candidate], config=cfg)
    new_result = place_session(result, cfg, session_id=1, day=0, start_period=1, room=cfg.lab_rooms[0])
    assert new_result.placed[0].periods == [1, 2]


def test_place_rejects_block_straddling_lunch_break():
    cfg = SchedulerConfig.default()  # break_after_period=4
    candidate = _session(1, kind="Practical", duration=2)
    result = _result(unplaced=[candidate], config=cfg)
    with pytest.raises(PlacementError, match="lunch break"):
        place_session(result, cfg, session_id=1, day=0, start_period=4, room=cfg.lab_rooms[0])


def test_place_rejects_block_overflowing_the_day():
    cfg = SchedulerConfig.default()  # 6 periods/day
    candidate = _session(1, kind="Practical", duration=2)
    result = _result(unplaced=[candidate], config=cfg)
    with pytest.raises(PlacementError, match="only has"):
        place_session(result, cfg, session_id=1, day=0, start_period=6, room=cfg.lab_rooms[0])


def test_place_rejects_daily_cap_overflow():
    cfg = SchedulerConfig.default()
    cap = cfg.daily_cap_for("Senior")  # 5 by default, one less than the 6-period day
    # Occupy periods 1,2,3,5,6 (5 periods total = cap), leaving period 4 free.
    occupied_periods = [1, 2, 3, 5, 6]
    existing = [
        PlacedSession(session=_session(i, teacher="Dr. A", nature="Senior"),
                      day=0, start_period=p, periods=[p], room=cfg.rooms[0])
        for i, p in enumerate(occupied_periods)
    ]
    candidate = _session(999, teacher="Dr. A", nature="Senior", course_code="CSTX")
    result = _result(placed=existing, unplaced=[candidate], config=cfg)
    with pytest.raises(PlacementError, match="daily cap"):
        place_session(result, cfg, session_id=999, day=0, start_period=4, room=cfg.rooms[1])


def test_place_rejects_too_many_consecutive_periods():
    cfg = SchedulerConfig.default()  # max_consecutive_periods=2
    existing = [
        PlacedSession(session=_session(1, teacher="Dr. A"), day=0, start_period=1, periods=[1], room=cfg.rooms[0]),
        PlacedSession(session=_session(2, teacher="Dr. A"), day=0, start_period=2, periods=[2], room=cfg.rooms[0]),
    ]
    candidate = _session(3, teacher="Dr. A", course_code="CST3")
    result = _result(placed=existing, unplaced=[candidate], config=cfg)
    with pytest.raises(PlacementError, match="back-to-back"):
        place_session(result, cfg, session_id=3, day=0, start_period=3, room=cfg.rooms[1])


def test_unplace_moves_session_back():
    cfg = SchedulerConfig.default()
    s = _session(1)
    placed = [PlacedSession(session=s, day=0, start_period=1, periods=[1], room=cfg.rooms[0])]
    result = _result(placed=placed, config=cfg)
    new_result = unplace_session(result, session_id=1)
    assert new_result.placed == []
    assert len(new_result.unplaced) == 1
    assert new_result.unplaced[0].id == 1


def test_unplace_unknown_id_raises():
    cfg = SchedulerConfig.default()
    result = _result(config=cfg)
    with pytest.raises(PlacementError):
        unplace_session(result, session_id=42)


def test_move_relocates_an_already_placed_session():
    cfg = SchedulerConfig.default()
    s = _session(1)
    placed = [PlacedSession(session=s, day=0, start_period=1, periods=[1], room=cfg.rooms[0])]
    result = _result(placed=placed, config=cfg)
    new_result = move_session(result, cfg, session_id=1, day=1, start_period=3, room=cfg.rooms[2])
    assert len(new_result.placed) == 1
    moved = new_result.placed[0]
    assert moved.day == 1 and moved.start_period == 3 and moved.room == cfg.rooms[2]


def test_move_does_not_clash_against_its_own_old_slot():
    """Moving a session to a slightly different room/period at the same
    time must not be rejected as a clash against itself."""
    cfg = SchedulerConfig.default()
    s = _session(1)
    placed = [PlacedSession(session=s, day=0, start_period=1, periods=[1], room=cfg.rooms[0])]
    result = _result(placed=placed, config=cfg)
    new_result = move_session(result, cfg, session_id=1, day=0, start_period=1, room=cfg.rooms[1])
    assert new_result.placed[0].room == cfg.rooms[1]


def test_move_still_rejects_clash_with_a_different_session():
    cfg = SchedulerConfig.default()
    s1 = _session(1, teacher="Dr. A")
    s2 = _session(2, teacher="Dr. B", course_code="CST2")
    placed = [
        PlacedSession(session=s1, day=0, start_period=1, periods=[1], room=cfg.rooms[0]),
        PlacedSession(session=s2, day=0, start_period=2, periods=[2], room=cfg.rooms[0]),
    ]
    result = _result(placed=placed, config=cfg)
    with pytest.raises(PlacementError, match="already in use"):
        move_session(result, cfg, session_id=1, day=0, start_period=2, room=cfg.rooms[0])
