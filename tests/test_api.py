"""Behaviour tests. Run against a throw-away database:  python -m pytest tests -q   (needs a local MongoDB)."""
import os
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

os.environ["MONGO_DB"] = "attendance_test"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ms(y, mo, d, h, mi, s=0, micro=0):
    return int(round(datetime(y, mo, d, h, mi, s, micro, tzinfo=IST).timestamp() * 1000))


@pytest.fixture(autouse=True)
def clean():
    main.employees.delete_many({})
    main.logs.delete_many({})
    main.ensure_indexes()
    yield


@pytest.fixture()
def c():
    with TestClient(main.app) as client:
        yield client


def emp(c, code="EMP0001", dept="Eng", start="09:30", end="18:30", joined="2026-01-05"):
    r = c.post("/employees", json={"emp_code": code, "name": code, "email": f"{code}@x.com", "department": dept,
                                    "shift_start": start, "shift_end": end, "joined_on": joined})
    assert r.status_code == 201, r.text
    return r.json()


def seed_log(code, d, status="PRESENT", late=0, ot=0, wh=None, half=False, pin=None, pout=None):
    main.logs.insert_one({"emp_code": code, "date": d, "status": status, "punch_in": pin, "punch_out": pout,
                          "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half,
                          "history": []})


# ------------------------------------------------------------------ employees
def test_health(c):
    assert c.get("/health").json() == {"status": "ok"}


def test_employee_crud_and_validation(c):
    e = emp(c)
    assert isinstance(e["created_at"], int) and "_id" not in e
    assert c.post("/employees", json={**e, "email": "a@b.co", "name": "x"}).status_code == 409
    bad = {"emp_code": "EMP0002", "name": "n", "email": "a@b.co", "department": "d", "joined_on": "2026-01-01"}
    assert c.post("/employees", json={**bad, "emp_code": "X1"}).status_code == 422
    assert c.post("/employees", json={**bad, "joined_on": "2026-02-30"}).status_code == 422
    assert c.post("/employees", json={**bad, "shift_start": "09:30", "shift_end": "09:30"}).status_code == 422
    assert c.post("/employees", json={**bad, "shift_start": "9:30"}).status_code == 422
    for i in range(1, 6):
        emp(c, f"EMP000{i + 1}", dept="HR" if i % 2 else "Eng")
    r = c.get("/employees", params={"department": "HR", "page_size": 2}).json()
    assert r["total"] == 3 and len(r["items"]) == 2 and r["page"] == 1
    assert [x["emp_code"] for x in r["items"]] == sorted(x["emp_code"] for x in r["items"])
    assert c.get("/employees", params={"page": 0}).status_code == 422
    assert c.get("/employees", params={"page_size": 101}).status_code == 422
    assert c.get("/employees", params={"page": 2, "page_size": 2}).json()["items"][0]["emp_code"] == "EMP0003"


def test_concurrent_employee_create(c):
    body = {"emp_code": "EMP0009", "name": "n", "email": "a@b.co", "department": "d", "joined_on": "2026-01-01"}
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: c.post("/employees", json=body).status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9


# ------------------------------------------------------------------ punch-in (R1, R2)
@pytest.mark.parametrize("h,m,s,micro,late", [
    (9, 40, 0, 0, 0), (9, 40, 0, 900_000, 0), (9, 40, 1, 0, 10), (10, 15, 59, 0, 45), (9, 0, 0, 0, 0), (9, 30, 0, 0, 0),
])
def test_late_rule(c, h, m, s, micro, late):
    emp(c)
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, h, m, s, micro)})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["late_minutes"] == late and j["date"] == "2026-07-06"
    assert j["punch_in"] % 1000 == 0  # truncated to seconds
    assert j["punch_out"] is None and j["work_hours"] is None and j["history"] == [] and j["half_day"] is False


def test_punch_in_errors(c):
    emp(c)
    assert c.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    for bad in (1783312500, 1783312500000.5, "1783312500000", True, 5_000_000_000_000):
        assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "WFH",
                                                 "punched_at": ms(2026, 7, 6, 9, 0)}).status_code == 201
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001",
                                                 "punched_at": ms(2026, 7, 6, 11, 0)}).status_code == 409


def test_punch_in_ist_date_boundary(c):
    emp(c)  # 00:30 IST on the 7th is still the 6th in UTC; the record's date must be the IST date
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 0, 30)})
    assert r.json()["date"] == "2026-07-07"


def test_concurrent_punch_in(c):
    emp(c)
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda _: c.post("/attendance/punch-in", json=body).status_code, range(12)))
    assert sorted(codes) == [201] + [409] * 11
    assert main.logs.count_documents({}) == 1


def test_overnight_shift(c):
    emp(c, "EMP0005", start="22:00", end="06:00")
    # 21:55 on the 6th: date = 6th, 5 minutes early -> not late
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)}).json()
    assert r["date"] == "2026-07-06" and r["late_minutes"] == 0
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["overtime_minutes"] == 40 and j["work_hours"] == 8.75 and j["date"] == "2026-07-06"
    # an early-morning punch-in (before shift_end) belongs to the previous day
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 9, 2, 0)}).json()
    assert r["date"] == "2026-07-08" and r["late_minutes"] == 240
    # exactly at shift_end it is a new day
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 12, 6, 0)}).json()
    assert r["date"] == "2026-07-12"


# ------------------------------------------------------------------ punch-out (R3 - R5)
def test_punch_out_rules(c):
    emp(c)
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    out = lambda *a: c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, *a)})
    assert out(9, 30).status_code == 422  # == punch_in
    assert c.post("/attendance/punch-out", json={"emp_code": "NOPE"}).status_code == 404
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                                                  "punched_at": ms(2026, 7, 7, 9, 31)}).status_code == 422  # >24h
    r = out(19, 0)  # 30 min after 18:30 -> counts
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 30 and r.json()["work_hours"] == 9.5
    assert out(20, 0).status_code == 409


def test_overtime_threshold_and_half_day(c):
    emp(c)
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 59, 59)}).json()
    assert r["overtime_minutes"] == 0  # 29m59s
    emp(c, "EMP0002")
    c.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ms(2026, 7, 6, 9, 0)})
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms(2026, 7, 6, 13, 29, 45)}).json()
    assert r["work_hours"] == 4.5 and r["half_day"] is False  # 4.4958 rounds half-up to 4.50, which is not < 4.50
    emp(c, "EMP0003")
    c.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": ms(2026, 7, 6, 9, 0)})
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": ms(2026, 7, 6, 13, 29, 0)}).json()
    assert r["work_hours"] == 4.48 and r["half_day"] is True


def test_work_hours_half_up():
    pin = datetime(2026, 7, 6, 4, tzinfo=timezone.utc)
    # 0.005 h = 18 s exactly -> 0.01 (banker's rounding / float rounding gives 0.0 or 0.01 inconsistently)
    assert main.compute_work_hours(pin, pin + timedelta(seconds=18)) == Decimal("0.01")
    assert main.compute_work_hours(pin, pin + timedelta(seconds=9 * 3600 + 27)) == Decimal("9.01")
    assert main.half_up(2.675, 2) == 2.68 and main.half_up(0.125, 2) == 0.13


def test_concurrent_punch_out(c):
    emp(c)
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda _: c.post("/attendance/punch-out", json=body).status_code, range(12)))
    assert sorted(codes) == [200] + [409] * 11


# ------------------------------------------------------------------ list
def test_attendance_list(c):
    emp(c)
    emp(c, "EMP0002")
    for d in range(1, 8):
        for code in ("EMP0001", "EMP0002"):
            seed_log(code, f"2026-07-0{d}", status="WFH" if d == 3 else "PRESENT")
    r = c.get("/attendance", params={"page_size": 5}).json()
    assert r["total"] == 14 and len(r["items"]) == 5
    assert [(i["date"], i["emp_code"]) for i in r["items"]][:2] == [("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002")]
    r = c.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-02", "date_to": "2026-07-04"}).json()
    assert r["total"] == 3 and [i["date"] for i in r["items"]] == ["2026-07-04", "2026-07-03", "2026-07-02"]
    assert c.get("/attendance", params={"status": "WFH"}).json()["total"] == 2
    assert c.get("/attendance", params={"date_from": "2026-07-05", "date_to": "2026-07-01"}).status_code == 422
    assert c.get("/attendance", params={"date_from": "07/01/2026"}).status_code == 422
    assert c.get("/attendance", params={"status": "BOGUS"}).status_code == 422
    assert "id" not in r["items"][0] and "_id" not in r["items"][0]


def test_legacy_record_defaults(c):
    emp(c)
    main.logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                          "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                          "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc), "work_hours": 8.98,
                          "late_minutes": 0, "overtime_minutes": 0})
    i = c.get("/attendance").json()["items"][0]
    assert i["history"] == [] and i["half_day"] is False
    r = c.patch("/attendance/EMP0001/2026-07-14", json={"punch_out": ms(2026, 7, 14, 17, 0),
                                                         "reason": "fixing", "regularized_by": "hr"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1


# ------------------------------------------------------------------ regularize
def test_regularize(c):
    emp(c)
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10)})
    p = lambda body: c.patch("/attendance/EMP0001/2026-07-06", json={"reason": "biometric glitch",
                                                                    "regularized_by": "hr", **body})
    r = p({"punch_in": ms(2026, 7, 6, 9, 28)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.7 and j["overtime_minutes"] == 40
    h = j["history"][0]
    assert h["by"] == "hr" and isinstance(h["at"], int)
    assert set(h["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert h["changes"]["punch_in"] == {"from": ms(2026, 7, 6, 10, 5), "to": ms(2026, 7, 6, 9, 28)}
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert p({"punch_in": ms(2026, 7, 6, 9, 28)}).status_code == 422  # nothing changes
    assert p({"status": "LEAVE", "punch_in": ms(2026, 7, 6, 9, 0)}).status_code == 422
    assert p({"punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422  # leaves the attendance date
    assert p({"punch_out": ms(2026, 7, 7, 9, 29)}).status_code == 422  # >24h
    assert p({"punch_out": ms(2026, 7, 6, 9, 28)}).status_code == 422  # not after punch_in
    assert c.patch("/attendance/EMP0001/2026-07-06", json={"status": "LEAVE", "reason": "x", "regularized_by": "h"}
                   ).status_code == 422  # reason too short
    r = p({"status": "LEAVE"}).json()
    assert r["punch_in"] is None and r["punch_out"] is None and r["work_hours"] is None
    assert r["overtime_minutes"] == 0 and r["late_minutes"] == 0 and len(r["history"]) == 2
    assert set(r["history"][1]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "overtime_minutes"}
    assert p({"status": "PRESENT"}).status_code == 422  # needs a punch_in
    assert p({"status": "PRESENT", "punch_in": ms(2026, 7, 6, 9, 30)}).status_code == 200
    assert c.patch("/attendance/EMP0001/2026-07-09", json={"status": "LEAVE", "reason": "reason", "regularized_by": "h"}
                   ).status_code == 404
    assert c.patch("/attendance/NOPE/2026-07-06", json={"status": "LEAVE", "reason": "reason", "regularized_by": "h"}
                   ).status_code == 404
    assert c.patch("/attendance/EMP0001/bad", json={"status": "LEAVE", "reason": "reason", "regularized_by": "h"}
                   ).status_code == 422


def test_concurrent_regularize_keeps_history(c):
    emp(c)
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})

    def patch(i):
        return c.patch("/attendance/EMP0001/2026-07-06", json={"punch_in": ms(2026, 7, 6, 8, 0, i + 1),
                                                                "reason": "concurrent",
                                                                "regularized_by": f"u{i}"}).status_code
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(patch, range(8)))
    ok = codes.count(200)
    assert set(codes) <= {200, 409} and ok >= 1
    assert len(main.logs.find_one({})["history"]) == ok  # every success has its entry, none was lost


# ------------------------------------------------------------------ analytics
def test_employee_monthly(c):
    emp(c, joined="2026-07-08")  # joins on a Wednesday
    seed_log("EMP0001", "2026-07-08", late=20, ot=30, wh=9, pin=1, pout=1)          # Wed
    seed_log("EMP0001", "2026-07-09", status="WFH", wh=3.5, half=True)               # Thu, half day
    seed_log("EMP0001", "2026-07-10", status="LEAVE")                                 # Fri
    seed_log("EMP0001", "2026-07-11", status="ON_DUTY", late=15, ot=45, wh=5)        # Sat: only late/overtime count
    seed_log("EMP0001", "2026-06-30", late=99)                                        # other month
    r = c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    # Wed 8 .. Fri 31: 3 + 5*3 + 5(27..31 -> Mon-Fri) ... computed independently below
    expected_days = sum(1 for d in range(8, 32) if date(2026, 7, d).weekday() < 5)
    assert r == {"emp_code": "EMP0001", "month": "2026-07", "working_days": expected_days, "present_days": 1.5,
                 "leave_days": 1, "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 75,
                 "attendance_pct": float((Decimal("1.5") / expected_days * 100).quantize(Decimal("0.01"),
                                                                                       ROUND_HALF_UP))}
    assert c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-08"}).json()["present_days"] == 0
    assert c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-06"}).json()["attendance_pct"] is None
    assert c.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    assert c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422


def test_department_summary(c):
    emp(c, "EMP0001", "Eng")
    emp(c, "EMP0002", "Eng")                      # no logs, still counted
    emp(c, "EMP0003", "Eng", joined="2026-08-01")  # joins later: not counted for July
    emp(c, "EMP0004", "Ops", joined="2026-08-01")  # department with headcount 0 in July: omitted
    emp(c, "EMP0005", "HR")
    seed_log("EMP0001", "2026-07-06", wh=8.0, late=12)
    seed_log("EMP0001", "2026-07-07", wh=9.01)
    seed_log("EMP0001", "2026-07-08", status="ON_DUTY")               # no hours: not in the average
    seed_log("EMP0001", "2026-07-11", status="WFH", wh=5.0)           # Saturday: in the average, not in present_days
    seed_log("EMP0005", "2026-07-06", status="LEAVE")
    seed_log("EMP0005", "2026-07-07", status="ABSENT", wh=7.0)        # not present: not in the average
    seed_log("EMP0005", "2026-07-08", wh=3.5, half=True, late=40)
    r = c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert [i["department"] for i in r["items"]] == ["Eng", "HR"]
    eng, hr = r["items"]
    assert eng == {"department": "Eng", "headcount": 2, "present_days": 3.0, "avg_work_hours": 7.34, "late_count": 1,
                   "total_late_minutes": 12, "leave_count": 0, "on_duty_count": 1}  # (8+9.01+5)/3 = 7.3367
    assert hr["headcount"] == 1 and hr["present_days"] == 0.5 and hr["avg_work_hours"] == 3.5
    assert hr["leave_count"] == 1 and hr["late_count"] == 1
    r = c.get("/analytics/departments/summary", params={"month": "2026-08", "department": "Ops"}).json()
    assert r["items"][0]["headcount"] == 1 and r["items"][0]["avg_work_hours"] is None


def test_leaderboard_ties(c):
    for i, (code, dept) in enumerate([("EMP0001", "A"), ("EMP0002", "A"), ("EMP0003", "B"), ("EMP0004", "B"),
                                      ("EMP0005", "A"), ("EMP0006", "A")]):
        emp(c, code, dept)
    seed_log("EMP0001", "2026-07-06", late=50)
    seed_log("EMP0002", "2026-07-06", late=30)
    seed_log("EMP0003", "2026-07-06", late=30)
    seed_log("EMP0004", "2026-07-06", late=30)
    seed_log("EMP0005", "2026-07-06", late=10)
    seed_log("EMP0006", "2026-07-06", late=0)                      # total 0: not listed
    seed_log("GHOST", "2026-07-06", late=999)                      # unknown employee: ignored
    seed_log("EMP0001", "2026-08-01", late=500)                    # other month
    g = lambda **p: c.get("/analytics/leaderboard/late", params={"month": "2026-07", **p}).json()["items"]
    rows = g(limit=2)
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0003"), (2, "EMP0004")]
    assert [r["rank"] for r in g(limit=10)] == [1, 2, 2, 2, 5]
    assert [r["rank"] for r in g(limit=1)] == [1]
    rows = g(department="B")
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP0003"), (1, "EMP0004")]
    assert set(rows[0]) == {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


def test_department_trend(c):
    emp(c, "EMP0001", "Eng", joined="2026-07-01")
    emp(c, "EMP0002", "Eng", joined="2026-07-03")
    seed_log("EMP0001", "2026-07-01", late=5)
    seed_log("EMP0001", "2026-07-02")
    seed_log("EMP0001", "2026-07-04", status="ON_DUTY")               # Saturday
    seed_log("EMP0002", "2026-07-03", half=True)
    seed_log("EMP0002", "2026-07-06", status="ABSENT")
    r = c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-07-08"}).json()
    rows = r["items"]
    assert [x["date"] for x in rows] == [f"2026-07-0{d}" for d in range(1, 9)]
    by = {x["date"]: x for x in rows}
    assert by["2026-07-01"] == {"date": "2026-07-01", "is_working_day": True, "headcount": 1, "present_count": 1.0,
                                "late_count": 1, "attendance_rate": 1.0, "moving_avg_7d": 1.0}
    assert by["2026-07-03"]["headcount"] == 2 and by["2026-07-03"]["present_count"] == 0.5
    assert by["2026-07-03"]["attendance_rate"] == 0.25
    assert by["2026-07-04"]["is_working_day"] is False and by["2026-07-04"]["attendance_rate"] is None
    assert by["2026-07-04"]["present_count"] == 1.0  # weekend presence is still reported
    assert by["2026-07-06"]["attendance_rate"] == 0.0 and by["2026-07-07"]["attendance_rate"] == 0.0
    # rates: 07-01 1.0 (head 1), 07-02 1.0 (head 1), 07-03 0.25 (head 2, one half day) -> avg 0.75
    assert by["2026-07-02"]["attendance_rate"] == 1.0 and by["2026-07-02"]["moving_avg_7d"] == 1.0
    assert by["2026-07-03"]["moving_avg_7d"] == 0.75
    assert by["2026-07-04"]["moving_avg_7d"] == 0.75  # weekend row: window average of earlier non-null rates
    assert by["2026-07-06"]["moving_avg_7d"] == 0.5625  # (1 + 1 + .25 + 0) / 4
    assert c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-08"}
                 ).status_code == 404
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-08", "to": "2026-07-01"}
                 ).status_code == 422
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-06-01"}
                 ).status_code == 422
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01"}).status_code == 422
    assert len(c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-09-30"}
                     ).json()["items"]) == 92
    # a range with no data at all still returns one row per day
    r = c.get("/analytics/departments/Eng/trend", params={"from": "2030-01-01", "to": "2030-01-10"}).json()
    assert len(r["items"]) == 10 and r["items"][0]["attendance_rate"] == 0.0


def test_explain_uses_indexes(c):
    emp(c)
    seed_log("EMP0001", "2026-07-06", late=5)
    cases = {
        "attendance_list": {}, "attendance_list ": {"emp_code": "EMP0001", "date_from": "2026-07-01"},
        "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
        "department_summary": {"month": "2026-07"},
        "late_leaderboard": {"month": "2026-07"},
        "department_trend": {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"},
    }
    for name, params in cases.items():
        r = c.get(f"/admin/explain/{name.strip()}", params=params)
        assert r.status_code == 200, (name, r.text)
        text = r.text
        assert "IXSCAN" in text and "COLLSCAN" not in text, name
    assert c.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
    assert c.get("/admin/explain/nope").status_code == 422


# ------------------------------------------------------------------ randomized cross-check of the analytics
def test_analytics_match_python_reference(c):
    rnd = random.Random(7)
    depts = ["Eng", "Ops", "HR"]
    codes = [f"EMP{i:04d}" for i in range(1, 25)]
    info = {}
    for code in codes:
        joined = date(2026, 6, 20) + timedelta(days=rnd.randint(0, 25))
        info[code] = (rnd.choice(depts), joined)
        emp(c, code, info[code][0], joined=joined.isoformat())
    docs = []
    for code in codes:
        for off in range(0, 40):
            d = date(2026, 6, 25) + timedelta(days=off)
            if rnd.random() < 0.35:
                continue
            st = rnd.choice(["PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"])
            pres = st in ("PRESENT", "WFH", "ON_DUTY")
            wh = round(rnd.uniform(2, 11), 2) if pres and rnd.random() < 0.85 else None
            docs.append({"emp_code": code, "date": d.isoformat(), "status": st, "punch_in": None, "punch_out": None,
                         "work_hours": wh, "late_minutes": rnd.choice([0, 0, 11, 25, 25, 60]) if pres else 0,
                         "overtime_minutes": rnd.choice([0, 30, 75]), "half_day": bool(wh and wh < 4.5)})
    main.logs.insert_many(docs)
    month_first, month_last = date(2026, 7, 1), date(2026, 7, 31)

    def in_month(d):
        return "2026-07-01" <= d["date"] <= "2026-07-31"

    # department summary
    got = {i["department"]: i for i in c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]}
    for dept in depts:
        members = [k for k, (dp, j) in info.items() if dp == dept and j <= month_last]
        recs = [d for d in docs if d["emp_code"] in members and in_month(d)]
        pres = [d for d in recs if d["status"] in ("PRESENT", "WFH", "ON_DUTY")]
        present_days = sum((0.5 if d["half_day"] else 1) for d in pres if date.fromisoformat(d["date"]).weekday() < 5)
        hrs = [Decimal(str(d["work_hours"])) for d in pres if d["work_hours"] is not None]
        exp_avg = float((sum(hrs) / len(hrs)).quantize(Decimal("0.01"), ROUND_HALF_UP)) if hrs else None
        g = got[dept]
        assert g["headcount"] == len(members)
        assert g["present_days"] == present_days and g["avg_work_hours"] == exp_avg
        assert g["late_count"] == sum(1 for d in recs if d["late_minutes"] > 0)
        assert g["total_late_minutes"] == sum(d["late_minutes"] for d in recs)
        assert g["leave_count"] == sum(1 for d in recs if d["status"] == "LEAVE")
        assert g["on_duty_count"] == sum(1 for d in recs if d["status"] == "ON_DUTY")

    # leaderboard
    totals = {}
    for d in docs:
        if in_month(d) and d["late_minutes"] > 0:
            totals[d["emp_code"]] = totals.get(d["emp_code"], 0) + d["late_minutes"]
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ranks, expected = {}, []
    for idx, (code, tot) in enumerate(ordered):
        ranks[tot] = ranks.get(tot, idx + 1)
        expected.append((ranks[tot], code, tot))
    for limit in (1, 3, 10):
        rows = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit}).json()["items"]
        assert [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in rows] == [e for e in expected if e[0] <= limit]

    # monthly + trend for a department
    for code in codes[:6]:
        r = c.get(f"/analytics/employees/{code}/monthly", params={"month": "2026-07"}).json()
        recs = [d for d in docs if d["emp_code"] == code and in_month(d)]
        wd = sum(1 for k in range(31) if (month_first + timedelta(days=k)).weekday() < 5
                 and month_first + timedelta(days=k) >= info[code][1])
        pd_ = sum((0.5 if d["half_day"] else 1) for d in recs if d["status"] in ("PRESENT", "WFH", "ON_DUTY")
                  and date.fromisoformat(d["date"]).weekday() < 5)
        assert r["working_days"] == wd and r["present_days"] == pd_
        assert r["total_overtime_minutes"] == sum(d["overtime_minutes"] for d in recs)

    t = c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-07-20"}).json()["items"]
    rates = []
    members = [k for k, (dp, j) in info.items() if dp == "Eng"]
    for k, row in enumerate(t):
        d = date(2026, 7, 1) + timedelta(days=k)
        head = sum(1 for m in members if info[m][1] <= d)
        pres = sum((0.5 if x["half_day"] else 1) for x in docs if x["emp_code"] in members and x["date"] == d.isoformat()
                   and x["status"] in ("PRESENT", "WFH", "ON_DUTY"))
        rate = pres / head if d.weekday() < 5 and head else None
        rates.append(rate)
        window = [x for x in rates[max(0, k - 6):k + 1] if x is not None]
        assert row["date"] == d.isoformat() and row["headcount"] == head and row["present_count"] == pres
        assert row["attendance_rate"] == (None if rate is None else float(Decimal(str(rate)).quantize(
            Decimal("0.0001"), ROUND_HALF_UP)))
        exp_ma = None if not window else float((Decimal(str(sum(window))) / len(window)).quantize(
            Decimal("0.0001"), ROUND_HALF_UP))
        assert row["moving_avg_7d"] == pytest.approx(exp_ma, abs=1e-4) if exp_ma is not None else row["moving_avg_7d"] is None
