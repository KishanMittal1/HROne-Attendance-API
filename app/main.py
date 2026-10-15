"""
Employee Attendance & Analytics API

Run:
    python -m uvicorn app.main:app --reload --port 8000
"""

import os
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.1.0",
)


# ==================================================
# HELPERS
# ==================================================

def now_ist() -> datetime:
    return datetime.now(IST).replace(microsecond=0)


def normalize_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).replace(microsecond=0)


def timestamp_to_ist(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).astimezone(IST).replace(microsecond=0)


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def validate_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail="Date must use YYYY-MM-DD format",
        )


def validate_shift(value: str) -> None:
    try:
        datetime.strptime(value, "%H:%M")
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail="Shift times must use HH:MM format",
        )


def shift_datetimes(day: str, shift_start: str, shift_end: str):
    shift_day = date.fromisoformat(day)
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()

    start = datetime.combine(shift_day, start_time).replace(tzinfo=IST)
    end = datetime.combine(shift_day, end_time).replace(tzinfo=IST)

    if end <= start:
        end += timedelta(days=1)

    return start, end


def attendance_shift_date(punch_in: datetime, employee: dict) -> str:
    """For overnight shifts, early-morning punch-ins belong to the previous shift date."""
    punch_in = normalize_datetime(punch_in)
    start_clock = datetime.strptime(employee["shift_start"], "%H:%M").time()
    end_clock = datetime.strptime(employee["shift_end"], "%H:%M").time()
    overnight = end_clock <= start_clock

    if overnight and punch_in.time() < end_clock:
        return (punch_in.date() - timedelta(days=1)).isoformat()

    return punch_in.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    punch_in = normalize_datetime(punch_in)
    shift_start_dt = datetime.combine(
        punch_in.date(),
        datetime.strptime(shift_start, "%H:%M").time(),
    ).replace(tzinfo=IST)

    minutes = int((punch_in - shift_start_dt).total_seconds() // 60)
    # Late only when strictly more than 10 minutes after shift start.
    return max(0, minutes) if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in = normalize_datetime(punch_in)
    punch_out = normalize_datetime(punch_out)
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(max(0, seconds) / 3600, 2)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    shift_date: str,
    shift_start: str,
) -> int:
    punch_out = normalize_datetime(punch_out)
    _, end_dt = shift_datetimes(shift_date, shift_start, shift_end)
    minutes = int((punch_out - end_dt).total_seconds() // 60)
    return max(0, minutes) if minutes >= 30 else 0


def serialize_document(doc):
    if doc is None:
        return None
    if isinstance(doc, list):
        return [serialize_document(item) for item in doc]
    if isinstance(doc, dict):
        result = {}
        for key, value in doc.items():
            if key == "_id":
                # MongoDB's internal identifier must never be exposed by the API.
                continue
            elif isinstance(value, datetime):
                result[key] = normalize_datetime(value).isoformat()
            elif isinstance(value, (dict, list)):
                result[key] = serialize_document(value)
            else:
                result[key] = value
        return result
    return doc


def ensure_indexes():
    db.employees.create_index(
        [("emp_code", 1)], unique=True, name="uq_employee_code"
    )
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="ix_employee_department_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", -1)], name="ix_attendance_date"
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1)],
        name="ix_attendance_status_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", -1), ("status", 1)],
        name="ix_attendance_employee_date_status",
    )


@app.on_event("startup")
def startup():
    ensure_indexes()


# ==================================================
# REQUEST MODELS
# ==================================================

class EmployeeIn(BaseModel):
    emp_code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class AttendanceCorrectionIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=1)


# ==================================================
# HEALTH
# ==================================================

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok", "database": "connected"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database connection unavailable")


# ==================================================
# EMPLOYEES
# ==================================================

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    validate_date(body.joined_on)
    validate_shift(body.shift_start)
    validate_shift(body.shift_end)

    doc = body.model_dump()
    doc["created_at"] = now_ist()
    try:
        result = db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc["_id"] = result.inserted_id
    return serialize_document(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(query).sort("emp_code", 1).skip(skip).limit(page_size)
    )
    return {
        "items": serialize_document(items),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ==================================================
# PUNCH-IN
# ==================================================

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(400, "Status must be PRESENT, WFH or ON_DUTY")

    ts = timestamp_to_ist(body.punched_at) if body.punched_at is not None else now_ist()
    day = attendance_shift_date(ts, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, employee["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this shift date")

    doc["_id"] = result.inserted_id
    return serialize_document(doc)


# ==================================================
# PUNCH-OUT
# ==================================================

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    ts = timestamp_to_ist(body.punched_at) if body.punched_at is not None else now_ist()

    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": ts.date().isoformat(),
    })

    if record is None:
        previous_day = (ts.date() - timedelta(days=1)).isoformat()
        candidate = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "date": previous_day,
            "punch_out": None,
        })
        if candidate is not None and ts.date() > date.fromisoformat(candidate["date"]):
            record = candidate

    if not record:
        raise HTTPException(404, "Punch-in record not found for this shift")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_time = normalize_datetime(record["punch_in"])
    ts = normalize_datetime(ts)
    if ts < punch_in_time:
        raise HTTPException(400, "Punch-out cannot be before punch-in")

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime = compute_overtime(
        ts, employee["shift_end"], record["date"], employee["shift_start"]
    )

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": work_hours < 4.50,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Attendance record was updated concurrently")
    return serialize_document(updated)


# ==================================================
# LIST ATTENDANCE
# ==================================================

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status

    if date_from or date_to:
        date_query = {}
        if date_from:
            validate_date(date_from)
            date_query["$gte"] = date_from
        if date_to:
            validate_date(date_to)
            date_query["$lte"] = date_to
        query["date"] = date_query

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(query).sort("date", -1).skip(skip).limit(page_size)
    )
    return {
        "items": serialize_document(docs),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ==================================================
# ATTENDANCE CORRECTION
# ==================================================

@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(emp_code: str, date: str, body: AttendanceCorrectionIn):
    validate_date(date)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    if not changes:
        raise HTTPException(400, "No correction fields provided")

    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    if changes.get("status") is not None and changes["status"] not in allowed_statuses:
        raise HTTPException(400, "Invalid attendance status")

    for field in ("punch_in", "punch_out"):
        if field in changes and changes[field] is not None:
            changes[field] = timestamp_to_ist(changes[field])

    new_in = changes.get("punch_in", record.get("punch_in"))
    new_out = changes.get("punch_out", record.get("punch_out"))
    if new_in is not None:
        new_in = normalize_datetime(new_in)
    if new_out is not None:
        new_out = normalize_datetime(new_out)

    if new_in is not None and new_out is not None:
        if new_out < new_in:
            raise HTTPException(400, "Punch-out cannot be before punch-in")
        hours = compute_work_hours(new_in, new_out)
        changes["work_hours"] = hours
        changes["half_day"] = hours < 4.50
        changes["late_minutes"] = compute_late_minutes(new_in, employee["shift_start"])
        changes["overtime_minutes"] = compute_overtime(
            new_out, employee["shift_end"], date, employee["shift_start"]
        )

    history_entry = {
        "reason": body.reason,
        "changed_at": now_ist(),
        "changes": changes.copy(),
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "status": record.get("status"),
        },
        {"$set": changes, "$push": {"history": history_entry}},
    )
    if result.modified_count == 0:
        raise HTTPException(409, "Attendance record changed concurrently")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_document(updated)


# ==================================================
# MONTHLY EMPLOYEE ANALYTICS
# ==================================================

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day)
    joined = date.fromisoformat(employee["joined_on"])
    effective_start = max(start, joined)

    working_days = 0
    if effective_start <= end:
        for offset in range((end - effective_start).days + 1):
            day = effective_start + timedelta(days=offset)
            if day.weekday() < 5:
                working_days += 1

    end_exclusive = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start.isoformat(), "$lt": end_exclusive.isoformat()},
        }},
        {"$group": {
            "_id": None,
            "total_records": {"$sum": 1},
            # A half-day counts as 0.5 present day.
            "present_days": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                0,
            ]}},
            "late_days": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "half_days": {"$sum": {"$cond": [{"$eq": ["$half_day", True]}, 1, 0]}},
            "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
        }},
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {
        "total_records": 0,
        "present_days": 0,
        "late_days": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
        "half_days": 0,
        "total_work_hours": 0,
    }
    stats.pop("_id", None)
    stats["total_work_hours"] = round_half_up(stats["total_work_hours"], 2)
    stats["working_days"] = working_days
    stats["attendance_rate"] = (
        round_half_up(stats["present_days"] / working_days, 4) if working_days else 0.0
    )

    return {
        "emp_code": emp_code,
        "name": employee["name"],
        "department": employee["department"],
        "year": year,
        "month": month,
        **stats,
    }


# ==================================================
# DEPARTMENT SUMMARY
# Includes employees who have no attendance records.
# ==================================================

@app.get("/analytics/departments/summary")
def department_summary(date_from: str = Query(...), date_to: str = Query(...)):
    start = validate_date(date_from)
    end = validate_date(date_to)
    if start > end:
        raise HTTPException(400, "date_from must be <= date_to")

    pipeline = [
        {"$match": {"joined_on": {"$lte": date_to}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", date_from]},
                    {"$lte": ["$date", date_to]},
                ]}}},
                {"$group": {
                    "_id": None,
                    # Half-day attendance contributes 0.5 to present_days.
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$lte": [
                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}},
                                6,
                            ]},
                            {"$gte": [
                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}},
                                2,
                            ]},
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0,
                    ]}},
                    "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                    "work_hours_sum": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$work_hours", None]},
                        ]},
                        "$work_hours",
                        0,
                    ]}},
                    "work_hours_count": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$work_hours", None]},
                        ]},
                        1,
                        0,
                    ]}},
                }},
            ],
            "as": "attendance_stats",
        }},
        {"$set": {"stats": {"$ifNull": [
            {"$arrayElemAt": ["$attendance_stats", 0]},
            {"present_days": 0, "late_minutes": 0, "overtime_minutes": 0,
             "work_hours_sum": 0, "work_hours_count": 0},
        ]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "late_minutes": {"$sum": "$stats.late_minutes"},
            "overtime_minutes": {"$sum": "$stats.overtime_minutes"},
            "work_hours_sum": {"$sum": "$stats.work_hours_sum"},
            "work_hours_count": {"$sum": "$stats.work_hours_count"},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "late_minutes": 1,
            "overtime_minutes": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2]},
                None,
            ]},
        }},
        {"$sort": {"department": 1}},
    ]

    return {
        "date_from": date_from,
        "date_to": date_to,
        "departments": list(db.employees.aggregate(pipeline)),
    }


# ==================================================
# LATE LEADERBOARD
# Competition ranking: 1, 2, 2, 4.
# ==================================================

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    date_from: str = Query(...),
    date_to: str = Query(...),
    limit: int = Query(10, ge=1, le=100),
):
    start = validate_date(date_from)
    end = validate_date(date_to)
    if start > end:
        raise HTTPException(400, "date_from must be <= date_to")

    pipeline = [
        {"$match": {
            "date": {"$gte": date_from, "$lte": date_to},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "late_minutes": {"$sum": "$late_minutes"},
            "late_days": {"$sum": 1},
        }},
        {"$sort": {"late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$set": {"employee": {"$arrayElemAt": ["$employee", 0]}}},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "late_minutes": 1,
            "late_days": 1,
            "rank": 1,
        }},
        {"$sort": {"rank": 1, "emp_code": 1}},
    ]

    return {
        "date_from": date_from,
        "date_to": date_to,
        "limit": limit,
        "leaderboard": list(db.attendance_logs.aggregate(pipeline)),
    }


# ==================================================
# DEPARTMENT DAILY TREND
# Requires MongoDB version supporting $densify.
# ==================================================

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(...), date_to: str = Query(...)):
    start = validate_date(date_from)
    end = validate_date(date_to)
    if start > end:
        raise HTTPException(400, "date_from must be <= date_to")
    if (end - start).days > 91:
        raise HTTPException(400, "Date range cannot exceed 92 days")

    employee_codes = [
        employee["emp_code"]
        for employee in db.employees.find(
            {"department": department},
            {"_id": 0, "emp_code": 1},
        )
    ]
    if not employee_codes:
        return {
            "department": department,
            "date_from": date_from,
            "date_to": date_to,
            "trend": [],
        }

    start_dt = datetime.combine(start, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time()).replace(tzinfo=timezone.utc)

    pipeline = [
        {"$match": {
            "emp_code": {"$in": employee_codes},
            "date": {"$gte": date_from, "$lte": date_to},
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$eq": ["$half_day", True]},
                        0.5,
                        1
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
        {"$project": {
            "_id": 0,
            "day": {"$dateFromString": {
                "dateString": "$_id",
                "format": "%Y-%m-%d",
                "timezone": "UTC",
            }},
            "present_count": 1,
            "late_count": 1,
            "overtime_minutes": 1,
        }},
        {"$densify": {
            "field": "day",
            "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]},
        }},
        {"$fill": {"output": {
            "present_count": {"value": 0},
            "late_count": {"value": 0},
            "overtime_minutes": {"value": 0},
        }}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "moving_avg_present": {
                    "$avg": "$present_count",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {
                "date": "$day",
                "format": "%Y-%m-%d",
                "timezone": "UTC",
            }},
            "present_count": 1,
            "late_count": 1,
            "overtime_minutes": 1,
            "moving_avg_present": {"$round": ["$moving_avg_present", 2]},
        }},
        {"$sort": {"date": 1}},
    ]

    return {
        "department": department,
        "date_from": date_from,
        "date_to": date_to,
        "trend": list(db.attendance_logs.aggregate(pipeline)),
    }


# ==================================================
# ==================================================
# MONGODB EXPLAIN
# ==================================================



@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    import re

    supported = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in supported:
        raise HTTPException(
            status_code=400,
            detail=f"Supported endpoints: {', '.join(supported)}",
        )

    def required(value, name):
        if value is None or value == "":
            raise HTTPException(422, f"{name} is required")
        return value

    def check_month(value):
        required(value, "month")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            raise HTTPException(422, "month must use YYYY-MM format")
        return value

    def check_date(value, name):
        required(value, name)
        validate_date(value)
        return value

    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status

        if date_from or date_to:
            date_filter = {}
            if date_from:
                date_filter["$gte"] = check_date(date_from, "date_from")
            if date_to:
                date_filter["$lte"] = check_date(date_to, "date_to")
            query["date"] = date_filter

        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        emp_code = required(emp_code, "emp_code")
        month = check_month(month)
        start = month + "-01"
        year, mon = map(int, month.split("-"))
        end_exclusive = (
            date(year + 1, 1, 1)
            if mon == 12
            else date(year, mon + 1, 1)
        ).isoformat()

        command = {
            "aggregate": "attendance_logs",
            "pipeline": [
                {"$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start, "$lt": end_exclusive},
                }},
                {"$group": {
                    "_id": "$emp_code",
                    "total_records": {"$sum": 1},
                }},
            ],
            "cursor": {},
        }
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        month = check_month(month)
        year, mon = map(int, month.split("-"))
        start = month + "-01"
        end = (
            date(year + 1, 1, 1) - timedelta(days=1)
            if mon == 12
            else date(year, mon + 1, 1) - timedelta(days=1)
        ).isoformat()

        pipeline = [
            {"$match": {"joined_on": {"$lte": end}}},
        ]
        if department:
            pipeline[0]["$match"]["department"] = department
        pipeline.extend([
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", start]},
                        {"$lte": ["$date", end]},
                    ]}}},
                    {"$group": {
                        "_id": None,
                        "total_records": {"$sum": 1},
                    }},
                ],
                "as": "stats",
            }},
            {"$project": {
                "_id": 0,
                "emp_code": 1,
                "department": 1,
                "stats": 1,
            }},
        ])
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "employees"

    elif endpoint == "late_leaderboard":
        month = check_month(month)
        year, mon = map(int, month.split("-"))
        start = month + "-01"
        end = (
            date(year + 1, 1, 1) - timedelta(days=1)
            if mon == 12
            else date(year, mon + 1, 1) - timedelta(days=1)
        ).isoformat()

        match = {
            "date": {"$gte": start, "$lte": end},
            "late_minutes": {"$gt": 0},
        }
        pipeline = [{"$match": match}]
        pipeline.append({"$group": {
            "_id": "$emp_code",
            "late_minutes": {"$sum": "$late_minutes"},
            "late_days": {"$sum": 1},
        }})
        pipeline.append({"$sort": {"late_minutes": -1, "_id": 1}})
        pipeline.append({"$limit": limit})
        if department:
            pipeline.extend([
                {"$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }},
                {"$match": {"employee.department": department}},
            ])
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"

    else:  # department_trend
        department = required(department, "department")
        start = check_date(from_date, "from")
        end = check_date(to_date, "to")
        if start > end:
            raise HTTPException(422, "'from' must be <= 'to'")
        if (end - start).days > 91:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        pipeline = [
            {"$match": {"date": {"$gte": start, "$lte": end}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }},
            {"$match": {"employee.department": department}},
            {"$project": {"employee": 0}},
            {"$sort": {"date": 1}},
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"

    explanation = db.command(
        "explain",
        command,
        verbosity="executionStats",
    )
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explanation,
    }
