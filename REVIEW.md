# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses the punch-in's own clock (UTC/naive), not IST, so shift start lands on the wrong time/day. | Punch-in 09:35 IST is stored as 04:05 UTC; "shift 09:30" is applied to 04:05 and gives a nonsense result. | Convert to IST, build shift start from the attendance date. |
| 2 | `compute_late_minutes` | Grace check uses whole minutes: `int(10.016) = 10`, `10 > 10` is false. | Shift 09:30, punch 09:40:01 must give 10, returns 0. | Compare seconds: late if `secs > 600`, value is `secs // 60`. |
| 3 | `compute_late_minutes`, `punch_in` | Overnight shifts ignored (R1). | 22:00-06:00 shift, punch-in 01:00 gets the wrong date and a huge/negative lateness. | `attendance_date()` moves punches before `shift_end` to the previous day. |
| 4 | `compute_work_hours` | `round()` is banker's/float rounding. Seconds not truncated. | 9.125 h returns 9.12, R4 wants 9.13. | Exact `Decimal` division, `ROUND_HALF_UP`, instants truncated to seconds. |
| 5 | `compute_overtime` | No 30-minute minimum; shift end always on the same date; mixes naive and aware datetimes. | 18:45 punch-out gives 15 (should be 0); overnight shift gives negative -> 0; naive `punch_out` raises `TypeError` (500). | Threshold `>= 30`, end date +1 day for overnight, everything tz-aware. |
| 6 | whole file | `half_day` never computed from hours (R5); no work_hours/overtime on punch-out. | Punch-out endpoint missing. | `derived_fields()` computes all four values in one place. |
| 7 | `EmployeeIn` | No validation at all. | `emp_code="x"`, `shift_start="25:99"`, equal shifts, `joined_on="2026-02-30"` all return 201. | Patterns, lengths, real-date check, `shift_start != shift_end` -> 422. |
| 8 | `PunchInIn` | `status` is a free string; `punched_at` accepts seconds, and `if body.punched_at` treats 0 as missing. | Status `"ABSENT"` is accepted; `punched_at=1783312500` (seconds) is accepted. | `Literal` status, `StrictInt` in the epoch-ms range (rejects float/string/bool). |
| 9 | `/health` | Always returns ok, never talks to MongoDB. | Stop MongoDB: still 200. | Ping; 503 on failure. Short server-selection timeout so it answers quickly. |
| 10 | `create_employee` | Check-then-insert race and no unique index; `created_at` is naive local time; returns a datetime, not epoch ms. | 10 parallel creates -> several 201 and duplicates. | Unique index on `emp_code`, `DuplicateKeyError` -> 409; UTC `created_at`, epoch ms in response. |
| 11 | `list_employees` | `skip = page * page_size` skips the first page; `total` ignores the department filter; no sort; page params unvalidated. | Page 1 returns items 21-40; filter by department returns total of all employees. | `(page-1)*page_size`, `count_documents(filter)`, sort by `emp_code`, `ge/le` validation. |
| 12 | `punch_in` | Unknown employee -> `emp` is None -> `TypeError`. | POST with an unknown `emp_code` returns 500. | Explicit 404. |
| 13 | `punch_in` | `datetime.fromtimestamp` / `now()` use the server's local zone; date taken from that zone; microseconds kept. | Server in UTC: a 00:30 IST punch-in gets yesterday's date. | tz-aware UTC instants, truncated to seconds, date from R1 in IST. |
| 14 | `punch_in` | Check-then-insert race, no unique index. | Two parallel requests -> two 201 and two records for the day. | Unique `(emp_code, date)` index; duplicate -> 409. |
| 15 | `punch_in`, `list_attendance` | Leak `id` (ObjectId string) and return datetimes instead of epoch ms; `half_day`/`history` missing on legacy docs. | Response has an `id`; `punch_in` is an ISO string. | One `record_out()` serialiser with defaults for missing fields. |
| 16 | `list_attendance` | Loads the whole collection, sorts and slices in Python; no `emp_code` tie-break; `page=0` gives a negative slice; no validation of dates/status/page size. | 100k docs -> slow and memory heavy; `date_from > date_to` returns 200. | Query with sort/skip/limit in MongoDB, `count_documents`, validation -> 422. |
| 17 | startup | No indexes created. | Explain shows COLLSCAN. | `ensure_indexes()` in the lifespan hook (idempotent). |
| 18 | `MongoClient` | Naive datetimes (no `tz_aware`); hard-coded localhost fallback; 30 s default timeout. | Subtracting instants mixes naive/aware; app silently uses the wrong DB when env is missing. | `tz_aware=True`, env vars required, 4 s selection timeout. |

**Looked at and judged fine:** plain `def` endpoints (FastAPI runs them in a thread pool and the PyMongo client is thread-safe); `load_dotenv()` without `override` (real env wins); comparing ISO date strings with `$gte/$lte` (lexicographic order equals date order); `history: []` on insert; the `IST` constant.
