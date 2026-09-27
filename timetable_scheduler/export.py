"""
Writes the solved schedule out to a single .xlsx workbook with:

  Master        - flat list of every scheduled (and any unplaced) session
  Faculty_Load  - weekly contact hours per faculty member, by register category
  By_Room       - one grid per room, stacked vertically
  By_Teacher    - one grid per faculty member, stacked vertically
  By_Section    - one grid per class (parent section, groups combined), stacked vertically
"""

from __future__ import annotations

from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from .model import ScheduleResult, PlacedSession

FONT_NAME = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="22392F")
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)
TITLE_FONT = Font(name=FONT_NAME, bold=True, size=13, color="22392F")
LAB_FILL = PatternFill("solid", fgColor="E7EEE8")
THEORY_FILL = PatternFill("solid", fgColor="F5F0DE")
THIN = Side(style="thin", color="D9D3BF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top", horizontal="left")
CENTER = Alignment(horizontal="center", vertical="center")


def _cell_text(ps: PlacedSession) -> str:
    s = ps.session
    lines = [f"{s.course_code}", f"{s.teacher}", f"{s.section}"]
    tag = f"{s.kind}" + (" (2 pd)" if s.duration == 2 else "")
    if s.group:
        tag += f" [{s.group}]"
    lines.append(f"{tag} - Rm {ps.room}")
    return "\n".join(lines)


def _write_grid(ws, title: str, start_row: int, sessions_by_start: dict, days: list, periods: list) -> int:
    """Writes one Day x Period grid (days as rows, periods as columns) at
    start_row. Returns next free row."""
    ws.cell(row=start_row, column=1, value=title).font = TITLE_FONT
    header_row = start_row + 1
    ws.cell(row=header_row, column=1, value="Day").font = HEADER_FONT
    ws.cell(row=header_row, column=1).fill = HEADER_FILL
    for pi, period in enumerate(periods):
        c = ws.cell(row=header_row, column=2 + pi, value=period.label)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = CENTER

    occupied = set()  # (day_idx, period_idx) covered by a horizontal merge already written
    for di, day in enumerate(days):
        row = header_row + 1 + di
        dc = ws.cell(row=row, column=1, value=day)
        dc.font = Font(name=FONT_NAME, bold=True, size=9)
        dc.border = BORDER
        for pi, period in enumerate(periods):
            col = 2 + pi
            if (di, period.index) in occupied:
                continue
            key = (di, period.index)
            items = sessions_by_start.get(key, [])
            cell = ws.cell(row=row, column=col)
            cell.border = BORDER
            cell.alignment = WRAP
            if items:
                ps = items[0]
                cell.value = _cell_text(ps)
                cell.fill = LAB_FILL if ps.session.kind == "Practical" else THEORY_FILL
                if ps.session.duration == 2:
                    ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + 1)
                    occupied.add((di, period.index + 1))
    end_row = header_row + 1 + len(days)
    for pi in range(len(periods)):
        ws.column_dimensions[get_column_letter(2 + pi)].width = 26
    ws.column_dimensions["A"].width = 14
    return end_row + 2


def _group_starts(placed: list[PlacedSession]):
    """day_idx, start_period -> [PlacedSession]"""
    g = defaultdict(list)
    for ps in placed:
        g[(ps.day, ps.start_period)].append(ps)
    return g


def export_workbook(result: ScheduleResult, path: str) -> None:
    config = result.config
    days, periods, rooms = config.days, config.periods, config.rooms

    wb = Workbook()
    title_font = Font(name=FONT_NAME, bold=True, size=13, color="6E1423")
    subtitle_font = Font(name=FONT_NAME, italic=True, size=9.5, color="6B5C4E")

    def _write_letterhead(ws, span_cols: int):
        ws.cell(row=1, column=1, value="DAV UNIVERSITY, JALANDHAR").font = title_font
        ws.cell(row=2, column=1, value="Department of Computer Science & Engineering — Timetable Management System").font = subtitle_font
        return 4  # first free row after the letterhead

    # ---------------- Master sheet -----------------------------------
    ws = wb.active
    ws.title = "Master"
    start = _write_letterhead(ws, 11)
    headers = ["Course Code", "Course Name", "Kind", "Duration (pd)", "Teacher",
               "Register", "Section", "Day", "Start Period", "Room", "Status"]
    for ci, h in enumerate(headers, start=1):
        c = ws.cell(row=start, column=ci, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
    row = start + 1
    for ps in result.placed:
        s = ps.session
        vals = [s.course_code, s.course_name, s.kind, s.duration, s.teacher, s.nature,
                s.section, days[ps.day], periods[ps.start_period - 1].label, ps.room, "Placed"]
        for ci, v in enumerate(vals, start=1):
            ws.cell(row=row, column=ci, value=v)
        row += 1
    for s in result.unplaced:
        vals = [s.course_code, s.course_name, s.kind, s.duration, s.teacher, s.nature,
                s.section, "-", "-", "-", "UNPLACED"]
        for ci, v in enumerate(vals, start=1):
            c = ws.cell(row=row, column=ci, value=v)
            c.font = Font(name=FONT_NAME, color="B4432D", bold=True)
        row += 1
    for ci, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = max(14, len(h) + 4)

    # ---------------- Faculty load sheet -----------------------------
    ws2 = wb.create_sheet("Faculty_Load")
    start2 = _write_letterhead(ws2, 7)
    headers2 = ["Faculty", "Register", "Theory (hrs/wk)", "Practical (hrs/wk)",
                "Tutorial (hrs/wk)", "Total (hrs/wk)", "Busiest day (sessions)"]
    for ci, h in enumerate(headers2, start=1):
        c = ws2.cell(row=start2, column=ci, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL

    by_teacher = defaultdict(lambda: {"nature": "", "th": 0, "pr": 0, "tu": 0, "days": defaultdict(int)})
    for ps in result.placed:
        s = ps.session
        t = by_teacher[s.teacher]
        t["nature"] = s.nature
        if s.kind == "Theory":
            t["th"] += s.duration
        elif s.kind == "Practical":
            t["pr"] += s.duration
        elif s.kind == "Tutorial":
            t["tu"] += s.duration
        t["days"][ps.day] += 1

    row = start2 + 1
    for teacher, v in sorted(by_teacher.items(), key=lambda kv: -(kv[1]["th"] + kv[1]["pr"] + kv[1]["tu"])):
        total = v["th"] + v["pr"] + v["tu"]
        busiest = max(v["days"].values()) if v["days"] else 0
        vals = [teacher, v["nature"], v["th"], v["pr"], v["tu"], total, busiest]
        for ci, val in enumerate(vals, start=1):
            ws2.cell(row=row, column=ci, value=val)
        row += 1
    for ci, h in enumerate(headers2, start=1):
        ws2.column_dimensions[get_column_letter(ci)].width = max(16, len(h) + 2)

    # ---------------- Grid sheets -------------------------------------
    ws_room = wb.create_sheet("By_Room")
    r = _write_letterhead(ws_room, 6)
    for room in rooms:
        placed_here = [p for p in result.placed if p.room == room]
        grid = _group_starts(placed_here)
        r = _write_grid(ws_room, f"Room {room}", r, grid, days, periods)

    ws_teacher = wb.create_sheet("By_Teacher")
    r = _write_letterhead(ws_teacher, 6)
    teachers = sorted({p.session.teacher for p in result.placed})
    for t in teachers:
        placed_here = [p for p in result.placed if p.session.teacher == t]
        grid = _group_starts(placed_here)
        r = _write_grid(ws_teacher, t, r, grid, days, periods)

    ws_section = wb.create_sheet("By_Section")
    r = _write_letterhead(ws_section, 6)
    sections = sorted({p.session.base_section for p in result.placed})
    for sec in sections:
        placed_here = [p for p in result.placed if p.session.base_section == sec]
        grid = _group_starts(placed_here)
        r = _write_grid(ws_section, sec, r, grid, days, periods)

    wb.save(path)
