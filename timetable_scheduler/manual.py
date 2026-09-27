"""
Manual overrides to an already-generated ScheduleResult: dragging one
unplaced session onto a specific day/period/room ("place"), dragging an
already-placed session back out ("unplace"), or dragging a placed session
to a different day/period/room ("move"). This is what powers the web
app's drag-and-drop editor, so a user can hand-resolve the handful of
sessions the solver couldn't fit -- or fine-tune a placement -- without
re-running the whole solve.

Every one of these re-checks the same hard constraints the solver itself
enforces (room/teacher/section-or-group clashes, the lab-room requirement
for practicals, day-boundary/lunch-break validity, the daily load cap,
max-consecutive-periods, and the same-offering same-day repeat rule) so a
manual edit can never produce a timetable the solver would have rejected.
"""

from __future__ import annotations

from dataclasses import replace

from .config import SchedulerConfig
from .model import PlacedSession, ScheduleResult
from .sessions import Session


class PlacementError(ValueError):
    """A manual placement/move would violate a hard constraint, or refers
    to a session that isn't where the caller thinks it is."""


def _periods_for(start_period: int, duration: int) -> list[int]:
    return list(range(start_period, start_period + duration))


def _overlaps(a: list[int], b: list[int]) -> bool:
    return not set(a).isdisjoint(b)


def _sections_clash(a_section: str, a_base: str, a_group, b_section: str, b_base: str, b_group) -> bool:
    """Mirrors the solver's NoOverlap grouping (see model.py): the same
    exact section always clashes with itself; a shared (group=None)
    session clashes with every sub-group of its base_section; two
    DIFFERENT sub-groups of the same base_section do NOT clash against
    each other -- they're different students in different rooms."""
    if a_section == b_section:
        return True
    if a_base != b_base:
        return False
    return a_group is None or b_group is None


def _same_offering_kind_sessions(result: ScheduleResult, session: Session) -> list[Session]:
    all_sessions = [p.session for p in result.placed] + list(result.unplaced)
    return [s for s in all_sessions
            if s.offering_index == session.offering_index and s.kind == session.kind]


def validate_placement(
    result: ScheduleResult,
    config: SchedulerConfig,
    session: Session,
    day: int,
    start_period: int,
    room: str,
) -> list[int]:
    """Raises PlacementError if the requested slot would break a hard
    constraint; otherwise returns the list of periods it will occupy.

    `session` is excluded from the clash checks against `result.placed`
    (by id), so this works unchanged for both a brand-new placement and
    moving a session that's already sitting somewhere in `result.placed`.
    """
    days = config.days
    ppd = config.periods_per_day

    if not (0 <= day < len(days)):
        raise PlacementError(f"Day index {day} is out of range (0-{len(days) - 1}).")
    if start_period < 1 or start_period + session.duration - 1 > ppd:
        raise PlacementError(
            f"{session.kind} needs {session.duration} consecutive period(s) starting at "
            f"period {start_period}, but the day only has {ppd} periods."
        )
    periods = _periods_for(start_period, session.duration)
    if session.duration > 1 and any(p <= config.day_break_after_period < periods[-1] for p in periods[:-1]):
        raise PlacementError("That block would straddle the lunch break.")

    if room not in config.rooms:
        raise PlacementError(f"'{room}' is not a configured room.")
    lab_rooms = set(config.lab_rooms) or set(config.rooms)
    if session.kind == "Practical" and room not in lab_rooms:
        raise PlacementError(f"{session.course_code} is a practical and needs a lab room; '{room}' isn't one.")

    others = [p for p in result.placed if p.session.id != session.id]

    for p in others:
        if p.day != day or not _overlaps(p.periods, periods):
            continue
        if p.room == room:
            raise PlacementError(f"Room {room} is already in use by {p.session.course_code} at that time.")
        if p.session.teacher == session.teacher:
            raise PlacementError(f"{session.teacher} is already teaching {p.session.course_code} at that time.")
        if _sections_clash(session.section, session.base_section, session.group,
                            p.session.section, p.session.base_section, p.session.group):
            raise PlacementError(f"{session.section} already has {p.session.course_code} at that time.")

    # Same course-offering's sessions of this kind can't over-repeat on one
    # day -- mirrors the solver's AddAllDifferent-per-offering-per-kind
    # rule (or its ceil(count/num_days) cap when there are more sessions
    # of a kind than there are working days).
    num_days = len(days)
    total_of_kind = len(_same_offering_kind_sessions(result, session))
    cap_per_day = 1 if total_of_kind <= num_days else -(-total_of_kind // num_days)
    same_offering_today = sum(
        1 for p in others
        if p.day == day and p.session.offering_index == session.offering_index and p.session.kind == session.kind
    )
    if same_offering_today + 1 > cap_per_day:
        raise PlacementError(
            f"{session.course_code} can only have {cap_per_day} {session.kind.lower()} "
            f"session(s) on {days[day]}; it already has {same_offering_today} there."
        )

    # Daily load cap for this teacher's register category.
    cap = config.daily_cap_for(session.nature)
    same_teacher_today = [p for p in others if p.day == day and p.session.teacher == session.teacher]
    total_today = sum(p.session.duration for p in same_teacher_today) + session.duration
    if total_today > cap:
        raise PlacementError(
            f"{session.teacher} would have {total_today} periods on {days[day]}, over the "
            f"{cap}-period daily cap for {session.nature or 'this'} faculty."
        )

    # Max-consecutive-periods for this teacher on this day.
    occupied = set()
    for p in same_teacher_today:
        occupied.update(p.periods)
    occupied.update(periods)
    win_len = config.max_consecutive_periods + 1
    for start in range(1, ppd - win_len + 2):
        window = list(range(start, start + win_len))
        if any(config.day_break_after_period == w for w in window[:-1]):
            continue  # a run that crosses the lunch break doesn't count as unbroken
        if all(w in occupied for w in window):
            raise PlacementError(
                f"{session.teacher} would have {win_len} periods back-to-back on {days[day]}, "
                f"over the {config.max_consecutive_periods}-period limit."
            )

    return periods


def place_session(
    result: ScheduleResult, config: SchedulerConfig,
    session_id: int, day: int, start_period: int, room: str,
) -> ScheduleResult:
    """Moves one currently-unplaced session onto (day, start_period, room).
    Returns a NEW ScheduleResult -- does not mutate the one passed in."""
    session = next((s for s in result.unplaced if s.id == session_id), None)
    if session is None:
        raise PlacementError(f"Session {session_id} is not in the unplaced list (already placed, or unknown id).")
    periods = validate_placement(result, config, session, day, start_period, room)
    new_ps = PlacedSession(session=session, day=day, start_period=start_period, periods=periods, room=room)
    return replace(
        result,
        placed=result.placed + [new_ps],
        unplaced=[s for s in result.unplaced if s.id != session_id],
    )


def unplace_session(result: ScheduleResult, session_id: int) -> ScheduleResult:
    """Moves one currently-placed session back to unplaced (undo)."""
    match = next((p for p in result.placed if p.session.id == session_id), None)
    if match is None:
        raise PlacementError(f"Session {session_id} is not currently placed.")
    return replace(
        result,
        placed=[p for p in result.placed if p.session.id != session_id],
        unplaced=list(result.unplaced) + [match.session],
    )


def move_session(
    result: ScheduleResult, config: SchedulerConfig,
    session_id: int, day: int, start_period: int, room: str,
) -> ScheduleResult:
    """Moves an already-placed session to a different day/period/room."""
    match = next((p for p in result.placed if p.session.id == session_id), None)
    if match is None:
        raise PlacementError(f"Session {session_id} is not currently placed.")
    periods = validate_placement(result, config, match.session, day, start_period, room)
    new_ps = PlacedSession(session=match.session, day=day, start_period=start_period, periods=periods, room=room)
    return replace(result, placed=[new_ps if p.session.id == session_id else p for p in result.placed])
