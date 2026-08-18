"""
Pre-solve diagnostics: row-level validation, duplicate-row detection, and
resource-aware capacity pre-flight analysis.

None of this touches the solver. It exists so the application can tell
the user *before* (or alongside) generating a timetable:

  * which rows in the uploaded register look malformed (missing faculty,
    negative hours, etc) -- without silently dropping anything;
  * which rows look like accidental duplicates (vs legitimate repeated
    assignments, which are preserved either way);
  * whether the teaching load can plausibly fit the configured rooms,
    labs, faculty daily caps, and section/group weekly slots -- checked
    per resource type, not as one blended global number, since e.g.
    practical demand only competes for LAB_ROOMS and a faculty member's
    cap is independent of how many rooms exist.
"""

from __future__ import annotations

from collections import defaultdict

from .config import SchedulerConfig
from .parser import CourseOffering
from .sessions import Session


# =====================================================================
# Row-level validation
# =====================================================================
def validate_offerings(offerings: list[CourseOffering]) -> list[dict]:
    """Returns a list of {row, severity, field, message} diagnostics.
    `severity` is "error" (the row is genuinely malformed) or "warning"
    (the row parsed but looks suspicious). Nothing here removes a row --
    it's purely informational for the caller to display."""
    issues: list[dict] = []

    def add(row, severity, field, message):
        issues.append({"row": row, "severity": severity, "field": field, "message": message})

    for idx, o in enumerate(offerings):
        if not (o.faculty or "").strip():
            add(idx, "error", "faculty", "Missing faculty name.")
        if not o.course_code or o.course_code == "-":
            add(idx, "warning", "course_code", "Missing course code.")
        if not o.course_name or o.course_name == "Untitled course":
            add(idx, "warning", "course_name", "Missing course name.")
        if not o.programme or o.base_programme == "Unassigned":
            add(idx, "warning", "programme", "Missing or unrecognized programme/section.")
        if not (o.semester or "").strip():
            add(idx, "warning", "semester", "Missing semester.")

        for field_name, val in (("theory_hrs", o.theory_hrs), ("tutorial_hrs", o.tutorial_hrs),
                                 ("practical_hrs", o.practical_hrs)):
            if val < 0:
                add(idx, "error", field_name, f"Negative {field_name.replace('_', ' ')}: {val}.")

        if o.theory_hrs == 0 and o.tutorial_hrs == 0 and o.practical_hrs == 0:
            add(idx, "warning", "hours", "All contact hours are zero -- this row produces no teaching sessions.")

    return issues


# =====================================================================
# Duplicate-row detection
# =====================================================================
def detect_duplicate_offerings(offerings: list[CourseOffering]) -> list[dict]:
    """Groups rows that are identical in every field that matters for
    scheduling (faculty, course, exact schedulable section/group, and all
    three hour counts). These are *reported*, never auto-removed -- a
    real register can legitimately have two independent rows that happen
    to match (e.g. a genuinely repeated assignment), so the caller
    decides what to do with the report."""
    buckets: dict[tuple, list[int]] = defaultdict(list)
    for idx, o in enumerate(offerings):
        key = (
            (o.faculty or "").strip().lower(),
            (o.course_code or "").strip().lower(),
            o.section,
            o.theory_hrs, o.tutorial_hrs, o.practical_hrs,
        )
        buckets[key].append(idx)

    duplicates = []
    for key, idxs in buckets.items():
        if len(idxs) > 1:
            sample = offerings[idxs[0]]
            duplicates.append({
                "rows": idxs,
                "count": len(idxs),
                "faculty": sample.faculty,
                "course_code": sample.course_code,
                "section": sample.section,
                "message": (
                    f"{len(idxs)} rows are identical (same faculty, course, section and hours) at "
                    f"register rows {idxs}. If this is one course accidentally entered twice, remove "
                    f"the extra row; if it's a real repeated/team-taught assignment, no action is needed."
                ),
            })
    duplicates.sort(key=lambda d: -d["count"])
    return duplicates


# =====================================================================
# Resource-aware capacity pre-flight
# =====================================================================
def preflight_capacity(sessions: list[Session], config: SchedulerConfig) -> dict:
    """Compares demand against capacity separately per resource type
    (faculty, rooms, labs, sections/groups) *before* invoking the
    solver, so an overloaded load can be explained up front instead of
    only surfacing as an opaque solver failure."""
    ppd = config.periods_per_day
    num_days = len(config.days)
    total_slots = num_days * ppd

    total_required = sum(s.duration for s in sessions)
    practical_required = sum(s.duration for s in sessions if s.kind == "Practical")
    theory_tutorial_required = total_required - practical_required

    room_capacity = len(config.rooms) * total_slots
    lab_rooms = [r for r in config.lab_rooms if r in config.rooms] or list(config.rooms)
    lab_capacity = len(lab_rooms) * total_slots

    # ---- Faculty capacity: required periods vs. (daily cap x days) ----
    by_faculty: dict[str, dict] = defaultdict(lambda: {"required": 0, "nature": ""})
    for s in sessions:
        f = by_faculty[s.teacher]
        f["required"] += s.duration
        f["nature"] = s.nature

    faculty_load = []
    for name, v in by_faculty.items():
        daily_cap = config.daily_cap_for(v["nature"])
        weekly_capacity = daily_cap * num_days
        overload = max(0, v["required"] - weekly_capacity)
        faculty_load.append({
            "faculty": name, "nature": v["nature"], "required_periods": v["required"],
            "daily_cap": daily_cap, "weekly_capacity": weekly_capacity,
            "overloaded": overload > 0, "overload_periods": overload,
        })
    faculty_load.sort(key=lambda r: -r["overload_periods"])

    # ---- Section/group capacity: required periods vs. weekly slots ----
    by_section: dict[str, int] = defaultdict(int)
    for s in sessions:
        by_section[s.section] += s.duration
    section_load = []
    for section, required in sorted(by_section.items()):
        over = max(0, required - total_slots)
        section_load.append({
            "section": section, "required_periods": required,
            "weekly_slots": total_slots, "over_capacity": over > 0, "overload_periods": over,
        })
    section_load.sort(key=lambda r: -r["overload_periods"])

    warnings = []
    if practical_required > lab_capacity:
        warnings.append(
            f"Practical demand ({practical_required} periods/week) exceeds total lab capacity "
            f"({lab_capacity} periods/week across {len(lab_rooms)} lab room(s) x {total_slots} slots). "
            f"Some practicals will not fit even scheduling parallel groups in every lab."
        )
    if total_required > room_capacity:
        warnings.append(
            f"Total teaching demand ({total_required} periods/week) exceeds total room capacity "
            f"({room_capacity} periods/week across {len(config.rooms)} room(s) x {total_slots} slots)."
        )
    for f in faculty_load:
        if f["overloaded"]:
            warnings.append(
                f"{f['faculty']} ({f['nature'] or 'Regular'}): requires {f['required_periods']} "
                f"periods/week but the daily cap ({f['daily_cap']}/day x {num_days} days) allows at "
                f"most {f['weekly_capacity']} -- overloaded by {f['overload_periods']}."
            )
    for s in section_load:
        if s["over_capacity"]:
            warnings.append(
                f"{s['section']}: requires {s['required_periods']} periods/week, more than the "
                f"{total_slots} weekly slots available to a single section/group -- overloaded by "
                f"{s['overload_periods']}."
            )

    return {
        "weekly_slots_per_resource": total_slots,
        "total_required_periods": total_required,
        "practical_required_periods": practical_required,
        "theory_tutorial_required_periods": theory_tutorial_required,
        "room_capacity_periods": room_capacity,
        "lab_capacity_periods": lab_capacity,
        "faculty_load": faculty_load,
        "section_load": section_load,
        "warnings": warnings,
        "likely_infeasible": bool(warnings),
    }
