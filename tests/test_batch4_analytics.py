from app import main


def employee_payload(emp_code, department="Analytics", joined_on="2026-01-01"):
    return {
        "emp_code": emp_code,
        "name": f"Employee {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": joined_on,
    }


def create_employee(client, emp_code, department="Analytics", joined_on="2026-01-01"):
    response = client.post(
        "/employees",
        json=employee_payload(emp_code, department, joined_on),
    )
    assert response.status_code == 201, response.text
    return response.json()


def insert_attendance(
    emp_code,
    date,
    *,
    status="PRESENT",
    late_minutes=0,
    overtime_minutes=0,
    work_hours=8.0,
    half_day=False,
):
    main.db.attendance_logs.insert_one(
        {
            "emp_code": emp_code,
            "date": date,
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": work_hours,
            "late_minutes": late_minutes,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
            "history": [],
        }
    )


def test_monthly_analytics_handles_employee_with_zero_attendance(api_client):
    create_employee(api_client, "EMP0010", joined_on="2026-10-01")

    response = api_client.get(
        "/analytics/employees/EMP0010/monthly",
        params={"month": "2026-10"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["working_days"] == 22
    assert body["present_days"] == 0
    assert body["leave_days"] == 0
    assert body["late_count"] == 0
    assert body["total_late_minutes"] == 0
    assert body["total_overtime_minutes"] == 0
    assert body["attendance_pct"] == 0.0


def test_monthly_analytics_returns_null_rate_when_no_working_days(api_client):
    create_employee(api_client, "EMP0011", joined_on="2026-11-01")

    response = api_client.get(
        "/analytics/employees/EMP0011/monthly",
        params={"month": "2026-10"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["working_days"] == 0
    assert body["attendance_pct"] is None


def test_department_summary_headcount_includes_employee_without_logs(api_client):
    create_employee(api_client, "EMP0013", department="Platform")
    create_employee(api_client, "EMP0014", department="Platform")
    insert_attendance(
        "EMP0013",
        "2026-10-12",
        status="PRESENT",
        work_hours=8.0,
    )

    response = api_client.get(
        "/analytics/departments/summary",
        params={"month": "2026-10", "department": "Platform"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["department"] == "Platform"
    assert item["headcount"] == 2
    assert item["present_days"] == 1.0


def test_late_leaderboard_assigns_same_rank_to_ties(api_client):
    create_employee(api_client, "EMP0015", department="Platform")
    create_employee(api_client, "EMP0016", department="Platform")
    insert_attendance("EMP0015", "2026-10-12", late_minutes=15)
    insert_attendance("EMP0016", "2026-10-13", late_minutes=15)

    response = api_client.get(
        "/analytics/leaderboard/late",
        params={"month": "2026-10", "limit": 10},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 2
    assert {item["total_late_minutes"] for item in body["items"]} == {15}
    assert {item["rank"] for item in body["items"]} == {1}


def test_department_trend_fills_calendar_gaps(api_client):
    create_employee(api_client, "EMP0017", department="Platform")

    # Only middle date has attendance; trend must still return every date.
    insert_attendance("EMP0017", "2026-10-13", late_minutes=12)

    response = api_client.get(
        "/analytics/departments/Platform/trend",
        params={"from": "2026-10-12", "to": "2026-10-14"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["date"] for item in body["items"]] == [
        "2026-10-12",
        "2026-10-13",
        "2026-10-14",
    ]
    assert [item["headcount"] for item in body["items"]] == [1, 1, 1]
    assert [item["present_count"] for item in body["items"]] == [0, 1, 0]
    assert [item["late_count"] for item in body["items"]] == [0, 1, 0]


def test_department_trend_rejects_reversed_date_range(api_client):
    create_employee(api_client, "EMP0018", department="Platform")

    response = api_client.get(
        "/analytics/departments/Platform/trend",
        params={"from": "2026-10-15", "to": "2026-10-12"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "to must be on or after from"


def test_department_trend_rejects_range_over_92_days(api_client):
    create_employee(api_client, "EMP0019", department="Platform")

    response = api_client.get(
        "/analytics/departments/Platform/trend",
        params={"from": "2026-01-01", "to": "2026-04-03"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Date range must not exceed 92 days"


def test_analytics_rejects_invalid_month_format(api_client):
    create_employee(api_client, "EMP0012")

    response = api_client.get(
        "/analytics/employees/EMP0012/monthly",
        params={"month": "2026-13"},
    )

    assert response.status_code == 422
