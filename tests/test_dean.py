"""Dean's Office portal: separate login, walled-off from department routes,
and cross-department faculty availability."""

from fastapi.testclient import TestClient

from backend import app as app_module
from backend import dean


def _dean(client_factory):
    c, r = client_factory("", password="dean_admin123", username="dean")
    assert r.status_code == 200, r.text
    return c


def _row(faculty, code, programme, theory=3):
    return {"faculty": faculty, "nature": "Regular", "course_code": code, "course_name": f"Course {code}",
            "programme": programme, "semester": "1", "theory_hrs": theory, "tutorial_hrs": 0, "practical_hrs": 0}


def _generate(client, rows, rooms=None):
    if rooms:
        cfg = client.get("/api/config").json()["config"]
        cfg["rooms"], cfg["lab_rooms"] = rooms, rooms[-1:]
        assert client.put("/api/config", json=cfg).status_code == 200
    assert client.put("/api/courses", json=rows).status_code == 200
    r = client.post("/api/generate", json={"time_limit": 5, "workers": 2, "balance": False})
    assert r.status_code == 200 and r.json()["stats"]["unplaced"] == 0
    return r.json()


# ------------------------------------------------------------- login & walls
def test_dean_login_needs_no_department(client_factory):
    c, r = client_factory("", password="dean_admin123", username="dean")
    assert r.status_code == 200
    assert r.json()["role"] == "dean" and r.json()["redirect"] == "/dean.html"
    me = c.get("/api/me").json()
    assert me["authenticated"] and me["role"] == "dean" and me["department"] is None
    # username is case-insensitive, password is not; wrong password rejected
    assert client_factory("", password="dean_admin123", username="Dean")[1].status_code == 200
    assert client_factory("", password="nope", username="dean")[1].status_code == 401


def test_department_login_still_needs_department(client_factory):
    assert client_factory("", password="pw", username="admin")[1].status_code == 400


def test_portals_are_walled_off(client_factory):
    d = _dean(client_factory)
    assert d.get("/api/config").status_code == 403           # dean can't use department routes
    assert d.get("/api/courses").status_code == 403
    cse, _ = client_factory("cse")
    assert cse.get("/api/dean/overview").status_code == 403  # departments can't use dean routes
    assert TestClient(app_module.app).get("/api/dean/overview").status_code == 401


def test_page_routing(client_factory):
    d = _dean(client_factory)
    r = d.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"].endswith("/dean.html")
    assert d.get("/dean.html", follow_redirects=False).status_code == 200
    cse, _ = client_factory("cse")
    r = cse.get("/dean.html", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/"
    r = TestClient(app_module.app).get("/dean.html", follow_redirects=False)
    assert r.status_code in (302, 307) and "login.html" in r.headers["location"]


# ------------------------------------------------------- timetable snapshots
def test_dean_loads_each_departments_timetable(client_factory):
    civil, _ = client_factory("civil")
    d = _dean(client_factory)
    ov = {x["id"]: x for x in d.get("/api/dean/overview").json()["departments"]}
    assert ov["civil"]["has_timetable"] is False
    assert d.get("/api/dean/timetable/civil").status_code == 404

    _generate(civil, [_row("Dr A", "CE-1", "B.Tech Civil A")], rooms=["C1", "C2"])
    ov = {x["id"]: x for x in d.get("/api/dean/overview").json()["departments"]}
    assert ov["civil"]["has_timetable"] and ov["civil"]["placed"] == 3
    snap = d.get("/api/dean/timetable/civil").json()
    assert len(snap["placed"]) == 3 and snap["department"]["id"] == "civil" and snap["generated_at"]
    assert d.get("/api/dean/timetable/nope").status_code == 404

    civil.delete("/api/schedule")                             # reset board removes it from the dean's view
    assert d.get("/api/dean/timetable/civil").status_code == 404


def test_manual_edit_updates_dean_snapshot(client_factory):
    civil, _ = client_factory("civil")
    _generate(civil, [_row("Dr A", "CE-1", "B.Tech Civil A")], rooms=["C1", "C2"])
    d = _dean(client_factory)
    first = d.get("/api/dean/timetable/civil").json()["placed"][0]
    assert civil.post("/api/schedule/unplace", json={"session_id": first["id"]}).status_code == 200
    assert len(d.get("/api/dean/timetable/civil").json()["placed"]) == 2


# ------------------------------------------------------------- availability
def _setup_two_depts(client_factory):
    civil, _ = client_factory("civil")
    cse, _ = client_factory("cse")
    _generate(civil, [_row("Dr. Rahul Hans", "CE-1", "B.Tech Civil A"), _row("Dr Meera", "CE-2", "B.Tech Civil A")], rooms=["C1", "C2"])
    _generate(cse, [_row("Rahul Hans", "CS-1", "B.Tech CSE A"), _row("Dr Sanjay", "CS-2", "B.Tech CSE A")])
    return _dean(client_factory)


def test_same_person_in_two_departments_is_one_faculty(client_factory):
    d = _setup_two_depts(client_factory)
    fac = {f["key"]: f for f in d.get("/api/dean/faculty").json()["faculty"]}
    assert set(fac) >= {"rahul hans", "meera", "sanjay"}
    assert {x["id"] for x in fac["rahul hans"]["departments"]} == {"civil", "cse"}
    assert fac["rahul hans"]["sessions"] == 6


def _clock_of(snap, placed):
    pw = dean._period_windows(snap["config"])
    return pw[placed["start_period"]][0], pw[placed["start_period"] + placed["duration"] - 1][1]


def test_busy_shows_where_the_faculty_is(client_factory):
    d = _setup_two_depts(client_factory)
    snap = d.get("/api/dean/timetable/civil").json()
    cls = next(p for p in snap["placed"] if dean.normalize_name(p["teacher"]) == "rahul hans")
    s, e = _clock_of(snap, cls)
    r = d.post("/api/dean/availability", json={"faculty": ["rahul hans"], "day": cls["day_label"], "start": dean.hhmm(s)})
    p = r.json()["at_time"]["people"][0]
    assert p["free"] is False and r.json()["at_time"]["all_free"] is False
    here = next(b for b in p["busy"] if b["dept_id"] == "civil")
    assert here["room"] == cls["room"] and here["course_code"] == "CE-1" and here["start"] == dean.hhmm(s)
    assert dean.parse_hhmm(p["free_from"]) >= e


def test_free_when_no_class_and_next_class(client_factory):
    d = _setup_two_depts(client_factory)
    r = d.post("/api/dean/availability", json={"faculty": ["rahul hans", "meera"], "day": "Sunday", "start": "10:00", "end": "11:00"})
    at = r.json()["at_time"]
    assert at["all_free"] is True and at["busy_count"] == 0
    assert all(p["free"] and p["next_class"] is None for p in at["people"])   # nothing on Sundays


def test_group_grid_and_suggested_windows(client_factory):
    d = _setup_two_depts(client_factory)
    r = d.post("/api/dean/availability", json={"faculty": ["rahul hans", "meera", "sanjay"]}).json()
    assert r["at_time"] is None and r["has_data"] and r["slots"] and r["days"]
    # every busy cell names who is busy and where; suggested windows only cover all-free cells
    for day, cells in r["grid"].items():
        free_starts = {c["start"] for c in cells if c["all_free"]}
        for c in cells:
            assert c["all_free"] == (not c["busy"])
            for b in c["busy"]:
                assert b["name"] and b["room"]
        for a, b in r["suggested"][day]:
            assert any(a <= c["start"] and c["end"] <= b for c in cells if c["all_free"])
    # 3 faculty x ~3 periods each can't fill a 6-day week: some common free time must exist
    assert any(r["suggested"].values())


def test_availability_validation(client_factory):
    d = _setup_two_depts(client_factory)
    ok = {"faculty": ["meera"], "day": "Monday", "start": "10:00"}
    assert d.post("/api/dean/availability", json={**ok, "faculty": ["ghost"]}).status_code == 404
    assert d.post("/api/dean/availability", json={"faculty": ["meera"], "start": "10:00"}).status_code == 400   # no day
    assert d.post("/api/dean/availability", json={**ok, "end": "09:00"}).status_code == 400
    assert d.post("/api/dean/availability", json={**ok, "start": "25:99"}).status_code == 400
    assert d.post("/api/dean/availability", json={"faculty": []}).status_code == 422


def test_label_and_name_helpers():
    assert dean.parse_label("9:30-10:30") == (570, 630)
    assert dean.parse_label("12:30-1:30") == (750, 810)
    assert dean.parse_label("3:30-4:30") == (930, 990)
    assert dean.parse_label("Lunch") is None
    assert dean.normalize_name("Dr.  Rahul Hans") == dean.normalize_name("prof.rahul hans") == "rahul hans"
    assert dean.normalize_name("Drew Smith") == "drew smith"


# ------------------------------------------------ nothing stale is ever shown
def test_responses_are_never_cached(client_factory):
    d = _dean(client_factory)
    assert d.get("/api/dean/overview").headers["cache-control"] == "no-store"
    assert d.get("/api/dean/faculty").headers["cache-control"] == "no-store"
    assert d.get("/dean.html").headers["cache-control"] == "no-store"
    cse, _ = client_factory("cse")
    assert cse.get("/api/config").headers["cache-control"] == "no-store"


def test_dean_never_sees_a_timetable_that_no_longer_matches_the_department(client_factory):
    civil, _ = client_factory("civil")
    d = _dean(client_factory)

    def has_tt():
        return {x["id"]: x for x in d.get("/api/dean/overview").json()["departments"]}["civil"]["has_timetable"]

    rows = [_row("Dr A", "CE-1", "B.Tech Civil A")]
    _generate(civil, rows, rooms=["C1", "C2"])
    assert has_tt()

    civil.put("/api/courses", json=rows + [_row("Dr B", "CE-2", "B.Tech Civil A")])   # register edited -> old timetable is dropped
    assert not has_tt() and d.get("/api/dean/timetable/civil").status_code == 404
    assert civil.post("/api/generate", json={"time_limit": 5, "workers": 2, "balance": False}).status_code == 200
    assert has_tt()                                                                    # regenerated -> visible again

    cfg = civil.get("/api/config").json()["config"]
    cfg["rooms"] = ["C1", "C2", "C3"]
    civil.put("/api/config", json=cfg)                                                 # rooms changed -> dropped again
    assert not has_tt()
