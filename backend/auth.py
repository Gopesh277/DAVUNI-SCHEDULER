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

_lock = threading.RLock()
_sessions: dict[str, float] = {}  # token -> expiry (epoch seconds)


def verify_credentials(username: str, password: str) -> bool:
    username = (username or "").strip()
    password = password or ""
    # compare_digest needs equal-ish typed args and is constant-time,
    # which avoids leaking username/password validity via timing.
    user_ok = hmac.compare_digest(username, APP_USERNAME)
    pass_ok = hmac.compare_digest(password, APP_PASSWORD)
    return user_ok and pass_ok


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
