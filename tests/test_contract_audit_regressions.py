from datetime import datetime, timezone

from app import main


def employee_payload(emp_code="EMP9001", **overrides):
    payload = {
        "emp_code": emp_code,
        "name": "Contract Audit Employee",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Audit",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    payload.update(overrides)
    return payload


def epoch_ms(value):
    return int(value.timestamp() * 1000)


def create_employee(client, emp_code="EMP9001", **overrides):
    response = client.post("/employees", json=employee_payload(emp_code, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def test_punch_in_response_never_exposes_mongodb_id(api_client):
    create_employee(api_client)
    response = api_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9001", "punched_at": epoch_ms(datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc))},
    )
    assert response.status_code == 201, response.text
    assert "id" not in response.json()
    assert "_id" not in response.json()


def test_punch_in_rejects_explicit_null_punched_at(api_client):
    create_employee(api_client)
    response = api_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9001", "punched_at": None},
    )
    assert response.status_code == 422


def test_punch_out_rejects_explicit_null_punched_at(api_client):
    create_employee(api_client)
    punch_in = api_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9001", "punched_at": epoch_ms(datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc))},
    )
    assert punch_in.status_code == 201, punch_in.text

    response = api_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP9001", "punched_at": None},
    )
    assert response.status_code == 422


def test_attendance_list_rejects_invalid_status(api_client):
    response = api_client.get("/attendance", params={"status": "INVALID"})
    assert response.status_code == 422


def test_attendance_list_serializes_nested_history_and_legacy_defaults(api_client):
    create_employee(api_client)
    punch_in = datetime(2026, 10, 12, 4, 0, 0, tzinfo=timezone.utc)
    old_punch_in = datetime(2026, 10, 12, 4, 5, 0, tzinfo=timezone.utc)
    changed_at = datetime(2026, 10, 13, 5, 0, 0, tzinfo=timezone.utc)

    main.db.attendance_logs.insert_many(
        [
            {
                "emp_code": "EMP9001",
                "date": "2026-10-12",
                "status": "PRESENT",
                "punch_in": punch_in,
                "punch_out": None,
                "work_hours": None,
                "history": [
                    {
                        "at": changed_at,
                        "by": "hr.admin",
                        "reason": "corrected punch-in",
                        "changes": {
                            "punch_in": {"from": old_punch_in, "to": punch_in}
                        },
                    }
                ],
            },
            {
                "emp_code": "EMP9001",
                "date": "2026-10-13",
                "status": "PRESENT",
                "punch_in": punch_in,
                "punch_out": None,
                "work_hours": None,
            },
        ]
    )

    response = api_client.get("/attendance", params={"emp_code": "EMP9001"})
    assert response.status_code == 200, response.text
    records = response.json()["items"]
    legacy_without_history = records[0]
    assert legacy_without_history["date"] == "2026-10-13"
    assert legacy_without_history["history"] == []
    assert legacy_without_history["half_day"] is False
    assert legacy_without_history["late_minutes"] == 0
    assert legacy_without_history["overtime_minutes"] == 0

    record_with_history = records[1]
    assert isinstance(record_with_history["history"][0]["at"], int)
    assert isinstance(record_with_history["history"][0]["changes"]["punch_in"]["from"], int)
    assert isinstance(record_with_history["history"][0]["changes"]["punch_in"]["to"], int)


def test_correction_handles_legacy_record_missing_half_day(api_client):
    create_employee(api_client)
    punch_in = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
    main.db.attendance_logs.insert_one(
        {
            "emp_code": "EMP9001",
            "date": "2026-10-12",
            "status": "PRESENT",
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "history": [],
        }
    )

    response = api_client.patch(
        "/attendance/EMP9001/2026-10-12",
        json={"status": "WFH", "reason": "status correction", "regularized_by": "hr.admin"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "WFH"
    assert response.json()["half_day"] is False
    assert isinstance(response.json()["history"][0]["at"], int)
    stored = main.db.attendance_logs.find_one({"emp_code": "EMP9001", "date": "2026-10-12"})
    assert stored["history"][0]["at"].microsecond == 0


def test_employee_code_whitespace_is_not_silently_trimmed(api_client):
    response = api_client.post("/employees", json=employee_payload(emp_code=" EMP9001 "))
    assert response.status_code == 422


def test_email_whitespace_is_not_silently_trimmed(api_client):
    response = api_client.post(
        "/employees",
        json=employee_payload(emp_code="EMP9002", email=" user@example.com "),
    )
    assert response.status_code == 422
