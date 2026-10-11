# REVIEW.md

Audit of the supplied starter `app/main.py` against `openapi.yaml`, `DATA_MODEL.md`,
the problem statement, and the sample records.

| # | Where | Defect and observable case | Fix |
|---|---|---|---|
| 1 | Mongo client / `health` | No readiness ping; the route returned 200 even with MongoDB down. | Ping MongoDB and return 503 on PyMongo errors. |
| 2 | `compute_late_minutes` | Used punch-in's wall-clock date as shift start; overnight punches before shift-end were assigned the wrong shift. It also did not normalize seconds and integer conversion truncated negative values toward zero. | Normalize to whole UTC seconds, use R1's attendance date and IST shift start, and floor positive late minutes after the strict 10-minute grace check. |
| 3 | `compute_work_hours` | Python `round()` uses ties-to-even, contrary to half-up; inputs were not truncated to whole seconds. | Use `Decimal` with `ROUND_HALF_UP` after truncating instants. |
| 4 | `compute_overtime` | Always placed shift end on the attendance date, omitted the 30-minute minimum, and used naive/aware-incompatible datetime arithmetic. | Construct shift end in IST on the next date for overnight shifts and apply the 30-minute threshold. |
| 5 | `EmployeeIn` / `PunchInIn` | No contract validation for employee code, email, dates, shift format/equality, status, or strict epoch-millisecond type/range. | Add constrained Pydantic models and validators; ignore client-sent computed fields. |
| 6 | `create_employee` | Check-then-insert allowed concurrent duplicate codes; no unique index. `created_at` was local-naive and returned as an ISO string rather than epoch milliseconds. | Enforce a unique index, map duplicate-key races to 409, and store/return UTC epoch milliseconds. |
| 7 | `list_employees` | Page 1 skipped the first page (`page * page_size`); `total` ignored department; results were not sorted. | Apply filtered count, `emp_code` ascending sort, and `(page-1)*page_size`. |
| 8 | `punch_in` | Unknown employees caused a server error; timestamps used host-local time, falsey values bypassed validation, attendance date ignored overnight rules, and duplicate check plus insert raced. Response exposed a synthetic `id`. | Validate employee and strict timestamp, compute R1 date/derived fields, rely on the unique natural-key index for race-safe 201/409, and return only contract fields. |
| 9 | `list_attendance` | Loaded and sorted every matching document in Python before paging, creating unbounded memory/work; no date-order validation or deterministic employee tie-break; the response added `id`. | Use Mongo count, indexed sort, skip/limit, date validation, and an API serializer that never exposes `_id` or an `id`. |
| 10 | Application startup / all routes | No required indexes, including natural-key uniqueness, so both correctness and analytics query performance were unprotected. | Create idempotent employee and attendance indexes at startup when MongoDB is reachable. |
| 11 | Missing contract routes | Punch-out, regularization/history, four analytics routes, and explain were absent. | Implemented each route, with compare-and-set updates for punch-out and corrections, and MongoDB aggregation pipelines for analytics. |
| 12 | Attendance serialization | Seeded history/half-day fields may be absent; BSON datetimes and nested history timestamps were not converted to integer milliseconds. | Normalize legacy defaults and convert top-level and history instants at the API boundary. |
| 13 | Department trend / `attendance_rate` | The first implementation multiplied `present_count / headcount` by 100, but the contract defines a ratio (for example, 1.0 means full attendance). | Removed the percentage multiplier; retained four-place half-up rounding and the null rules for weekends and zero headcount. |
| 14 | Punch-out / legacy timestamps | A seeded `punch_in` with sub-second precision was used truncated for calculations but returned and left stored untruncated. | The successful compare-and-set now stores and returns the whole-second `punch_in`, as required by R1. |
| 15 | Startup / index creation | A broad `PyMongoError` handler also swallowed failures from index creation, allowing the service to start without indexes that enforce concurrency guarantees. | Only a failed initial ping is tolerated; if MongoDB answers but index creation fails, startup fails instead of serving without required indexes. |
| 16 | Employee request / `joined_on` | Pydantic's general date parsing did not explicitly enforce the contract's canonical `YYYY-MM-DD` string input. | Added strict string and canonical ISO date validation. |
| 17 | Test coverage | The kit had no repeatable automated tests, so the correction and analytics rules were not captured for reruns. | Added database-free contract/unit tests and isolated MongoDB integration tests, which require an explicitly named `test_` database. |

**Checked and retained:** `emp_code` is the intended business key; calendar dates remain ISO strings in MongoDB; MongoDB's default `_id` remains internal; `history` is reserved for manual regularizations. The sample documents agree with the documented storage types and derived-field ownership.
