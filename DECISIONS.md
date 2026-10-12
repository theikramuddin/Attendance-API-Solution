# DECISIONS.md

1. **Indexes.** employees: unique `emp_code`; `(department, emp_code)` for the sorted filtered list; `(department, joined_on)` and `(joined_on)` for headcount. attendance_logs: unique `(emp_code, date)` is the natural key and the punch-in race guard; `(date, emp_code)` serves the global sorted list; `(date, late_minutes, emp_code)` covers the leaderboard; `(emp_code, punch_in)` finds the open record at punch-out. Rejected: an index on `status`, because it has only five values and is cheap to filter after the date index.
2. **Punch-in race.** Both requests compute the same date and try to insert. The unique index lets the database accept one; the other gets a duplicate-key error that I turn into 409. There is no read-then-insert.
3. **Ties.** I rank first with `$rank` on the totals, then keep `rank <= limit`. Tied employees share a rank, so both come back and the list can be longer than `limit`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and joins logs with `$lookup`, so each employee is one document whether or not they have logs.
5. **100x data.** Keep per-employee monthly totals updated on punch/PATCH, so analytics read small summary documents, and partition logs by month.
