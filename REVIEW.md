# REVIEW.md

Reviewed starter `app/main.py` against `openapi.yaml`, `DATA_MODEL.md`, and `PROBLEM_STATEMENT.docx`. Rows below describe starter defects and the corresponding corrections in the completed implementation.

| # | Where (starter function / area) | What is wrong | How to notice | How fixed |
|---:|---|---|---|---|
| 1 | `EmployeeIn.validate_emp_code` | Starter accepts a broad alphanumeric/underscore/hyphen code instead of contract pattern `EMP` plus 4–6 digits. | Submit `ABC1234` or `EMP12`; starter accepts values outside contract. | Validate against `^EMP\d{4,6}$`. |
| 2 | `EmployeeIn` field constraints | `email` and `department` maximum lengths differ from contract (254 vs 120; 100 vs 50). | Submit values longer than contract limits. | Align field limits with OpenAPI. |
| 3 | Pydantic request models | `str_strip_whitespace=True` normalizes input before pattern/length validation. | Send padded employee code or email; invalid input can become valid after trimming. | Preserve supplied strings and validate original values. |
| 4 | `create_indexes` | Missing standalone `employees.joined_on` index needed for headcount queries that filter by join date without a department equality filter. | Inspect indexes or explain headcount/summary query on seeded data. | Add `ix_employee_joined_on`. |
| 5 | `list_employees` | `if department:` treats an explicitly supplied empty string as if filter were omitted. | Request `/employees?department=`; result is unfiltered. | Distinguish omitted parameter (`None`) from supplied empty string. |
| 6 | `list_employees` | `created_at` remains a BSON datetime in list results, so API serialization emits a date string instead of contract epoch milliseconds. | Compare `GET /employees` item shape with `Employee.created_at` schema. | Serialize `created_at` to epoch milliseconds. |
| 7 | `punch_in` response | Returns MongoDB-generated `id`, which contract explicitly forbids in API responses. | Inspect successful `POST /attendance/punch-in` body. | Use shared attendance serializer and omit `_id`/`id`. |
| 8 | `PunchInIn.validate_punched_at`, `PunchOutIn.validate_punched_at` | Optional timestamp validators allow explicitly supplied `punched_at: null`, conflating null with omitted value. | Send `{"emp_code":"EMP0001","punched_at":null}`. | Reject explicit null; omission still defaults to current time. |
| 9 | `list_attendance` | Truthiness checks silently omit explicitly supplied empty `emp_code`, `date_from`, `date_to`, and `status` filters. | Request `/attendance?status=` or `/attendance?date_from=`. | Check `is not None`; validate supplied empty values. |
| 10 | `list_attendance` | Accepts unsupported status filters rather than returning 422. | Request `/attendance?status=UNKNOWN`. | Validate against `PRESENT`, `ABSENT`, `LEAVE`, `WFH`, `ON_DUTY`. |
| 11 | `list_attendance` serializer | History conversion targets legacy names such as `old_punch_in` and `changed_at`, not contract `history[].at` and `history[].changes.*.from/to`. | Create a correction, then inspect history timestamps in attendance response. | Convert audit timestamp and nested punch-time values to epoch milliseconds. |
| 12 | Attendance response serialization | Legacy records missing optional stored fields can omit required API fields such as `history`, `half_day`, `late_minutes`, or `overtime_minutes`. | Read seeded/legacy record with absent fields. | Normalize responses with contract defaults (`history=[]`, `half_day=false`, minute totals `0`, `work_hours=null`). |
| 13 | `punch_out` record lookup | A punch-out timestamp earlier than a future punch-in can be reported as 404 (“No punch-in found”) instead of 422 for invalid chronology. | Punch out before the recorded punch-in instant. | Detect future punch-in and return 422. |
| 14 | `correct_attendance` audit entry | `history[].at` is stored with fractional seconds even though contract requires instants truncated to whole seconds before storage. | Inspect stored audit timestamp after PATCH. | Truncate audit timestamp before storing. |
| 15 | Analytics implementation | Starter has no employee-monthly, department-summary, late-leaderboard, or department-trend endpoints. | Call each route from OpenAPI. | Implement all four as MongoDB aggregation pipelines, including cross-collection lookups, ranking, and trend gap filling. |
| 16 | Explain endpoint | Starter has no `GET /admin/explain/{endpoint}` implementation. | Call each of the five supported explain modes. | Add execution-stats explain output for each endpoint's matching query/pipeline and parameters. |

## Inspected and retained

- The unique `employees.emp_code` index correctly enforces unique employee codes, including concurrent duplicate creates.
- The unique `(emp_code, date)` attendance index correctly enforces one record per employee/day and makes simultaneous duplicate punch-ins resolve to one create and conflicts for the rest.
- Punch-out's conditional `find_one_and_update` prevents two concurrent requests from both changing the same open record successfully.
- Correction writes use a conditional update plus `$push` for audit history, avoiding silent overwrite of a concurrently changed record.
- `attendance_date_for`, `compute_late_minutes`, `compute_work_hours`, and `compute_overtime` were retained where their R1–R5 logic matched the contract; work-hour rounding uses `Decimal` with `ROUND_HALF_UP`.
- Pagination bounds and ordering in the starter already enforce page numbers from 1 and `page_size` from 1 to 100; they were retained and tested.

The contract is authoritative for behavior. One wording conflict remains in the supplied OpenAPI description: its global convention says derived request fields should be ignored, while `RegularizeRequest` says derived fields cannot be sent. The correction model follows the endpoint-specific `extra="forbid"` wording.
