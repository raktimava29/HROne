"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from datetime import datetime, time, timedelta, timezone
from datetime import date as date_module
from typing import Annotated, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from decimal import Decimal, ROUND_HALF_UP
from bson import ObjectId, Decimal128
from bson.timestamp import Timestamp
import base64

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

def create_indexes() -> None:
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )

    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined",
    )

    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employee_joined_on",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )

    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_date",
    )


@app.on_event("startup")
def initialize_database() -> None:
    db.command("ping")
    create_indexes()

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


def truncate_seconds(value: datetime) -> datetime:
    """Normalize instant to UTC and discard fractional seconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    """Convert validated epoch milliseconds to UTC datetime."""
    return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC))


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Convert MongoDB datetime to epoch milliseconds."""
    if value is None:
        return None

    value = truncate_seconds(value)
    return int(value.timestamp() * 1000)


def attendance_to_api(record: dict) -> dict:
    """Normalize attendance document to complete HTTP contract shape."""
    result = dict(record)
    result.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        value = result.get(field)
        result[field] = datetime_to_epoch_ms(value) if isinstance(value, datetime) else value

    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)

    history = []
    for raw_entry in result.get("history") or []:
        entry = dict(raw_entry)
        at_value = entry.get("at")
        if isinstance(at_value, datetime):
            entry["at"] = datetime_to_epoch_ms(at_value)

        changes = {}
        for changed_field, raw_change in (entry.get("changes") or {}).items():
            change = dict(raw_change)
            if changed_field in {"punch_in", "punch_out"}:
                for side in ("from", "to"):
                    value = change.get(side)
                    if isinstance(value, datetime):
                        change[side] = datetime_to_epoch_ms(value)
            changes[changed_field] = change
        entry["changes"] = changes
        history.append(entry)

    result["history"] = history
    return result


def attendance_date_for(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """Return IST attendance date, including overnight-shift handling."""
    local = truncate_seconds(punch_in).astimezone(IST)
    start = time.fromisoformat(shift_start)
    end = time.fromisoformat(shift_end)

    day = local.date()

    if end <= start and local.time() < end:
        day -= timedelta(days=1)

    return day.isoformat()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    """Apply strict 10-minute grace period; report minutes from shift start."""
    local = truncate_seconds(punch_in).astimezone(IST)
    start_time = time.fromisoformat(shift_start)
    start = datetime.combine(
        date_module.fromisoformat(attendance_date),
        start_time,
        tzinfo=IST,
    )

    elapsed = int((local - start).total_seconds())

    if elapsed <= 600:
        return 0

    return elapsed // 60


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    """Calculate work hours with two-decimal half-up rounding."""
    start = truncate_seconds(punch_in)
    end = truncate_seconds(punch_out)

    seconds = Decimal(str((end - start).total_seconds()))
    hours = seconds / Decimal("3600")

    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    attendance_date: str,
    shift_start: str,
) -> int:
    """Count overtime only when at least 30 whole minutes are accumulated."""
    day = date_module.fromisoformat(attendance_date)
    start_time = time.fromisoformat(shift_start)
    end_time = time.fromisoformat(shift_end)

    if end_time <= start_time:
        day += timedelta(days=1)

    shift_end_at = datetime.combine(day, end_time, tzinfo=IST)
    local_out = truncate_seconds(punch_out).astimezone(IST)

    elapsed = int((local_out - shift_end_at).total_seconds())

    if elapsed < 1800:
        return 0

    return elapsed // 60

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(min_length=7, max_length=9)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must match EMP followed by 4 to 6 digits")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        try:
            time.fromisoformat(value)
        except ValueError:
            raise ValueError("Time must use HH:MM format")

        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM format")

        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            date_module.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value
        
    @model_validator(mode="after")
    def validate_shift_range(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=30)
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, value):
        if value is None:
            raise ValueError("punched_at must be epoch milliseconds when supplied")
        if not isinstance(value, int) or isinstance(value, bool) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"PRESENT", "WFH", "ON_DUTY"}:
            raise ValueError("Invalid punch-in status")
        return value

class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=30)
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, value):
        if value is None:
            raise ValueError("punched_at must be epoch milliseconds when supplied")
        if not isinstance(value, int) or isinstance(value, bool) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value

class AttendanceCorrectionIn(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        allowed = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
        if value is not None and value not in allowed:
            raise ValueError("Invalid attendance status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("Timestamp must be epoch milliseconds")
        return value
    
# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created_at = truncate_seconds(datetime.now(UTC))
    doc["created_at"] = created_at

    try:
        result = db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )

    doc["created_at"] = datetime_to_epoch_ms(created_at)
    doc.pop("_id", None)

    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {}

    if department is not None:
        query["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = datetime_to_epoch_ms(item["created_at"])

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    try:
        emp = db.employees.find_one({"emp_code": body.emp_code})

        if emp is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        # Use supplied epoch milliseconds or current UTC instant.
        if body.punched_at is not None:
            punched_at = epoch_ms_to_datetime(body.punched_at)
        else:
            punched_at = truncate_seconds(datetime.now(UTC))

        attendance_date = attendance_date_for(
            punched_at,
            emp["shift_start"],
            emp["shift_end"],
        )

        late_minutes = compute_late_minutes(
            punched_at,
            emp["shift_start"],
            attendance_date,
        )

        doc = {
            "emp_code": body.emp_code,
            "date": attendance_date,
            "status": body.status,
            "punch_in": punched_at,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }

        result = db.attendance_logs.insert_one(doc)

        return attendance_to_api(doc)

    except HTTPException:
        raise
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        date_filter = {}

        if date_from is not None:
            try:
                parsed_from = date_module.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="date_from must be YYYY-MM-DD",
                )

            if parsed_from.isoformat() != date_from:
                raise HTTPException(
                    status_code=422,
                    detail="date_from must be YYYY-MM-DD",
                )

            date_filter["$gte"] = date_from

        if date_to is not None:
            try:
                parsed_to = date_module.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="date_to must be YYYY-MM-DD",
                )

            if parsed_to.isoformat() != date_to:
                raise HTTPException(
                    status_code=422,
                    detail="date_to must be YYYY-MM-DD",
                )

            date_filter["$lte"] = date_to

        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must be on or before date_to",
            )

        query["date"] = date_filter

    if status is not None:
        if status not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
            raise HTTPException(status_code=422, detail="Invalid status")
        query["status"] = status

    try:
        total = db.attendance_logs.count_documents(query)

        cursor = (
            db.attendance_logs.find(query, {"_id": 1, "emp_code": 1, "date": 1,
                                             "status": 1, "punch_in": 1,
                                             "punch_out": 1, "work_hours": 1,
                                             "late_minutes": 1, "overtime_minutes": 1,
                                             "half_day": 1, "history": 1})
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        items = []

        for doc in cursor:
            items.append(attendance_to_api(doc))

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )



# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    try:
        emp = db.employees.find_one({"emp_code": body.emp_code})

        if emp is None:
            raise HTTPException(404, "Employee not found")

        punched_at = (
            epoch_ms_to_datetime(body.punched_at)
            if body.punched_at is not None
            else truncate_seconds(datetime.now(UTC))
        )

        # Find latest punch-in at or before requested punch-out instant.
        record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": punched_at},
            },
            sort=[("punch_in", DESCENDING)],
        )

        if record is None:
            # Distinguish an out-of-order punch-out from an unknown/missing punch-in.
            future_record = db.attendance_logs.find_one(
                {"emp_code": body.emp_code, "punch_in": {"$gt": punched_at}},
                sort=[("punch_in", ASCENDING)],
            )
            if future_record is not None:
                raise HTTPException(
                    422,
                    "punched_at must be after punch_in and within 24 hours",
                )
            raise HTTPException(404, "No punch-in found")

        if record.get("punch_out") is not None:
            raise HTTPException(409, "Already punched out")

        punch_in_at = truncate_seconds(record["punch_in"])
        elapsed = (punched_at - punch_in_at).total_seconds()

        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(
                422,
                "punched_at must be after punch_in and within 24 hours",
            )

        work_hours = compute_work_hours(punch_in_at, punched_at)
        overtime_minutes = compute_overtime(
            punched_at,
            emp["shift_end"],
            record["date"],
            emp["shift_start"],
        )
        half_day = work_hours < 4.50

        # Conditional update guarantees only one simultaneous punch-out succeeds.
        updated = db.attendance_logs.find_one_and_update(
            {
                "_id": record["_id"],
                "punch_out": None,
            },
            {
                "$set": {
                    "punch_out": punched_at,
                    "work_hours": work_hours,
                    "overtime_minutes": overtime_minutes,
                    "half_day": half_day,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

        if updated is None:
            raise HTTPException(409, "Already punched out")

        return attendance_to_api(updated)

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")
    
#   PATCH /attendance/{emp_code}/{date}
@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(
    emp_code: str,
    date: str,
    body: AttendanceCorrectionIn,
):
    try:
        try:
            parsed_date = date_module.fromisoformat(date)
        except ValueError:
            raise HTTPException(422, "date must be YYYY-MM-DD")

        if parsed_date.isoformat() != date:
            raise HTTPException(422, "date must be YYYY-MM-DD")

        employee = db.employees.find_one({"emp_code": emp_code})
        if employee is None:
            raise HTTPException(404, "Employee not found")

        record = db.attendance_logs.find_one(
            {"emp_code": emp_code, "date": date}
        )
        if record is None:
            raise HTTPException(404, "Attendance record not found")

        supplied = body.model_dump(exclude_unset=True)
        requested_fields = {"status", "punch_in", "punch_out"}

        if not requested_fields.intersection(supplied):
            raise HTTPException(422, "No attendance changes supplied")

        # Explicit null punch timestamps are invalid.
        for field in ("punch_in", "punch_out"):
            if field in supplied and supplied[field] is None:
                raise HTTPException(422, f"{field} cannot be null")

        old_status = record["status"]
        old_in = record.get("punch_in")
        old_out = record.get("punch_out")
        
        if old_in is not None and old_in.tzinfo is None:
            old_in = old_in.replace(tzinfo=UTC)

        if old_out is not None and old_out.tzinfo is None:
            old_out = old_out.replace(tzinfo=UTC)

        if "status" in supplied and supplied["status"] is None:
            raise HTTPException(422, "status cannot be null")

        new_status = supplied.get("status", old_status)

        if new_status in {"ABSENT", "LEAVE"}:
            if "punch_in" in supplied or "punch_out" in supplied:
                raise HTTPException(
                    422,
                    "ABSENT and LEAVE cannot be combined with supplied punch timestamps",
                )

            new_in = None
            new_out = None
            late_minutes = 0
            work_hours = None
            overtime_minutes = 0
            half_day = False

        else:
            new_in = (
                epoch_ms_to_datetime(supplied["punch_in"])
                if "punch_in" in supplied
                else old_in
            )
            new_out = (
                epoch_ms_to_datetime(supplied["punch_out"])
                if "punch_out" in supplied
                else old_out
            )

            if new_in is None:
                raise HTTPException(
                    422,
                    "punch_in required for presence status",
                )

            # Corrected punch-in must belong to original attendance date.
            calculated_date = attendance_date_for(
                new_in,
                employee["shift_start"],
                employee["shift_end"],
            )
            if calculated_date != date:
                raise HTTPException(
                    422,
                    "Corrected punch_in must remain on attendance date",
                )

            late_minutes = compute_late_minutes(
                new_in,
                employee["shift_start"],
                date,
            )

            if new_out is not None:
                elapsed = (new_out - new_in).total_seconds()
                if elapsed <= 0 or elapsed > 24 * 60 * 60:
                    raise HTTPException(
                        422,
                        "punch_out must be after punch_in and within 24 hours",
                    )

                work_hours = compute_work_hours(new_in, new_out)
                overtime_minutes = compute_overtime(
                    new_out,
                    employee["shift_end"],
                    date,
                    employee["shift_start"],
                )
                half_day = work_hours < 4.50
            else:
                work_hours = None
                overtime_minutes = 0
                half_day = False

        final_values = {
            "status": new_status,
            "punch_in": new_in,
            "punch_out": new_out,
            "work_hours": work_hours,
            "late_minutes": late_minutes,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }

        # Compare every mutable and derived field.
        changes = {}
        for field, new_value in final_values.items():
            old_value = record.get(field)

            if (
                field in {"punch_in", "punch_out"}
                and old_value is not None
                and old_value.tzinfo is None
            ):
                old_value = old_value.replace(tzinfo=UTC)

            if old_value != new_value:
                changes[field] = {
                    "from": old_value,
                    "to": new_value,
                }

        if not changes:
            raise HTTPException(422, "Correction would not change record")

        audit_entry = {
            "at": truncate_seconds(datetime.now(UTC)),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        # Optimistic concurrency: stale requests lose with 409.
        concurrency_filter = {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
            "work_hours": record.get("work_hours"),
            "late_minutes": record.get("late_minutes"),
            "overtime_minutes": record.get("overtime_minutes"),
        }
        if "half_day" in record:
            concurrency_filter["half_day"] = record["half_day"]
        else:
            concurrency_filter["half_day"] = {"$exists": False}

        updated = db.attendance_logs.find_one_and_update(
            concurrency_filter,
            {
                "$set": final_values,
                "$push": {"history": audit_entry},
            },
            return_document=ReturnDocument.AFTER,
        )

        if updated is None:
            raise HTTPException(
                409,
                "Attendance record changed; retry correction",
            )

        return attendance_to_api(updated)

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")

def count_weekdays_inclusive(start: date_module, end: date_module) -> int:
    """Count Monday-Friday dates in an inclusive range without scanning records."""
    if end < start:
        return 0

    days = (end - start).days + 1
    weekdays = (days // 7) * 5
    for offset in range(days % 7):
        if (start.weekday() + offset) % 7 < 5:
            weekdays += 1
    return weekdays

def _json_safe(value):
    """Convert BSON values in explain output to JSON-serializable values."""
    if isinstance(value, Timestamp):
        return {"time": value.time, "increment": value.inc}
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date_module):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _explain_aggregation(collection_name: str, pipeline: list) -> dict:
    """Run MongoDB explain against the exact aggregation pipeline."""
    return db.command({
        "explain": {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        },
        "verbosity": "executionStats",
    })
    
#   GET   /analytics/employees/{emp_code}/monthly
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
):
    """Aggregate one employee's stored attendance metrics for a calendar month."""
    try:
        employee = db.employees.find_one(
            {"emp_code": emp_code},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
        if employee is None:
            raise HTTPException(status_code=404, detail="Employee not found")

        year, month_number = (int(part) for part in month.split("-"))
        month_start = date_module(year, month_number, 1)
        if month_number == 12:
            next_month_start = date_module(year + 1, 1, 1)
        else:
            next_month_start = date_module(year, month_number + 1, 1)
        month_end = next_month_start - timedelta(days=1)

        joined_on = date_module.fromisoformat(employee["joined_on"])
        working_start = max(month_start, joined_on)
        working_days = count_weekdays_inclusive(working_start, month_end)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                                0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        summary = next(db.attendance_logs.aggregate(pipeline), None)

        present_days = float(
            Decimal(str(summary["present_days"] if summary else 0)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        attendance_pct = None
        if working_days > 0:
            attendance_pct = float(
                (Decimal(str(present_days)) / Decimal(working_days) * Decimal("100")).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
            )

        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": present_days,
            "leave_days": summary["leave_days"] if summary else 0,
            "late_count": summary["late_count"] if summary else 0,
            "total_late_minutes": summary["total_late_minutes"] if summary else 0,
            "total_overtime_minutes": summary["total_overtime_minutes"] if summary else 0,
            "attendance_pct": attendance_pct,
        }
    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")

#   GET   /analytics/departments/summary
@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: Optional[str] = Query(default=None),
):
    """Aggregate monthly attendance by department, including employees without logs."""
    try:
        year, month_number = (int(part) for part in month.split("-"))
        month_start = date_module(year, month_number, 1)
        if month_number == 12:
            next_month_start = date_module(year + 1, 1, 1)
        else:
            next_month_start = date_module(year, month_number + 1, 1)
        month_end = next_month_start - timedelta(days=1)

        employee_match = {"joined_on": {"$lte": month_end.isoformat()}}
        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"employee_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$employee_code"]},
                                        {"$gte": ["$date", month_start.isoformat()]},
                                        {"$lte": ["$date", month_end.isoformat()]},
                                    ]
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_days": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {
                                                        "$in": [
                                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                                            [2, 3, 4, 5, 6],
                                                        ]
                                                    },
                                                ]
                                            },
                                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                                },
                                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                                "work_hours_sum": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                                ]
                                            },
                                            "$work_hours",
                                            0,
                                        ]
                                    }
                                },
                                "work_hours_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "monthly_attendance",
                }
            },
            {
                "$set": {
                    "monthly_stats": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$monthly_attendance", 0]},
                            {
                                "present_days": 0,
                                "late_count": 0,
                                "total_late_minutes": 0,
                                "leave_count": 0,
                                "on_duty_count": 0,
                                "work_hours_sum": 0,
                                "work_hours_count": 0,
                            },
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$monthly_stats.present_days"},
                    "late_count": {"$sum": "$monthly_stats.late_count"},
                    "total_late_minutes": {"$sum": "$monthly_stats.total_late_minutes"},
                    "leave_count": {"$sum": "$monthly_stats.leave_count"},
                    "on_duty_count": {"$sum": "$monthly_stats.on_duty_count"},
                    "work_hours_sum": {"$sum": "$monthly_stats.work_hours_sum"},
                    "work_hours_count": {"$sum": "$monthly_stats.work_hours_count"},
                }
            },
            {"$sort": {"_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                    "work_hours_sum": 1,
                    "work_hours_count": 1,
                }
            },
        ]

        rows = list(db.employees.aggregate(pipeline))
        items = []
        for row in rows:
            hours_count = row["work_hours_count"]
            avg_work_hours = None
            if hours_count:
                avg_work_hours = float(
                    (Decimal(str(row["work_hours_sum"])) / Decimal(hours_count)).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
            present_days = float(
                Decimal(str(row["present_days"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
            items.append(
                {
                    "department": row["department"],
                    "headcount": row["headcount"],
                    "present_days": present_days,
                    "avg_work_hours": avg_work_hours,
                    "late_count": row["late_count"],
                    "total_late_minutes": row["total_late_minutes"],
                    "leave_count": row["leave_count"],
                    "on_duty_count": row["on_duty_count"],
                }
            )

        return {"month": month, "items": items}
    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")

#   GET   /analytics/leaderboard/late
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = Query(default=None),
):
    """Rank employees by monthly late minutes using MongoDB aggregation."""
    try:
        year, month_number = (int(part) for part in month.split("-"))
        month_start = date_module(year, month_number, 1)
        if month_number == 12:
            next_month_start = date_module(year + 1, 1, 1)
        else:
            next_month_start = date_module(year, month_number + 1, 1)
        month_end = next_month_start - timedelta(days=1)

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lte": month_end.isoformat(),
                    },
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})

        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": "$_id",
                        "name": "$employee.name",
                        "department": "$employee.department",
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
            ]
        )

        items = list(db.attendance_logs.aggregate(pipeline))
        return {"month": month, "items": items}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")

#   GET   /analytics/departments/{department}/trend
def _build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    """Build department trend pipeline with indexed employee/date attendance joins."""
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$set": {
                "_trend_start": {"$dateFromString": {"dateString": from_date}},
                "_trend_end": {"$dateFromString": {"dateString": to_date}},
            }
        },
        {
            "$set": {
                "_calendar": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": "$_trend_start",
                                                "endDate": "$_trend_end",
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": "$_trend_start",
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_calendar"},
        {"$project": {"_id": 0, "date": "$_calendar"}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", {"$literal": department}]},
                                    {"$lte": ["$joined_on", "$$trend_date"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "_department_employees",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "_department_employees.emp_code",
                "foreignField": "emp_code",
                "let": {"trend_date": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$trend_date"]}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "_attendance_rows",
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$_department_employees"},
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$_attendance_rows.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$_attendance_rows.late_count", 0]}, 0]},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}},
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "_moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": [{"$ifNull": ["$_moving_avg_raw", None]}, None]},
                        None,
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": ["$_moving_avg_raw", 10000]}, 0.5]}},
                                10000,
                            ]
                        },
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[str, Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    to_date: Annotated[str, Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")],
):
    """Return one row per calendar day, with MongoDB-generated gap filling."""
    try:
        try:
            start_day = date_module.fromisoformat(from_date)
            end_day = date_module.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Dates must be valid ISO calendar dates")

        if end_day < start_day:
            raise HTTPException(status_code=422, detail="to must be on or after from")
        day_count = (end_day - start_day).days + 1
        if day_count > 92:
            raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail="Department not found")

        pipeline = _build_department_trend_pipeline(department, from_date, to_date)

        items = list(db.employees.aggregate(pipeline))
        return {"department": department, "items": items}
    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")

#   GET   /admin/explain/{endpoint}
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Explain main query for one documented endpoint using executionStats."""
    supported = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in supported:
        raise HTTPException(status_code=422, detail="Unsupported endpoint")

    try:
        if endpoint == "attendance_list":
            query = {}
            if emp_code is not None:
                query["emp_code"] = emp_code
            if date_from is not None or date_to is not None:
                date_filter = {}
                for field, value, operator in (
                    ("date_from", date_from, "$gte"),
                    ("date_to", date_to, "$lte"),
                ):
                    if value is not None:
                        try:
                            parsed = date_module.fromisoformat(value)
                        except ValueError:
                            raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD")
                        if parsed.isoformat() != value:
                            raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD")
                        date_filter[operator] = value
                if date_from and date_to and date_from > date_to:
                    raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
                query["date"] = date_filter
            if status is not None:
                if status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
                    raise HTTPException(status_code=422, detail="Invalid status")
                query["status"] = status
            explain = db.command({
                "explain": {
                    "find": "attendance_logs",
                    "filter": query,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": (page - 1) * page_size,
                    "limit": page_size,
                },
                "verbosity": "executionStats",
            })
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _json_safe(explain)}

        if endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required")
            employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1})
            if employee is None:
                raise HTTPException(status_code=404, detail="Employee not found")
            year, month_number = (int(part) for part in month.split("-"))
            month_start = date_module(year, month_number, 1)
            next_month_start = date_module(year + (month_number == 12), month_number % 12 + 1, 1)
            month_end = next_month_start - timedelta(days=1)
            pipeline = [{'$match': {'emp_code': emp_code, 'date': {'$gte': month_start.isoformat(), '$lte': month_end.isoformat()}}}, {'$group': {'_id': None, 'present_days': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', ['PRESENT', 'WFH', 'ON_DUTY']]}, {'$in': [{'$dayOfWeek': {'$dateFromString': {'dateString': '$date'}}}, [2, 3, 4, 5, 6]]}]}, {'$cond': [{'$eq': [{'$ifNull': ['$half_day', False]}, True]}, 0.5, 1]}, 0]}}, 'leave_days': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}, 'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}}, 'total_overtime_minutes': {'$sum': {'$ifNull': ['$overtime_minutes', 0]}}}}]
            collection_name = "attendance_logs"

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            year, month_number = (int(part) for part in month.split("-"))
            month_start = date_module(year, month_number, 1)
            next_month_start = date_module(year + (month_number == 12), month_number % 12 + 1, 1)
            month_end = next_month_start - timedelta(days=1)
            employee_match = {"joined_on": {"$lte": month_end.isoformat()}}
            if department is not None:
                employee_match["department"] = department
            pipeline = [{'$match': employee_match}, {'$lookup': {'from': 'attendance_logs', 'let': {'employee_code': '$emp_code'}, 'pipeline': [{'$match': {'$expr': {'$and': [{'$eq': ['$emp_code', '$$employee_code']}, {'$gte': ['$date', month_start.isoformat()]}, {'$lte': ['$date', month_end.isoformat()]}]}}}, {'$group': {'_id': None, 'present_days': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', ['PRESENT', 'WFH', 'ON_DUTY']]}, {'$in': [{'$dayOfWeek': {'$dateFromString': {'dateString': '$date'}}}, [2, 3, 4, 5, 6]]}]}, {'$cond': [{'$eq': [{'$ifNull': ['$half_day', False]}, True]}, 0.5, 1]}, 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}, 'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}}, 'leave_count': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}}, 'on_duty_count': {'$sum': {'$cond': [{'$eq': ['$status', 'ON_DUTY']}, 1, 0]}}, 'work_hours_sum': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', ['PRESENT', 'WFH', 'ON_DUTY']]}, {'$ne': [{'$ifNull': ['$work_hours', None]}, None]}]}, '$work_hours', 0]}}, 'work_hours_count': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', ['PRESENT', 'WFH', 'ON_DUTY']]}, {'$ne': [{'$ifNull': ['$work_hours', None]}, None]}]}, 1, 0]}}}}], 'as': 'monthly_attendance'}}, {'$set': {'monthly_stats': {'$ifNull': [{'$arrayElemAt': ['$monthly_attendance', 0]}, {'present_days': 0, 'late_count': 0, 'total_late_minutes': 0, 'leave_count': 0, 'on_duty_count': 0, 'work_hours_sum': 0, 'work_hours_count': 0}]}}}, {'$group': {'_id': '$department', 'headcount': {'$sum': 1}, 'present_days': {'$sum': '$monthly_stats.present_days'}, 'late_count': {'$sum': '$monthly_stats.late_count'}, 'total_late_minutes': {'$sum': '$monthly_stats.total_late_minutes'}, 'leave_count': {'$sum': '$monthly_stats.leave_count'}, 'on_duty_count': {'$sum': '$monthly_stats.on_duty_count'}, 'work_hours_sum': {'$sum': '$monthly_stats.work_hours_sum'}, 'work_hours_count': {'$sum': '$monthly_stats.work_hours_count'}}}, {'$sort': {'_id': 1}}, {'$project': {'_id': 0, 'department': '$_id', 'headcount': 1, 'present_days': 1, 'late_count': 1, 'total_late_minutes': 1, 'leave_count': 1, 'on_duty_count': 1, 'work_hours_sum': 1, 'work_hours_count': 1}}]
            collection_name = "employees"

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            year, month_number = (int(part) for part in month.split("-"))
            month_start = date_module(year, month_number, 1)
            next_month_start = date_module(year + (month_number == 12), month_number % 12 + 1, 1)
            month_end = next_month_start - timedelta(days=1)
            pipeline = [
                {"$match": {"date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}, "late_minutes": {"$gt": 0}}},
                {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
                {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
            ]
            if department is not None:
                pipeline.append({"$match": {"employee.department": department}})
            pipeline.extend([
                {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
                {"$match": {"rank": {"$lte": limit}}},
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
            ])
            collection_name = "attendance_logs"

        else:  # department_trend
            if not department or not from_date or not to_date:
                raise HTTPException(status_code=422, detail="department, from and to are required")
            try:
                start_day = date_module.fromisoformat(from_date)
                end_day = date_module.fromisoformat(to_date)
            except ValueError:
                raise HTTPException(status_code=422, detail="Dates must be valid ISO calendar dates")
            if start_day.isoformat() != from_date or end_day.isoformat() != to_date:
                raise HTTPException(status_code=422, detail="Dates must be valid ISO calendar dates")
            if end_day < start_day:
                raise HTTPException(status_code=422, detail="to must be on or after from")
            if (end_day - start_day).days + 1 > 92:
                raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")
            if db.employees.find_one({"department": department}, {"_id": 1}) is None:
                raise HTTPException(status_code=404, detail="Department not found")
            pipeline = _build_department_trend_pipeline(department, from_date, to_date)
            collection_name = "employees"

        explain = _explain_aggregation(collection_name, pipeline)
        return {"endpoint": endpoint, "collection": collection_name, "explain": _json_safe(explain)}
    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")

# --------------------------------------------------------------------------- #