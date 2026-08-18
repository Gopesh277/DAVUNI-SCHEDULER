"""Parser tests: merged faculty cells, whitespace normalization, groups,
multiple sections, and blank/malformed rows."""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from timetable_scheduler.parser import parse_teaching_load, CourseOffering


def _write_csv(tmp_path, rows) -> Path:
    p = tmp_path / "load.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return p


HEADER = ["S. No.", "Faculty Name", "Employee ID", "Nature of Appointment",
          "Course Code", "Course Name", "Course Type", "Programme/s", "Semester",
          "Theory", "Tutorial", "Practical", "Th", "Pr", "Tu", "Total"]
SUBHEADER = [""] * 9 + ["Theory", "Tutorial", "Practical", "", "", "", ""]


def test_merged_faculty_cells_forward_filled(tmp_path):
    rows = [HEADER, SUBHEADER,
            [1, "Dr. A", 100, "Regular", "C1", "Course One", "Th", "B.Tech CSE A", "4th", 3, 0, 0],
            [None, None, None, None, "C2", "Course Two", "Th", "B.Tech CSE A", "4th", 2, 0, 0]]
    path = _write_csv(tmp_path, rows)
    offerings = parse_teaching_load(path)
    assert len(offerings) == 2
    assert offerings[0].faculty == "Dr. A"
    assert offerings[1].faculty == "Dr. A"  # forward-filled from the blank row
    assert offerings[1].nature == "Regular"


def test_whitespace_and_programme_normalization(tmp_path):
    rows = [HEADER, SUBHEADER,
            [1, "Dr. B", 101, "Regular", "C1", "Course One", "Th", "B.tech  CSE A", "4th", 2, 0, 0]]
    path = _write_csv(tmp_path, rows)
    offerings = parse_teaching_load(path)
    assert offerings[0].base_programme == "B.Tech CSE A"


def test_blank_rows_and_malformed_rows_skipped(tmp_path):
    rows = [HEADER, SUBHEADER,
            [1, "Dr. C", 102, "Regular", "C1", "Course One", "Th", "B.Tech CSE A", "4th", 2, 0, 0],
            [None] * 12,  # fully blank row -- must be skipped
            [None, None, None, None, None, None, None, None, None, None, None, None]]
    path = _write_csv(tmp_path, rows)
    offerings = parse_teaching_load(path)
    assert len(offerings) == 1


def test_no_rows_raises_useful_error(tmp_path):
    rows = [HEADER, SUBHEADER]
    path = _write_csv(tmp_path, rows)
    try:
        parse_teaching_load(path)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "No course rows" in str(e)


def test_group_marker_parsing():
    o1 = CourseOffering(faculty="Dr. D", nature="Regular", course_code="C1", course_name="X",
                         course_type="Pr", programme="B.Tech CSE B(G1)", semester="8th", practical_hrs=2)
    assert o1.group == "G1"
    assert o1.base_programme == "B.Tech CSE B"
    assert o1.base_section == "B.Tech CSE B - 8th Sem"
    assert o1.section == "B.Tech CSE B (G1) - 8th Sem"

    o2 = CourseOffering(faculty="Dr. D", nature="Regular", course_code="C1", course_name="X",
                         course_type="Pr", programme="B.Tech CSE B", semester="8th", practical_hrs=2)
    assert o2.group is None
    assert o2.base_section == o1.base_section  # same parent section

    o3 = CourseOffering(faculty="Dr. D", nature="Regular", course_code="C1", course_name="X",
                         course_type="Pr", programme="B.Tech CSE B (G1, G2)", semester="8th", practical_hrs=2)
    assert o3.group is None  # joint session naming both groups == whole class
    assert o3.base_section == o1.base_section
