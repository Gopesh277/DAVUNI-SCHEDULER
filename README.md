# DAV University CSE — Timetable Generator

Generates a clash-free weekly timetable from the department's teaching-load
register, using Google OR-Tools **CP-SAT** as the constraint solver.

Two ways to use it:

* **CLI** (`main.py`) — point it at a register file, get an `.xlsx` back.
* **Web app** (`backend/` + `frontend/`) — a FastAPI backend and a browser
  UI where you can upload/edit the register, change rooms/days/periods/solver
  settings, generate, and download — all without touching a terminal again.

Both share the same `timetable_scheduler/` engine, so the scheduling logic
only lives in one place.

## What it does

1. **Parses** `teaching_load*.xlsx` (or a `.csv` export of it), forward-filling
   the merged Faculty Name / Employee ID / Register columns.
2. **Expands** each course row into atomic sessions: one per theory hour, one
   per tutorial hour, and practical hours grouped into continuous 2-period
   lab blocks (per the department's "2 lec continuous" convention).
3. **Solves** for a day/period/room for every session with CP-SAT, subject to:
   - no teacher double-booked
   - no section (class) double-booked
   - no room double-booked
   - lab blocks never cross the lunch break or a day boundary
   - practicals only placed in lab-capable rooms
   - **no 3 consecutive periods** — a teacher is never scheduled for more
     than `max_consecutive_periods` (default 2) periods back-to-back on
     the same day; a run that crosses the lunch break doesn't count as
     unbroken
   - **daily load by faculty position** — a teacher never exceeds their
     register category's periods/day cap (defaults: Regular 6, Senior 5,
     Super Senior 4, Contractual 7, New Faculty 7 — configurable)
   - **load balancer**: minimises the busiest single day for any one
     faculty member, so hours are spread across the week
4. **Exports** an `.xlsx` with `Master`, `Faculty_Load`, `By_Room`,
   `By_Teacher`, `By_Section` sheets.

## Setup

```bash
pip install -r requirements.txt
```

## Option A — command line

```bash
python main.py --input sample_data/teaching_load24252updated.xlsx --output timetable.xlsx
```

| Flag | Default | Meaning |
|---|---|---|
| `--input, -i` | *required* | Teaching-load `.xlsx` or `.csv` |
| `--output, -o` | `timetable_output.xlsx` | Output workbook path |
| `--time-limit` | `60` | Solver time budget in seconds |
| `--workers` | `8` | CP-SAT parallel search workers |
| `--no-balance` | off | Skip the load-balancing objective — finds *any* feasible timetable faster |
| `--verbose` | off | Print the CP-SAT search log |

## Option B — web app

```bash
uvicorn backend.app:app --reload --port 8000
```

Then open **http://localhost:8000** — the FastAPI app serves the `frontend/`
static files itself, so there's nothing else to run.

On first launch the app seeds itself from `sample_data/teaching_load24252updated.xlsx`
and generates automatically. From there, everything is editable in the browser:

- **Room-wise / Teacher-wise / Section-wise** — pick from a dropdown, see that
  view's Day × Period grid.
- **Faculty load** — weekly hours per faculty member, register category, and
  the busiest single day (what the load balancer is minimising).
- **Manage data** — edit any cell, add or delete course rows, or drag in a
  whole new teaching-load `.xlsx`/`.csv` — "Save & regenerate" reruns the
  solver on the new data.
- **Settings** — change the room list, which rooms are lab-capable, working
  days, period timings, where the lunch break sits, the max back-to-back
  periods a teacher can have, daily load caps per faculty register category,
  and the solver's time limit / worker count / load-balancing toggle. "Save
  settings & regenerate" applies them immediately.
- **Download .xlsx** — exports the currently-generated timetable in the same
  five-sheet format as the CLI.

Your edits (register + settings) are saved to `backend/data/app_state.json`
on the server, so they survive a restart. Delete that file to go back to a
clean slate seeded from the sample register.

### API summary

| Method & path | Purpose |
|---|---|
| `GET /api/config` | current rooms/days/periods + solver settings |
| `PUT /api/config` | update rooms/days/periods/lunch break |
| `PUT /api/solver-settings` | update time limit / workers / balance toggle |
| `GET /api/courses` | current teaching-load register |
| `PUT /api/courses` | replace the whole register (used by the editor) |
| `POST /api/courses/upload` | upload a new `.xlsx`/`.csv` register |
| `POST /api/courses/reset` | reset to the bundled sample register |
| `POST /api/generate` | run the solver, returns the full schedule as JSON |
| `GET /api/schedule` | fetch the last-generated schedule without resolving |
| `GET /api/schedule/download` | download the current schedule as `.xlsx` |

## Editing department settings from code

Everything the web UI's Settings tab controls also lives in
`timetable_scheduler/config.py` as sensible defaults (used by the CLI, or
whenever the web app has no saved overrides yet).

## If it comes back infeasible

Both the CLI and the web app will tell you which sessions couldn't be
placed. This almost always means the register is asking for more hours than
the week (or the lab-capable rooms) can hold for one teacher/section/room
combination, or the fatigue rules are too tight for the load a teacher is
carrying. Thin out that row, add rooms, widen the lab-room list, raise the
relevant daily cap in Settings, or increase the solver time limit.

## Note on solve time

The no-3-consecutive and per-position daily-cap rules make the search
noticeably harder than plain clash-avoidance — sometimes dramatically so,
if a cap leaves little weekly headroom for your busiest-loaded faculty
member (check the Faculty load tab's "busiest day / cap" column). The
solver runs in two passes: a fast feasibility-only pass to get *some* valid
timetable, then (time permitting) a second pass that optimizes load balance,
warm-started from the first. The default time budget is 150 seconds; the
solver may return `FEASIBLE` (a fully valid, constraint-respecting
timetable) rather than `OPTIMAL` (provably the most evenly balanced one)
within that budget — that's expected and fine as long as `unplaced` is 0.

If it's still timing out or coming back infeasible:
- Check the Faculty load tab for anyone with little headroom under their
  daily cap (weekly hours close to `cap × working days`) and raise their
  category's cap in Settings.
- Raise `--time-limit` (CLI) or the Settings time limit (web) — 150-250s is
  reasonable for a once-in-a-while generation.
- Loosen "Max back-to-back periods" if 2 is too strict for your register.

## Project layout

```
main.py                        CLI entry point
backend/
  app.py                       FastAPI routes
  store.py                     persisted app state (courses/config/settings)
  schemas.py                   pydantic request/response models
  data/                        app_state.json (created at runtime)
frontend/
  index.html, styles.css, app.js   the browser UI (served by FastAPI)
timetable_scheduler/
  config.py                    SchedulerConfig + module-level defaults
  parser.py                    reads the teaching-load register
  sessions.py                  expands course rows into atomic sessions
  model.py                     the CP-SAT model (constraints + objective)
  export.py                    writes the output .xlsx
sample_data/
  teaching_load24252updated.xlsx
requirements.txt
```
