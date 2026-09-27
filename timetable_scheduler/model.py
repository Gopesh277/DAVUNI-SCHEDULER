"""
Builds and solves the timetable as a Constraint Programming problem
with Google OR-Tools CP-SAT.

Timeline encoding
------------------
Each (day, period) pair is flattened into a single integer
`t = day_index * periods_per_day + (period_index - 1)`, `t` in
`[0, num_days * periods_per_day)`. A session of duration `L` occupies
`[start, start+L)` on this timeline. Valid start values are
precomputed per duration so that a block never crosses midnight
(day boundary) or the lunch break.

Hard constraints
-----------------
* A teacher cannot teach two sessions at once (NoOverlap on intervals).
* A section (class) cannot attend two sessions at once (NoOverlap).
  Whole-section sessions and every group of that section share one
  NoOverlap group per group (so G1 conflicts with the whole-section
  session and G2 conflicts with the whole-section session, but G1 and
  G2 do NOT conflict with each other -- they're different students).
* A room cannot host two sessions at once (NoOverlap via optional
  intervals, one boolean per session-room pair).
* Practical sessions may only use lab-capable rooms.
* A teacher never has more than `config.max_consecutive_periods`
  periods back-to-back on the same day (a run that crosses the lunch
  break doesn't count as unbroken).
* A teacher never has more periods on a single day than their register
  category's daily cap (`config.position_daily_caps`).

Two-stage strategy
-------------------
`build_and_solve` never simply reports "no timetable" when a complete
schedule can't be found:

  Stage 1 -- COMPLETE timetable. Every session is mandatory. Runs a
  fast hard-constraints-only pass, then (if requested and time
  remains) a load-balancing pass warm-started from it. If this
  succeeds, every required session is scheduled and the result status
  is OPTIMAL/FEASIBLE.

  Stage 2 -- BEST-POSSIBLE timetable. Only entered if Stage 1 could
  not schedule everything within its time budget. Rebuilds the model
  with a Boolean `active[i]` variable per session (so a session may be
  left unscheduled instead of making the whole model infeasible),
  maximises the total number of scheduled teaching periods first, and
  minimises a load-balance term second. The result status is PARTIAL
  when some sessions remain unplaced, so the caller can still report
  exactly which teaching hours are missing and why (see
  `ScheduleResult.unplaced`) instead of an all-or-nothing failure.

Status is only reported as INFEASIBLE when literally nothing can be
scheduled (e.g. no room/period combination exists at all for some
session duration), and UNKNOWN only if even the relaxed Stage-2 model
can't produce a solution within the time budget.
"""

from __future__ import annotations

from dataclasses import dataclass

from ortools.sat.python import cp_model

from .config import SchedulerConfig, DEFAULT_TIME_LIMIT_SECONDS, DEFAULT_NUM_WORKERS
from .sessions import Session

# Share of the total time budget given to the fast feasibility-only pass.
# The fatigue rules (no-3-consecutive, position daily caps) make even
# finding *a* valid schedule considerably harder than plain clash-
# avoidance, so this pass gets the bulk of the budget by default.
PHASE1_TIME_FRACTION = 0.65
PHASE1_MIN_SECONDS = 20
PHASE1_MAX_SECONDS = 100

# Stage 2 (relaxed) objective weight: maximising scheduled periods must
# always dominate the load-balance term, whose range is only
# [0, periods_per_day]. Any weight comfortably larger than that range
# enforces the lexicographic priority "schedule first, balance second".
RELAXED_SCHEDULE_WEIGHT = 1000


@dataclass
class PlacedSession:
    session: Session
    day: int          # 0-based index into config.days
    start_period: int  # 1-based
    periods: list[int]  # 1-based period indices occupied
    room: str


@dataclass
class ScheduleResult:
    status: str                 # "OPTIMAL" | "FEASIBLE" | "PARTIAL" | "INFEASIBLE" | "UNKNOWN"
    placed: list[PlacedSession]
    unplaced: list[Session]     # non-empty whenever the load couldn't fully fit
    wall_time_seconds: float
    config: SchedulerConfig      # config actually used to produce this result

    @property
    def is_complete(self) -> bool:
        return not self.unplaced and self.status in ("OPTIMAL", "FEASIBLE")


def _valid_starts_for_duration(duration: int, config: SchedulerConfig) -> list[int]:
    """Global timeline start positions where a `duration`-period block
    fits inside one day without crossing the lunch break."""
    ppd = config.periods_per_day
    num_days = len(config.days)
    starts = []
    for d in range(num_days):
        for p in range(1, ppd - duration + 2):
            span = list(range(p, p + duration))
            if span[-1] > ppd:
                continue
            if duration > 1 and any(s <= config.day_break_after_period < span[-1] for s in span[:-1]):
                continue  # would straddle the lunch break
            starts.append(d * ppd + (p - 1))
    return starts


def _consecutive_windows(config: SchedulerConfig) -> list[list[int]]:
    """All windows of (max_consecutive_periods + 1) back-to-back period
    indices, on a single day, that do NOT already contain the lunch
    break."""
    ppd = config.periods_per_day
    win_len = config.max_consecutive_periods + 1
    windows = []
    for p in range(1, ppd - win_len + 2):
        span = list(range(p, p + win_len))
        if any(config.day_break_after_period == s for s in span[:-1]):
            continue
        windows.append(span)
    return windows


class _ModelContext:
    """Everything needed to solve or re-solve one instance of the model.

    When `relaxed` is False every schedulable session is mandatory (a
    solution places all of them). When `relaxed` is True each session
    gets a Boolean `active[i]` variable and may be left unscheduled --
    this is Stage 2's "best possible timetable" model.
    """
    def __init__(self, sessions, config: SchedulerConfig, relaxed: bool = False):
        self.config = config
        self.sessions = sessions
        self.relaxed = relaxed
        ppd = config.periods_per_day
        num_days = len(config.days)
        self.ppd, self.num_days = ppd, num_days
        timeline_len = num_days * ppd

        self.model = model = cp_model.CpModel()
        room_count = len(config.rooms)
        lab_room_idx = [i for i, r in enumerate(config.rooms) if r in config.lab_rooms]
        if not lab_room_idx:
            lab_room_idx = list(range(room_count))

        valid_starts_cache: dict[int, list[int]] = {}
        for s in sessions:
            if s.duration not in valid_starts_cache:
                valid_starts_cache[s.duration] = _valid_starts_for_duration(s.duration, config)

        self.schedulable = [s for s in sessions if valid_starts_cache.get(s.duration)]
        self.pre_unplaced = [s for s in sessions if not valid_starts_cache.get(s.duration)]

        self.starts, self.ends, self.intervals = [], [], []
        self.active: list = []  # BoolVar per schedulable session if relaxed, else [] (unused)
        self._onday_active_cache: dict[tuple[int, int], cp_model.IntVar] = {}
        self.room_presence: dict[tuple[int, int], cp_model.IntVar] = {}
        room_intervals: dict[int, list] = {r: [] for r in range(room_count)}

        for s in self.schedulable:
            domain = cp_model.Domain.FromValues(valid_starts_cache[s.duration])
            dur = s.duration
            start = model.NewIntVarFromDomain(domain, f"start_{s.id}")
            end = model.NewIntVar(0, timeline_len, f"end_{s.id}")
            model.Add(end == start + dur)

            if relaxed:
                active = model.NewBoolVar(f"active_{s.id}")
                self.active.append(active)
                interval = model.NewOptionalIntervalVar(start, dur, end, active, f"ivl_{s.id}")
            else:
                interval = model.NewIntervalVar(start, dur, end, f"ivl_{s.id}")

            self.starts.append(start)
            self.ends.append(end)
            self.intervals.append(interval)

            allowed_rooms = lab_room_idx if s.kind == "Practical" else list(range(room_count))
            presence_vars = []
            for r in range(room_count):
                if r in allowed_rooms:
                    b = model.NewBoolVar(f"room_{s.id}_{r}")
                    opt_ivl = model.NewOptionalIntervalVar(start, dur, end, b, f"opt_ivl_{s.id}_{r}")
                    room_intervals[r].append(opt_ivl)
                    self.room_presence[(s.id, r)] = b
                    presence_vars.append(b)
            if relaxed:
                # Present in exactly one room IF scheduled, in none if not.
                model.Add(sum(presence_vars) == self.active[-1])
            else:
                model.AddExactlyOne(presence_vars)

        self.by_teacher: dict[str, list[int]] = {}
        self.by_section: dict[str, list[int]] = {}
        for i, s in enumerate(self.schedulable):
            self.by_teacher.setdefault(s.teacher, []).append(i)
            self.by_section.setdefault(s.section, []).append(i)

        for idxs in self.by_teacher.values():
            model.AddNoOverlap([self.intervals[i] for i in idxs])
        for idxs in self.by_section.values():
            model.AddNoOverlap([self.intervals[i] for i in idxs])
        for r in range(room_count):
            if room_intervals[r]:
                model.AddNoOverlap(room_intervals[r])

        # A sub-group's sessions must never clash with its parent class's
        # shared sessions -- same physical students. Sibling groups (G1 vs
        # G2) CAN run at the same time, since they're different students in
        # different rooms, so they get separate NoOverlap calls rather than
        # being merged into one.
        by_base: dict[str, dict] = {}
        for i, s in enumerate(self.schedulable):
            entry = by_base.setdefault(s.base_section, {"shared": [], "groups": {}})
            if s.group:
                entry["groups"].setdefault(s.group, []).append(i)
            else:
                entry["shared"].append(i)
        for entry in by_base.values():
            shared = entry["shared"]
            for group_idxs in entry["groups"].values():
                combined = shared + group_idxs
                if len(combined) > 1:
                    model.AddNoOverlap([self.intervals[i] for i in combined])

        # day / period channeling
        self.day_vars, self.period_vars = [], []
        self.is_on_day: dict[tuple[int, int], cp_model.IntVar] = {}
        self.is_at_period: dict[tuple[int, int], cp_model.IntVar] = {}
        for i, s in enumerate(self.schedulable):
            dv = model.NewIntVar(0, num_days - 1, f"day_{s.id}")
            model.AddDivisionEquality(dv, self.starts[i], ppd)
            self.day_vars.append(dv)

            pv = model.NewIntVar(1, ppd, f"period_{s.id}")
            mod_v = model.NewIntVar(0, ppd - 1, f"mod_{s.id}")
            model.AddModuloEquality(mod_v, self.starts[i], ppd)
            model.Add(pv == mod_v + 1)
            self.period_vars.append(pv)

            for d in range(num_days):
                b = model.NewBoolVar(f"onday_{s.id}_{d}")
                model.Add(dv == d).OnlyEnforceIf(b)
                model.Add(dv != d).OnlyEnforceIf(b.Not())
                self.is_on_day[(i, d)] = b
            for k in range(1, ppd + 1):
                b = model.NewBoolVar(f"atperiod_{s.id}_{k}")
                model.Add(pv == k).OnlyEnforceIf(b)
                model.Add(pv != k).OnlyEnforceIf(b.Not())
                self.is_at_period[(i, k)] = b

        # hard rule: the same lecture never repeats on the same day -- all
        # sessions of one course offering's Theory hours land on distinct
        # days, all its Tutorial hours on distinct days, all its Practical
        # blocks on distinct days (a course can still have e.g. a Theory
        # and a Practical on the same day -- those are different lectures).
        # In the relaxed model this only applies between two sessions that
        # are BOTH actually scheduled -- an unscheduled session shouldn't
        # constrain the days of the ones that did make it in.
        by_offering_kind: dict[tuple[int, str], list[int]] = {}
        for i, s in enumerate(self.schedulable):
            by_offering_kind.setdefault((s.offering_index, s.kind), []).append(i)
        for idxs in by_offering_kind.values():
            count = len(idxs)
            if count <= 1:
                continue
            if not relaxed and count <= num_days:
                model.AddAllDifferent([self.day_vars[i] for i in idxs])
            elif not relaxed:
                # More sessions of this kind than there are working days --
                # can't avoid repeats entirely, so spread them as evenly as
                # possible instead of leaving the rule unenforced.
                cap_per_day = -(-count // num_days)  # ceil division
                for d in range(num_days):
                    model.Add(sum(self.is_on_day[(i, d)] for i in idxs) <= cap_per_day)
            else:
                # relaxed: pairwise, only enforced when both sessions in
                # the pair are actually scheduled.
                for a in range(len(idxs)):
                    for b in range(a + 1, len(idxs)):
                        i, j = idxs[a], idxs[b]
                        model.Add(self.day_vars[i] != self.day_vars[j]).OnlyEnforceIf(
                            [self.active[i], self.active[j]]
                        )

        # hard rule: daily load cap by register/position category -- this
        # only needs total periods-taught per day, so it's expressed
        # directly over session durations (cheap) rather than the
        # per-period occupancy grid used by the consecutive-run rule below.
        # In the relaxed model an unscheduled session contributes nothing.
        teacher_nature: dict[str, str] = {}
        for s in self.schedulable:
            teacher_nature.setdefault(s.teacher, s.nature)
        for teacher, idxs in self.by_teacher.items():
            cap = config.daily_cap_for(teacher_nature.get(teacher, ""))
            for d in range(num_days):
                if not relaxed:
                    model.Add(
                        sum(self.schedulable[i].duration * self.is_on_day[(i, d)] for i in idxs) <= cap
                    )
                else:
                    terms = [
                        self.schedulable[i].duration * self._onday_active(i, d)
                        for i in idxs
                    ]
                    model.Add(sum(terms) <= cap)

        # occupancy grid, forced to 1 whenever a session covers (teacher, d, p)
        # -- only needed for the consecutive-run rule below, so it's built
        # after (and independent of) the daily cap rule above. In the
        # relaxed model, only actually-scheduled sessions force occupancy.
        self.occ: dict[tuple[str, int, int], cp_model.IntVar] = {}
        for teacher, idxs in self.by_teacher.items():
            for i in idxs:
                s = self.schedulable[i]
                for d in range(num_days):
                    for k in range(1, ppd + 1):
                        covered = [k] if s.duration == 1 else [k, k + 1]
                        covered = [p for p in covered if 1 <= p <= ppd]
                        for p in covered:
                            enforce_lits = [self.is_on_day[(i, d)], self.is_at_period[(i, k)]]
                            if relaxed:
                                enforce_lits.append(self.active[i])
                            model.Add(self._occ(teacher, d, p) == 1).OnlyEnforceIf(enforce_lits)

        # hard rule: no more than max_consecutive_periods back-to-back
        windows = _consecutive_windows(config)
        for teacher in self.by_teacher:
            for d in range(num_days):
                for window in windows:
                    model.Add(sum(self._occ(teacher, d, p) for p in window) <= config.max_consecutive_periods)

    def _occ(self, teacher: str, d: int, p: int) -> cp_model.IntVar:
        key = (teacher, d, p)
        if key not in self.occ:
            self.occ[key] = self.model.NewBoolVar(f"occ_{teacher}_{d}_{p}")
        return self.occ[key]

    def _onday_active(self, i: int, d: int) -> cp_model.IntVar:
        """Boolean AND of (session i is on day d) and (session i is
        scheduled at all). Only meaningful/used in the relaxed model."""
        key = (i, d)
        cache = self._onday_active_cache
        if key not in cache:
            v = self.model.NewBoolVar(f"odact_{i}_{d}")
            a, b = self.is_on_day[(i, d)], self.active[i]
            self.model.AddBoolAnd([a, b]).OnlyEnforceIf(v)
            self.model.AddBoolOr([a.Not(), b.Not()]).OnlyEnforceIf(v.Not())
            cache[key] = v
        return cache[key]

    def add_balance_objective(self):
        max_daily_load = self.model.NewIntVar(0, self.ppd, "max_daily_load")
        for teacher, idxs in self.by_teacher.items():
            for d in range(self.num_days):
                self.model.Add(
                    sum(self.schedulable[i].duration * self.is_on_day[(i, d)] for i in idxs) <= max_daily_load
                )
        self.model.Minimize(max_daily_load)

    def add_relaxed_objective(self):
        """Stage 2 objective: maximise total scheduled teaching periods
        first, then minimise the busiest single day for any one faculty
        member (same load-balance idea as `add_balance_objective`, just
        gated by `active` and folded into one weighted objective so a
        single solve does both in priority order)."""
        total_scheduled = sum(
            self.schedulable[i].duration * self.active[i] for i in range(len(self.schedulable))
        )
        max_daily_load = self.model.NewIntVar(0, self.ppd, "max_daily_load_relaxed")
        for teacher, idxs in self.by_teacher.items():
            for d in range(self.num_days):
                terms = [self.schedulable[i].duration * self._onday_active(i, d) for i in idxs]
                self.model.Add(sum(terms) <= max_daily_load)
        self.model.Maximize(RELAXED_SCHEDULE_WEIGHT * total_scheduled - max_daily_load)

    def extract(self, solver: cp_model.CpSolver) -> tuple[list[PlacedSession], dict[int, int]]:
        """Returns (placed sessions, {session_index: start_value}) for
        hinting. In the relaxed model, sessions solved inactive (active=0)
        are skipped -- they belong in `unplaced`, not `placed`."""
        placed = []
        start_values = {}
        for i, s in enumerate(self.schedulable):
            if self.relaxed and solver.Value(self.active[i]) == 0:
                continue
            t = solver.Value(self.starts[i])
            start_values[i] = t
            day = t // self.ppd
            start_period = (t % self.ppd) + 1
            periods = list(range(start_period, start_period + s.duration))
            room = None
            for r in range(len(self.config.rooms)):
                key = (s.id, r)
                if key in self.room_presence and solver.Value(self.room_presence[key]):
                    room = self.config.rooms[r]
                    break
            placed.append(PlacedSession(session=s, day=day, start_period=start_period,
                                         periods=periods, room=room))
        return placed, start_values

    def inactive_sessions(self, solver: cp_model.CpSolver) -> list[Session]:
        """Only meaningful for a relaxed model: the schedulable sessions
        that the solver chose to leave unscheduled."""
        if not self.relaxed:
            return []
        return [self.schedulable[i] for i in range(len(self.schedulable)) if solver.Value(self.active[i]) == 0]


def _solve(ctx: _ModelContext, time_limit: float, num_workers: int, log_progress: bool):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(1.0, time_limit)
    solver.parameters.num_search_workers = num_workers
    solver.parameters.log_search_progress = log_progress
    status = solver.Solve(ctx.model)
    return solver, status


def _solve_relaxed(
    sessions: list[Session],
    config: SchedulerConfig,
    time_limit_seconds: float,
    num_workers: int,
    log_progress: bool,
) -> ScheduleResult:
    """Stage 2: best-possible (partial) timetable. Sessions are optional;
    the solver maximises how many get scheduled, then balances load."""
    ctx2 = _ModelContext(sessions, config, relaxed=True)

    if not ctx2.schedulable:
        # Nothing at all can be scheduled -- e.g. no room/period combo
        # exists for any session's duration. Genuinely nothing to place.
        return ScheduleResult(status="INFEASIBLE", placed=[], unplaced=list(sessions),
                               wall_time_seconds=0.0, config=config)

    ctx2.add_relaxed_objective()
    solver2, status2 = _solve(ctx2, time_limit_seconds, num_workers, log_progress)

    if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        placed2, _ = ctx2.extract(solver2)
        unplaced2 = list(ctx2.pre_unplaced) + ctx2.inactive_sessions(solver2)
        status_name = "PARTIAL" if unplaced2 else ("OPTIMAL" if status2 == cp_model.OPTIMAL else "FEASIBLE")
        return ScheduleResult(status=status_name, placed=placed2, unplaced=unplaced2,
                               wall_time_seconds=solver2.WallTime(), config=config)

    # Even the relaxed model couldn't produce anything within the budget.
    return ScheduleResult(status="UNKNOWN", placed=[], unplaced=list(sessions),
                           wall_time_seconds=solver2.WallTime(), config=config)


def build_and_solve(
    sessions: list[Session],
    config: SchedulerConfig | None = None,
    time_limit_seconds: int = DEFAULT_TIME_LIMIT_SECONDS,
    num_workers: int = DEFAULT_NUM_WORKERS,
    balance_load: bool = True,
    log_progress: bool = False,
) -> ScheduleResult:
    config = config or SchedulerConfig.default()

    if not sessions:
        return ScheduleResult(status="OPTIMAL", placed=[], unplaced=[], wall_time_seconds=0.0, config=config)

    # ---- Stage 1: hard constraints only, no objective -- fast pass to
    #      get *a* complete valid timetable before spending time
    #      optimizing it. Every schedulable session is mandatory here, so
    #      success means the FULL load fits.
    phase1_budget = min(PHASE1_MAX_SECONDS, max(PHASE1_MIN_SECONDS, time_limit_seconds * PHASE1_TIME_FRACTION))
    if not balance_load:
        phase1_budget = time_limit_seconds  # no phase 2 planned -- use the full budget now

    ctx1 = _ModelContext(sessions, config, relaxed=False)
    total_wall_time = 0.0
    baseline_placed, baseline_starts, baseline_status = None, None, None

    if ctx1.schedulable:
        solver1, status1 = _solve(ctx1, phase1_budget, num_workers, log_progress)
        total_wall_time += solver1.WallTime()
        if status1 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            baseline_placed, baseline_starts = ctx1.extract(solver1)
            baseline_status = "OPTIMAL" if status1 == cp_model.OPTIMAL else "FEASIBLE"
        elif balance_load:
            # The quick feasibility pass came up empty -- rather than give
            # up, spend the rest of the budget on feasibility alone (still
            # a much easier search than with the objective attached) before
            # falling through to Stage 2.
            remaining_for_retry = max(3.0, time_limit_seconds - total_wall_time)
            if remaining_for_retry > 2.0:
                solver1b, status1b = _solve(ctx1, remaining_for_retry, num_workers, log_progress)
                total_wall_time += solver1b.WallTime()
                if status1b in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    baseline_placed, baseline_starts = ctx1.extract(solver1b)
                    baseline_status = "OPTIMAL" if status1b == cp_model.OPTIMAL else "FEASIBLE"

    if baseline_status is not None:
        # A COMPLETE timetable exists (every schedulable session placed).
        if not balance_load:
            return ScheduleResult(status=baseline_status, placed=baseline_placed,
                                   unplaced=list(ctx1.pre_unplaced), wall_time_seconds=total_wall_time,
                                   config=config)

        # ---- Phase 2 (still Stage 1): full model with the load-balancing
        #      objective, warm started from the baseline, for the
        #      remaining time.
        remaining = max(3.0, time_limit_seconds - total_wall_time)
        ctx1b = _ModelContext(sessions, config, relaxed=False)
        ctx1b.add_balance_objective()
        for i, t in baseline_starts.items():
            ctx1b.model.AddHint(ctx1b.starts[i], t)
        solver1c, status1c = _solve(ctx1b, remaining, num_workers, log_progress)
        total_wall_time += solver1c.WallTime()

        if status1c in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            placed1c, _ = ctx1b.extract(solver1c)
            status_name = "OPTIMAL" if status1c == cp_model.OPTIMAL else "FEASIBLE"
            return ScheduleResult(status=status_name, placed=placed1c, unplaced=list(ctx1b.pre_unplaced),
                                   wall_time_seconds=total_wall_time, config=config)

        # Balancing pass didn't finish in time (or somehow failed) -- fall
        # back to the already-valid, already-complete baseline.
        return ScheduleResult(status=baseline_status, placed=baseline_placed,
                               unplaced=list(ctx1.pre_unplaced), wall_time_seconds=total_wall_time,
                               config=config)

    # ---- Stage 2: a COMPLETE timetable could not be found within the
    #      time budget (either it's truly impossible under the hard
    #      constraints, or it just didn't finish in time). Rather than
    #      report failure, build the relaxed model and return the best
    #      PARTIAL timetable possible.
    remaining = max(5.0, time_limit_seconds - total_wall_time)
    relaxed_result = _solve_relaxed(sessions, config, remaining, num_workers, log_progress)
    relaxed_result.wall_time_seconds += total_wall_time
    return relaxed_result
