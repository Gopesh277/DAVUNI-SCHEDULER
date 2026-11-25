"""Regression tests for the security fixes: default password, input limits,
upload limits, spreadsheet formula injection, session rotation, headers."""

import io
import zipfile

import openpyxl

from backend import auth

ROW = {"faculty": "T", "nature": "Regular", "course_code": "C1", "course_name": "X",
       "programme": "BTech A", "semester": "1", "theory_hrs": 2, "tutorial_hrs": 0, "practical_hrs": 0}


def test_default_password_must_be_changed_first(client_factory, monkeypatch):
    monkeypatch.setattr(auth, "APP_PASSWORD", auth.DEFAULT_PASSWORD)
    c, r = client_factory("cse", password="admin123")
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert c.get("/api/me").json()["must_change_password"] is True
    # nothing else works until a new password is chosen
    r = c.get("/api/config")
    assert r.status_code == 403 and r.json()["must_change_password"] is True
    assert c.put("/api/courses", json=[ROW]).status_code == 403
    # can't "choose" the default again
    assert c.post("/api/change-password", json={"current_password": "admin123",
                                                 "new_password": "admin123"}).status_code == 400
    r = c.post("/api/change-password", json={"current_password": "admin123", "new_password": "Civil-2026!"})
    assert r.status_code == 200
    assert c.get("/api/config").status_code == 200
    assert c.get("/api/me").json()["must_change_password"] is False
    # default no longer works for this department; still works (with forced change) for another
    assert client_factory("cse", password="admin123")[1].status_code == 401
    other, r = client_factory("civil", password="admin123")
    assert r.status_code == 200 and other.get("/api/config").status_code == 403


def test_real_env_password_not_forced_to_change(client_factory):
    c, r = client_factory("cse")   # conftest sets APP_PASSWORD="pw"
    assert r.json()["must_change_password"] is False
    assert c.get("/api/config").status_code == 200


def test_huge_hours_rejected(client_factory):
    c, _ = client_factory("cse")
    r = c.put("/api/courses", json=[{**ROW, "theory_hrs": 1_000_000_000}])
    assert r.status_code == 422


def test_uploaded_register_limits_enforced(client_factory):
    c, _ = client_factory("cse")
    csv = ("S.No,Faculty Name,Employee ID,Nature,Course Code,Course Name,Course Type,Programme,Semester,Contact Hours,,\n"
           ",,,,,,,,,Th,Tu,Pr\n"
           "1,T,1,Regular,C1,X,Th,BTech A,1,999999,0,0\n")
    r = c.post("/api/courses/upload", files={"file": ("r.csv", csv.encode(), "text/csv")})
    assert r.status_code in (400, 422), r.text


def test_zip_bomb_rejected(client_factory):
    c, _ = client_factory("cse")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/big.xml", b"0" * (60 * 1024 * 1024))
    r = c.post("/api/courses/upload", files={"file": ("r.xlsx", buf.getvalue(), "application/octet-stream")})
    assert r.status_code == 413


def test_export_has_no_formulas(client_factory, tmp_path):
    c, _ = client_factory("cse")
    evil = '=HYPERLINK("http://evil.example/","x")'
    assert c.put("/api/courses", json=[{**ROW, "faculty": evil, "course_code": evil}]).status_code == 200
    assert c.post("/api/generate", json={"time_limit": 5}).status_code == 200
    r = c.get("/api/schedule/download")
    assert r.status_code == 200
    p = tmp_path / "out.xlsx"
    p.write_bytes(r.content)
    wb = openpyxl.load_workbook(p)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                assert cell.data_type != "f", (ws.title, cell.coordinate, cell.value)


def test_password_change_rotates_session(client_factory):
    c, _ = client_factory("cse")
    old = c.cookies.get(auth.SESSION_COOKIE)
    r = c.post("/api/change-password", json={"current_password": "pw", "new_password": "N3w-secret!"})
    assert r.status_code == 200
    new = c.cookies.get(auth.SESSION_COOKIE)
    assert new and new != old
    assert not auth.is_valid_session(old)
    assert c.get("/api/config").status_code == 200


def test_security_headers(client_factory):
    c, _ = client_factory("cse")
    r = c.get("/api/health")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_api_docs_not_public(client_factory):
    c, _ = client_factory("cse")
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404, path


def test_unreadable_department_file_is_kept(client_factory, tmp_path):
    from backend import store as store_mod
    bad = tmp_path / "dept_civil.json"
    bad.write_text("{ this is not json")
    c, _ = client_factory("civil")
    assert c.get("/api/config").status_code == 200      # starts empty instead of crashing
    kept = list(tmp_path.glob("dept_civil.json.unreadable-*"))
    assert kept and kept[0].read_text() == "{ this is not json"
    assert not list(tmp_path.glob("*.tmp"))              # atomic write left no temp file
    store_mod.registry.get("civil")                      # still loadable
