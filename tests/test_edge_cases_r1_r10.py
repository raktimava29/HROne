from datetime import datetime, timezone

import pytest

from app import main


def epoch_ms(value):
    return int(value.timestamp() * 1000)


def employee_payload(emp_code="EMP9101", **overrides):
    payload = {
        "emp_code": emp_code,
        "name": "Edge Case Employee",
        "email": f"{emp_code.lower()}@example.com",
        "department": "EdgeCases",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    payload.update(overrides)
    return payload


def create_employee(client, emp_code="EMP9101", **overrides):
    response = client.post("/employees", json=employee_payload(emp_code, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def punch_in(client, emp_code, at):
    return client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": at, "status": "PRESENT"},
    )


def punch_out(client, emp_code, at):
    return client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": at},
    )


@pytest.mark.parametrize("bad_value", [1791909000, 1791909000000.5, "1791909000000"])
def test_punch_in_rejects_non_epoch_millisecond_values(api_client, bad_value):
    create_employee(api_client)
    response = api_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9101", "punched_at": bad_value, "status": "PRESENT"},
    )
    assert response.status_code == 422


def test_punch_out_equal_to_punch_in_returns_422(api_client):
    create_employee(api_client)
    at = epoch_ms(datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc))
    assert punch_in(api_client, "EMP9101", at).status_code == 201
    response = punch_out(api_client, "EMP9101", at)
    assert response.status_code == 422, response.text


def test_punch_out_before_punch_in_returns_422(api_client):
    create_employee(api_client)
    at = epoch_ms(datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc))
    assert punch_in(api_client, "EMP9101", at).status_code == 201
    response = punch_out(api_client, "EMP9101", at - 1000)
    assert response.status_code == 422, response.text


def test_punch_out_more_than_24_hours_after_punch_in_returns_422(api_client):
    create_employee(api_client)
    at = epoch_ms(datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc))
    assert punch_in(api_client, "EMP9101", at).status_code == 201
    response = punch_out(api_client, "EMP9101", at + 24 * 60 * 60 * 1000 + 1000)
    assert response.status_code == 422, response.text


def test_punch_out_exactly_24_hours_after_punch_in_is_allowed(api_client):
    create_employee(api_client)
    at = epoch_ms(datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc))
    assert punch_in(api_client, "EMP9101", at).status_code == 201
    response = punch_out(api_client, "EMP9101", at + 24 * 60 * 60 * 1000)
    assert response.status_code == 200, response.text
    assert response.json()["work_hours"] == 24.0


@pytest.mark.parametrize("params", [{"date_from": ""}, {"date_to": ""}])
def test_attendance_list_rejects_empty_date_filter(api_client, params):
    response = api_client.get("/attendance", params=params)
    assert response.status_code == 422, response.text


def test_monthly_metrics_exclude_weekend_presence_but_include_weekend_lateness(api_client):
    create_employee(api_client)
    main.db.attendance_logs.insert_many(
        [
            {
                "emp_code": "EMP9101",
                "date": "2026-10-10",  # Saturday
                "status": "PRESENT",
                "punch_in": None,
                "punch_out": None,
                "work_hours": 8.0,
                "late_minutes": 20,
                "overtime_minutes": 5,
                "half_day": False,
                "history": [],
            },
            {
                "emp_code": "EMP9101",
                "date": "2026-10-12",  # Monday
                "status": "WFH",
                "punch_in": None,
                "punch_out": None,
                "work_hours": 4.5,
                "late_minutes": 11,
                "overtime_minutes": 0,
                "half_day": True,
                "history": [],
            },
        ]
    )

    response = api_client.get(
        "/analytics/employees/EMP9101/monthly", params={"month": "2026-10"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["present_days"] == 0.5
    assert body["late_count"] == 2
    assert body["total_late_minutes"] == 31
    assert body["total_overtime_minutes"] == 5


def test_trend_weekend_rate_is_null_and_moving_average_ignores_nulls(api_client):
    create_employee(api_client, department="EdgeCases")
    # Saturday with no records, then Monday with one present employee.
    response = api_client.get(
        "/analytics/departments/EdgeCases/trend",
        params={"from": "2026-10-10", "to": "2026-10-12"},
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [row["date"] for row in items] == ["2026-10-10", "2026-10-11", "2026-10-12"]
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    assert items[1]["attendance_rate"] is None
    assert items[2]["attendance_rate"] == 0.0
    assert items[2]["moving_avg_7d"] == 0.0
