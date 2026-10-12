"""Multi-department behaviour: login picks a department, each department has
its own isolated data, and only CSE starts with rooms."""

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

    def make(dept, password="pw"):
        c = TestClient(app_module.app)
        r = c.post("/api/login", json={"department": dept, "username": "admin", "password": password})
        return c, r
    return make


def test_department_list_is_public_and_includes_cse():
    c = TestClient(app_module.app)
    ids = [d["id"] for d in c.get("/api/departments").json()["departments"]]
    assert "cse" in ids and len(ids) > 1


def test_login_requires_valid_department(client_factory):
    c = TestClient(app_module.app)
    assert c.post("/api/login", json={"department": "", "username": "admin", "password": "pw"}).status_code == 400
    assert c.post("/api/login", json={"department": "nope", "username": "admin", "password": "pw"}).status_code == 400
    _, r = client_factory("cse", password="wrong")
    assert r.status_code == 401


def test_me_reports_department(client_factory):
    c, r = client_factory("cse")
    assert r.status_code == 200
    me = c.get("/api/me").json()
    assert me["authenticated"] and me["department"]["display"] == "Department of Computer Science & Engineering"
    c2, _ = client_factory("civil")
    assert c2.get("/api/me").json()["department"]["display"] == "Department of Civil Engineering"


def test_only_cse_has_preloaded_rooms(client_factory):
    cse, _ = client_factory("cse")
    civil, _ = client_factory("civil")
    a = cse.get("/api/config").json()
    b = civil.get("/api/config").json()
    assert a["rooms_configured"] is True and len(a["config"]["rooms"]) == 12
    assert b["rooms_configured"] is False and b["config"]["rooms"] == []
    # cannot generate until rooms are set up
    r = civil.post("/api/generate")
    assert r.status_code == 400 and "rooms" in r.json()["detail"].lower()


def test_departments_are_isolated(client_factory):
    cse, _ = client_factory("cse")
    civil, _ = client_factory("civil")
    cfg = civil.get("/api/config").json()["config"]
    cfg["rooms"], cfg["lab_rooms"] = ["C1", "C2", "LabC"], ["LabC"]
    assert civil.put("/api/config", json=cfg).status_code == 200
    assert civil.get("/api/config").json()["rooms_configured"] is True
    assert len(cse.get("/api/config").json()["config"]["rooms"]) == 12  # CSE untouched

    row = {"faculty": "Dr A", "nature": "Regular", "course_code": "CE-101", "course_name": "Statics",
           "programme": "B.Tech Civil A", "semester": "1", "theory_hrs": 2, "tutorial_hrs": 0, "practical_hrs": 0}
    assert civil.put("/api/courses", json=[row]).status_code == 200
    assert len(civil.get("/api/courses").json()["courses"]) == 1
    assert cse.get("/api/courses").json()["courses"] == []

    # reset board only clears the signed-in department
    assert cse.put("/api/courses", json=[{**row, "course_code": "CS-1", "programme": "B.Tech CSE A"}]).status_code == 200
    assert civil.delete("/api/schedule").status_code == 200
    assert civil.get("/api/courses").json()["courses"] == []
    assert len(cse.get("/api/courses").json()["courses"]) == 1


def test_state_persists_per_department(client_factory, tmp_path):
    civil, _ = client_factory("civil")
    cfg = civil.get("/api/config").json()["config"]
    cfg["rooms"], cfg["lab_rooms"] = ["C1"], []
    civil.put("/api/config", json=cfg)
    assert (tmp_path / "dept_civil.json").exists()
    # a brand-new registry (i.e. server restart) reloads it
    store_mod.registry = store_mod._Registry()
    assert store_mod.registry.get("civil").rooms_configured is True


def test_generate_works_for_new_department_after_rooms(client_factory):
    civil, _ = client_factory("civil")
    cfg = civil.get("/api/config").json()["config"]
    cfg["rooms"], cfg["lab_rooms"] = ["C1", "C2"], ["C2"]
    civil.put("/api/config", json=cfg)
    row = {"faculty": "Dr A", "nature": "Regular", "course_code": "CE-101", "course_name": "Statics",
           "programme": "B.Tech Civil A", "semester": "1", "theory_hrs": 3, "tutorial_hrs": 0, "practical_hrs": 0}
    civil.put("/api/courses", json=[row])
    r = civil.post("/api/generate", json={"time_limit": 5, "workers": 2, "balance": False})
    assert r.status_code == 200
    assert r.json()["stats"]["unplaced"] == 0
    dl = civil.get("/api/schedule/download")
    assert dl.status_code == 200 and "timetable_civil.xlsx" in dl.headers["content-disposition"]


def test_per_department_password_override(client_factory, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD_LAW", "lawpw")
    _, bad = client_factory("law", password="pw")
    _, good = client_factory("law", password="lawpw")
    assert bad.status_code == 401 and good.status_code == 200
    _, other = client_factory("cse", password="pw")  # other departments keep the shared password
    assert other.status_code == 200


def test_data_routes_require_login():
    c = TestClient(app_module.app)
    assert c.get("/api/config").status_code == 401
