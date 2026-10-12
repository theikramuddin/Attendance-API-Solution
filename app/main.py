"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded; real environment variables win)

Layout of this single file
    1. configuration + database handle
    2. time / rounding helpers and the business rules R1-R8
    3. request models (validation)
    4. serialisation (Mongo document -> API JSON)
    5. index creation (runs at startup, idempotent)
    6. employees + attendance endpoints
    7. pipeline builders + analytics endpoints
    8. explain endpoint (re-uses the SAME builders as the real endpoints)
"""
import calendar
import json
import logging
import os
import time as _time
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
# load_dotenv() does NOT override variables that already exist in the real
# environment, so injected values always win over a local .env file.
load_dotenv()

log = logging.getLogger("attendance")

if not os.getenv("MONGO_URI") or not os.getenv("MONGO_DB"):
    raise RuntimeError("MONGO_URI and MONGO_DB must be set (environment or .env)")

# tz_aware=True -> PyMongo returns aware UTC datetimes instead of naive ones, so
# subtracting / converting instants can never silently use the server's local zone.
client = MongoClient(os.environ["MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=4000)
db = client[os.environ["MONGO_DB"]]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000  # EpochMillis range in the contract

_indexes_ready = False


# --------------------------------------------------------------------------- #
# 2. Helpers and business rules
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch ms. Naive values are treated as UTC (Mongo stores UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _round_half_up(x: Any, places: int) -> float:
    """Half-up rounding on the *decimal* repr of x (python's round() is banker's/float based)."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


def r2(x: Any) -> float:
    return _round_half_up(x, 2)


def r4(x: Any) -> float:
    return _round_half_up(x, 4)


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts belong to the day they started."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), hhmm(shift_start), tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(date.fromisoformat(date_str), hhmm(shift_end), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; value counted from shift start."""
    secs = int((punch_in - shift_start_dt(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, counted only when >= 30."""
    secs = int((punch_out - shift_end_dt(date_str, shift_start, shift_end)).total_seconds())
    mins = secs // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (exact, via Decimal)."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(emp: dict, date_str: str, status: str,
                   punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All four derived fields in one place (used by punch-in, punch-out and PATCH)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, date_str, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"])
        out["half_day"] = Decimal(repr(wh)) < Decimal("4.50")  # R5, on the rounded value
    return out


def check_date_str(value: str, name: str) -> str:
    """YYYY-MM-DD and a real calendar date, else 422."""
    ok = len(value) == 10 and value[4] == "-" and value[7] == "-" and value.replace("-", "").isascii() \
        and value.replace("-", "").isdigit()
    if ok:
        try:
            date.fromisoformat(value)
        except ValueError:
            ok = False
    if not ok:
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")
    return value


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31'); string compare works because dates are ISO strings."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise HTTPException(422, "month year out of range")
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(first: str, last: str, joined_on: str) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last."""
    start = date.fromisoformat(max(first, joined_on))
    end = date.fromisoformat(last)
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
EpochMs = StrictInt  # StrictInt: rejects floats, strings and bools (-> 422)


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = Field(None, ge=MIN_MS, le=MAX_MS)
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = Field(None, ge=MIN_MS, le=MAX_MS)


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = Field(None, ge=MIN_MS, le=MAX_MS)
    punch_out: Optional[EpochMs] = Field(None, ge=MIN_MS, le=MAX_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # "omitted" means unchanged; an explicit null is not allowed by the contract
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


# --------------------------------------------------------------------------- #
# 4. Serialisation
# --------------------------------------------------------------------------- #
def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"], "name": d.get("name"), "email": d.get("email"),
        "department": d.get("department"), "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"), "joined_on": d.get("joined_on"),
        "created_at": dt_to_ms(d.get("created_at")),
    }


def _hist_value(field: str, v: Any) -> Any:
    return dt_to_ms(v) if field in ("punch_in", "punch_out") else v


def record_out(d: dict) -> dict:
    """Mongo attendance document -> contract JSON. Missing legacy fields get their defaults."""
    history = []
    for h in d.get("history") or []:
        history.append({
            "at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"),
            "changes": {k: {"from": _hist_value(k, v.get("from")), "to": _hist_value(k, v.get("to"))}
                        for k, v in (h.get("changes") or {}).items()},
        })
    return {
        "emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")), "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 5. Indexes (idempotent; created at startup)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    global _indexes_ready
    # employees
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # attendance_logs
    try:
        # the natural key; also the guard that makes punch-in race-safe
        logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], unique=True, name="emp_date_unique")
    except OperationFailure as e:  # e.g. dirty seed data containing duplicates: still start, still index
        log.error("unique (emp_code,date) index failed (%s); creating a non-unique one", e)
        logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="emp_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")  # list + sort
    logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
                      name="date_late_emp")  # leaderboard, covered
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")  # punch-out lookup
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    deadline = _time.monotonic() + 15
    while True:
        try:
            ensure_indexes()
            break
        except PyMongoError as e:
            if _time.monotonic() > deadline:
                log.error("could not create indexes at startup: %s (will retry from /health)", e)
                break
            _time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 6. Endpoints: health, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # unique index decides the winner of a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    skip = (page - 1) * page_size
    items = []
    if skip < total:
        items = [employee_out(d) for d in
                 employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": d, "status": body.status,
        "punch_in": ts, "punch_out": None,
        **derived_fields(emp, d, body.status, ts, None),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                        sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = rec["punch_in"]
    if ts <= p_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - p_in > timedelta(hours=24):
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")
    fields = derived_fields(emp, rec["date"], rec["status"], p_in, ts)
    # compare-and-set: only succeeds while the record is still open and unchanged
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": p_in},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was already punched out or changed concurrently")
    return record_out(updated)


def build_attendance_find(emp_code, date_from, date_to, status, page, page_size):
    """Shared by GET /attendance and the explain endpoint."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort, (page - 1) * page_size, page_size


def _check_date_range(date_from, date_to):
    if date_from:
        check_date_str(date_from, "date_from")
    if date_to:
        check_date_str(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    _check_date_range(date_from, date_to)
    q, sort, skip, limit = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(q)
    items = []
    if skip < total:
        items = [record_out(d) for d in logs.find(q, {"_id": 0}).sort(sort).skip(skip).limit(limit)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    check_date_str(date, "date")
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    cur_status, cur_in, cur_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or cur_status
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else cur_in
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else cur_out

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE records cannot carry punch times")
        new_in = new_out = None
    else:
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if body.punch_in is not None and \
                attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    old = {
        "status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derived_fields(emp, date, new_status, new_in, new_out)}
    if all(old[k] == new[k] for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "request changes nothing")
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # optimistic concurrency: the write only applies if the record is exactly as we read it
    # (same status / punches / number of history entries); otherwise 409 and nothing is lost.
    flt: dict = {"_id": rec["_id"], "status": cur_status, "punch_in": cur_in, "punch_out": cur_out}
    if rec.get("history") is None:
        flt["history"] = None
        update = {"$set": {**new, "history": [entry]}}
    else:
        flt["history"] = {"$size": len(rec["history"])}
        update = {"$set": new, "$push": {"history": entry}}
    updated = logs.find_one_and_update(flt, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 7. Analytics: pipeline builders + endpoints
# --------------------------------------------------------------------------- #
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d",
                                           "onError": None, "onNull": None}}}  # 1=Sun ... 7=Sat
_IS_WEEKDAY = {"$in": [_DOW, [2, 3, 4, 5, 6]]}
_IS_PRESENT = {"$in": ["$status", PRESENCE]}
_PRESENT_WEIGHT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}  # R5
_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def pipeline_employee_monthly(emp_code: str, month: str):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _PRESENT_WEIGHT, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return "attendance_logs", pipeline


def pipeline_department_summary(month: str, department: Optional[str]):
    first, last = month_bounds(month)
    has_wh = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}
    per_employee = [  # runs inside $lookup: one stats document per employee (or none if no logs)
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _PRESENT_WEIGHT, 0]}},
            "work_sum": {"$sum": {"$cond": [has_wh, "$work_hours", 0]}},
            "work_n": {"$sum": {"$cond": [has_wh, 1, 0]}},
            "late_count": {"$sum": {"$cond": [_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    emp_match: dict = {"joined_on": {"$lte": last}}  # R9
    if department is not None:
        emp_match["department"] = department
    pipeline = [
        # start from EMPLOYEES so people with zero logs still count in headcount
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "s"}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},  # one document per employee, logs or not
            "present_days": {"$sum": {"$sum": "$s.present_days"}},
            "work_sum": {"$sum": {"$sum": "$s.work_sum"}},
            "work_n": {"$sum": {"$sum": "$s.work_n"}},
            "late_count": {"$sum": {"$sum": "$s.late_count"}},
            "total_late_minutes": {"$sum": {"$sum": "$s.total_late_minutes"}},
            "leave_count": {"$sum": {"$sum": "$s.leave_count"}},
            "on_duty_count": {"$sum": {"$sum": "$s.on_duty_count"}},
        }},
        {"$sort": {"_id": 1}},
    ]
    return "employees", pipeline


def pipeline_late_leaderboard(month: str, limit: int, department: Optional[str]):
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank INSIDE the department
    pipeline += [
        # $rank = standard competition ranking (1,2,2,4); ties are decided on total only
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name",
                      "department": "$e.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipeline


def pipeline_department_trend(department: str, d_from: str, d_to: str):
    f, t = date.fromisoformat(d_from), date.fromisoformat(d_to)
    from_dt = datetime(f.year, f.month, f.day, tzinfo=UTC)
    end_excl = datetime(t.year, t.month, t.day, tzinfo=UTC) + timedelta(days=1)
    dept = {"$literal": department}
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                                  {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                                                "late_minutes": 1}}],
                     "as": "l"}},
        {"$unwind": "$l"},
        {"$group": {
            "_id": "$l.date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$l.status", PRESENCE]},
                                                 {"$cond": [{"$eq": [{"$ifNull": ["$l.half_day", False]}, True]},
                                                            0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$l.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "present_count": 1, "late_count": 1,
                      "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # guaranteed seed row so $densify always has input, even when the range has no logs at all
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"_id": 0, "day": {"$literal": from_dt}, "present_count": {"$literal": 0},
                          "late_count": {"$literal": 0}}}]}},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        # gap filling: one row per calendar day, weekends and empty days included
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [from_dt, end_excl]}}},
        {"$match": {"day": {"$gte": from_dt, "$lt": end_excl}}},
        {"$addFields": {"present_count": {"$ifNull": ["$present_count", 0]},
                        "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$sort": {"day": 1}},
        {"$addFields": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                        "dow": {"$dayOfWeek": "$day"}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"},
                     "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", dept]},
                                                                  {"$lte": ["$joined_on", "$$d"]}]}}},
                                  {"$count": "n"}],
                     "as": "hc"}},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                        "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                                           "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]
    return "employees", pipeline


def _run(coll: str, pipeline: list) -> list:
    return list(db[coll].aggregate(pipeline, allowDiskUse=True))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    first, last = month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    coll, pipeline = pipeline_employee_monthly(emp_code, month)
    row = (_run(coll, pipeline) or [{}])[0]
    working_days = count_working_days(first, last, emp["joined_on"])
    present = row.get("present_days", 0)
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": r2(present), "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": r2(Decimal(repr(float(present))) / working_days * 100) if working_days else None,
    }


def _avg_hours(work_sum: float, work_n: int) -> Optional[float]:
    if not work_n:
        return None
    cents = round(work_sum * 100)  # sum of 2-decimal values is a whole number of cents: kills float noise
    return float((Decimal(cents) / Decimal(100 * work_n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    coll, pipeline = pipeline_department_summary(month, department)
    items = [{
        "department": r["_id"], "headcount": int(r["headcount"]),
        "present_days": r2(r["present_days"]),
        "avg_work_hours": _avg_hours(r["work_sum"], int(r["work_n"])),
        "late_count": int(r["late_count"]), "total_late_minutes": int(r["total_late_minutes"]),
        "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
    } for r in _run(coll, pipeline)]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(pattern=MONTH_RE), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    coll, pipeline = pipeline_late_leaderboard(month, limit, department)
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
              "department": r["department"], "total_late_minutes": int(r["total_late_minutes"]),
              "late_count": int(r["late_count"])} for r in _run(coll, pipeline)]
    return {"month": month, "items": items}


def _check_trend_range(d_from: str, d_to: str) -> None:
    check_date_str(d_from, "from")
    check_date_str(d_to, "to")
    days = (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1
    if days < 1:
        raise HTTPException(422, "to must not be before from")
    if days > 92:
        raise HTTPException(422, "range must not exceed 92 days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, d_from: str = Query(alias="from"), d_to: str = Query(alias="to")):
    _check_trend_range(d_from, d_to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    coll, pipeline = pipeline_department_trend(department, d_from, d_to)
    items = []
    for r in _run(coll, pipeline):
        items.append({
            "date": r["date"], "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]), "present_count": r["present_count"],
            "late_count": int(r["late_count"]),
            "attendance_rate": None if r.get("attendance_rate") is None else r4(r["attendance_rate"]),
            "moving_avg_7d": None if r.get("moving_avg_7d") is None else r4(r["moving_avg_7d"]),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    d_from: Optional[str] = Query(None, alias="from"),
    d_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Each branch builds the query with the very same builder function the real endpoint uses.
    if endpoint == "attendance_list":
        _check_date_range(date_from, date_to)
        q, sort, skip, lim = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        inner = {"find": coll, "filter": q, "sort": dict(sort), "projection": {"_id": 0},
                 "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            coll, pipeline = pipeline_employee_monthly(emp_code, month)
        elif endpoint == "department_summary":
            _need(month=month)
            coll, pipeline = pipeline_department_summary(month, department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            coll, pipeline = pipeline_late_leaderboard(month, limit, department)
        else:
            _need(department=department, **{"from": d_from, "to": d_to})
            _check_trend_range(d_from, d_to)
            coll, pipeline = pipeline_department_trend(department, d_from, d_to)
        inner = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": inner, "verbosity": "executionStats"})
    # explain output holds BSON types (Timestamp, Int64, ...): convert to plain JSON
    return {"endpoint": endpoint, "collection": coll,
            "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))}
