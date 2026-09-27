"""
Minimal username/password auth.

Single set of credentials (this is a small departmental tool, not a
multi-user system) checked in constant time, backed by an in-memory
session-token store keyed off an httponly cookie. No external auth
dependency is required.

Credentials are read from the APP_USERNAME / APP_PASSWORD environment
variables so they can be set per-deployment instead of living in source
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

MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 5 * 60  # lock out an IP for 5 minutes after 5 failures

_lock = threading.RLock()
_sessions: dict[str, float] = {}  # token -> expiry (epoch seconds)
_login_attempts: dict[str, list[float]] = {}  # client IP -> failed-attempt timestamps


def verify_credentials(username: str, password: str) -> bool:
    username = (username or "").strip()
    password = password or ""
    # compare_digest needs equal-ish typed args and is constant-time,
    # which avoids leaking username/password validity via timing.
    user_ok = hmac.compare_digest(username, APP_USERNAME)
    pass_ok = hmac.compare_digest(password, APP_PASSWORD)
    return user_ok and pass_ok


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


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _prune_locked()
        _sessions[token] = time.time() + SESSION_TTL_SECONDS
    return token


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def is_valid_session(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        expiry = _sessions.get(token)
        if expiry is None:
            return False
        if expiry < time.time():
            _sessions.pop(token, None)
            return False
        return True


def _prune_locked() -> None:
    now = time.time()
    for t in [t for t, exp in _sessions.items() if exp < now]:
        _sessions.pop(t, None)
