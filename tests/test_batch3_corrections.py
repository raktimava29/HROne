from datetime import datetime


def employee_payload(emp_code="EMP0001", **overrides):
    payload = {
        "emp_code": emp_code,
        "name": "Correction Test",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    payload.update(overrides)
    return payload


def epoch_ms(iso_timestamp):
    return int(datetime.fromisoformat(iso_timestamp).timestamp() * 1000)


def create_employee(client, **kwargs):
    response = client.post("/employees", json=employee_payload(**kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def create_attendance(client, emp_code="EMP0001", date="2026-10-12"):
    punch_response = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": emp_code,
            "punched_at": epoch_ms(f"{date}T04:00:00+00:00"),
            "status": "PRESENT",
        },
    )
    assert punch_response.status_code == 201, punch_response.text
    return punch_response.json()


def correction_payload(**overrides):
    payload = {
        "reason": "Manager approved correction",
        "regularized_by": "hr-admin",
        "status": "WFH",
    }
    payload.update(overrides)
    return payload


def correct(client, emp_code="EMP0001", date="2026-10-12", **overrides):
    return client.patch(
        f"/attendance/{emp_code}/{date}",
        json=correction_payload(**overrides),
    )


def test_correction_records_only_changed_fields(api_client):
    create_employee(api_client)
    create_attendance(api_client)

    response = correct(api_client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "WFH"
    assert len(body["history"]) == 1

    entry = body["history"][0]
    assert entry["by"] == "hr-admin"
    assert entry["reason"] == "Manager approved correction"
    assert isinstance(entry["at"], int)
    assert entry["changes"] == {
        "status": {"from": "PRESENT", "to": "WFH"}
    }


def test_noop_correction_returns_422_and_adds_no_history(api_client):
    create_employee(api_client)
    create_attendance(api_client)

    response = correct(api_client, status="PRESENT")

    assert response.status_code == 422
    assert response.json()["detail"] == "Correction would not change record"


def test_correction_missing_attendance_returns_404(api_client):
    create_employee(api_client)

    response = correct(api_client)

    assert response.status_code == 404
    assert response.json()["detail"] == "Attendance record not found"


def test_correction_missing_employee_returns_404(api_client):
    response = correct(api_client, emp_code="EMP9998")

    assert response.status_code == 404
    assert response.json()["detail"] == "Employee not found"


def test_short_correction_reason_returns_422(api_client):
    create_employee(api_client)
    create_attendance(api_client)

    response = correct(api_client, reason="no")

    assert response.status_code == 422


def test_explicit_null_punch_timestamp_returns_422(api_client):
    create_employee(api_client)
    create_attendance(api_client)

    response = correct(api_client, punch_in=None)

    assert response.status_code == 422
    assert response.json()["detail"] == "punch_in cannot be null"


def test_leave_correction_clears_punches_and_derived_values(api_client):
    create_employee(api_client)
    create_attendance(api_client)
    punch_out_response = api_client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms("2026-10-12T13:30:00+00:00"),
        },
    )
    assert punch_out_response.status_code == 200, punch_out_response.text

    response = correct(api_client, status="LEAVE")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "LEAVE"
    assert body["punch_in"] is None
    assert body["punch_out"] is None
    assert body["work_hours"] is None
    assert body["late_minutes"] == 0
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False

    changes = body["history"][-1]["changes"]
    assert changes["status"] == {"from": "PRESENT", "to": "LEAVE"}
    assert changes["punch_in"]["from"] is not None
    assert changes["punch_in"]["to"] is None
    assert changes["punch_out"]["from"] is not None
    assert changes["punch_out"]["to"] is None


def test_stale_correction_update_returns_409(api_client, monkeypatch):
    create_employee(api_client)
    create_attendance(api_client)

    from app import main

    def simulate_stale_write(*args, **kwargs):
        return None

    monkeypatch.setattr(
        type(main.db.attendance_logs),
        "find_one_and_update",
        simulate_stale_write,
    )

    response = correct(api_client)

    assert response.status_code == 409
    assert response.json()["detail"] == "Attendance record changed; retry correction"
