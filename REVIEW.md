# REVIEW.md

The original starter version was not included in the current workspace snapshot, so the entries below document concrete risks found while reviewing the current implementation rather than claiming a line-by-line comparison against the original starter.

| # | Where | What is wrong / risk | How to notice | Fix or required follow-up |
|---|---|---|---|---|
| 1 | `serialize_document` | It renames MongoDB `_id` to `id`, while the assignment says Mongo `_id` must never appear in API responses. | Inspect any create/list response. | Remove `_id` entirely rather than exposing a derived `id`. |
| 2 | `compute_late_minutes` | For an overnight shift, the helper builds shift start on the punch-in calendar date. Early-morning punch-ins assigned to the previous shift date need the previous day's start. | Test a 22:00–06:00 shift punched in at 00:30. | Pass the shift date and compute the shift start using that date. |
| 3 | `POST /attendance/punch-out` | Lookup first checks the punch-out calendar date and then only one previous day; the chosen record logic needs explicit overnight-shift handling and race tests. | Test punch-out after midnight and concurrent punch-outs. | Resolve record from employee shift/date rules; preserve conditional atomic update. |
| 4 | `PATCH /attendance/{emp_code}/{date}` | Optimistic update compares only selected fields; simultaneous corrections can still conflict on missing/defaulted fields or other changed fields. | Send parallel corrections with different reasons and inspect history. | Use an atomic update strategy that appends each history entry without silently overwriting another correction; test the chosen conflict semantics. |
| 5 | `GET /analytics/employees/{emp_code}/monthly` | Working-day count is computed in a Python loop, whereas the assignment specifically requires analytics endpoints to use MongoDB aggregation pipelines. | Inspect implementation and query profile. | Move working-day calculation to a suitable aggregation strategy or document/test why the contract permits this helper calculation. |
| 6 | `GET /analytics/departments/summary` | Pipeline starts from employees and includes employees who joined after `date_to`; it does not filter the employee population by `joined_on <= date_to`. | Add an employee who joins after the requested period. | Add a pre-lookup `$match` on `joined_on <= date_to`. |
| 7 | `GET /analytics/departments/summary` | It counts all qualifying-status records as present days even on weekends; the stated rule says present_days counts only weekdays. | Add a Saturday PRESENT record. | Filter weekday records for present-day contribution while retaining weekend late/overtime totals. |
| 8 | `GET /analytics/departments/{department}/trend` | Department employee codes are fetched into Python and then supplied as a potentially large `$in` list; this is not ideal at scale. | Test with a large department. | Use a pipeline `$lookup`/join or another indexed strategy that keeps work in MongoDB. |
| 9 | `GET /analytics/departments/{department}/trend` | Attendance rate is not returned, though the contract requires null for nonworking days or zero headcount. | Compare response fields with `openapi.yaml`. | Add weekday/headcount-aware `attendance_rate` to each daily row. |
| 10 | `GET /admin/explain/{endpoint}` | Only `employees` and `attendance` are accepted; required names are `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend`, with matching filters. | Call the required endpoint names in Swagger. | Implement all required query/pipeline explains with the same filters as the real endpoint. |
| 11 | `ensure_indexes` | The attendance index set includes overlapping compound indexes; the optimal set should be verified against actual query plans and write cost. | Run explain and measure index usage on representative data. | Retain indexes that support required filters/sorts and verify no COLLSCAN at the assignment's scale. |
| 12 | Request models / validation | `punched_at` and correction timestamps are optional and unconstrained Python `int` fields can accept booleans; malformed shift/date values can return 400 rather than the contract's required 422. | Try missing/boolean/seconds/string timestamps and malformed dates. | Use strict integer validation and contract-aligned 422 validation. |

## Reviewed and retained
- The unique `(emp_code, date)` index is necessary to enforce one attendance record per employee and shift date under concurrent punch-ins.
- Conditional `find_one_and_update` for punch-out is a useful atomic guard against two successful punch-outs, but must be verified with concurrent tests.
- Decimal `ROUND_HALF_UP` is appropriate for the specified rounding rule.

This review is intentionally explicit about unresolved items. Do not treat the project as submission-ready until these items are fixed and tested.
