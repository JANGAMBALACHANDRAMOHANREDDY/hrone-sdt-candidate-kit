"""MongoDB integration tests; require an explicitly isolated test database."""

import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


TEST_URI = os.getenv("MONGO_TEST_URI")
TEST_DB = os.getenv("MONGO_TEST_DB")
SKIP_REASON = None
if not TEST_URI or not TEST_DB:
    SKIP_REASON = "set MONGO_TEST_URI and MONGO_TEST_DB to run isolated MongoDB integration tests"
elif not TEST_DB.startswith("test_"):
    SKIP_REASON = "MONGO_TEST_DB must start with 'test_' to protect non-test databases"
elif os.getenv("MONGO_TEST_ALLOW_DELETE") != "1":
    SKIP_REASON = "set MONGO_TEST_ALLOW_DELETE=1 only after confirming this is a disposable test database"

TEST_CONFIGURED = SKIP_REASON is None
if TEST_CONFIGURED:
    os.environ["MONGO_URI"] = TEST_URI
    os.environ["MONGO_DB"] = TEST_DB
    main = importlib.import_module("app.main")
    if main.MONGO_URI != TEST_URI or main.MONGO_DB != TEST_DB:
        main.client.close()
        main = importlib.reload(main)
else:
    main = None

pytestmark = pytest.mark.skipif(not TEST_CONFIGURED, reason=SKIP_REASON or "")

UTC = timezone.utc


@pytest.fixture(scope="session")
def api():
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clear_test_collections(api):
    # This only touches the explicitly supplied test_ database, never the
    # sample/default or a production database.
    main.db.employees.delete_many({})
    main.db.attendance_logs.delete_many({})
    yield
    main.db.employees.delete_many({})
    main.db.attendance_logs.delete_many({})


def create_employee(api, code, department="Engineering", joined_on="2026-07-01", start="09:30", end="18:30"):
    return api.post(
        "/employees",
        json={
            "emp_code": code,
            "name": f"Name {code}",
            "email": f"{code.lower()}@example.com",
            "department": department,
            "shift_start": start,
            "shift_end": end,
            "joined_on": joined_on,
        },
    )


def ist(day, hour, minute, second=0, microsecond=0):
    return datetime(2026, 7, day, hour, minute, second, microsecond, tzinfo=main.IST)


def insert_log(code, day, status="PRESENT", punch_in=None, punch_out=None):
    employee = main.db.employees.find_one({"emp_code": code})
    punch_in = punch_in.astimezone(UTC) if punch_in is not None else None
    punch_out = punch_out.astimezone(UTC) if punch_out is not None else None
    computed = main._computed_fields(employee, day, punch_in, punch_out)
    record = {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "history": [],
        **computed,
    }
    main.db.attendance_logs.insert_one(record)
    return record


def test_database_health_employee_crud_pagination_and_duplicate(api):
    assert api.get("/health").status_code == 200
    assert create_employee(api, "EMP0002").status_code == 201
    assert create_employee(api, "EMP0001").status_code == 201
    assert create_employee(api, "EMP0003", department="Sales").status_code == 201
    duplicate = create_employee(api, "EMP0001")
    assert duplicate.status_code == 409

    response = api.get("/employees", params={"department": "Engineering", "page": 1, "page_size": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["items"][0]["emp_code"] == "EMP0001"
    assert isinstance(response.json()["items"][0]["created_at"], int)


def test_missing_resources_and_request_validation(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    assert create_employee(api, "EMP0001").status_code == 201
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    assert api.post(
        "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1_780_000_000}
    ).status_code == 422
    assert api.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Invalid Date",
            "email": "invalid@example.com",
            "department": "Engineering",
            "joined_on": 20260701,
        },
    ).status_code == 422


def test_punch_in_out_and_regularization_history(api):
    assert create_employee(api, "EMP0001").status_code == 201
    punch_in = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": main._to_epoch_ms(ist(6, 10, 15, 59, 999_000))},
    )
    assert punch_in.status_code == 201
    assert punch_in.json()["punch_in"] == main._to_epoch_ms(ist(6, 10, 15, 59))
    assert punch_in.json()["late_minutes"] == 45
    assert punch_in.json()["history"] == []

    punch_out = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": main._to_epoch_ms(ist(6, 19, 0, 59))},
    )
    assert punch_out.status_code == 200
    assert punch_out.json()["work_hours"] == 8.75
    assert punch_out.json()["overtime_minutes"] == 30

    corrected = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_in": main._to_epoch_ms(ist(6, 9, 30)),
            "reason": "Correct biometric time",
            "regularized_by": "hr.admin",
        },
    )
    assert corrected.status_code == 200
    assert corrected.json()["late_minutes"] == 0
    assert len(corrected.json()["history"]) == 1
    assert "punch_in" in corrected.json()["history"][0]["changes"]
    assert api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"reason": "No actual change", "regularized_by": "hr.admin"},
    ).status_code == 422


def test_punch_in_is_race_safe(api):
    assert create_employee(api, "EMP0001").status_code == 201
    payload = {"emp_code": "EMP0001", "punched_at": main._to_epoch_ms(ist(6, 9, 30))}
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: api.post("/attendance/punch-in", json=payload), range(8)))
    assert sum(response.status_code == 201 for response in responses) == 1
    assert sum(response.status_code == 409 for response in responses) == 7


def test_punch_out_is_race_safe(api):
    assert create_employee(api, "EMP0001").status_code == 201
    assert api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": main._to_epoch_ms(ist(6, 9, 30))},
    ).status_code == 201
    payload = {"emp_code": "EMP0001", "punched_at": main._to_epoch_ms(ist(6, 18, 30))}
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: api.post("/attendance/punch-out", json=payload), range(8)))
    assert sum(response.status_code == 200 for response in responses) == 1
    assert sum(response.status_code == 409 for response in responses) == 7


def test_concurrent_regularizations_do_not_lose_history(api):
    assert create_employee(api, "EMP0001").status_code == 201
    assert api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": main._to_epoch_ms(ist(6, 9, 30))},
    ).status_code == 201
    requests = [
        {
            "punch_in": main._to_epoch_ms(ist(6, 9, minute)),
            "reason": f"Correction {minute}",
            "regularized_by": "hr.admin",
        }
        for minute in (31, 32)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda body: api.patch("/attendance/EMP0001/2026-07-06", json=body),
                requests,
            )
        )
    successes = sum(response.status_code == 200 for response in responses)
    assert successes >= 1
    assert all(response.status_code in (200, 409) for response in responses)
    record = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert len(record["history"]) == successes


def test_punch_out_persists_and_returns_whole_second_punch_in(api):
    assert create_employee(api, "EMP0001").status_code == 201
    punch_in = ist(6, 9, 30, microsecond=900_000).astimezone(UTC)
    employee = main.db.employees.find_one({"emp_code": "EMP0001"})
    main.db.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": punch_in,
            "punch_out": None,
            "history": [],
            **main._computed_fields(employee, "2026-07-06", punch_in, None),
        }
    )
    response = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": main._to_epoch_ms(ist(6, 18, 30))},
    )
    expected = main._to_epoch_ms(ist(6, 9, 30))
    assert response.status_code == 200
    assert response.json()["punch_in"] == expected
    assert main.db.attendance_logs.find_one({"emp_code": "EMP0001"})["punch_in"].microsecond == 0


def test_monthly_department_leaderboard_and_gap_filled_trend(api):
    assert create_employee(api, "EMP0001").status_code == 201
    assert create_employee(api, "EMP0002", joined_on="2026-07-15").status_code == 201
    assert create_employee(api, "EMP0003").status_code == 201
    assert create_employee(api, "EMP0004").status_code == 201
    assert create_employee(api, "EMP0005", department="Support", start="22:00", end="06:00").status_code == 201

    insert_log("EMP0001", "2026-07-06", punch_in=ist(6, 9, 30), punch_out=ist(6, 17, 30))
    insert_log("EMP0001", "2026-07-08", status="WFH", punch_in=ist(8, 9, 30), punch_out=ist(8, 13, 59, 41))
    insert_log("EMP0001", "2026-07-11", punch_in=ist(11, 9, 45), punch_out=ist(11, 18, 45))
    insert_log("EMP0003", "2026-07-06", punch_in=ist(6, 10, 0), punch_out=ist(6, 18, 30))
    insert_log("EMP0003", "2026-07-07", punch_in=ist(7, 10, 0), punch_out=ist(7, 18, 30))
    insert_log("EMP0003", "2026-07-08", status="LEAVE")
    insert_log("EMP0004", "2026-07-06", punch_in=ist(6, 10, 30), punch_out=ist(6, 18, 30))
    insert_log("EMP0005", "2026-07-06", punch_in=ist(6, 21, 55), punch_out=ist(7, 6, 40))

    monthly = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert monthly["working_days"] == 23
    assert monthly["present_days"] == 1.5
    assert monthly["late_count"] == 1  # weekend late records remain in late totals

    department = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    engineering = next(item for item in department["items"] if item["department"] == "Engineering")
    assert engineering["headcount"] == 4  # includes the mid-month hire with no logs
    assert engineering["present_days"] == 4.5
    assert engineering["leave_count"] == 1
    assert engineering["avg_work_hours"] == 7.75  # (8.00 + 4.49 + 9.00 + 8.50 + 8.50 + 8.00) / 6

    leaderboard = api.get(
        "/analytics/leaderboard/late",
        params={"month": "2026-07", "department": "Engineering", "limit": 1},
    ).json()["items"]
    assert [(item["emp_code"], item["rank"]) for item in leaderboard] == [
        ("EMP0003", 1),
        ("EMP0004", 1),
    ]

    trend = api.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2026-07-06", "to": "2026-07-12"},
    ).json()["items"]
    assert len(trend) == 7
    assert trend[0]["attendance_rate"] == 1.0
    assert trend[1]["attendance_rate"] == 0.3333
    assert trend[2]["attendance_rate"] == 0.1667
    assert trend[3]["attendance_rate"] == 0.0  # a missing weekday is zero, not omitted
    assert trend[5]["present_count"] == 1.0  # weekend records still count
    assert trend[5]["attendance_rate"] is None
    assert trend[0]["moving_avg_7d"] == 1.0
    assert trend[5]["moving_avg_7d"] is not None

    overnight = api.get("/analytics/employees/EMP0005/monthly", params={"month": "2026-07"}).json()
    assert overnight["total_overtime_minutes"] == 40


def test_all_explain_routes_return_execution_stats(api):
    assert create_employee(api, "EMP0001").status_code == 201
    insert_log("EMP0001", "2026-07-06", punch_in=ist(6, 10, 0), punch_out=ist(6, 18, 30))
    cases = [
        ("/admin/explain/attendance_list", {"page": 1, "page_size": 20}),
        ("/admin/explain/employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("/admin/explain/department_summary", {"month": "2026-07"}),
        ("/admin/explain/late_leaderboard", {"month": "2026-07", "limit": 1}),
        (
            "/admin/explain/department_trend",
            {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-12"},
        ),
    ]
    for path, params in cases:
        response = api.get(path, params=params)
        assert response.status_code == 200, response.text
        assert response.json()["explain"]
