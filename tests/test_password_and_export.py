"""Admin password change + view-wise timetable downloads."""

import io

import openpyxl

from backend import auth, store as store_mod

COURSES = [
    {"faculty": "Dr A", "nature": "Regular", "course_code": "CS101", "course_name": "Intro",
     "programme": "BTech", "semester": "3", "theory_hrs": 3, "tutorial_hrs": 0, "practical_hrs": 2},
    {"faculty": "Dr B", "nature": "Contractual", "course_code": "CS102", "course_name": "DS",
     "programme": "BTech", "semester": "3", "theory_hrs": 3, "tutorial_hrs": 0, "practical_hrs": 0},
]


# ---------------------------------------------------------------- password
def test_change_password_then_login_with_new(client_factory):
    c, r = client_factory("cse")
    assert r.status_code == 200
    r = c.post("/api/change-password", json={"current_password": "pw", "new_password": "N3w-secret!"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # old password no longer works, new one does
    _, old = client_factory("cse", password="pw")
    assert old.status_code == 401
    _, new = client_factory("cse", password="N3w-secret!")
    assert new.status_code == 200
    # stored hashed, never in plain text
    raw = auth.CREDENTIALS_FILE.read_text()
    assert "N3w-secret!" not in raw and "scrypt" not in raw and '"hash"' in raw


def test_wrong_current_password_rejected(client_factory):
    c, _ = client_factory("cse")
    r = c.post("/api/change-password", json={"current_password": "nope", "new_password": "N3w-secret!"})
    assert r.status_code == 400
    assert not auth.CREDENTIALS_FILE.exists()
    _, still = client_factory("cse", password="pw")
    assert still.status_code == 200


def test_weak_or_unchanged_password_rejected(client_factory):
    c, _ = client_factory("cse")
    assert c.post("/api/change-password", json={"current_password": "pw", "new_password": "short"}).status_code == 400
    assert c.post("/api/change-password", json={"current_password": "pw", "new_password": "pw"}).status_code == 400


def test_requires_login(client_factory):
    c, _ = client_factory("cse")
    c.post("/api/logout")
    r = c.post("/api/change-password", json={"current_password": "pw", "new_password": "N3w-secret!"})
    assert r.status_code == 401


def test_password_change_is_per_department(client_factory):
    c, _ = client_factory("cse")
    c.post("/api/change-password", json={"current_password": "pw", "new_password": "N3w-secret!"})
    other, r = client_factory("civil", password="pw")   # another department still uses the env password
    assert r.status_code == 200


def test_other_sessions_signed_out(client_factory):
    a, _ = client_factory("cse")
    b, _ = client_factory("cse")
    assert b.get("/api/config").status_code == 200
    a.post("/api/change-password", json={"current_password": "pw", "new_password": "N3w-secret!"})
    assert a.get("/api/config").status_code == 200       # the session that changed it stays in
    assert b.get("/api/config").status_code == 401       # the other one is gone


# ---------------------------------------------------------------- downloads
def _generate(c):
    c.put("/api/courses", json=COURSES)
    r = c.post("/api/generate", json={"time_limit": 10, "workers": 2, "balance": False})
    assert r.status_code == 200 and r.json()["stats"]["placed"] > 0


def _sheets(resp):
    return openpyxl.load_workbook(io.BytesIO(resp.content)).sheetnames


def test_download_views(client_factory):
    c, _ = client_factory("cse")
    _generate(c)
    assert _sheets(c.get("/api/schedule/download")) == ["Master", "Faculty_Load", "Section_Wise", "Teacher_Wise", "Room_Wise"]
    assert _sheets(c.get("/api/schedule/download?view=sections")) == ["Section_Wise"]
    assert _sheets(c.get("/api/schedule/download?view=teachers")) == ["Teacher_Wise"]
    assert _sheets(c.get("/api/schedule/download?view=rooms")) == ["Room_Wise"]
    assert c.get("/api/schedule/download?view=bogus").status_code == 400


def test_grid_sheet_has_one_block_per_entity(client_factory):
    c, _ = client_factory("cse")
    _generate(c)
    ws = openpyxl.load_workbook(io.BytesIO(c.get("/api/schedule/download?view=teachers").content))["Teacher_Wise"]
    titles = [row[0].value for row in ws.iter_rows(min_col=1, max_col=1) if row[0].value in ("Dr A", "Dr B")]
    assert titles == ["Dr A", "Dr B"]
