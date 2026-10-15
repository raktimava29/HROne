# HROne Employee Attendance & Analytics API

FastAPI service backed by MongoDB for employee records, attendance punch-in/out, manual regularization, and attendance analytics. API behavior is defined by [`openapi.yaml`](openapi.yaml); MongoDB document shapes are defined by [`DATA_MODEL.md`](DATA_MODEL.md). Keep both as contract references.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (assignment grader uses MongoDB 7.0)
- Dependencies from `requirements.txt`

## Local setup

Create and activate a virtual environment, then install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Create `.env` in repository root (do not commit it):

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Use a disposable local database for sample data. `python sample_seed.py` replaces documents in the `employees` and `attendance_logs` collections of the configured database; do not run it against data you need to preserve.

Start the API from repository root:

```powershell
uvicorn app.main:app --port 8000 --reload
```

Check readiness at `GET /health`. App reads `MONGO_URI` and `MONGO_DB` from environment; environment variables take precedence over `.env`. Indexes are created idempotently at startup.

## API surface

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}` for `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, or `department_trend`

Read `openapi.yaml` for exact parameters, schemas, status codes, and examples. Instants use epoch milliseconds in the API; calendar dates and months use `YYYY-MM-DD` and `YYYY-MM`. Shift/calendar rules use IST (UTC+05:30). Derived attendance fields are computed by the API and read from stored values by analytics.

## Contract rules implemented

- **R1:** IST attendance date, including overnight shifts; instants truncated to whole seconds.
- **R2:** 10-minute grace period; late only when strictly over 10 minutes, with whole minutes measured from shift start.
- **R3:** Overtime counted in whole minutes only after at least 30 minutes beyond shift end.
- **R4–R5:** Work hours rounded to two decimals using half-up rounding; half-day uses rounded hours below `4.50`.
- **R6–R7:** Presence statuses and Monday–Friday counting follow the contract; weekend records still contribute to late/overtime totals.
- **R8–R9:** Reported values use contract rounding; headcount includes employees joined by period end even without attendance logs.
- **R10:** Pagination starts at page 1, defaults to 20 rows, caps `page_size` at 100, and reports filtered totals.

Analytics are MongoDB aggregation pipelines. Department trend gap-fills calendar days and computes its moving average in MongoDB rather than a Python loop. The explain endpoint returns MongoDB `executionStats` for the corresponding query/pipeline.

## Tests and performance checks

Configure `MONGO_URI` and a separate test database before running tests. `MONGO_TEST_DB` must differ from `MONGO_DB` and include `test` in its name; tests clear collections in that test database.

```powershell
$env:MONGO_TEST_DB = "attendance_test_db"
python -m compileall app
pytest -q
```

For the 100,000-record performance check, seed only the isolated `hrone_perf_test` database and run the API with `MONGO_DB=hrone_perf_test`; see `seed_perf_data.py` and `run_perf_benchmark.py`. Never use `--reset` against the application database.

Latest recorded benchmark run: 5 measured runs after 2 warmups; all cases returned HTTP 200 with zero failed cases. Median latencies: attendance page 176.5 ms, date range 159.2 ms, status filter 157.3 ms, employee monthly 148.6 ms, department summary 604.4 ms, late leaderboard 218.5 ms, 90-day trend 399.8 ms, and attendance-list explain 89.3 ms. The status-filter p95 had a 3.24-second outlier; repeat benchmark if using these numbers for a formal performance claim. Results depend on MongoDB tier, network, and host.

## Submission hygiene

Do not commit `.env`, credentials, database dumps, virtual environments, or generated explain/benchmark artifacts. Keep this repository public through October 31, 2026, as required by the assignment.
