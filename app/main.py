"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Everything lives in this one file:
  1. configuration + indexes      4. models
  2. time / rounding helpers      5. endpoints (employees, attendance)
  3. aggregation fragments        6. analytics + explain
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns aware UTC datetimes instead of naive ones, so conversions never depend on the
# machine's local time zone.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
EMP_CODE_RE = r"^EMP\d{4,6}$"

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MAX_SHIFT_SPAN = timedelta(hours=24)
MAX_TREND_DAYS = 92


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
INDEXES = [
    # (collection, keys, options)
    # Guarantees one record per (emp_code, date): punch-in race safety. Also serves per-employee lookups, the
    # monthly summary and the lookups inside the department pipelines.
    (logs, [("emp_code", 1), ("date", 1)], {"name": "emp_code_date_uq", "unique": True}),
    # Default list order (date desc, emp_code asc) and month-wide scans (leaderboard).
    (logs, [("date", -1), ("emp_code", 1)], {"name": "date_emp_code"}),
    # Status filter + the same sort.
    (logs, [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp_code"}),
    # Punch-out: most recent record of an employee whose punch_in <= punched_at.
    (logs, [("emp_code", 1), ("punch_in", -1)], {"name": "emp_code_punch_in"}),
    (employees, [("emp_code", 1)], {"name": "emp_code_uq", "unique": True}),
    # Department filter + emp_code order (list), department pipelines.
    (employees, [("department", 1), ("emp_code", 1)], {"name": "department_emp_code"}),
    # Headcount (R9): joined_on <= end of period, without a department filter.
    (employees, [("joined_on", 1)], {"name": "joined_on"}),
]


def ensure_indexes() -> None:
    for coll, keys, opts in INDEXES:
        try:
            coll.create_index(keys, **opts)  # idempotent: no-op when the index already exists
        except PyMongoError as exc:  # e.g. duplicates in a unique-index build; never stop the app from starting
            log.error("index %s on %s failed: %s", opts.get("name"), coll.name, exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    for attempt in range(3):
        try:
            ensure_indexes()
            break
        except Exception as exc:  # pragma: no cover
            log.error("startup index creation attempt %s failed: %s", attempt + 1, exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def unprocessable(msg: str, loc: str = "body") -> HTTPException:
    """422 with the same body shape FastAPI uses for validation errors."""
    return HTTPException(422, [{"loc": [loc], "msg": msg, "type": "value_error"}])


# --------------------------------------------------------------------------- #
# Helpers: instants, calendar values, rounding
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def trunc_dt(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds."""
    return as_utc(dt).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    return trunc_dt(ms_to_dt(ms))


def now_utc() -> datetime:
    return trunc_dt(datetime.now(UTC))


def parse_date(value: str, loc: str = "date") -> date:
    if not re.match(DATE_RE, value or ""):
        raise unprocessable("date must be YYYY-MM-DD", loc)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable("not a valid calendar date", loc)


def month_bounds(month: str) -> tuple[date, date]:
    if not re.match(MONTH_RE, month or ""):
        raise unprocessable("month must be YYYY-MM", "month")
    year, mon = int(month[:4]), int(month[5:])
    try:
        first = date(year, mon, 1)
    except ValueError:
        raise unprocessable("not a valid month", "month")
    nxt = date(year + (mon == 12), mon % 12 + 1, 1) if year < 9999 else date(9999, 12, 31) + timedelta(days=1)
    return first, nxt - timedelta(days=1)


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value if value is not None else 0))


def half_up(value: Any, places: int) -> float:
    """R8: round half-up (not banker's rounding, not binary-float rounding)."""
    return float(to_decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def minutes_of(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# --------------------------------------------------------------------------- #
# Helpers: business rules R1 - R5
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of the punch-in; early-morning punch-ins of an overnight shift belong to the day before."""
    local = punch_in.astimezone(IST)
    day = local.date()
    start, end = minutes_of(emp["shift_start"]), minutes_of(emp["shift_end"])
    seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
    if end <= start and seconds_into_day < end * 60:
        day -= timedelta(days=1)
    return day


def shift_window(att_date: date, emp: dict) -> tuple[datetime, datetime]:
    """Shift start and end as aware IST datetimes for the shift that belongs to att_date."""
    s, e = minutes_of(emp["shift_start"]), minutes_of(emp["shift_end"])
    start = datetime.combine(att_date, time(s // 60, s % 60), tzinfo=IST)
    end = datetime.combine(att_date, time(e // 60, e % 60), tzinfo=IST)
    if e <= s:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: exact decimal arithmetic, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, only when at least 30."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(emp: dict, att_date: date, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """All derived fields of a presence record (R2 - R5)."""
    start, end = shift_window(att_date, emp)
    out = {"late_minutes": compute_late_minutes(punch_in, start), "work_hours": None,
           "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=float(hours), overtime_minutes=compute_overtime(punch_out, end),
                   half_day=hours < HALF_DAY_HOURS)
    return out


NO_PUNCH_DERIVED = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def _api_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"], "name": d.get("name"), "email": d.get("email"),
        "department": d.get("department"), "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"), "joined_on": d.get("joined_on"),
        "created_at": dt_to_ms(d.get("created_at")),
    }


def history_out(h: dict) -> dict:
    changes = {k: {"from": _api_value(v.get("from")), "to": _api_value(v.get("to"))}
               for k, v in (h.get("changes") or {}).items()}
    return {"at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def record_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")), "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [history_out(h) for h in (d.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=EMP_CODE_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        date.fromisoformat(v)  # ValueError becomes a 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index on emp_code decides races
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = employees.count_documents(q)
    docs = employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, att_date, ts, None),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): of N concurrent requests exactly one inserts
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = trunc_dt(rec["punch_in"])
    if ts <= pin:
        raise unprocessable("punched_at must be after punch_in", "punched_at")
    if ts - pin > MAX_SHIFT_SPAN:
        raise unprocessable("punch-out is more than 24 hours after punch-in", "punched_at")
    fields = derive(emp, date.fromisoformat(rec["date"]), pin, ts)
    fields.pop("late_minutes")  # late_minutes was fixed at punch-in
    # Compare-and-set on punch_out == null: of two concurrent punch-outs exactly one matches.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


def build_attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                            status: Optional[str]) -> dict:
    q: dict = {}
    d_from = parse_date(date_from, "date_from") if date_from is not None else None
    d_to = parse_date(date_to, "date_to") if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        raise unprocessable("date_from must not be after date_to", "date_from")
    if emp_code is not None:
        q["emp_code"] = emp_code
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from.isoformat()
        if d_to:
            q["date"]["$lte"] = d_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(q) if q else logs.estimated_document_count()
    cursor = (logs.find(q, {"_id": 0}).sort(list(ATTENDANCE_SORT.items()))
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    att_date = parse_date(date)
    emp = get_employee(emp_code)

    for _ in range(5):
        rec = logs.find_one({"emp_code": emp_code, "date": att_date.isoformat()})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")

        old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
        status = body.status or rec["status"]
        if status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise unprocessable(f"punch times cannot be supplied with status {status}")
            new_in = new_out = None
            derived = dict(NO_PUNCH_DERIVED)
        else:
            new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
            new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out
            if new_in is None:
                raise unprocessable(f"status {status} requires a punch_in")
            if body.punch_in is not None and attendance_date(new_in, emp) != att_date:
                raise unprocessable("punch_in must stay on the record's attendance date", "punch_in")
            if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
                if new_out <= new_in:
                    raise unprocessable("punch_out must be after punch_in", "punch_out")
                if new_out - new_in > MAX_SHIFT_SPAN:
                    raise unprocessable("punch_out must be within 24 hours of punch_in", "punch_out")
            derived = derive(emp, att_date, trunc_dt(new_in), trunc_dt(new_out) if new_out else None)

        new = {"status": status, "punch_in": new_in, "punch_out": new_out, **derived}
        old = {"status": rec["status"], "punch_in": old_in, "punch_out": old_out,
               "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
               "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
        changes = {}
        for field, new_val in new.items():
            old_val = old[field]
            if isinstance(old_val, datetime) or isinstance(new_val, datetime):
                same = dt_to_ms(old_val) == dt_to_ms(new_val)
            else:
                same = old_val == new_val
            if not same:
                changes[field] = {"from": old_val, "to": new_val}
        if not changes:
            raise unprocessable("request changes nothing")

        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        n_history = len(rec.get("history") or [])
        history_guard = ({"history": {"$size": n_history}} if n_history
                         else {"$or": [{"history": {"$size": 0}}, {"history": None}]})
        # Optimistic concurrency: only apply if the record is exactly as we read it (same history length and
        # punch times). A concurrent edit makes this match nothing, and we re-read instead of overwriting history.
        updated = logs.find_one_and_update(
            {"_id": rec["_id"], "status": rec["status"], "punch_in": old_in, "punch_out": old_out, **history_guard},
            {"$set": new, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# Aggregation fragments (analytics read the STORED derived values)
# --------------------------------------------------------------------------- #
_LATE = {"$ifNull": ["$late_minutes", 0]}
_IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
# 1 = half a day, 2 = a full day; summed as integers so the arithmetic stays exact (present_days = halves / 2).
_HALVES = {"$cond": [_IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}, 0]}
_DAY_OF_WEEK = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d",
                                                   "onError": None, "onNull": None}}}  # 1 = Sunday ... 7 = Saturday
_IS_WEEKDAY = {"$in": [_DAY_OF_WEEK, [2, 3, 4, 5, 6]]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def month_totals_accumulators() -> dict:
    return {
        "present_halves": {"$sum": {"$cond": [_IS_WEEKDAY, _HALVES, 0]}},  # R7: weekday records only
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
        "total_late": {"$sum": _LATE},
        "total_overtime": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [_HAS_HOURS, {"$toDecimal": "$work_hours"}, 0]}},  # exact decimal sum
        "hours_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
    }


def month_range_match(first: date, last: date) -> dict:
    return {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}


def working_days(first: date, last: date, joined_on: date) -> int:
    """R7: Mon-Fri between the month start (or joined_on, if later) and the month end."""
    day, count = max(first, joined_on), 0
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pipeline builders (shared by the endpoints and the explain endpoint)
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, **month_range_match(first, last)}},
        {"$group": {"_id": None, **month_totals_accumulators()}},
    ]


def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    totals = month_totals_accumulators()
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        # One indexed lookup per employee; employees without logs simply get an empty array (and still count).
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "s",
            "pipeline": [{"$match": month_range_match(first, last)}, {"$group": {"_id": None, **totals}}],
        }},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{k: {"$sum": {"$ifNull": [{"$first": f"$s.{k}"}, 0]}} for k in totals}}},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {**month_range_match(first, last), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    n_days = (d_to - d_from).days + 1
    date_expr = {"$dateToString": {"format": "%Y-%m-%d", "date": {
        "$dateAdd": {"startDate": {"$dateFromString": {"dateString": d_from.isoformat()}},
                     "unit": "day", "amount": "$offset"}}}}
    return [
        {"$match": {"department": department}},
        {"$facet": {
            # how many employees joined on each date (for the running headcount, R9)
            "joins": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
            # per-day presence and lateness, computed from the stored values via one indexed lookup per employee
            "daily": [
                {"$lookup": {
                    "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "s",
                    "pipeline": [
                        {"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
                        {"$project": {"_id": 0, "date": 1, "halves": _HALVES,
                                      "late": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}}},
                    ],
                }},
                {"$unwind": "$s"},
                {"$group": {"_id": "$s.date", "halves": {"$sum": "$s.halves"}, "late": {"$sum": "$s.late"}}},
            ],
        }},
        # Gap filling: one row for every calendar day of the range, whether or not anything happened that day.
        {"$project": {"joins": 1, "daily": 1, "offset": {"$range": [0, n_days]}}},
        {"$unwind": "$offset"},
        {"$set": {"date": date_expr}},
        {"$set": {
            "headcount": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j._id", "$date"]}}},
                "as": "j", "in": "$$j.n"}}},
            "day": {"$arrayElemAt": [
                {"$filter": {"input": "$daily", "as": "d", "cond": {"$eq": ["$$d._id", "$date"]}}}, 0]},
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
        }},
        {"$project": {
            "_id": 0, "date": 1, "headcount": 1,
            "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
            "present_halves": {"$ifNull": ["$day.halves", 0]},
            "late_count": {"$ifNull": ["$day.late", 0]},
        }},
        {"$set": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$divide": ["$present_halves", 2]}, "$headcount"]}, None]}}},
        # The window never looks before the first row of the requested range (documents: current + 6 before).
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"moving_avg": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    first, last = month_bounds(month)
    emp = get_employee(emp_code)
    t = next(logs.aggregate(employee_monthly_pipeline(emp_code, first, last)), None) or {}
    days = working_days(first, last, date.fromisoformat(emp["joined_on"]))
    present = Decimal(t.get("present_halves", 0)) / 2
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": float(present),
        "leave_days": t.get("leave_count", 0),
        "late_count": t.get("late_count", 0),
        "total_late_minutes": t.get("total_late", 0),
        "total_overtime_minutes": t.get("total_overtime", 0),
        "attendance_pct": half_up(present / days * 100, 2) if days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in employees.aggregate(department_summary_pipeline(first, last, department)):
        n = r["hours_n"]
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": float(Decimal(r["present_halves"]) / 2),
            "avg_work_hours": half_up(to_decimal(r["hours_sum"]) / n, 2) if n else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    return {"month": month, "items": list(logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))}


def validate_trend_range(from_: str, to: str) -> tuple[date, date]:
    d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise unprocessable("to must not be before from", "to")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"range must not exceed {MAX_TREND_DAYS} days", "to")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    d_from, d_to = validate_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in employees.aggregate(department_trend_pipeline(department, d_from, d_to)):
        present = Decimal(r["present_halves"]) / 2
        rate = present / r["headcount"] if r["is_working_day"] and r["headcount"] > 0 else None
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(present),
            "late_count": r["late_count"],
            "attendance_rate": half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": half_up(r["moving_avg"], 4) if r.get("moving_avg") is not None else None,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def _jsonable(doc: dict) -> dict:
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


def _require(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, [{"loc": ["query", k], "msg": "field required", "type": "missing"} for k in missing])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {"find": collection,
                   "filter": build_attendance_filter(emp_code, date_from, date_to, status),
                   "sort": ATTENDANCE_SORT, "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            first, last = month_bounds(month)
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, first, last)
        elif endpoint == "department_summary":
            _require(month=month)
            first, last = month_bounds(month)
            collection, pipeline = "employees", department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            first, last = month_bounds(month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(first, last, limit, department)
        else:
            _require(department=department, **{"from": from_, "to": to})
            d_from, d_to = validate_trend_range(from_, to)
            collection, pipeline = "employees", department_trend_pipeline(department, d_from, d_to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": _jsonable(result)}
