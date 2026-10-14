# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
python sample_seed.py                                  # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```
Real environment variables win over `.env`. Indexes are created at startup (idempotent); `/health` is 200 once MongoDB answers.

## Tests
`pip install pytest httpx && python -m pytest tests -q` (needs a local MongoDB; uses the throw-away database `attendance_test`).
The tests cover R1-R5, the validation and status codes, concurrency (punch-in, punch-out, create, regularize) and compare
the analytics against a pure-Python reference on randomized data. `tests/` is not needed to run the app.

## Status
Everything in the contract is implemented: Part A fixes (see `REVIEW.md`), all endpoints, the three analytics aggregations,
the trend and the explain endpoint. Checked against 100k attendance records: every explain plan shows `IXSCAN` and no `COLLSCAN`.

## Architecture: before and after

### Before (the starter)
Five endpoints, no indexes, and the logic that mattered (dates, rules, uniqueness) lived in Python with check-then-insert.

```mermaid
flowchart LR
    Client["Client"] --> API["FastAPI starter<br/>5 endpoints, no validation"]
    API --> H["Helpers<br/>naive local time, float rounding,<br/>no overnight shifts"]
    API -->|"find_one, then insert_one<br/>(race window)"| DB[("MongoDB<br/>_id index only")]
    API -->|"find() everything,<br/>sort and page in Python"| DB
    API --> Leak["Response leaks id and raw datetimes"]
```

### After (this submission)
All endpoints are implemented. Rules live in one helper layer, uniqueness and concurrency are decided by MongoDB, and every query is backed by an index.

```mermaid
flowchart TB
    Client["Client"] --> API["FastAPI app/main.py<br/>strict Pydantic validation, 422 / 404 / 409"]

    subgraph Logic["Helper layer (IST, whole seconds, half-up Decimal)"]
        R1["R1 attendance date<br/>overnight aware"]
        R25["R2-R5 late, overtime,<br/>work hours, half day"]
        Ser["Serializers<br/>datetime to epoch ms, no _id"]
    end

    API --> Logic

    subgraph Writes["Write path (race safe)"]
        PI["punch-in<br/>insert_one + unique index"]
        PO["punch-out<br/>compare-and-set on punch_out null"]
        PA["PATCH correction<br/>optimistic lock + $push history"]
    end

    subgraph Reads["Read path"]
        LA["GET /attendance<br/>indexed sort, skip, limit"]
        AN["Analytics<br/>aggregation pipelines"]
        EX["/admin/explain<br/>same builders as the endpoints"]
    end

    API --> Writes
    API --> Reads

    subgraph Mongo["MongoDB, indexes created at startup"]
        E[("employees<br/>emp_code unique<br/>department + emp_code<br/>joined_on")]
        L[("attendance_logs<br/>emp_code + date unique<br/>date + emp_code<br/>status + date + emp_code<br/>emp_code + punch_in")]
    end

    Writes --> L
    LA --> L
    AN -->|"monthly, leaderboard<br/>$group, $rank"| L
    AN -->|"department summary, trend<br/>employees + $lookup + $facet<br/>+ $setWindowFields"| E
    E -.->|"$lookup by emp_code"| L
    EX --> Mongo
```

| Area | Before | After |
|---|---|---|
| Time handling | Naive, machine-local time | Aware UTC in storage, IST for dates and shifts, epoch ms in the API |
| Uniqueness and races | Check, then insert | Unique indexes plus atomic insert and update |
| Listing | Load all, sort in Python | Indexed sort, skip and limit in MongoDB |
| Analytics | Not present | Aggregation pipelines over stored derived values |
| Proof | None | `/admin/explain/*`, `IXSCAN` and no `COLLSCAN` at 100k records |

## Interpretation choices worth knowing
* Punch-out closes the latest record with `punch_in <= punched_at`; if there is none (including a `punched_at` before every
  punch-in) the answer is 404, as the contract describes. `punched_at == punch_in` and more than 24 h later are 422.
* Trend range limit: at most 92 calendar days counting both ends. `moving_avg_7d` averages the unrounded daily rates and
  rounds the result half-up; the rates shown in the rows are rounded the same way.
* Analytics sum the stored `work_hours`, `late_minutes`, `overtime_minutes` and `half_day`; nothing is recomputed from punch times.
* A regularization that loses an optimistic-concurrency race is retried (re-read and recomputed) up to 5 times, then 409.
