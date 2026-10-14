from datetime import datetime


def employee_payload(emp_code="EMP0001", **overrides):
    payload = {
        "emp_code": emp_code,
        "name": "Calculation Test",
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


def punch_in(client, emp_code, timestamp, status="PRESENT"):
    return client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": timestamp, "status": status},
    )


def punch_out(client, emp_code, timestamp):
    return client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": timestamp},
    )


def test_late_grace_exactly_10_minutes_is_not_late(api_client):
    create_employee(api_client)
    response = punch_in(
        api_client,
        "EMP0001",
        epoch_ms("2026-10-12T04:10:00+00:00"),  # 09:40 IST; shift starts 09:30
    )

    assert response.status_code == 201, response.text
    assert response.json()["late_minutes"] == 0


def test_late_minutes_count_after_grace_period(api_client):
    create_employee(api_client)
    response = punch_in(
        api_client,
        "EMP0001",
        epoch_ms("2026-10-12T04:11:00+00:00"),  # 09:41 IST; 11 minutes late
    )

    assert response.status_code == 201, response.text
    assert response.json()["late_minutes"] == 11


def test_punch_timestamp_truncated_to_whole_seconds(api_client):
    create_employee(api_client)
    base_ms = epoch_ms("2026-10-12T04:00:00+00:00")
    response = punch_in(api_client, "EMP0001", base_ms + 987)

    assert response.status_code == 201, response.text
    assert response.json()["punch_in"] == base_ms


def test_overtime_below_30_minutes_is_zero(api_client):
    create_employee(api_client)
    start_ms = epoch_ms("2026-10-12T04:00:00+00:00")  # 09:30 IST
    end_ms = epoch_ms("2026-10-12T13:29:59+00:00")    # 18:59:59 IST

    start_response = punch_in(api_client, "EMP0001", start_ms)
    assert start_response.status_code == 201, start_response.text

    end_response = punch_out(api_client, "EMP0001", end_ms)
    assert end_response.status_code == 200, end_response.text
    assert end_response.json()["overtime_minutes"] == 0


def test_overtime_exactly_30_minutes_is_counted(api_client):
    create_employee(api_client)
    start_ms = epoch_ms("2026-10-12T04:00:00+00:00")  # 09:30 IST
    end_ms = epoch_ms("2026-10-12T13:30:00+00:00")    # 19:00 IST

    start_response = punch_in(api_client, "EMP0001", start_ms)
    assert start_response.status_code == 201, start_response.text

    end_response = punch_out(api_client, "EMP0001", end_ms)
    assert end_response.status_code == 200, end_response.text
    assert end_response.json()["overtime_minutes"] == 30


def test_half_day_below_4_50_hours(api_client):
    create_employee(api_client)
    start_ms = epoch_ms("2026-10-12T04:00:00+00:00")  # 09:30 IST
    end_ms = epoch_ms("2026-10-12T08:29:00+00:00")    # 13:59 IST; 4h29m

    start_response = punch_in(api_client, "EMP0001", start_ms)
    assert start_response.status_code == 201, start_response.text

    end_response = punch_out(api_client, "EMP0001", end_ms)
    assert end_response.status_code == 200, end_response.text
    assert end_response.json()["work_hours"] == 4.48
    assert end_response.json()["half_day"] is True


def test_exactly_4_50_work_hours_is_not_half_day(api_client):
    create_employee(api_client)
    start_ms = epoch_ms("2026-10-12T04:00:00+00:00")  # 09:30 IST
    end_ms = epoch_ms("2026-10-12T08:30:00+00:00")    # 14:00 IST; 4h30m

    start_response = punch_in(api_client, "EMP0001", start_ms)
    assert start_response.status_code == 201, start_response.text

    end_response = punch_out(api_client, "EMP0001", end_ms)
    assert end_response.status_code == 200, end_response.text
    assert end_response.json()["work_hours"] == 4.5
    assert end_response.json()["half_day"] is False


def test_overnight_shift_uses_shift_start_date(api_client):
    create_employee(
        api_client,
        emp_code="EMP0002",
        shift_start="22:00",
        shift_end="06:00",
    )
    response = punch_in(
        api_client,
        "EMP0002",
        epoch_ms("2026-10-11T19:30:00+00:00"),  # 2026-10-12 01:00 IST
    )

    assert response.status_code == 201, response.text
    assert response.json()["date"] == "2026-10-11"


def test_overnight_shift_punch_out_calculates_8_hours(api_client):
    create_employee(
        api_client,
        emp_code="EMP0003",
        shift_start="22:00",
        shift_end="06:00",
    )
    start_ms = epoch_ms("2026-10-12T16:30:00+00:00")  # 22:00 IST
    end_ms = epoch_ms("2026-10-13T00:30:00+00:00")    # 06:00 IST next day

    start_response = punch_in(api_client, "EMP0003", start_ms)
    assert start_response.status_code == 201, start_response.text
    assert start_response.json()["date"] == "2026-10-12"

    end_response = punch_out(api_client, "EMP0003", end_ms)
    assert end_response.status_code == 200, end_response.text
    assert end_response.json()["work_hours"] == 8.0
    assert end_response.json()["overtime_minutes"] == 0
    assert end_response.json()["half_day"] is False
