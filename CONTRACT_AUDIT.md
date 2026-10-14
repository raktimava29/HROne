# HROne contract audit — first pass

Sources checked: `openapi.yaml`, `DATA_MODEL.md`, and current `app/main.py`.

## Fixed in this patch

1. `POST /attendance/punch-in` no longer returns MongoDB `id` / `_id`.
2. Attendance responses share serializer that returns required fields and legacy defaults: `history=[]`, `half_day=false`, `late_minutes=0`, `overtime_minutes=0`, `work_hours=null` when absent.
3. Nested `history[].at` and `history[].changes.punch_in/punch_out.from/to` BSON dates convert to epoch milliseconds.
4. `POST /attendance/punch-in` and `POST /attendance/punch-out` reject explicitly supplied `punched_at: null`; omission still uses current time.
5. `GET /attendance` rejects statuses outside `PRESENT`, `ABSENT`, `LEAVE`, `WFH`, `ON_DUTY`.
6. Supplied empty query strings are not silently treated as omitted filters.
7. Correction audit `history[].at` is truncated to whole seconds before storage.
8. Optimistic concurrency handles legacy records where `half_day` is missing.
9. Request strings are no longer silently stripped before validation, so padded employee codes and email addresses fail contract patterns.

## Still needs verification

- Run full MongoDB-backed suite against your `MONGO_TEST_DB`. This environment lacked `pymongo`, so only Python syntax compilation was verified here.
- Run black-box tests for all R1-R10 edge cases and 100k-record behavior, including explain plans on the actual MongoDB version.
- Verify explain endpoint outputs against real `executionStats` for all five supported `endpoint` values.

## Source-contract inconsistency

The top-level `openapi.yaml` description says clients sending derived fields should have them ignored. The `RegularizeRequest` description says derived fields cannot be sent. Current correction model forbids extra fields, following the endpoint-specific wording. This needs a deliberate interpretation; do not silently treat both statements as equivalent.

## No confirmed analytics mismatch in this pass

Monthly summaries, department summary, leaderboard tie ranking, and trend calculations were inspected against R1-R10. Static inspection did not establish a definite analytics mismatch; edge-case and seeded-data tests remain necessary.
