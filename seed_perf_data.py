"""Seed exactly 100,000 synthetic records into isolated hrone_perf_test DB."""
import argparse
import os
from datetime import date, datetime, time, timedelta, timezone
from itertools import islice

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient

load_dotenv()

DB_NAME = "hrone_perf_test"
DEPARTMENTS = ["Engineering", "HR", "Finance", "Sales", "Operations"]
STATUSES = ["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE", "PRESENT", "WFH", "PRESENT"]
UTC = timezone.utc


def chunks(iterable, size):
    iterator = iter(iterable)
    while batch := list(islice(iterator, size)):
        yield batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop ONLY hrone_perf_test before seeding")
    parser.add_argument("--employees", type=int, default=1000)
    parser.add_argument("--days", type=int, default=100)
    args = parser.parse_args()

    if not os.getenv("MONGO_URI"):
        raise SystemExit("MONGO_URI missing. Set it in .env or environment first.")
    if args.employees < 1 or args.employees > 1000:
        raise SystemExit("--employees must be between 1 and 1000")
    if args.days < 1 or args.days > 100:
        raise SystemExit("--days must be between 1 and 100")

    client = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=10000)
    client.admin.command("ping")
    db = client[DB_NAME]

    if args.reset:
        print(f"Dropping isolated database: {DB_NAME}")
        client.drop_database(DB_NAME)
    elif db.employees.count_documents({}) or db.attendance_logs.count_documents({}):
        raise SystemExit(f"{DB_NAME} already contains data. Rerun with --reset to replace synthetic data.")

    now = datetime.now(UTC).replace(microsecond=0)
    employees = []
    for number in range(1, args.employees + 1):
        employees.append({
            "emp_code": f"EMP{number:04d}",
            "name": f"Performance Employee {number:04d}",
            "email": f"employee{number:04d}@perf.example.com",
            "department": DEPARTMENTS[(number - 1) % len(DEPARTMENTS)],
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2025-01-01",
            "created_at": now,
        })
    if employees:
        db.employees.insert_many(employees, ordered=False)

    start_day = date(2026, 1, 1)
    attendance_count = 0
    docs = []
    for day_offset in range(args.days):
        day = start_day + timedelta(days=day_offset)
        date_text = day.isoformat()
        for employee_number in range(1, args.employees + 1):
            index = (day_offset * args.employees + employee_number) % len(STATUSES)
            status = STATUSES[index]
            has_punch = status in {"PRESENT", "WFH", "ON_DUTY"}
            punch_in = datetime.combine(day, time(4, 0 if index % 3 else 15), tzinfo=UTC) if has_punch else None
            punch_out = datetime.combine(day, time(13, 0), tzinfo=UTC) if has_punch else None
            late = 15 if has_punch and index % 3 == 0 else 0
            docs.append({
                "emp_code": f"EMP{employee_number:04d}",
                "date": date_text,
                "status": status,
                "punch_in": punch_in,
                "punch_out": punch_out,
                "work_hours": 8.75 if has_punch else None,
                "late_minutes": late,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            })
            if len(docs) >= 5000:
                db.attendance_logs.insert_many(docs, ordered=False)
                attendance_count += len(docs)
                docs.clear()
    if docs:
        db.attendance_logs.insert_many(docs, ordered=False)
        attendance_count += len(docs)

    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="ix_attendance_status_date")

    print(f"Database: {DB_NAME}")
    print(f"Employees: {db.employees.count_documents({}):,}")
    print(f"Attendance records: {db.attendance_logs.count_documents({}):,}")
    print("Indexes:")
    for collection_name in ("employees", "attendance_logs"):
        for index in db[collection_name].list_indexes():
            print(f"  {collection_name}: {index['name']} -> {index['key']}")
    client.close()


if __name__ == "__main__":
    main()
