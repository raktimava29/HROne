# DECISIONS.md

1. **Indexes.** I use a unique `employees.emp_code` index for employee identity, plus `(department, joined_on)` and `joined_on` indexes for department membership and eligible headcount. Attendance uses a unique `(emp_code, date)` index to enforce one daily record, `(date DESC, emp_code ASC)` for list ordering, and `(status, date DESC)` for status/date filtering. I rejected a separate attendance `emp_code`-only index because the unique compound index already has `emp_code` as its leading key; duplicating it would add storage and write cost.

2. **Punch-in race.** Both requests may read the employee and calculate the same attendance date. MongoDB's unique `(emp_code, date)` index arbitrates the inserts atomically: one insert succeeds with 201; duplicate inserts raise `DuplicateKeyError` and return 409.

3. **Ties.** `$rank` assigns equal late-minute totals the same rank. Filtering `rank <= limit` happens after ranking, so ties at the cutoff remain included; output ties sort by `emp_code`.

4. **Headcount.** Department summary starts from employees joined by month end, then looks up attendance. Employees with no logs remain in the employee stream and count toward headcount.

5. **100x data.** I would benchmark indexed query plans and move repeated monthly/trend calculations to maintained rollups or materialized summaries, with explicit refresh and consistency rules.
