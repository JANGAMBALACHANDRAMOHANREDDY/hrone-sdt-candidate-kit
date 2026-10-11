"""Employee Attendance & Analytics API, contract v2.0."""
import calendar
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=2500,
    connectTimeoutMS=2500,
)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


def _create_indexes() -> None:
    """Create named, idempotent indexes required by writes and query patterns."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on"
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_on_department"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="attendance_employee_date_status",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING), ("date", DESCENDING)],
        name="attendance_employee_latest_punch",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)], name="attendance_date_late"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_status_employee",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_employee",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Keep the HTTP process available when MongoDB is temporarily offline so
    # /health can report 503. With a reachable database, indexes are ready
    # before serving requests.
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        logger.warning("MongoDB unavailable during startup; indexes deferred: %s", exc)
    else:
        # Do not mask index creation failures: serving without the unique
        # natural-key indexes would break the concurrency contract.
        _create_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _truncate_seconds(value: datetime) -> datetime:
    return _ensure_utc(value).replace(microsecond=0)


def _from_epoch_ms(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    try:
        return _truncate_seconds(EPOCH + timedelta(milliseconds=value))
    except (OverflowError, ValueError):
        raise HTTPException(422, "timestamp is out of range") from None


def _to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    value = _ensure_utc(value)
    delta = value - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _now_seconds() -> datetime:
    return _truncate_seconds(datetime.now(UTC))


def _shift_time(text: str) -> time:
    hour, minute = map(int, text.split(":"))
    return time(hour, minute)


def _attendance_date(punch_in: datetime, employee: dict) -> date:
    local = _ensure_utc(punch_in).astimezone(IST)
    shift_start = _shift_time(employee["shift_start"])
    shift_end = _shift_time(employee["shift_end"])
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < shift_end:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: str | date) -> int:
    day = date.fromisoformat(attendance_day) if isinstance(attendance_day, str) else attendance_day
    start_time = _shift_time(shift_start)
    shift_start_at = datetime.combine(day, start_time, tzinfo=IST).astimezone(UTC)
    elapsed_seconds = int((_truncate_seconds(punch_in) - shift_start_at).total_seconds())
    if elapsed_seconds <= 10 * 60:
        return 0
    return max(0, elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((_truncate_seconds(punch_out) - _truncate_seconds(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def _shift_end_instant(shift_end: str, attendance_day: str | date, shift_start: str) -> datetime:
    day = date.fromisoformat(attendance_day) if isinstance(attendance_day, str) else attendance_day
    if _shift_time(shift_end) <= _shift_time(shift_start):
        day += timedelta(days=1)
    return datetime.combine(day, _shift_time(shift_end), tzinfo=IST).astimezone(UTC)


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: str | date, shift_start: str = "00:00") -> int:
    end = _shift_end_instant(shift_end, attendance_day, shift_start)
    elapsed_seconds = int((_truncate_seconds(punch_out) - end).total_seconds())
    minutes = elapsed_seconds // 60
    return minutes if minutes >= 30 else 0


def _computed_fields(employee: dict, attendance_day: str, punch_in: datetime | None, punch_out: datetime | None) -> dict:
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, employee["shift_start"], attendance_day)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(
            punch_out, employee["shift_end"], attendance_day, employee["shift_start"]
        ),
        "half_day": hours < 4.50,
    }


def _api_history(history: list | None) -> list[dict]:
    result = []
    for entry in history or []:
        changes = {}
        for key, value in (entry.get("changes") or {}).items():
            if key in ("punch_in", "punch_out"):
                changes[key] = {
                    "from": _to_epoch_ms(value.get("from")),
                    "to": _to_epoch_ms(value.get("to")),
                }
            else:
                changes[key] = value
        result.append(
            {
                "at": _to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return result


def _api_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _to_epoch_ms(doc.get("punch_in")),
        "punch_out": _to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _api_history(doc.get("history")),
    }


def _api_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _to_epoch_ms(doc["created_at"]),
    }


def _month_bounds(month: str) -> tuple[str, str, date, date]:
    try:
        year, month_num = map(int, month.split("-"))
        start = date(year, month_num, 1)
        end = date(year, month_num, calendar.monthrange(year, month_num)[1])
    except (TypeError, ValueError):
        raise HTTPException(422, "month must be a valid YYYY-MM calendar month") from None
    return start.isoformat(), end.isoformat(), start, end


def _date_from_string(field: str) -> dict:
    return {"$dateFromString": {"dateString": field, "format": "%Y-%m-%d", "timezone": "UTC"}}


def _weekday_expr(date_field: str) -> dict:
    return {"$in": [{"$dayOfWeek": _date_from_string(date_field)}, [2, 3, 4, 5, 6]]}


def _round_half_up_expr(expr, digits: int) -> dict:
    scale = Decimal128(str(10**digits))
    return {
        "$divide": [
            {
                "$floor": {
                    "$add": [
                        {"$multiply": [{"$toDecimal": expr}, scale]},
                        Decimal128("0.5"),
                    ]
                }
            },
            scale,
        ]
    }


def _attendance_query(
    emp_code: str | None,
    date_from: date | None,
    date_to: date | None,
    status: str | None,
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds = {}
        if date_from is not None:
            bounds["$gte"] = date_from.isoformat()
        if date_to is not None:
            bounds["$lte"] = date_to.isoformat()
        query["date"] = bounds
    if status is not None:
        query["status"] = status
    return query


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end, _, _ = _month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$set": {
                "_month_start": start,
                "_month_end": end,
                "_working_start": {"$cond": [{"$gt": ["$joined_on", start]}, "$joined_on", start]},
            }
        },
        {
            "$set": {
                "_days": {
                    "$cond": [
                        {"$gt": ["$_working_start", "$_month_end"]},
                        0,
                        {
                            "$add": [
                                {
                                    "$dateDiff": {
                                        "startDate": _date_from_string("$_working_start"),
                                        "endDate": _date_from_string("$_month_end"),
                                        "unit": "day",
                                    }
                                },
                                1,
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$set": {
                "_calendar": {
                    "$map": {
                        "input": {"$range": [0, "$_days"]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": _date_from_string("$_working_start"),
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {
            "$set": {
                "_working_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_calendar",
                            "as": "day",
                            "cond": {"$in": [{"$dayOfWeek": "$$day"}, [2, 3, 4, 5, 6]]},
                        }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "overtime_minutes": 1}},
                ],
                "as": "_logs",
            }
        },
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$emp_code",
                "emp_code": {"$first": "$emp_code"},
                "month": {"$first": month},
                "working_days": {"$first": "$_working_days"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$_logs", None]},
                                    {"$in": ["$_logs.status", list(PRESENCE)]},
                                    _weekday_expr("$_logs.date"),
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$_logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$_logs.status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$_logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$_logs.late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$_logs.overtime_minutes", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": 1,
                "working_days": 1,
                "present_days": {"$toDouble": _round_half_up_expr("$present_days", 2)},
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        {
                            "$toDouble": _round_half_up_expr(
                                {
                                    "$multiply": [
                                        {"$divide": [{"$toDecimal": "$present_days"}, {"$toDecimal": "$working_days"}]},
                                        Decimal128("100"),
                                    ]
                                },
                                4,
                            )
                        },
                        None,
                    ]
                },
            }
        },
    ]


def _department_summary_pipeline(month: str, department: str | None) -> list[dict]:
    start, end, _, _ = _month_bounds(month)
    employee_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "status": 1, "date": 1, "work_hours": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "_logs",
            }
        },
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "department": {"$first": "$department"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$_logs", None]},
                                    {"$in": ["$_logs.status", list(PRESENCE)]},
                                    _weekday_expr("$_logs.date"),
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$_logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$_logs.status", list(PRESENCE)]},
                                    {"$ne": ["$_logs.work_hours", None]},
                                ]
                            },
                            {"$toDecimal": "$_logs.work_hours"},
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$_logs.status", list(PRESENCE)]},
                                    {"$ne": ["$_logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$_logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$_logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$_logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$_logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "department": {"$first": "$department"},
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": {"$toDouble": _round_half_up_expr("$present_days", 2)},
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$toDouble": _round_half_up_expr(
                                {"$divide": ["$work_hours_sum", {"$toDecimal": "$work_hours_count"}]},
                                2,
                            )
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def _leaderboard_pipeline(month: str, limit: int, department: str | None) -> list[dict]:
    start, end, _, _ = _month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "emp_code": {"$first": "$emp_code"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": "$_employee.name",
                    "department": "$_employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def _trend_pipeline(department: str, from_day: date, to_day: date) -> list[dict]:
    first = from_day.isoformat()
    last = to_day.isoformat()
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$set": {
                "_calendar": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {"$dateDiff": {"startDate": _date_from_string(first), "endDate": _date_from_string(last), "unit": "day"}},
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {"$dateAdd": {"startDate": _date_from_string(first), "unit": "day", "amount": "$$offset"}},
                    }
                }
            }
        },
        {"$unwind": "$_calendar"},
        {
            "$set": {
                "_date": {"$dateToString": {"date": "$_calendar", "format": "%Y-%m-%d", "timezone": "UTC"}},
                "_is_working_day": {"$in": [{"$dayOfWeek": "$_calendar"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$_date", "dept": "$department"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$department", "$$dept"]}, {"$lte": ["$joined_on", "$$day"]}]}}},
                    {"$count": "count"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$_date", "dept": "$department"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "let": {"code": "$emp_code", "dept": "$$dept"},
                            "pipeline": [
                                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$department", "$$dept"]}]}}},
                                {"$project": {"_id": 0, "emp_code": 1}},
                            ],
                            "as": "_employee",
                        }
                    },
                    {"$unwind": "$_employee"},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE)]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                        }
                    },
                    {"$project": {"_id": 0}},
                ],
                "as": "_daily",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]},
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.late_count", 0]}, 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$_is_working_day", {"$gt": ["$headcount", 0]}]},
                        _round_half_up_expr(
                            {
                                "$divide": [
                                    {"$toDecimal": "$present_count"},
                                    {"$toDecimal": "$headcount"},
                                ]
                            },
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_calendar": 1},
                "output": {"_moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_date",
                "is_working_day": "$_is_working_day",
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {"$toDouble": "$attendance_rate"},
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_moving_avg", None]},
                        None,
                        {"$toDouble": _round_half_up_expr("$_moving_avg", 4)},
                    ]
                },
            }
        },
    ]


def _explain_aggregate(collection: str, pipeline: list[dict]) -> dict:
    return db.command(
        {
            "explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
    )


def _json_safe(value):
    # Explain output may contain BSON values; Extended JSON preserves them
    # without dropping fields from the server's raw explain document.
    return json_util.loads(json_util.dumps(value))


class InputModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(InputModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @field_validator("joined_on", mode="before")
    @classmethod
    def strict_joined_on(cls, value):
        if not isinstance(value, str):
            raise ValueError("joined_on must be a YYYY-MM-DD string")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from None
        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return parsed

    @field_validator("name", "department")
    @classmethod
    def non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self

    def database_document(self) -> dict:
        data = self.model_dump()
        data["joined_on"] = self.joined_on.isoformat()
        data["created_at"] = _now_seconds()
        return data


class PunchInIn(InputModel):
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: PresenceStatus = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_null_punched_at(cls, value):
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return value


class PunchOutIn(InputModel):
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)

    @model_validator(mode="before")
    @classmethod
    def reject_null_punched_at(cls, value):
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return value


class RegularizeIn(InputModel):
    status: Status | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp_fields(cls, value):
        if isinstance(value, dict):
            for field in ("punch_in", "punch_out"):
                if field in value and value[field] is None:
                    raise ValueError(f"{field} must be an epoch-millisecond integer when supplied")
            if "status" in value and value["status"] is None:
                raise ValueError("status must be a supported status when supplied")
        return value

    @field_validator("reason", "regularized_by")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.database_document()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return _api_employee(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort([("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [_api_employee(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = _from_epoch_ms(body.punched_at) if body.punched_at is not None else _now_seconds()
    attendance_day = _attendance_date(punched_at, employee).isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], attendance_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date") from None
    return _api_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: Status | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = _attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_api_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = _from_epoch_ms(body.punched_at) if body.punched_at is not None else _now_seconds()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    punch_in_at = _truncate_seconds(record["punch_in"])
    duration = (punched_at - punch_in_at).total_seconds()
    if duration <= 0 or duration > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    computed = _computed_fields(employee, record["date"], punch_in_at, punched_at)
    update = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_in": record["punch_in"],
            "punch_out": None,
        },
        {"$set": {"punch_in": punch_in_at, "punch_out": punched_at, **computed}},
    )
    if update.modified_count != 1:
        raise HTTPException(409, "attendance record is already punched out")
    record.update({"punch_in": punch_in_at, "punch_out": punched_at, **computed})
    return _api_attendance(record)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn):
    date_str = date.isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    old_status = record["status"]
    old_in = _truncate_seconds(record["punch_in"]) if record.get("punch_in") is not None else None
    old_out = _truncate_seconds(record["punch_out"]) if record.get("punch_out") is not None else None
    fields_set = body.model_fields_set
    new_status = body.status if "status" in fields_set else old_status
    new_in = _from_epoch_ms(body.punch_in) if "punch_in" in fields_set else old_in
    new_out = _from_epoch_ms(body.punch_out) if "punch_out" in fields_set else old_out

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in fields_set or "punch_out" in fields_set:
            raise HTTPException(422, "punch times cannot be supplied with ABSENT or LEAVE")
        new_in = new_out = None
    elif new_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    if new_in is not None and _attendance_date(new_in, employee).isoformat() != date_str:
        raise HTTPException(422, "punch_in must belong to the attendance date")
    if new_out is not None:
        if new_in is None or new_out <= new_in or (new_out - new_in).total_seconds() > 24 * 60 * 60:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    computed = _computed_fields(employee, date_str, new_in, new_out)
    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **computed,
    }
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    changes = {}
    for key in fields:
        old_value = old_values[key]
        new_value = new_values[key]
        if key in ("punch_in", "punch_out"):
            same = old_value == new_value
        elif key == "work_hours":
            same = old_value == new_value
        else:
            same = old_value == new_value
        if not same:
            if key in ("punch_in", "punch_out"):
                changes[key] = {"from": _to_epoch_ms(old_value), "to": _to_epoch_ms(new_value)}
            else:
                changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "regularization does not change the record")

    # Match every mutable value as it was read. A concurrent regularization
    # that changes any field makes this update lose its compare-and-set race;
    # $push appends history atomically with the successful correction.
    snapshot: dict = {"_id": record["_id"]}
    for key in fields:
        if key in record:
            snapshot[key] = record[key]
        else:
            snapshot[key] = {"$exists": False}
    history_entry = {"at": _now_seconds(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    update = db.attendance_logs.update_one(
        snapshot,
        {"$set": new_values, "$push": {"history": history_entry}},
    )
    if update.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; reload before regularizing")
    record.update(new_values)
    record["history"] = [*(record.get("history") or []), history_entry]
    return _api_attendance(record)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    result = list(db.employees.aggregate(_employee_monthly_pipeline(emp_code, month)))
    return result[0]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
):
    items = list(db.employees.aggregate(_department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: str | None = None,
):
    items = list(db.attendance_logs.aggregate(_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_day: date = Query(alias="from"),
    to_day: date = Query(alias="to"),
):
    if to_day < from_day:
        raise HTTPException(422, "to must be on or after from")
    if (to_day - from_day).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(_trend_pipeline(department, from_day, to_day)))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = None,
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: date | None = None,
    date_to: date | None = None,
    status: Status | None = None,
    from_day: date | None = Query(default=None, alias="from"),
    to_day: date | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    supported = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in supported:
        raise HTTPException(422, f"endpoint must be one of {', '.join(sorted(supported))}")
    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        query = _attendance_query(emp_code, date_from, date_to, status)
        raw = db.command(
            {
                "explain": {
                    "find": "attendance_logs",
                    "filter": query,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": (page - 1) * page_size,
                    "limit": page_size,
                },
                "verbosity": "executionStats",
            }
        )
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        raw = _explain_aggregate("employees", _employee_monthly_pipeline(emp_code, month))
        collection = "employees"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        raw = _explain_aggregate("employees", _department_summary_pipeline(month, department))
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        raw = _explain_aggregate("attendance_logs", _leaderboard_pipeline(month, limit, department))
        collection = "attendance_logs"
    else:
        if not department or from_day is None or to_day is None:
            raise HTTPException(422, "department, from and to are required")
        if to_day < from_day or (to_day - from_day).days + 1 > 92:
            raise HTTPException(422, "trend date range must be ordered and at most 92 days")
        raw = _explain_aggregate("employees", _trend_pipeline(department, from_day, to_day))
        collection = "employees"
    return {"endpoint": endpoint, "collection": collection, "explain": _json_safe(raw)}
