# Employee Attendance & Analytics API

FastAPI backend for employee attendance, regularization, and analytics. Department summaries, late leaderboards, and daily trends use MongoDB aggregation pipelines. The interactive API preview is available at `/docs` when the service is running.

## Requirements

- Python 3.11 or newer.
- MongoDB 6.0 or newer.
- Set `MONGO_URI` and `MONGO_DB` in the environment. Defaults are `mongodb://localhost:27017` and `attendance_db`.
- No `.env` file is included; environment variables take precedence.

## Run

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

Indexes are created idempotently at startup. Check readiness with `curl -i http://localhost:8000/health`; the endpoint returns 200 only when MongoDB answers, otherwise 503.

## Tests

Run the database-free contract tests:

```bash
python -m pytest -q tests/test_contract.py
```

MongoDB integration tests require a dedicated, disposable MongoDB 6.0+ database. The suite clears its two collections before and after each test, so never point it at a production or shared database. It skips unless all three safeguards are configured:

```bash
export MONGO_TEST_URI='mongodb://localhost:27017'
export MONGO_TEST_DB='test_hrone_sdt'
export MONGO_TEST_ALLOW_DELETE='1'  # explicit opt-in; this database's two collections are cleared
python -m pytest -q tests/test_mongodb.py
```

In the environment used to prepare this submission, the database-free suite passed (10 tests) and the MongoDB integration tests were skipped (9 tests) because no MongoDB server was available. MongoDB execution plans have not been verified here; use a representative large dataset to check for `IXSCAN` and no `COLLSCAN`. No deployment is included.
