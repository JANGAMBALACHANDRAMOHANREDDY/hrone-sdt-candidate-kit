from datetime import datetime, timedelta, timezone

import pytest
from bson import BSON
from bson.decimal128 import Decimal128
from fastapi import HTTPException

from app import main


UTC = timezone.utc


def ist_time(day: int, hour: int, minute: int, second: int = 0, microsecond: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, second, microsecond, tzinfo=main.IST)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (ist_time(6, 9, 40), 0),
        (ist_time(6, 9, 40, 1), 10),
        (ist_time(6, 10, 15, 59), 45),
    ],
)
def test_late_grace_and_floor_boundaries(instant, expected):
    assert main.compute_late_minutes(instant, "09:30", "2026-07-06") == expected


def test_overtime_threshold_and_overnight_end():
    assert main.compute_overtime(ist_time(6, 18, 59, 59), "18:30", "2026-07-06") == 0
    assert main.compute_overtime(ist_time(6, 19, 0), "18:30", "2026-07-06") == 30
    assert main.compute_overtime(ist_time(7, 6, 29, 59), "06:00", "2026-07-06", "22:00") == 0
    assert main.compute_overtime(ist_time(7, 6, 30), "06:00", "2026-07-06", "22:00") == 30


def test_half_up_hours_and_half_day_use_rounded_hours():
    assert main.compute_work_hours(ist_time(6, 9, 30), ist_time(6, 13, 59, 41)) == 4.49
    assert main.compute_work_hours(ist_time(6, 9, 30), ist_time(6, 13, 59, 42)) == 4.5
    employee = {"shift_start": "09:30", "shift_end": "18:30"}
    assert main._computed_fields(employee, "2026-07-06", ist_time(6, 9, 30), ist_time(6, 13, 59, 41))["half_day"]
    assert not main._computed_fields(
        employee, "2026-07-06", ist_time(6, 9, 30), ist_time(6, 13, 59, 42)
    )["half_day"]


def test_overnight_attendance_date_uses_strict_shift_end_boundary():
    employee = {"shift_start": "22:00", "shift_end": "06:00"}
    before_six = datetime(2026, 7, 7, 0, 29, 59, tzinfo=UTC)
    exactly_six = datetime(2026, 7, 7, 0, 30, 0, tzinfo=UTC)
    assert main._attendance_date(before_six, employee).isoformat() == "2026-07-06"
    assert main._attendance_date(exactly_six, employee).isoformat() == "2026-07-07"


def test_timestamp_conversion_is_strict_and_millisecond_exact():
    with pytest.raises(HTTPException) as seconds:
        main._from_epoch_ms(1_780_000_000)
    assert seconds.value.status_code == 422
    with pytest.raises(HTTPException):
        main._from_epoch_ms(True)
    with pytest.raises(HTTPException):
        main._from_epoch_ms(1_780_000_000_000.0)

    value = datetime(2026, 7, 6, 4, 0, 0, 123_456, tzinfo=UTC)
    assert main._to_epoch_ms(value) % 1000 == 123
    assert main._to_epoch_ms(value) == main._to_epoch_ms(value.replace(microsecond=123_999))


def test_employee_date_is_canonical_string_and_shift_times_must_differ():
    payload = {
        "emp_code": "EMP1234",
        "name": "Test Employee",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01",
    }
    assert main.EmployeeIn.model_validate(payload).joined_on.isoformat() == "2026-07-01"
    for invalid in (20260701, "2026-7-1", "2026-02-30"):
        with pytest.raises(ValueError):
            main.EmployeeIn.model_validate({**payload, "joined_on": invalid})
    with pytest.raises(ValueError):
        main.EmployeeIn.model_validate({**payload, "shift_end": "09:30"})


def _walk(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)
    else:
        yield value


def test_all_analytics_pipelines_serialize_and_trend_is_a_ratio():
    pipelines = [
        main._employee_monthly_pipeline("EMP0001", "2026-07"),
        main._department_summary_pipeline("2026-07", None),
        main._leaderboard_pipeline("2026-07", 10, None),
        main._trend_pipeline("Engineering", datetime(2026, 7, 6).date(), datetime(2026, 7, 12).date()),
    ]
    for pipeline in pipelines:
        BSON.encode({"pipeline": pipeline})

    trend_decimals = [
        value.to_decimal()
        for value in _walk(pipelines[-1])
        if isinstance(value, Decimal128)
    ]
    assert Decimal128("100").to_decimal() not in trend_decimals
    assert any(stage.get("$setWindowFields", {}).get("output", {}).get("_moving_avg") for stage in pipelines[-1])


def test_openapi_has_twelve_operations_across_eleven_paths():
    spec = main.app.openapi()
    operation_count = sum(
        1
        for path_item in spec["paths"].values()
        for method in ("get", "post", "patch", "put", "delete")
        if method in path_item
    )
    assert len(spec["paths"]) == 11
    assert operation_count == 12
    assert {
        ("/employees", "get"),
        ("/employees", "post"),
        ("/attendance/punch-in", "post"),
        ("/attendance/punch-out", "post"),
        ("/attendance", "get"),
        ("/attendance/{emp_code}/{date}", "patch"),
        ("/analytics/employees/{emp_code}/monthly", "get"),
        ("/analytics/departments/summary", "get"),
        ("/analytics/leaderboard/late", "get"),
        ("/analytics/departments/{department}/trend", "get"),
        ("/admin/explain/{endpoint}", "get"),
        ("/health", "get"),
    } <= {
        (path, method)
        for path, item in spec["paths"].items()
        for method in ("get", "post", "patch", "put", "delete")
        if method in item
    }
