"""
Static department configuration.

Edit this file (not the scheduler code) when the department's rooms,
working days, or period timings change -- everything else in the
package reads from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Period:
    index: int      # 1-based period number within a day
    label: str      # human readable time range


# ---- Rooms -----------------------------------------------------------
# 12 CSE department rooms, from the department's schedule notes: 9
# general lecture/tutorial rooms plus 3 dedicated lab rooms.
ROOMS = ["216", "312", "315", "316", "317", "422", "421", "401", "301",
         "102", "302", "402"]

# Rooms that are lab/practical-capable. Must be a subset of ROOMS -- a
# lab room that isn't also in ROOMS is not a schedulable resource at
# all, and every consumer of this list (SchedulerConfig.from_dict, the
# CP-SAT model's `lab_room_idx` lookup) silently falls back to "every
# room is a lab" the moment this set doesn't intersect ROOMS, which
# defeats the whole point of separating lecture-only rooms from labs.
LAB_ROOMS = ["102", "302", "402"]

# ---- Days --------------------------------------------------------------
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

# ---- Periods -------------------------------------------------------------
# 6 teaching periods/day (9:30-4:30) with a lunch break between period 4
# and period 5. [Previously this comment said "7 teaching periods/day",
# which did not match the PERIODS list, DAY_BREAK_AFTER_PERIOD, or the
# explicit time labels below (all of which only ever defined 6 periods)
# -- the "7" was a stale comment, not a missing period, so it has been
# corrected here rather than inventing an unlabelled 7th slot.]
# A practical/lab block (2 continuous periods) is never allowed to
# start at period 4, since period 5 sits on the far side of lunch.
PERIODS = [
    Period(1, "9:30-10:30"),
    Period(2, "10:30-11:30"),
    Period(3, "11:30-12:30"),
    Period(4, "12:30-1:30"),
    Period(5, "2:30-3:30"),
    Period(6, "3:30-4:30"),
]
PERIODS_PER_DAY = len(PERIODS)

# Period index (1-based) after which a block may NOT continue into the
# next period (i.e. the period right before a break). Practicals needing
# `duration` periods cannot start at one of these indices if it would
# make them span the break.
DAY_BREAK_AFTER_PERIOD = 4

# ---- Scheduling rules ----------------------------------------------------
# Practical/lab hours are grouped into continuous blocks of this size
# (department register note: "2 lec continuous"). The final block for an
# odd remainder falls back to 1 period.
PRACTICAL_BLOCK_SIZE = 2

# Faculty "register" categories used for load-balancing priority. Anything
# not matching a contractual-style keyword is treated as a senior/regular
# faculty member for priority purposes.
CONTRACTUAL_KEYWORDS = ("contract",)

# ---- Fatigue / position load rules ---------------------------------------
# A teacher may never have more than this many periods in an unbroken run
# on the same day (e.g. 2 means periods 1-2 back-to-back is fine, but
# 1-2-3 in a row is not -- a run that crosses the lunch break doesn't
# count as "unbroken").
DEFAULT_MAX_CONSECUTIVE_PERIODS = 2

# Maximum periods/day allowed per faculty register category -- reflects
# that senior faculty typically carry a lighter daily teaching load than
# contractual/junior faculty. Matched case-insensitively against each
# course row's "nature of appointment" value; anything not listed falls
# back to DEFAULT_POSITION_DAILY_CAP.
DEFAULT_POSITION_DAILY_CAPS = {
    "Regular": 6,
    "Senior": 5,
    "Super Senior": 4,
    "Contractual": 7,
    "New Faculty": 7,
}
DEFAULT_POSITION_DAILY_CAP = 6

# Solver tuning defaults (overridable via CLI flags / the API). The
# consecutive-period and position-cap rules make the search noticeably
# harder than plain clash-avoidance, so the default budget is generous.
DEFAULT_TIME_LIMIT_SECONDS = 150
DEFAULT_NUM_WORKERS = 8


@dataclass
class SchedulerConfig:
    """A bundle of department settings passed explicitly into the model
    and exporter, instead of reading the module-level constants above
    directly. This is what lets the web API change rooms/days/periods
    per request without touching this file. CLI usage that never passes
    a config keeps using the module-level defaults via `.default()`."""

    rooms: list[str]
    lab_rooms: list[str]
    days: list[str]
    periods: list[Period]
    day_break_after_period: int = DAY_BREAK_AFTER_PERIOD
    practical_block_size: int = PRACTICAL_BLOCK_SIZE
    max_consecutive_periods: int = DEFAULT_MAX_CONSECUTIVE_PERIODS
    position_daily_caps: dict = field(default_factory=lambda: dict(DEFAULT_POSITION_DAILY_CAPS))
    default_position_daily_cap: int = DEFAULT_POSITION_DAILY_CAP

    @property
    def periods_per_day(self) -> int:
        return len(self.periods)

    def daily_cap_for(self, nature: str) -> int:
        n = (nature or "").strip().lower()
        for k, v in self.position_daily_caps.items():
            if k.strip().lower() == n:
                return v
        return self.default_position_daily_cap

    @classmethod
    def default(cls) -> "SchedulerConfig":
        return cls(
            rooms=list(ROOMS),
            lab_rooms=list(LAB_ROOMS),
            days=list(DAYS),
            periods=list(PERIODS),
            day_break_after_period=DAY_BREAK_AFTER_PERIOD,
            practical_block_size=PRACTICAL_BLOCK_SIZE,
            max_consecutive_periods=DEFAULT_MAX_CONSECUTIVE_PERIODS,
            position_daily_caps=dict(DEFAULT_POSITION_DAILY_CAPS),
            default_position_daily_cap=DEFAULT_POSITION_DAILY_CAP,
        )

    def to_dict(self) -> dict:
        return {
            "rooms": self.rooms,
            "lab_rooms": self.lab_rooms,
            "days": self.days,
            "periods": [{"index": p.index, "label": p.label} for p in self.periods],
            "day_break_after_period": self.day_break_after_period,
            "practical_block_size": self.practical_block_size,
            "max_consecutive_periods": self.max_consecutive_periods,
            "position_daily_caps": self.position_daily_caps,
            "default_position_daily_cap": self.default_position_daily_cap,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SchedulerConfig":
        periods = [Period(index=p["index"], label=p["label"]) for p in d["periods"]]
        rooms = list(d["rooms"])
        lab_rooms = [r for r in d.get("lab_rooms", rooms) if r in rooms]
        return cls(
            rooms=rooms,
            lab_rooms=lab_rooms or rooms,
            days=list(d["days"]),
            periods=periods,
            day_break_after_period=int(d.get("day_break_after_period", DAY_BREAK_AFTER_PERIOD)),
            practical_block_size=int(d.get("practical_block_size", PRACTICAL_BLOCK_SIZE)),
            max_consecutive_periods=int(d.get("max_consecutive_periods", DEFAULT_MAX_CONSECUTIVE_PERIODS)),
            position_daily_caps=dict(d.get("position_daily_caps", DEFAULT_POSITION_DAILY_CAPS)),
            default_position_daily_cap=int(d.get("default_position_daily_cap", DEFAULT_POSITION_DAILY_CAP)),
        )
