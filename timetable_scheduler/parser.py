"""
Parses the department's teaching-load register (.xlsx or .csv) into a
flat list of normalized course-offering records.

The register has merged cells: S.No / Faculty Name / Employee ID /
Nature of Appointment only appear on a faculty member's first row, with
every subsequent course row for that faculty left blank in those
columns. This module forward-fills those columns before building the
records that the rest of the application consumes.

The only real assumption is the column *order*, matching the
department's standard template:

    S.No | Faculty Name | Employee ID | Nature of Appointment |
    Course Code | Course Name | Course Type | Programme/s | Semester |
    Contact Hours(Theory, Tutorial, Practical) | Teaching Load(...) | Signature

The Teaching Load columns are informational totals in the source sheet
and are recomputed by this application rather than trusted verbatim, so
they are not read here.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# Matches a trailing group marker on a programme string, e.g.
# "B.Tech CSE B(G1)", "B.Tech CSE B (G2)", "B.Tech CS & AI(G1, G2)".
_GROUP_RE = re.compile(r"\(?\s*((?:g\s*\d+\s*,?\s*)+)\)?\s*$", re.IGNORECASE)


def _normalize_programme_text(raw: str) -> str:
    """Collapses whitespace/casing/punctuation variants that are the same
    programme in practice (seen in real registers: 'B.tech CSE A',
    'B.Tech  CSE A' with a double space, 'CS &AI' vs 'CS& AI' vs 'CS&AI')
    so they resolve to one section instead of fragmenting into several."""
    s = (raw or "Unassigned").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*&\s*", " & ", s)
    s = re.sub(r"(?i)^b\.\s*tech\b", "B.Tech", s)
    s = re.sub(r"(?i)^m\.\s*tech\b", "M.Tech", s)
    return s


@dataclass
class CourseOffering:
    faculty: str
    nature: str
    course_code: str
    course_name: str
    course_type: str
    programme: str
    semester: str
    theory_hrs: int = 0
    tutorial_hrs: int = 0
    practical_hrs: int = 0
    emp_id: Any = None

    @property
    def group(self) -> str | None:
        """The sub-group this row is for ('G1', 'G2', ...), or None if the
        row applies to the whole class. A trailing marker naming *multiple*
        groups together (e.g. "(G1, G2)") means the whole class is meant --
        that's a shared/joint session, not a per-group split."""
        m = _GROUP_RE.search((self.programme or "").strip())
        if not m:
            return None
        tokens = sorted({t.upper().replace(" ", "") for t in re.findall(r"g\s*\d+", m.group(1), re.IGNORECASE)})
        return tokens[0] if len(tokens) == 1 else None

    @property
    def base_programme(self) -> str:
        """Programme text with any group marker and text-quality noise
        stripped -- this is what ties a group's practicals back to its
        parent class."""
        prog = (self.programme or "Unassigned").strip()
        m = _GROUP_RE.search(prog)
        if m:
            prog = prog[:m.start()]
        return _normalize_programme_text(prog)

    @property
    def base_section(self) -> str:
        """The parent class this row belongs to, regardless of sub-group --
        what the Section-wise view groups by, and what a sub-group's
        sessions must never clash with."""
        sem = (self.semester or "").strip()
        return f"{self.base_programme} - {sem} Sem" if sem else self.base_programme

    @property
    def section(self) -> str:
        """The exact schedulable unit: the base class, with a group suffix
        if this row is a per-group split."""
        sem = (self.semester or "").strip()
        label = f"{self.base_programme} ({self.group})" if self.group else self.base_programme
        return f"{label} - {sem} Sem" if sem else label

    def to_dict(self) -> dict:
        return asdict(self)


def _to_int(v) -> int:
    try:
        if v is None or v == "":
            return 0
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _rows_from_xlsx(path: Path) -> list[list]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    sheet_name = "Sheet1" if "Sheet1" in wb.sheetnames else wb.sheetnames[0]
    ws = wb[sheet_name]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _rows_from_csv(path: Path) -> list[list]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [row for row in csv.reader(f)]


def _find_header_row(rows: list[list]) -> int:
    for i, row in enumerate(rows):
        if row and any(str(c).strip() == "Course Code" for c in row if c is not None):
            return i
    return 0


def parse_teaching_load(path: str | Path) -> list[CourseOffering]:
    """Parse a teaching-load register file into CourseOffering records.

    Accepts .xlsx or .csv exported in the department's standard layout.
    Raises ValueError if no recognizable course rows are found.
    """
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        rows = _rows_from_xlsx(path)
    elif path.suffix.lower() == ".csv":
        rows = _rows_from_csv(path)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}. Use .xlsx or .csv.")

    header_idx = _find_header_row(rows)
    data_rows = rows[header_idx + 2:]  # skip header + units sub-header row

    offerings: list[CourseOffering] = []
    cur_faculty = cur_emp = cur_nature = None

    for row in data_rows:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue
        padded = list(row) + [None] * max(0, 12 - len(row))
        sno, fname, emp, nature, ccode, cname, ctype, prog, sem, th, tu, pr = padded[:12]

        if _clean(sno) is not None:
            cur_faculty, cur_emp, cur_nature = _clean(fname), emp, _clean(nature) or "Regular"

        is_empty_course = not any([_clean(ccode), _clean(cname), _clean(prog), _clean(sem),
                                    _to_int(th), _to_int(tu), _to_int(pr)])
        if is_empty_course or not cur_faculty:
            continue

        offerings.append(CourseOffering(
            faculty=cur_faculty,
            nature=cur_nature or "Regular",
            course_code=_clean(ccode) or "-",
            course_name=_clean(cname) or "Untitled course",
            course_type=_clean(ctype) or "",
            programme=_clean(prog) or "Unassigned",
            semester=_clean(sem) or "",
            theory_hrs=_to_int(th),
            tutorial_hrs=_to_int(tu),
            practical_hrs=_to_int(pr),
            emp_id=cur_emp,
        ))

    if not offerings:
        raise ValueError(
            f"No course rows found in {path}. Expected the standard department "
            "register columns (Faculty Name ... Course Code ... Contact Hours)."
        )
    return offerings


def parse_teaching_load_bytes(data: bytes, filename: str) -> list[CourseOffering]:
    """Same as parse_teaching_load, but for an in-memory upload (FastAPI
    UploadFile etc). Writes to a temp file since openpyxl/csv both expect
    a path or file-like seekable object, then reuses the same parsing path."""
    import tempfile

    suffix = Path(filename).suffix or ".xlsx"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return parse_teaching_load(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
