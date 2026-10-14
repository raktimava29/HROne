from datetime import datetime, timezone


def employee_payload(emp_code="EMP0001", **overrides):
    payload = {
        "emp_code": emp_code,
        "name": "Test Employee",
        "email": "test.employee@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    payload.update(overrides)
    return payload


def epoch_ms(iso_timestamp):
    value = datetime.fromisoformat(iso_timestamp).replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def test_create_employee_returns_201_and_contract_fields(api_client):
    response = api_client.post("/employees", json=employee_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["emp_code"] == "EMP0001"
    assert "id" not in body
    assert isinstance(body["created_at"], int)


def test_invalid_employee_email_returns_422(api_client):
    response = api_client.post(
        "/employees",
        json=employee_payload(email="not-an-email"),
    )

    assert response.status_code == 422


def test_equal_shift_start_and_end_is_rejected(api_client):
    response = api_client.post(
        "/employees",
        json=employee_payload(shift_start="09:30", shift_end="09:30"),
    )

    assert response.status_code == 422


def test_invalid_employee_code_returns_422(api_client):
    response = api_client.post("/employees", json=employee_payload(emp_code="TEST-001"))
    assert response.status_code == 422


def test_duplicate_employee_code_returns_409(api_client):
    first = api_client.post("/employees", json=employee_payload())
    second = api_client.post(
        "/employees",
        json=employee_payload(email="another@example.com"),
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "emp_code already exists"


def test_punch_in_for_missing_employee_returns_404(api_client):
    response = api_client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP9999",
            "punched_at": epoch_ms("2026-10-12T04:00:00"),
            "status": "PRESENT",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Employee not found"


def test_invalid_punch_in_timestamp_returns_422(api_client):
    response = api_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": "not-a-timestamp"},
    )

    assert response.status_code == 422


def test_out_of_range_punch_in_timestamp_returns_422(api_client):
    response = api_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": 1},
    )

    assert response.status_code == 422


def test_duplicate_attendance_for_same_employee_and_date_returns_409(api_client):
    create_response = api_client.post("/employees", json=employee_payload())
    assert create_response.status_code == 201

    # 04:00 UTC = 09:30 IST on 2026-10-12, matching employee shift start.
    punch_payload = {
        "emp_code": "EMP0001",
        "punched_at": epoch_ms("2026-10-12T04:00:00"),
        "status": "PRESENT",
    }

    first = api_client.post("/attendance/punch-in", json=punch_payload)
    second = api_client.post("/attendance/punch-in", json=punch_payload)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "already punched in for this date"
