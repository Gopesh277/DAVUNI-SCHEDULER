from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import zipfile
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

from timetable_scheduler import (
    SolveCancelled,
    SchedulerConfig, Period, CourseOffering,
    build_sessions, build_and_solve, export_workbook,
    parse_teaching_load_bytes,
    validate_offerings, detect_duplicate_offerings, preflight_capacity,
    PlacementError, place_session, unplace_session, move_session,
)
from . import auth, departments
from . import store as store_mod
from .schemas import MAX_COURSE_ROWS, MAX_HOURS_PER_ROW
from .schemas import CourseIn, ConfigIn, SolverSettingsIn, GenerateRequest, LoginIn, ChangePasswordIn, PlaceRequest, MoveRequest, UnplaceRequest
from .store import store

# Uvicorn only configures its own loggers; give the app's an INFO handler too
# so sign-ins, password changes and solves show up in `docker compose logs`.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("davuni")

# The interactive API docs (/docs, /redoc, /openapi.json) sit outside /api/ and so
# would be public; only turn them on for development.
_API_DOCS = os.environ.get("ENABLE_API_DOCS", "false").strip().lower() in ("true", "1", "yes")
app = FastAPI(
    title="DAV University Timetable API",
    docs_url="/docs" if _API_DOCS else None,
    redoc_url="/redoc" if _API_DOCS else None,
    openapi_url="/openapi.json" if _API_DOCS else None,
)

# No CORSMiddleware: the frontend is served by this same app (see the
# StaticFiles mount below), so browser requests are same-origin and don't
# need CORS at all. Add it back, scoped to a specific allow_origins list,
# only if the frontend ever moves to a different host than the API.

# API paths reachable without a session -- everything else under /api/
# requires a valid login.
PUBLIC_API_PATHS = {"/api/login", "/api/logout", "/api/me", "/api/health", "/api/departments"}
# All a department still on the default admin123 password may do: choose its own.
DEFAULT_PASSWORD_API_PATHS = PUBLIC_API_PATHS | {"/api/change-password"}

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB -- a teaching-load register is a
                                     # few hundred rows at most; this just
                                     # bounds worst-case memory use per upload.
MAX_XLSX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # an .xlsx is a zip: refuse "zip bombs"

# Sent on every response. The CSP only allows this app's own scripts (no
# inline <script>), so even if some text slipped into the page unescaped it
# couldn't run; inline style="" attributes and Google Fonts are still allowed
# because the UI uses them. frame-ancestors/X-Frame-Options stop clickjacking.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate the API and the app shell behind the username/password login.

    Static assets (css/js/images) stay public so the login page itself can
    load them; only the API (data) and the app's index page are protected.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        token = request.cookies.get(auth.SESSION_COOKIE)
        info = auth.session_info(token)
        role = info["role"] if info else None
        dept_id = info["dept"] if info else None
        if role == auth.ROLE_DEPT and not departments.exists(dept_id):
            info = role = dept_id = None  # department was removed from departments.json
        authed = info is not None
        if role == auth.ROLE_DEPT:
            # every route handler's `store` now resolves to this department
            store_mod.set_current_department(dept_id)

        if path.startswith("/api/"):
            if path not in PUBLIC_API_PATHS:
                if not authed:
                    return JSONResponse({"detail": "Not authenticated."}, status_code=401)
            if authed and path not in DEFAULT_PASSWORD_API_PATHS and auth.using_default_password(dept_id):
                return JSONResponse(
                    {"detail": "Choose a new password before using the app.", "must_change_password": True},
                    status_code=403)
        elif path in ("/", "/index.html"):
            if not authed:
                return RedirectResponse(url="/login.html")

        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if path.startswith("/api/") or path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-store"   # data / app shell is never served from a browser cache
        return response


app.add_middleware(AuthMiddleware)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


class _Run:
    """One in-flight solve."""

    def __init__(self, key):
        self.key = key                    # (time_limit, workers, balance, data_version)
        self.cancel = threading.Event()   # set when a newer request replaces this solve
        self.done = threading.Event()
        self.response = None
        self.error: Exception | None = None


class _GenerateCoordinator:
    """Keeps /api/generate to one solve at a time per department.

    * Same settings + same data as the solve already running (two browser
      tabs, a page reload while the page's own auto-generate is still
      solving...): the new request waits for it and shares its result instead
      of starting a competing CP-SAT solve.
    * DIFFERENT settings or data (e.g. you raised the time limit, or edited
      the register): the running solve is out of date, so it is stopped and
      the new request runs with the new values. Previously such a request
      silently joined the old solve and got a result computed with the OLD
      time limit -- which is why raising the limit "still took 150 s".
      A request that was replaced gets SolveCancelled.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._current: _Run | None = None

    def run(self, key, solve_fn):
        while True:
            with self._lock:
                cur = self._current
                if cur is None:
                    mine = _Run(key)
                    self._current = mine
                    action = "lead"
                elif cur.key == key and not cur.cancel.is_set():
                    action = "join"
                else:
                    cur.cancel.set()
                    action = "supersede"
            if action == "lead":
                break
            cur.done.wait()
            if action == "join":
                if cur.error is not None:
                    raise cur.error
                return cur.response
            # "supersede": the old solve has stopped; loop and take over

        try:
            mine.response = solve_fn(mine.cancel)
        except Exception as e:
            mine.error = e
            raise
        finally:
            with self._lock:
                if self._current is mine:
                    self._current = None
            mine.done.set()
        return mine.response


_coordinators: dict[str, _GenerateCoordinator] = {}
_coordinators_lock = threading.Lock()


def _generate_coordinator_for_current_dept() -> _GenerateCoordinator:
    """One coordinator per department, so one department's solve is never
    mistaken for (or handed to) another department's request."""
    dept = store_mod.current_department_id()
    with _coordinators_lock:
        if dept not in _coordinators:
            _coordinators[dept] = _GenerateCoordinator()
        return _coordinators[dept]


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


def _result_response() -> dict:
    """Current timetable as JSON for the API (used after manual place/move/unplace edits)."""
    return _result_to_dict(store.last_result)


# =====================================================================
# Auth
# =====================================================================
@app.get("/api/departments")
def list_departments():
    return {"departments": departments.list_departments()}


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True, secure=auth.COOKIE_SECURE, samesite="lax", max_age=auth.SESSION_TTL_SECONDS, path="/",
    )


@app.post("/api/login")
def login(payload: LoginIn, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = auth.check_rate_limit(client_ip)
    if not allowed:
        log.warning("login locked out ip=%s", client_ip)
        raise HTTPException(429, f"Too many failed login attempts. Try again in {retry_after}s.")

    dept = departments.get(payload.department)
    if dept is None:
        raise HTTPException(400, "Please select your department.")
    if not auth.verify_credentials(payload.username, payload.password, dept["id"]):
        auth.record_failed_attempt(client_ip)
        log.warning("login failed dept=%s ip=%s", dept["id"], client_ip)
        raise HTTPException(401, "Invalid username or password.")
    auth.clear_failed_attempts(client_ip)
    log.info("login ok dept=%s ip=%s", dept["id"], client_ip)
    _set_session_cookie(response, auth.create_session(dept["id"]))
    return {"ok": True, "role": auth.ROLE_DEPT, "username": payload.username.strip(), "department": dept, "redirect": "/",
            "must_change_password": auth.using_default_password(dept["id"])}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/change-password")
def change_password(payload: ChangePasswordIn, request: Request, response: Response):
    """Let the signed-in department admin change their own password.

    Requires the current password (so a walk-up on an unlocked browser can't
    lock the owner out), enforces a minimum length, and signs every OTHER
    session of this department out afterwards."""
    token = request.cookies.get(auth.SESSION_COOKIE)
    dept_id = auth.session_department(token)
    if not dept_id:
        raise HTTPException(403, "Only a department admin can change a password.")

    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = auth.check_rate_limit(client_ip)
    if not allowed:
        raise HTTPException(429, f"Too many failed attempts. Try again in {retry_after}s.")

    username = auth.expected_username(dept_id)
    if not auth.verify_credentials(username, payload.current_password, dept_id):
        auth.record_failed_attempt(client_ip)
        raise HTTPException(400, "Current password is incorrect.")
    try:
        auth.validate_new_password(payload.new_password, payload.current_password)
    except auth.PasswordChangeError as e:
        raise HTTPException(400, str(e))

    auth.set_password(dept_id, payload.new_password)
    auth.clear_failed_attempts(client_ip)
    # Issue a fresh session token for this browser and kill every other one
    # (including the old token), so a token captured before the change is useless.
    new_token = auth.create_session(dept_id)
    signed_out = auth.destroy_other_sessions(dept_id, new_token)
    _set_session_cookie(response, new_token)
    log.info("password changed dept=%s ip=%s", dept_id, client_ip)
    return {"ok": True, "other_sessions_signed_out": max(signed_out - 1, 0)}


@app.get("/api/me")
def me(request: Request):
    info = auth.session_info(request.cookies.get(auth.SESSION_COOKIE))
    dept = departments.get(info["dept"]) if info else None
    authed = dept is not None
    return {
        "authenticated": authed,
        "role": auth.ROLE_DEPT if authed else None,
        "username": auth.expected_username(info["dept"]) if authed else None,
        "department": dept,
        "must_change_password": auth.using_default_password(info["dept"]) if authed else False,
    }


# =====================================================================
# Config & solver settings
# =====================================================================
@app.get("/api/config")
def get_config():
    return {
        "config": _config_to_dict(store.config),
        "solver_settings": store.solver_settings,
        "rooms_configured": store.rooms_configured,
        "department": departments.get(store_mod.current_department_id()),
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


def _check_register_limits(offerings: list[CourseOffering]) -> None:
    """Reject registers big enough to exhaust memory when expanded into sessions."""
    if len(offerings) > MAX_COURSE_ROWS:
        raise HTTPException(400, f"Too many course rows ({len(offerings)}). The limit is {MAX_COURSE_ROWS}.")
    for i, o in enumerate(offerings, start=1):
        for field in ("theory_hrs", "tutorial_hrs", "practical_hrs"):
            v = getattr(o, field)
            if not 0 <= v <= MAX_HOURS_PER_ROW:
                raise HTTPException(
                    400, f"Row {i} ({o.course_code}): {field} is {v}; it must be between 0 and {MAX_HOURS_PER_ROW}.")


def _check_xlsx_not_zip_bomb(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile:
        return  # not a zip; the parser reports the real problem
    if total > MAX_XLSX_UNCOMPRESSED_BYTES:
        raise HTTPException(413, "This spreadsheet expands to too much data to be a teaching-load register.")


@app.put("/api/courses")
def put_courses(courses: list[CourseIn]):
    offerings = [CourseOffering(**c.dict()) for c in courses]
    _check_register_limits(offerings)
    store.set_courses(offerings)
    return {
        "courses": [_course_to_dict(c, i) for i, c in enumerate(store.courses)],
        "diagnostics": _course_diagnostics(store.courses),
    }


@app.post("/api/courses/upload")
async def upload_courses(file: UploadFile = File(...)):
    # read at most one byte past the limit instead of the whole (possibly huge) file
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large. Max is {MAX_UPLOAD_BYTES / 1e6:.0f} MB.")
    _check_xlsx_not_zip_bomb(data)
    try:
        offerings = parse_teaching_load_bytes(data, file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _check_register_limits(offerings)
    store.set_courses(offerings)
    return {
        "courses": [_course_to_dict(c, i) for i, c in enumerate(store.courses)],
        "diagnostics": _course_diagnostics(store.courses),
    }


@app.get("/api/analyze")
def analyze_courses():
    if not store.rooms_configured:
        raise HTTPException(400, "No rooms set up for this department yet. Add your rooms first (Settings → Rooms).")
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
    if not store.rooms_configured:
        raise HTTPException(400, "No rooms set up for this department yet. Add your rooms first (Settings → Rooms).")
    if not store.courses:
        raise HTTPException(400, "No courses loaded. Upload a teaching-load file first.")

    settings = dict(store.solver_settings)
    if req.time_limit is not None:
        settings["time_limit"] = req.time_limit
    if req.workers is not None:
        settings["workers"] = req.workers
    if req.balance is not None:
        settings["balance"] = req.balance

    key = (settings["time_limit"], settings["workers"], settings["balance"], store.data_version)

    def _solve(cancel):
        sessions = build_sessions(store.courses, config=store.config)
        capacity = preflight_capacity(sessions, store.config)
        result = build_and_solve(
            sessions,
            config=store.config,
            time_limit_seconds=settings["time_limit"],
            num_workers=settings["workers"],
            balance_load=settings["balance"],
            cancel_event=cancel,
        )
        store.last_result = result
        log.info("solve dept=%s status=%s placed=%d unplaced=%d %.1fs", store_mod.current_department_id(),
                 result.status, len(result.placed), len(result.unplaced), result.wall_time_seconds)
        response = _result_to_dict(result)
        response["capacity"] = capacity
        return response

    try:
        return _generate_coordinator_for_current_dept().run(key, _solve)
    except SolveCancelled:
        raise HTTPException(409, "This generate request was replaced by a newer one (settings or data changed).")



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
    return _result_response()


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
    return _result_response()


@app.post("/api/schedule/unplace")
def unplace_schedule_session(req: UnplaceRequest):
    """Drag-and-drop: move a placed session back to the unplaced tray."""
    if store.last_result is None:
        raise HTTPException(404, "No schedule generated yet. POST /api/generate first.")
    try:
        store.last_result = unplace_session(store.last_result, req.session_id)
    except PlacementError as e:
        raise HTTPException(409, str(e))
    return _result_response()


DOWNLOAD_VIEWS = {"all", "sections", "teachers", "rooms"}


@app.get("/api/schedule/download")
def download_schedule(view: str = "all"):
    """Download the timetable as .xlsx.

    view=all       -> every sheet (Master, Faculty_Load, Section/Teacher/Room-wise)
    view=sections  -> section-wise timetables only
    view=teachers  -> teacher-wise timetables only
    view=rooms     -> room-wise timetables only
    """
    view = (view or "all").strip().lower()
    if view not in DOWNLOAD_VIEWS:
        raise HTTPException(400, "view must be one of: all, sections, teachers, rooms.")
    if store.last_result is None:
        raise HTTPException(404, "No schedule generated yet. POST /api/generate first.")
    dept = departments.get(store_mod.current_department_id()) or {}
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        path = tmp.name
    try:
        export_workbook(store.last_result, path,
                        department_name=dept.get("display", "Department of Computer Science & Engineering"),
                        views=None if view == "all" else [view])
    except Exception:
        os.unlink(path)  # don't leave a half-written temp file behind
        raise
    suffix = "" if view == "all" else f"_{view[:-1]}_wise"
    return FileResponse(
        path, filename=f"timetable_{dept.get('id', 'dept')}{suffix}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=BackgroundTask(os.unlink, path),
    )


@app.get("/api/health")
def health():
    return {"ok": True}


# =====================================================================
# Frontend (static)
# =====================================================================
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
