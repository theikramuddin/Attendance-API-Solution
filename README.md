# Employee Attendance & Analytics API

## Run
```
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or put both lines in a local .env
export MONGO_DB="attendance"
uvicorn app.main:app --port 8000
```
Needs MongoDB 6.0+ (uses `$setWindowFields`, `$rank`, `$densify`). All indexes are created at startup.
Real environment variables take priority over `.env`. Nothing is hard-coded; the app refuses to start without both variables.

## Notes
- Everything is in `app/main.py`: helpers/rules, models, endpoints, pipeline builders, explain.
- `/admin/explain/{endpoint}` calls the same builder functions as the real endpoints.
- A 409 is returned to the loser of a concurrent PATCH (optimistic concurrency, nothing is overwritten).
