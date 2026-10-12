"""
Minimal username/password auth.

Single set of credentials (this is a small departmental tool, not a
multi-user system) checked in constant time, backed by an in-memory
session-token store keyed off an httponly cookie. No external auth
dependency is required.

Credentials are read from the APP_USERNAME / APP_PASSWORD environment
variables (optionally overridden per department with APP_USERNAME_<ID> /
APP_PASSWORD_<ID>, e.g. APP_PASSWORD_CIVIL) so they can be set per-deployment instead of living in source
control. If unset, they fall back to admin / admin123 -- change this in
production via the environment.

Also tracks failed login attempts per client IP and locks out further
attempts for a while past a threshold, so the login endpoint can't be
brute-forced at will.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time

SESSION_COOKIE = "tt_session"
SESSION_TTL_SECONDS = 12 * 60 * 60  # 12 hours

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin123")

# Whether the session cookie gets the `Secure` flag (browser only sends it
# back over HTTPS -- localhost is exempted by browsers, so local dev over
# plain http://localhost still works). Defaults on; set COOKIE_SECURE=false
# only if you're deploying somewhere without TLS in front (not recommended).
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").strip().lower() not in ("false", "0", "no")

# The Dean's Office login. It is not tied to any department and opens the
# read-only dean portal (all departments' timetables + faculty availability).
# Override with DEAN_USERNAME / DEAN_PASSWORD -- change these in production.
DEAN_USERNAME = os.environ.get("DEAN_USERNAME", "dean")
DEAN_PASSWORD = os.environ.get("DEAN_PASSWORD", "dean_admin123")

ROLE_DEPT = "department"
ROLE_DEAN = "dean"

MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 5 * 60  # lock out an IP for 5 minutes after 5 failures

_lock = threading.RLock()
_sessions: dict[str, tuple[float, str | None, str]] = {}  # token -> (expiry epoch s, department id or None, role)
_login_attempts: dict[str, list[float]] = {}  # client IP -> failed-attempt timestamps


def _expected_credentials(dept_id: str | None) -> tuple[str, str]:
    """Shared APP_USERNAME / APP_PASSWORD, unless a department has its own
    APP_USERNAME_<ID> / APP_PASSWORD_<ID> set (e.g. APP_PASSWORD_CIVIL)."""
    key = (dept_id or "").strip().upper()
    user = os.environ.get(f"APP_USERNAME_{key}", APP_USERNAME) if key else APP_USERNAME
    pwd = os.environ.get(f"APP_PASSWORD_{key}", APP_PASSWORD) if key else APP_PASSWORD
    return user, pwd


def verify_credentials(username: str, password: str, dept_id: str | None = None) -> bool:
    username = (username or "").strip()
    password = password or ""
    exp_user, exp_pass = _expected_credentials(dept_id)
    # compare_digest is constant-time, which avoids leaking
    # username/password validity via timing.
    user_ok = hmac.compare_digest(username.encode(), exp_user.encode())
    pass_ok = hmac.compare_digest(password.encode(), exp_pass.encode())
    return user_ok and pass_ok


def is_dean_username(username: str) -> bool:
    """True if the typed username is the (reserved) dean username."""
    return hmac.compare_digest((username or "").strip().lower().encode(), DEAN_USERNAME.lower().encode())


def verify_dean(username: str, password: str) -> bool:
    return is_dean_username(username) and hmac.compare_digest((password or "").encode(), DEAN_PASSWORD.encode())


def check_rate_limit(client_ip: str) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds). Once MAX_LOGIN_ATTEMPTS
    failures have happened for this IP within LOGIN_LOCKOUT_SECONDS,
    further attempts are blocked until the oldest one ages out."""
    now = time.time()
    with _lock:
        attempts = [t for t in _login_attempts.get(client_ip, []) if t > now - LOGIN_LOCKOUT_SECONDS]
        if attempts:
            _login_attempts[client_ip] = attempts
        else:
            _login_attempts.pop(client_ip, None)  # opportunistic cleanup
        if len(attempts) >= MAX_LOGIN_ATTEMPTS:
            retry_after = int(LOGIN_LOCKOUT_SECONDS - (now - attempts[0])) + 1
            return False, max(retry_after, 1)
    return True, 0


def record_failed_attempt(client_ip: str) -> None:
    with _lock:
        _login_attempts.setdefault(client_ip, []).append(time.time())


def clear_failed_attempts(client_ip: str) -> None:
    with _lock:
        _login_attempts.pop(client_ip, None)


def create_session(dept_id: str | None, role: str = ROLE_DEPT) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _prune_locked()
        _sessions[token] = (time.time() + SESSION_TTL_SECONDS, dept_id, role)
    return token


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def session_info(token: str | None) -> dict | None:
    """{"role": "department"|"dean", "dept": id|None} for a live session, else None."""
    if not token:
        return None
    with _lock:
        entry = _sessions.get(token)
        if entry is None:
            return None
        expiry, dept_id, role = entry
        if expiry < time.time():
            _sessions.pop(token, None)
            return None
        return {"role": role, "dept": dept_id}


def session_department(token: str | None) -> str | None:
    """Department id of a department session (None for dean / missing / expired)."""
    info = session_info(token)
    return info["dept"] if info and info["role"] == ROLE_DEPT else None


def is_valid_session(token: str | None) -> bool:
    return session_info(token) is not None


def _prune_locked() -> None:
    now = time.time()
    for t in [t for t, (exp, _d, _r) in _sessions.items() if exp < now]:
        _sessions.pop(t, None)
