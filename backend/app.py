from __future__ import annotations

import io
import tempfile
import threading
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from timetable_scheduler import (
    SchedulerConfig, Period, CourseOffering,
    build_sessions, build_and_solve, export_workbook,
    parse_teaching_load_bytes,
    validate_offerings, detect_duplicate_offerings, preflight_capacity,
    PlacementError, place_session, unplace_session, move_session,
)
from . import auth
from .schemas import CourseIn, ConfigIn, SolverSettingsIn, GenerateRequest, LoginIn, PlaceRequest, MoveRequest, UnplaceRequest
from .store import store

app = FastAPI(title="DAV CSE Timetable API")

# No CORSMiddleware: the frontend is served by this same app (see the
# StaticFiles mount below), so browser requests are same-origin and don't
# need CORS at all. Add it back, scoped to a specific allow_origins list,
# only if the frontend ever moves to a different host than the API.

# API paths reachable without a session -- everything else under /api/
# requires a valid login.
PUBLIC_API_PATHS = {"/api/login", "/api/logout", "/api/me", "/api/health"}

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB -- a teaching-load register is a
                                     # few hundred rows at most; this just
                                     # bounds worst-case memory use per upload.


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate the API and the app shell behind the username/password login.

    Static assets (css/js/images) stay public so the login page itself can
    load them; only the API (data) and the app's index page are protected.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        token = request.cookies.get(auth.SESSION_COOKIE)
        authed = auth.is_valid_session(token)

        if path.startswith("/api/"):
            if path not in PUBLIC_API_PATHS and not authed:
                return JSONResponse({"detail": "Not authenticated."}, status_code=401)
        elif path in ("/", "/index.html"):
            if not authed:
                return RedirectResponse(url="/login.html")

        return await call_next(request)


app.add_middleware(AuthMiddleware)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


class _GenerateCoordinator:
    """Coalesces concurrent /api/generate calls into a single solve.

    Without this, two requests arriving close together -- two browser tabs,
    a page reload while the frontend's own auto-generate-on-first-load is
    still solving, etc. -- would each kick off an independent CP-SAT solve.
    That doubles OR-Tools worker-thread contention (both solves get slower)
    and races non-deterministically over which result ends up in
    store.last_result. Instead, a request that arrives while a solve is
    already running waits for it and reuses its result rather than starting
    a competing one.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._in_progress = False
        self._done = threading.Event()
        self._response = None
        self._error: Exception | None = None

    def run(self, solve_fn):
        with self._lock:
            if self._in_progress:
                join_existing = True
            else:
                self._in_progress = True
                self._done.clear()
                self._response = None
                self._error = None
                join_existing = False

        if join_existing:
            self._done.wait()
            if self._error is not None:
                raise self._error
            return self._response

        try:
            self._response = solve_fn()
        except Exception as e:
            self._error = e
            raise
        finally:
            self._in_progress = False
            self._done.set()
        return self._response


_generate_coordinator = _GenerateCoordinator()


# =====================================================================
# Serialization helpers
# =====================================================================
def _course_to_dict(c: CourseOffering, idx: int) -> dict:
    d = c.to_dict()
    d["row_id"] = idx
    d["section"] = c.section
    d["base_section"] = c.base_section
    d["group"] = c.group
    return d


def _config_to_dict(cfg: SchedulerConfig) -> dict:
    return cfg.to_dict()


def _course_diagnostics(offerings: list[CourseOffering]) -> dict:
    """Validation + duplicate-row report for the current register, shared
    by the upload/put endpoints and the standalone /api/analyze
    endpoint. Never removes or blocks a row -- purely informational."""
    issues = validate_offerings(offerings)
    duplicates = detect_duplicate_offerings(offerings)
    return {
        "issues": issues,
        "error_count": sum(1 for i in issues if i["severity"] == "error"),
        "warning_count": sum(1 for i in issues if i["severity"] == "warning"),
        "duplicates": duplicates,
    }


def _result_to_dict(result) -> dict:
    placed = []
    for ps in result.placed:
        s = ps.session
        placed.append({
            "id": s.id,
            "course_code": s.course_code,
            "course_name": s.course_name,
            "kind": s.kind,
            "duration": s.duration,
            "teacher": s.teacher,
            "nature": s.nature,
            "section": s.section,
            "base_section": s.base_section,
            "group": s.group,
            "day": ps.day,
            "day_label": result.config.days[ps.day],
            "start_period": ps.start_period,
            "periods": ps.periods,
            "room": ps.room,
        })
    unplaced = [{
        "id": s.id,
        "course_code": s.course_code, "course_name": s.course_name, "kind": s.kind,
        "duration": s.duration, "teacher": s.teacher, "section": s.section,
        "base_section": s.base_section, "group": s.group,
    } for s in result.unplaced]

    faculty = sorted({p["teacher"] for p in placed} | {u["teacher"] for u in unplaced})
    sections = sorted({p["base_section"] for p in placed} | {u["base_section"] for u in unplaced})

    # Per-course-offering required/scheduled/missing periods, so nothing is
    # silently dropped -- every unplaced session is traceable back to a
    # specific course + section + kind and a period count.
    course_key = lambda r: (r["course_code"], r["section"], r["kind"])
    required_by_course = defaultdict(int)
    scheduled_by_course = defaultdict(int)
    course_meta = {}
    for p in placed:
        k = course_key(p)
        required_by_course[k] += p["duration"]
        scheduled_by_course[k] += p["duration"]
        course_meta[k] = p
    for u in unplaced:
        k = course_key(u)
        required_by_course[k] += u["duration"]
        course_meta.setdefault(k, u)
    missing_courses = []
    for k, required in required_by_course.items():
        scheduled = scheduled_by_course.get(k, 0)
        missing = required - scheduled
        if missing > 0:
            m = course_meta[k]
            missing_courses.append({
                "course_code": m["course_code"], "course_name": m["course_name"],
                "kind": m["kind"], "teacher": m["teacher"], "section": m["section"],
                "required_periods": required, "scheduled_periods": scheduled, "missing_periods": missing,
            })
    missing_courses.sort(key=lambda r: -r["missing_periods"])

    load = defaultdict(lambda: {"nature": "", "theory": 0, "practical": 0, "tutorial": 0, "days": defaultdict(int)})
    for p in placed:
        t = load[p["teacher"]]
        t["nature"] = p["nature"]
        key = {"Theory": "theory", "Practical": "practical", "Tutorial": "tutorial"}[p["kind"]]
        t[key] += p["duration"]
        t["days"][p["day"]] += 1
    faculty_load = []
    for name, v in load.items():
        faculty_load.append({
            "faculty": name, "nature": v["nature"],
            "theory": v["theory"], "practical": v["practical"], "tutorial": v["tutorial"],
            "total": v["theory"] + v["practical"] + v["tutorial"],
            "busiest_day": max(v["days"].values()) if v["days"] else 0,
            "daily_cap": result.config.daily_cap_for(v["nature"]),
        })
    faculty_load.sort(key=lambda r: -r["total"])

    required_periods = sum(p["duration"] for p in placed) + sum(u["duration"] for u in unplaced)
    scheduled_periods = sum(p["duration"] for p in placed)
    completion_pct = round(100.0 * scheduled_periods / required_periods, 2) if required_periods else 100.0

    if result.status in ("OPTIMAL", "FEASIBLE") and not unplaced:
        message = (f"Complete timetable: all {scheduled_periods} required teaching periods "
                   f"were scheduled with zero teacher/section/room clashes.")
    elif result.status == "PARTIAL" or (result.status in ("OPTIMAL", "FEASIBLE") and unplaced):
        message = (f"Complete timetable could not be found. A best-effort timetable was generated: "
                   f"{scheduled_periods}/{required_periods} periods scheduled ({completion_pct}%). "
                   f"{len(missing_courses)} course offering(s) have missing hours -- see 'missing_courses'.")
    elif result.status == "INFEASIBLE":
        message = "Nothing could be scheduled: no valid room/period combination exists for this load and configuration."
    else:  # UNKNOWN
        message = ("The solver did not finish within the time limit and could not confirm any schedule. "
                   "Try raising the time limit.")

    return {
        "status": result.status,
        "message": message,
        "wall_time_seconds": round(result.wall_time_seconds, 2),
        "config": _config_to_dict(result.config),
        "stats": {
            "sessions_total": len(placed) + len(unplaced),
            "placed": len(placed),
            "unplaced": len(unplaced),
            "faculty": len(faculty),
            "sections": len(sections),
            "rooms": len(result.config.rooms),
            "required_periods": required_periods,
            "scheduled_periods": scheduled_periods,
            "completion_percentage": completion_pct,
        },
        "placed": placed,
        "unplaced": unplaced,
        "missing_courses": missing_courses,
        "faculty_load": faculty_load,
    }


# =====================================================================
# Auth
# =====================================================================
@app.post("/api/login")
def login(payload: LoginIn, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = auth.check_rate_limit(client_ip)
    if not allowed:
        raise HTTPException(429, f"Too many failed login attempts. Try again in {retry_after}s.")
    if not auth.verify_credentials(payload.username, payload.password):
        auth.record_failed_attempt(client_ip)
        raise HTTPException(401, "Invalid username or password.")
    auth.clear_failed_attempts(client_ip)
    token = auth.create_session()
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True, secure=auth.COOKIE_SECURE, samesite="lax", max_age=auth.SESSION_TTL_SECONDS, path="/",
    )
    return {"ok": True, "username": auth.APP_USERNAME}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    authed = auth.is_valid_session(request.cookies.get(auth.SESSION_COOKIE))
    return {"authenticated": authed, "username": auth.APP_USERNAME if authed else None}


# =====================================================================
# Config & solver settings
# =====================================================================
@app.get("/api/config")
def get_config():
    return {
        "config": _config_to_dict(store.config),
        "solver_settings": store.solver_settings,
    }


@app.put("/api/config")
def put_config(cfg: ConfigIn):
    if not cfg.rooms:
        raise HTTPException(400, "At least one room is required.")
    if not cfg.days:
        raise HTTPException(400, "At least one day is required.")
    if not cfg.periods:
        raise HTTPException(400, "At least one period is required.")
    new_config = SchedulerConfig(
        rooms=cfg.rooms,
        lab_rooms=[r for r in cfg.lab_rooms if r in cfg.rooms] or list(cfg.rooms),
        days=cfg.days,
        periods=[Period(index=p.index, label=p.label) for p in cfg.periods],
        day_break_after_period=cfg.day_break_after_period,
        practical_block_size=cfg.practical_block_size,
        max_consecutive_periods=cfg.max_consecutive_periods,
        position_daily_caps=cfg.position_daily_caps or dict(store.config.position_daily_caps),
        default_position_daily_cap=cfg.default_position_daily_cap,
    )
    store.set_config(new_config)
    return {"config": _config_to_dict(store.config)}


@app.put("/api/solver-settings")
def put_solver_settings(s: SolverSettingsIn):
    store.set_solver_settings(time_limit=s.time_limit, workers=s.workers, balance=s.balance)
    return {"solver_settings": store.solver_settings}


# =====================================================================
# Courses (the teaching-load register)
# =====================================================================
@app.get("/api/courses")
def get_courses():
    return {"courses": [_course_to_dict(c, i) for i, c in enumerate(store.courses)]}


@app.put("/api/courses")
def put_courses(courses: list[CourseIn]):
    offerings = [CourseOffering(**c.dict()) for c in courses]
    store.set_courses(offerings)
    return {
        "courses": [_course_to_dict(c, i) for i, c in enumerate(store.courses)],
        "diagnostics": _course_diagnostics(store.courses),
    }


@app.post("/api/courses/upload")
async def upload_courses(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large ({len(data) / 1e6:.1f} MB). Max is {MAX_UPLOAD_BYTES / 1e6:.0f} MB.")
    try:
        offerings = parse_teaching_load_bytes(data, file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.set_courses(offerings)
    return {
        "courses": [_course_to_dict(c, i) for i, c in enumerate(store.courses)],
        "diagnostics": _course_diagnostics(store.courses),
    }


@app.get("/api/analyze")
def analyze_courses():
    """Pre-solve diagnostics: row-level validation, duplicate-row report,
    and resource-aware capacity pre-flight (faculty/room/lab/section
    demand vs. capacity) for the currently loaded register and config --
    without invoking the solver. Lets the UI explain a likely-infeasible
    load up front rather than only after a slow solve attempt."""
    if not store.courses:
        raise HTTPException(400, "No courses loaded. Upload a teaching-load file first.")
    sessions = build_sessions(store.courses, config=store.config)
    return {
        "diagnostics": _course_diagnostics(store.courses),
        "capacity": preflight_capacity(sessions, store.config),
    }


# =====================================================================
# Generate / fetch / download schedule
# =====================================================================
@app.post("/api/generate")
def generate(req: GenerateRequest = GenerateRequest()):
    if not store.courses:
        raise HTTPException(400, "No courses loaded. Upload a teaching-load file first.")

    settings = dict(store.solver_settings)
    if req.time_limit is not None:
        settings["time_limit"] = req.time_limit
    if req.workers is not None:
        settings["workers"] = req.workers
    if req.balance is not None:
        settings["balance"] = req.balance

    def _solve():
        sessions = build_sessions(store.courses, config=store.config)
        capacity = preflight_capacity(sessions, store.config)
        result = build_and_solve(
            sessions,
            config=store.config,
            time_limit_seconds=settings["time_limit"],
            num_workers=settings["workers"],
            balance_load=settings["balance"],
        )
        store.last_result = result
        response = _result_to_dict(result)
        response["capacity"] = capacity
        return response

    return _generate_coordinator.run(_solve)


@app.get("/api/schedule")
def get_schedule():
    if store.last_result is None:
        raise HTTPException(404, "No schedule generated yet. POST /api/generate first.")
    return _result_to_dict(store.last_result)


@app.delete("/api/schedule")
def reset_schedule():
    """Reset the board: discard the generated timetable (including any
    manual drag-and-drop placements) AND wipe the teaching-load register.
    Rooms/days/periods and solver settings are left untouched."""
    store.last_result = None
    store.set_courses([])
    return {"ok": True}


@app.post("/api/schedule/place")
def place_schedule_session(req: PlaceRequest):
    """Drag-and-drop: place one currently-unplaced session onto a specific
    day/period/room. Re-validates every hard constraint server-side (see
    timetable_scheduler.manual) -- a 409 means the drop would create a
    clash or break a load/consecutive-periods/lunch-break rule."""
    if store.last_result is None:
        raise HTTPException(404, "No schedule generated yet. POST /api/generate first.")
    try:
        store.last_result = place_session(
            store.last_result, store.config, req.session_id, req.day, req.start_period, req.room,
        )
    except PlacementError as e:
        raise HTTPException(409, str(e))
    return _result_to_dict(store.last_result)


@app.post("/api/schedule/move")
def move_schedule_session(req: MoveRequest):
    """Drag-and-drop: move an already-placed session to a different
    day/period/room."""
    if store.last_result is None:
        raise HTTPException(404, "No schedule generated yet. POST /api/generate first.")
    try:
        store.last_result = move_session(
            store.last_result, store.config, req.session_id, req.day, req.start_period, req.room,
        )
    except PlacementError as e:
        raise HTTPException(409, str(e))
    return _result_to_dict(store.last_result)


@app.post("/api/schedule/unplace")
def unplace_schedule_session(req: UnplaceRequest):
    """Drag-and-drop: move a placed session back to the unplaced tray."""
    if store.last_result is None:
        raise HTTPException(404, "No schedule generated yet. POST /api/generate first.")
    try:
        store.last_result = unplace_session(store.last_result, req.session_id)
    except PlacementError as e:
        raise HTTPException(409, str(e))
    return _result_to_dict(store.last_result)


@app.get("/api/schedule/download")
def download_schedule():
    if store.last_result is None:
        raise HTTPException(404, "No schedule generated yet. POST /api/generate first.")
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        export_workbook(store.last_result, tmp.name)
        path = tmp.name
    return FileResponse(
        path, filename="timetable.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/health")
def health():
    return {"ok": True, "courses_loaded": len(store.courses), "has_schedule": store.last_result is not None}


# =====================================================================
# Frontend (static)
# =====================================================================
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
