from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from timetable_scheduler.parser import CourseOffering
from timetable_scheduler.sessions import build_sessions


def _offering(programme, **kw):
    base = dict(faculty="Dr. E", nature="Regular", course_code="CST1", course_name="X",
                course_type="Pr", semester="4th", practical_hrs=2)
    base.update(kw)
    return CourseOffering(programme=programme, **base)


def test_whole_and_groups_share_one_parent_section():
    offs = [_offering("4A"), _offering("4A(G1)"), _offering("4A(G2)")]
    base_sections = {o.base_section for o in offs}
    assert len(base_sections) == 1, "whole/G1/G2 rows must resolve to ONE parent section"

    schedulable_sections = {o.section for o in offs}
    assert len(schedulable_sections) == 3, "whole, G1, G2 remain distinct schedulable units"


def test_whitespace_variants_do_not_fragment_sections():
    offs = [_offering("4A"), _offering(" 4A "), _offering("4A (G1)"), _offering("4A(G1)")]
    assert len({o.base_section for o in offs}) == 1
    assert len({o.section for o in offs}) == 2  # whole vs G1


def test_section_count_is_derived_not_hardcoded():
    programmes = ["4A", "4B", "4C", "5A(G1)", "5A(G2)", "6A"]
    offs = [_offering(p) for p in programmes]
    parent_sections = {o.base_section for o in offs}
    # 4A,4B,4C,5A,6A = 5 parents, no matter how many rows/groups exist
    assert len(parent_sections) == 5


def test_group_never_becomes_an_independent_parent():
    offs = [_offering("7A(G1)"), _offering("7A(G2)")]
    for o in offs:
        assert o.base_section == "7A - 4th Sem"
        assert "G1" not in o.base_section and "G2" not in o.base_section


def test_sessions_carry_group_and_base_section_through():
    offs = [_offering("4A"), _offering("4A(G1)")]
    sessions = build_sessions(offs)
    kinds = {(s.group, s.base_section) for s in sessions}
    assert (None, "4A - 4th Sem") in kinds
    assert ("G1", "4A - 4th Sem") in kinds
