"""
Expands each course-offering row into the atomic teaching sessions that
actually need a day/period/room: one session per theory hour, one per
tutorial hour, and practical hours grouped into continuous blocks.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import PRACTICAL_BLOCK_SIZE, CONTRACTUAL_KEYWORDS, SchedulerConfig
from .parser import CourseOffering


@dataclass
class Session:
    id: int
    offering_index: int
    course_code: str
    course_name: str
    teacher: str
    section: str
    base_section: str
    group: str | None
    nature: str
    kind: str        # "Theory" | "Tutorial" | "Practical"
    duration: int     # in periods

    def is_contractual(self) -> bool:
        n = (self.nature or "").lower()
        return any(k in n for k in CONTRACTUAL_KEYWORDS)


def build_sessions(offerings: list[CourseOffering], config: SchedulerConfig | None = None) -> list[Session]:
    """Expand course-offering rows into atomic sessions.

    `config` supplies the practical block size (`config.practical_block_size`).
    It defaults to `SchedulerConfig.default()` -- which reads the same
    module-level `PRACTICAL_BLOCK_SIZE` constant -- so existing callers
    that don't pass a config (e.g. main.py) keep their current behaviour,
    while callers that build a per-request config (e.g. the API) get
    sessions that actually reflect it.
    """
    block_size = (config or SchedulerConfig.default()).practical_block_size
    if block_size < 1:
        block_size = PRACTICAL_BLOCK_SIZE

    sessions: list[Session] = []
    sid = 0
    for oi, off in enumerate(offerings):
        common = dict(
            offering_index=oi,
            course_code=off.course_code,
            course_name=off.course_name,
            teacher=off.faculty,
            section=off.section,
            base_section=off.base_section,
            group=off.group,
            nature=off.nature,
        )
        for _ in range(off.theory_hrs):
            sessions.append(Session(id=sid, kind="Theory", duration=1, **common))
            sid += 1
        for _ in range(off.tutorial_hrs):
            sessions.append(Session(id=sid, kind="Tutorial", duration=1, **common))
            sid += 1
        remaining = off.practical_hrs
        while remaining > 0:
            block = min(block_size, remaining)
            sessions.append(Session(id=sid, kind="Practical", duration=block, **common))
            sid += 1
            remaining -= block
    return sessions
