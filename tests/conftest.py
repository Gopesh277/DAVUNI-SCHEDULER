"""Shared test fixtures: isolated data dir + a helper that signs in as a department or the dean."""

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import auth, store as store_mod


@pytest.fixture()
def client_factory(tmp_path, monkeypatch):
    # fresh data dir + fresh registry so tests never touch real saved state
    monkeypatch.setattr(store_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store_mod, "LEGACY_STATE_FILE", tmp_path / "app_state.json")
    monkeypatch.setattr(store_mod, "registry", store_mod._Registry())
    monkeypatch.setattr(auth, "_login_attempts", {})
    monkeypatch.setattr(auth, "APP_USERNAME", "admin")
    monkeypatch.setattr(auth, "APP_PASSWORD", "pw")
    monkeypatch.setattr(auth, "COOKIE_SECURE", False)  # test client speaks plain http
    app_module._coordinators.clear()

    def make(dept, password="pw", username="admin"):
        c = TestClient(app_module.app)
        r = c.post("/api/login", json={"department": dept, "username": username, "password": password})
        return c, r
    return make
