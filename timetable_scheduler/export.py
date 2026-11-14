"""
Writes the solved schedule out to a single .xlsx workbook with:

  Master        - flat list of every scheduled (and any unplaced) session
  Faculty_Load  - weekly contact hours per faculty member, by register category
  Section_Wise  - one grid per class (parent section, groups combined)
  Teacher_Wise  - one grid per faculty member
  Room_Wise     - one grid per room

The three grid sheets copy the look of the web app's timetable board: a maroon
header row of period timings, one row per day, tan cells for theory/tutorial,
mauve cells for practicals, bold course code, and the same detail lines the app
shows for that view. Every grid starts on its own printed page (landscape,
fit-to-width), so the sheets can be printed and handed out as-is.

`export_workbook(..., views=...)` can restrict the file to some of the sheets,
e.g. views=["sections"] for a section-wise-only download.
"""

from __future__ import annotations

from collections import defaultdict

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break

from .model import ScheduleResult, PlacedSession

FONT_NAME = "Arial"

# palette copied from frontend/styles.css
MAROON = "6E1423"
MAROON_DEEP = "450B15"
GOLD = "C9A227"
INK = "241712"
INK_SOFT = "6B5C4E"
LINE = "E3D4B0"
PAPER_DIM = "F1E9D4"
THEORY_BG = "F3E9C9"
LAB_BG = "EDE1E3"

HEADER_FILL = PatternFill("solid", fgColor=MAROON)
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)
TITLE_FONT = Font(name=FONT_NAME, bold=True, size=14, color=MAROON)
SUB_FONT = Font(name=FONT_NAME, italic=True, size=9.5, color=INK_SOFT)
DAY_FILL = PatternFill("solid", fgColor=PAPER_DIM)
DAY_FONT = Font(name=FONT_NAME, bold=True, size=10, color=INK_SOFT)
LAB_FILL = PatternFill("solid", fgColor=LAB_BG)
THEORY_FILL = PatternFill("solid", fgColor=THEORY_BG)
THIN = Side(style="thin", color=LINE)
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top", horizontal="left")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)

VIEW_ALIASES = {
    "sections": "sections", "section": "sections",
    "teachers": "teachers", "teacher": "teachers",
    "rooms": "rooms", "room": "rooms",
    "master": "master", "load": "load",
}
ALL_VIEWS = ("master", "load", "sections", "teachers", "rooms")


def _slot_rich_text(items: list[PlacedSession], line2) -> CellRichText:
    """Same three lines the app prints in a slot: bold course code, a detail
    line that depends on the view, then `kind - periods - Room - group`."""
    code_font = InlineFont(rFont=FONT_NAME, b=True, sz=10, color=MAROON)
    meta_font = InlineFont(rFont=FONT_NAME, sz=9, color=INK_SOFT)
    parts = []
    for n, ps in enumerate(items):
        s = ps.session
        if n:
            parts.append(TextBlock(meta_font, "\n\n"))
        tag = s.kind + (" · 2 periods" if s.duration == 2 else "") + f" · Room {ps.room}"
        if s.group:
            tag += f" · {s.group}"
        parts.append(TextBlock(code_font, s.course_code))
        parts.append(TextBlock(meta_font, "\n" + line2(ps)))
        parts.append(TextBlock(meta_font, "\n" + tag))
    return CellRichText(*parts)


def _plain_text(items: list[PlacedSession], line2) -> str:
    out = []
    for ps in items:
        s = ps.session
        tag = s.kind + (" · 2 periods" if s.duration == 2 else "") + f" · Room {ps.room}"
        if s.group:
            tag += f" · {s.group}"
        out.append(f"{s.course_code}\n{line2(ps)}\n{tag}")
    return "\n\n".join(out)


def _write_grid(ws, title: str, subtitle: str, start_row: int, placed: list[PlacedSession],
                line2, days: list, periods: list) -> int:
    """One Day x Period grid (days as rows, periods as columns), laid out like
    the app. Returns the next free row."""
    by_start = defaultdict(list)
    for ps in placed:
        by_start[(ps.day, ps.start_period)].append(ps)

    last_col = 1 + len(periods)
    ws.cell(row=start_row, column=1, value=title).font = TITLE_FONT
    ws.cell(row=start_row + 1, column=1, value=subtitle).font = SUB_FONT
    header_row = start_row + 2

    c = ws.cell(row=header_row, column=1, value="Day")
    c.font, c.fill, c.alignment, c.border = HEADER_FONT, HEADER_FILL, CENTER, BORDER
    for pi, period in enumerate(periods):
        c = ws.cell(row=header_row, column=2 + pi, value=period.label)
        c.font, c.fill, c.alignment, c.border = HEADER_FONT, HEADER_FILL, CENTER, BORDER
    ws.row_dimensions[header_row].height = 22

    skip = set()  # (day_idx, period_index) already covered by a 2-period merge
    for di, day in enumerate(days):
        row = header_row + 1 + di
        ws.row_dimensions[row].height = 64
        dc = ws.cell(row=row, column=1, value=day)
        dc.font, dc.fill, dc.border = DAY_FONT, DAY_FILL, BORDER
        dc.alignment = Alignment(horizontal="center", vertical="center")
        for pi, period in enumerate(periods):
            col = 2 + pi
            cell = ws.cell(row=row, column=col)
            cell.border = BORDER
            cell.alignment = WRAP
            if (di, period.index) in skip:
                continue
            items = by_start.get((di, period.index), [])
            if not items:
                continue
            first = items[0]
            try:
                cell.value = _slot_rich_text(items, line2)
            except Exception:  # very old openpyxl without rich text support
                cell.value = _plain_text(items, line2)
            cell.fill = LAB_FILL if first.session.kind == "Practical" else THEORY_FILL
            if first.session.duration == 2 and pi + 1 < len(periods):
                nxt = ws.cell(row=row, column=col + 1)
                nxt.border = BORDER
                ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + 1)
                skip.add((di, period.index + 1))
            if len(items) > 1:
                ws.row_dimensions[row].height = max(ws.row_dimensions[row].height, 64 * len(items) * 0.9)

    ws.column_dimensions["A"].width = 14
    for pi in range(len(periods)):
        ws.column_dimensions[get_column_letter(2 + pi)].width = 24
    return header_row + 1 + len(days) + 2


def _page_setup(ws, last_row: int, last_col: int):
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.sheet_view.showGridLines = False


def _normalise_views(views) -> list[str]:
    if not views:
        return list(ALL_VIEWS)
    if isinstance(views, str):
        views = [views]
    out = []
    for v in views:
        key = VIEW_ALIASES.get(str(v).strip().lower())
        if key is None:
            raise ValueError(f"Unknown view {v!r}. Use one of: all, sections, teachers, rooms.")
        if key not in out:
            out.append(key)
    return [v for v in ALL_VIEWS if v in out]


def export_workbook(result: ScheduleResult, path: str,
                    department_name: str = "Department of Computer Science & Engineering",
                    views=None) -> None:
    """views: None/"all" for the full workbook, or any of
    "sections", "teachers", "rooms", "master", "load" (str or list)."""
    if isinstance(views, str) and views.strip().lower() == "all":
        views = None
    wanted = _normalise_views(views)

    config = result.config
    days, periods, rooms = config.days, config.periods, config.rooms
    grid_cols = 1 + len(periods)

    wb = Workbook()
    wb.remove(wb.active)

    def _letterhead(ws):
        ws.cell(row=1, column=1, value="DAV UNIVERSITY, JALANDHAR").font = Font(
            name=FONT_NAME, bold=True, size=13, color=MAROON)
        ws.cell(row=2, column=1, value=f"{department_name} — Timetable Management System").font = SUB_FONT
        return 4  # first free row after the letterhead

    def _grid_sheet(name: str, groups: list, make_title, make_subtitle, line2):
        """groups: [(key, [PlacedSession])]. One grid per key, each on its own printed page."""
        ws = wb.create_sheet(name)
        r = _letterhead(ws)
        if not groups:
            ws.cell(row=r, column=1, value="Nothing is scheduled yet.").font = SUB_FONT
        for i, (key, placed) in enumerate(groups):
            if i:
                ws.row_breaks.append(Break(id=r - 1))
                ws.cell(row=r, column=1, value="DAV UNIVERSITY, JALANDHAR").font = Font(
                    name=FONT_NAME, bold=True, size=10, color=MAROON)
                ws.cell(row=r + 1, column=1, value=f"{department_name}").font = SUB_FONT
                r += 3
            r = _write_grid(ws, make_title(key), make_subtitle(key, placed), r, placed, line2, days, periods)
        _page_setup(ws, r, grid_cols)
        ws.freeze_panes = None
        return ws

    # ---------------- Master sheet -----------------------------------
    if "master" in wanted:
        ws = wb.create_sheet("Master")
        start = _letterhead(ws)
        headers = ["Course Code", "Course Name", "Kind", "Duration (pd)", "Teacher",
                   "Register", "Section", "Day", "Start Period", "Room", "Status"]
        for ci, h in enumerate(headers, start=1):
            c = ws.cell(row=start, column=ci, value=h)
            c.font, c.fill = HEADER_FONT, HEADER_FILL
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
                ws.cell(row=row, column=ci, value=v).font = Font(name=FONT_NAME, color="B4432D", bold=True)
            row += 1
        for ci, h in enumerate(headers, start=1):
            ws.column_dimensions[get_column_letter(ci)].width = max(14, len(h) + 4)
        ws.column_dimensions["B"].width = 34
        ws.column_dimensions["E"].width = 26

    # ---------------- Faculty load sheet -----------------------------
    if "load" in wanted:
        ws2 = wb.create_sheet("Faculty_Load")
        start2 = _letterhead(ws2)
        headers2 = ["Faculty", "Register", "Theory (hrs/wk)", "Practical (hrs/wk)",
                    "Tutorial (hrs/wk)", "Total (hrs/wk)", "Busiest day (sessions)"]
        for ci, h in enumerate(headers2, start=1):
            c = ws2.cell(row=start2, column=ci, value=h)
            c.font, c.fill = HEADER_FONT, HEADER_FILL

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
            for ci, val in enumerate([teacher, v["nature"], v["th"], v["pr"], v["tu"], total, busiest], start=1):
                ws2.cell(row=row, column=ci, value=val)
            row += 1
        for ci, h in enumerate(headers2, start=1):
            ws2.column_dimensions[get_column_letter(ci)].width = max(16, len(h) + 2)
        ws2.column_dimensions["A"].width = 30

    def _hours(placed):
        return sum(p.session.duration for p in placed)

    # ---------------- Section-wise -----------------------------------
    if "sections" in wanted:
        secs = sorted({p.session.base_section for p in result.placed})
        groups = [(s, [p for p in result.placed if p.session.base_section == s]) for s in secs]

        def sec_sub(key, placed):
            n_groups = len({p.session.group for p in placed if p.session.group})
            extra = f" · incl. {n_groups} sub-group{'s' if n_groups > 1 else ''}" if n_groups else ""
            return f"Section-wise (student) timetable · {_hours(placed)} class hrs/week{extra}"

        _grid_sheet("Section_Wise", groups, lambda k: f"Section {k}", sec_sub,
                    lambda ps: f"{ps.session.course_code} · {ps.session.teacher}")

    # ---------------- Teacher-wise -----------------------------------
    if "teachers" in wanted:
        teachers = sorted({p.session.teacher for p in result.placed})
        groups = [(t, [p for p in result.placed if p.session.teacher == t]) for t in teachers]

        def teacher_sub(key, placed):
            nature = placed[0].session.nature if placed else ""
            return f"Teacher-wise timetable · {_hours(placed)} contact hrs/week" + (f" · {nature}" if nature else "")

        _grid_sheet("Teacher_Wise", groups, lambda k: k, teacher_sub,
                    lambda ps: f"{ps.session.course_code} · {ps.session.section}")

    # ---------------- Room-wise --------------------------------------
    if "rooms" in wanted:
        groups = [(rm, [p for p in result.placed if p.room == rm]) for rm in rooms]
        groups = [(rm, pl) for rm, pl in groups if pl] or groups

        _grid_sheet("Room_Wise", groups, lambda k: f"Room {k}",
                    lambda k, placed: f"Room-wise timetable · {len(placed)} session-slots this week",
                    lambda ps: f"{ps.session.teacher} · {ps.session.section}")

    wb.save(path)
