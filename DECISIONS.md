# DECISIONS.md

1. **Indexes.** I create a unique index on `employees.emp_code` so employee codes cannot duplicate, and a unique compound index on `attendance_logs.emp_code + date` so concurrent punch-ins cannot create two records for one shift date. Department/code supports employee filtering and sorting. Date, status/date, and employee/date/status indexes support attendance filters and common date-range queries. I would verify every index with `explain("executionStats")`, because extra indexes slow writes and consume memory.

2. **Punch-in race.** Both requests can look up the employee, but the unique attendance index allows only one insert for the same `(emp_code, date)`. The winning insert returns 201; MongoDB raises a duplicate-key error for the other request, which the API maps to 409.

3. **Ties.** The leaderboard uses MongoDB competition ranking. Equal late-minute totals share the same rank and the next rank is skipped; filtering by rank after ranking keeps all employees tied at the cutoff. Tied output is then sorted by employee code.

4. **Headcount.** The summary starts from employees and looks up matching logs, so an eligible employee with no attendance still contributes one to headcount and zero attendance totals. Employees who joined after the period end must be filtered out first.

5. **100x data.** I would benchmark query plans and indexes with production-like data, reduce redundant indexes, and add integration tests for concurrency and edge cases before considering sharding or pre-aggregated reporting.
