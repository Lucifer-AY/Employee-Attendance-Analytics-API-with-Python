"""
Employee Attendance & Analytics API
FastAPI + MongoDB (PyMongo) backend implementing business rules R1 - R10.
"""
from __future__ import annotations

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load environment variables (.env fallback, real environment variables win)
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes Setup (Idempotent at startup)
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all required indexes idempotently at application startup."""
    # employees collection
    db.employees.create_index("emp_code", unique=True, name="idx_emp_code_unique")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_emp_dept_joined")
    db.employees.create_index("joined_on", name="idx_emp_joined_on")

    # attendance_logs collection
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="idx_att_emp_date_unique"
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="idx_att_date_desc_emp_asc"
    )
    db.attendance_logs.create_index(
        [("date", 1), ("late_minutes", 1), ("emp_code", 1)],
        name="idx_att_date_late_emp"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="idx_att_emp_punch_in_desc"
    )
    db.attendance_logs.create_index("status", name="idx_att_status")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helper Functions: Mathematics, Rounding, Timezones & Business Rules R1-R8
# --------------------------------------------------------------------------- #
def round_half_up(value: Union[float, Decimal, int], decimals: int = 2) -> float:
    """R8 Rounding: exact half-up rounding (not banker's or IEEE float rounding)."""
    if value is None:
        return None
    d = Decimal(str(value))
    target = Decimal("1" if decimals == 0 else "0." + "0" * (decimals - 1) + "1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds before any calculation."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds into a truncated UTC datetime."""
    # Truncate to whole seconds
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert BSON/UTC datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    sec = int(dt.timestamp())
    return sec * 1000


def parse_time(time_str: str) -> time:
    """Parse 'HH:MM' string to time object."""
    h, m = map(int, time_str.split(":"))
    return time(hour=h, minute=m)


def calculate_attendance_date(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """
    R1: Attendance date in IST.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end belongs
    to the shift that started the previous calendar day.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    start_t = parse_time(shift_start_str)
    end_t = parse_time(shift_end_str)

    if end_t <= start_t:  # Overnight shift
        if dt_ist.time() < end_t:
            return (dt_ist.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    return dt_ist.date().strftime("%Y-%m-%d")


def compute_late_minutes(
    punch_in_utc: datetime,
    shift_start_str: str,
    shift_end_str: str,
    date_str: str
) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_truncated = truncate_to_seconds(punch_in_utc)
    punch_in_ist = punch_in_truncated.astimezone(IST)
    att_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    start_t = parse_time(shift_start_str)
    shift_start_ist = datetime.combine(att_date, start_t, tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:  # strictly more than 10:00
        return int(diff_sec // 60)
    return 0


def compute_overtime(
    punch_out_utc: datetime,
    shift_start_str: str,
    shift_end_str: str,
    date_str: str
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out,
    counted only if that is >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    punch_out_truncated = truncate_to_seconds(punch_out_utc)
    punch_out_ist = punch_out_truncated.astimezone(IST)
    att_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    start_t = parse_time(shift_start_str)
    end_t = parse_time(shift_end_str)

    if end_t <= start_t:
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_ist = datetime.combine(shift_end_date, end_t, tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_sec >= 1800:  # at least 30 minutes
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    Instants truncated to whole seconds.
    """
    p_in = truncate_to_seconds(punch_in_utc)
    p_out = truncate_to_seconds(punch_out_utc)
    sec = int((p_out - p_in).total_seconds())
    d_sec = Decimal(str(sec))
    hours = (d_sec / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def count_working_days_in_month(month_str: str, joined_on_str: str) -> int:
    """
    R7: Mon-Fri working days in the given month, counted from joined_on for mid-month joiners.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, num_days)
    joined_date = datetime.strptime(joined_on_str, "%Y-%m-%d").date()

    effective_start = max(first_day, joined_date)
    if effective_start > last_day:
        return 0

    working_days = 0
    cur = effective_start
    while cur <= last_day:
        if cur.weekday() < 5:  # Mon-Fri
            working_days += 1
        cur += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Document Serializers for API Output
# --------------------------------------------------------------------------- #
def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes_out = {}
    for field, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field in ("punch_in", "punch_out"):
            from_val = datetime_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
            to_val = datetime_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
        changes_out[field] = {
            "from": from_val,
            "to": to_val
        }
    return {
        "at": datetime_to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_out,
    }


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas Matching openapi.yaml
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @field_validator("shift_end")
    @classmethod
    def validate_shifts(cls, v: str, info) -> str:
        start = info.data.get("shift_start")
        if start and start == v:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints: System
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: 200 only if MongoDB responds to ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unavailable: {str(e)}"
        )


# --------------------------------------------------------------------------- #
# Endpoints: Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee. Enforced by database unique index.
    409 on duplicate emp_code, 422 on invalid input.
    """
    doc = body.model_dump()
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """List employees sorted by emp_code ascending. Total reflects filters."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Endpoints: Attendance CRUD
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record a punch-in. Race-safe via unique compound index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown employee")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_dt = truncate_to_seconds(datetime.now(timezone.utc))

    date_str = calculate_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], emp["shift_end"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already punched in for that date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes most recent record whose punch_in <= punched_at.
    Race-safe: two simultaneous punch-outs yield exactly one 200 and one 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown employee")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)]
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="record is already punched out")

    # Validation: punched_at <= punch_in or more than 24 hours after it
    p_in = record["punch_in"]
    if p_in.tzinfo is None:
        p_in = p_in.replace(tzinfo=timezone.utc)
    diff_sec = (punch_out_dt - p_in).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in and within 24 hours"
        )

    work_hrs = compute_work_hours(p_in, punch_out_dt)
    ot_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    is_half = work_hrs < 4.50

    # Atomic update to guarantee race-safety
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="record is already punched out")

    return serialize_attendance_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    List attendance records sorted by date DESC, emp_code ASC.
    Streamed and indexed to stay fast at 100k records.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must not exceed date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_record(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_val: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields, appends to history, and protects against lost updates.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee does not exist")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_val})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no record found for that date")

    # Determine final intended status
    new_status = body.status if body.status is not None else record["status"]

    # Rules regarding ABSENT / LEAVE
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE"
            )
        target_punch_in = None
        target_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            target_punch_in = record.get("punch_in")
            if target_punch_in is not None and target_punch_in.tzinfo is None:
                target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        if target_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="presence status requires a punch_in"
            )

        # Validate punch_in stays on record's attendance date
        calculated_date = calculate_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calculated_date != date_val:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date"
            )

        # Determine punch_out
        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            target_punch_out = record.get("punch_out")
            if target_punch_out is not None and target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)

        # Validate punch_out order and 24h limit
        if target_punch_out is not None:
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in and within 24 hours"
                )

    # Compute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], emp["shift_end"], date_val)
        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_overtime_minutes = compute_overtime(target_punch_out, emp["shift_start"], emp["shift_end"], date_val)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Calculate exact delta changes
    changes = {}

    def normalize_dt(d):
        if d is None:
            return None
        if d.tzinfo is None:
            return d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)

    if record.get("status") != new_status:
        changes["status"] = {"from": record.get("status"), "to": new_status}

    old_pin = normalize_dt(record.get("punch_in"))
    new_pin = normalize_dt(target_punch_in)
    if old_pin != new_pin:
        changes["punch_in"] = {"from": old_pin, "to": new_pin}

    old_pout = normalize_dt(record.get("punch_out"))
    new_pout = normalize_dt(target_punch_out)
    if old_pout != new_pout:
        changes["punch_out"] = {"from": old_pout, "to": new_pout}

    if record.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}

    if record.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}

    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}

    if record.get("half_day", False) != new_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="request changes nothing"
        )

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency safe update: ensure history array length matches snapshot
    curr_history_len = len(record.get("history", []))
    update_res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": curr_history_len}
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not update_res:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="concurrent regularization detected")

    return serialize_attendance_record(update_res)


# --------------------------------------------------------------------------- #
# Endpoints: Analytics (Aggregation Pipelines)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown employee")

    working_days = count_working_days_in_month(month, emp["joined_on"])
    month_start = f"{month}-01"
    month_end = f"{month}-31"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6]  # Mon to Fri in MongoDB (Sunday=1, Monday=2)
                    ]
                },
                "is_presence": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", "$is_working_day"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]

    agg_result = list(db.attendance_logs.aggregate(pipeline))
    if agg_result:
        row = agg_result[0]
        present_days = float(round_half_up(row["present_days"], 2))
        leave_days = int(row["leave_days"])
        late_count = int(row["late_count"])
        total_late = int(row["total_late_minutes"])
        total_ot = int(row["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    """
    Department summary for a month across both collections.
    Headcount includes people with zero logs and excludes later joiners.
    """
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    last_day_of_month = f"{month}-{num_days:02d}"
    month_start = f"{month}-01"
    month_end = f"{month}-31"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": last_day_of_month}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": "$present_days",
                "avg_work_hours": {"$avg": "$work_hours_list"},
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count"
            }
        },
        {"$sort": {"department": 1}}
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        avg_wh = round_half_up(d["avg_work_hours"], 2) if d["avg_work_hours"] is not None else None
        items.append({
            "department": d["department"],
            "headcount": d["headcount"],
            "present_days": round_half_up(d["present_days"], 2),
            "avg_work_hours": avg_wh,
            "late_count": d["late_count"],
            "total_late_minutes": d["total_late_minutes"],
            "leave_count": d["leave_count"],
            "on_duty_count": d["on_duty_count"]
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    """
    Late-comers leaderboard for a month using standard competition ranking ($rank).
    Ties share a rank and the next rank is skipped (1, 2, 2, 4).
    Limit applied after ranking (rank <= limit).
    """
    month_start = f"{month}-01"
    month_end = f"{month}-31"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$project": {
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    docs = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": d["rank"],
            "emp_code": d["emp_code"],
            "name": d["name"],
            "department": d["department"],
            "total_late_minutes": d["total_late_minutes"],
            "late_count": d["late_count"]
        }
        for d in docs
    ]

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    """
    Daily series with gap-filling ($densify) and 7-day moving average ($setWindowFields).
    """
    # 404 for unknown department
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown department")

    try:
        dt_from = datetime.strptime(from_date, "%Y-%m-%d").date()
        dt_to = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid date format")

    if dt_to < dt_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' date must be on or after 'from' date")

    diff_days = (dt_to - dt_from).days + 1
    if diff_days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date range exceeds 92 days")

    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]

    bounds_from = datetime.combine(dt_from, time.min)
    bounds_to = datetime.combine(dt_to + timedelta(days=1), time.min)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "date_str": "$_id",
                "date_val": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_val",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [bounds_from, bounds_to]
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_val"}},
                "date_val": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}}
                ],
                "as": "dept_employees"
            }
        },
        {
            "$project": {
                "date": 1,
                "date_val": 1,
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_val"}, [2, 3, 4, 5, 6]]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]}
                        }
                    }
                },
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$project": {
                "date": 1,
                "date_val": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        }
    ]

    docs = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for d in docs:
        att_rate = round_half_up(d["attendance_rate"], 4) if d["attendance_rate"] is not None else None
        mavg = round_half_up(d["moving_avg_7d"], 4) if d["moving_avg_7d"] is not None else None
        items.append({
            "date": d["date"],
            "is_working_day": d["is_working_day"],
            "headcount": d["headcount"],
            "present_count": round_half_up(d["present_count"], 2),
            "late_count": d["late_count"],
            "attendance_rate": att_rate,
            "moving_avg_7d": mavg
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints: Admin (Explain Query Plan)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    MongoDB explain output with executionStats verbosity for an endpoint's main query.
    Built identically to the target endpoint.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }
        raw_explain = db.command("explain", command, verbosity="executionStats")
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code and month are required")
        month_start = f"{month}-01"
        month_end = f"{month}-31"
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end}
                }
            },
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "is_working_day": {
                        "$in": [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            [2, 3, 4, 5, 6]
                        ]
                    },
                    "is_presence": {
                        "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_presence", "$is_working_day"]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"}
                }
            }
        ]
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        last_day_of_month = f"{month}-{num_days:02d}"
        month_start = f"{month}-01"
        month_end = f"{month}-31"

        match_stage: Dict[str, Any] = {"joined_on": {"$lte": last_day_of_month}}
        if department:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$department",
                    "employees": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                                [2, 3, 4, 5, 6]
                                            ]
                                        }
                                    ]
                                },
                                {"$cond": ["$logs.half_day", 0.5, 1.0]},
                                0.0
                            ]
                        }
                    },
                    "work_hours_list": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$logs.work_hours", None]}
                                    ]
                                },
                                "$logs.work_hours",
                                "$$REMOVE"
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                    },
                    "on_duty_count": {
                        "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                    }
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "headcount": {"$size": "$employees"},
                    "present_days": "$present_days",
                    "avg_work_hours": {"$avg": "$work_hours_list"},
                    "late_count": "$late_count",
                    "total_late_minutes": "$total_late_minutes",
                    "leave_count": "$leave_count",
                    "on_duty_count": "$on_duty_count"
                }
            },
            {"$sort": {"department": 1}}
        ]
        raw_explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": clean_explain
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        month_start = f"{month}-01"
        month_end = f"{month}-31"
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp"
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}}
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1
                }
            }
        ])
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required"
            )
        try:
            dt_from = datetime.strptime(from_date, "%Y-%m-%d").date()
            dt_to = datetime.strptime(to_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid date format")

        if dt_to < dt_from or (dt_to - dt_from).days + 1 > 92:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid date range")

        dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]

        bounds_from = datetime.combine(dt_from, time.min)
        bounds_to = datetime.combine(dt_to + timedelta(days=1), time.min)

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date}
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                        }
                    }
                }
            },
            {
                "$project": {
                    "date_str": "$_id",
                    "date_val": {"$dateFromString": {"dateString": "$_id"}},
                    "present_count": 1,
                    "late_count": 1
                }
            },
            {
                "$densify": {
                    "field": "date_val",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [bounds_from, bounds_to]
                    }
                }
            },
            {
                "$project": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_val"}},
                    "date_val": 1,
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]}
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "pipeline": [{"$match": {"department": department}}],
                    "as": "dept_employees"
                }
            },
            {
                "$project": {
                    "date": 1,
                    "date_val": 1,
                    "is_working_day": {
                        "$in": [{"$dayOfWeek": "$date_val"}, [2, 3, 4, 5, 6]]
                    },
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": "$dept_employees",
                                "as": "e",
                                "cond": {"$lte": ["$$e.joined_on", "$date"]}
                            }
                        }
                    },
                    "present_count": 1,
                    "late_count": 1
                }
            },
            {
                "$project": {
                    "date": 1,
                    "date_val": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]}
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None
                        ]
                    }
                }
            },
            {"$sort": {"date": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, "current"]}
                        }
                    }
                }
            }
        ]
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain
        }
