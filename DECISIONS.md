# DECISIONS.md

1. **Indexes.** On `employees`, we created `emp_code` (unique) to enforce identity uniqueness and accelerate joins, plus `(department, joined_on)` for department-filtered headcount range checks. On `attendance_logs`, we created `(emp_code, date)` (unique compound) to prevent duplicate daily punches, `(date: -1, emp_code: 1)` to back the paginated list view, `(date, late_minutes, emp_code)` for the late leaderboard, and `(emp_code, punch_in: -1)` for punch-out lookups. We rejected indexing `status` alone because low cardinality yields poor selectivity and wastes write overhead.

2. **Punch-in race.** Both requests compute the same attendance date. Instead of check-then-insert, both execute `insert_one(doc)`. MongoDB's WiredTiger engine serializes the unique key insertion on `(emp_code, date)`. Exactly one request successfully commits and returns 201; the concurrent request raises `DuplicateKeyError`, which our exception handler intercepts to return HTTP 409.

3. **Ties.** We use standard competition ranking (`$rank` in `$setWindowFields`), skipping ranks after ties (1, 2, 2, 4). Because `limit` is evaluated after ranking (`rank <= limit`), all tied employees at the boundary are included, ordered by `total_late_minutes` descending and `emp_code` ascending.

4. **Headcount.** The aggregation initiates on `employees` (not logs), matching members where `joined_on <= cutoff_date`. We then perform an outer `$lookup` into `attendance_logs` with `preserveNullAndEmptyArrays: True`. Headcount is computed via distinct employee IDs (`$addToSet: "$emp_code"`), guaranteeing employees with zero logs are fully retained.

5. **100x Data Change.** At ~10M records, we would replace on-the-fly aggregations with pre-computed daily and monthly rollup documents (materialized views via MongoDB Change Streams) for O(1) analytics reads, and shard `attendance_logs` on a compound hashed key `(emp_code, date)`.
