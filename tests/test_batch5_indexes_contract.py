from datetime import datetime, timezone

from app import main


def employee_payload(emp_code, **overrides):
    payload = {
        "emp_code": emp_code,
        "name": f"Employee {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Platform",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    payload.update(overrides)
    return payload


def epoch_ms(iso_timestamp):
    return int(datetime.fromisoformat(iso_timestamp).timestamp() * 1000)


def create_employee(client, emp_code, **overrides):
    response = client.post("/employees", json=employee_payload(emp_code, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def insert_attendance(emp_code, date, status="PRESENT"):
    punch_in = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
    main.db.attendance_logs.insert_one(
        {
            "emp_code": emp_code,
            "date": date,
            "status": status,
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
    )


def all_index_names(collection):
    return {index["name"] for index in collection.list_indexes()}


def collect_index_names(value):
    names = set()
    if isinstance(value, dict):
        if isinstance(value.get("indexName"), str):
            names.add(value["indexName"])
        for child in value.values():
            names.update(collect_index_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(collect_index_names(child))
    return names


def test_required_indexes_exist(api_client):
    employee_indexes = all_index_names(main.db.employees)
    attendance_indexes = all_index_names(main.db.attendance_logs)

    assert "uq_employee_code" in employee_indexes
    assert "ix_employee_department_joined" in employee_indexes
    assert "ix_employee_joined_on" in employee_indexes

    assert "uq_attendance_employee_date" in attendance_indexes
    assert "ix_attendance_date_employee" in attendance_indexes
    assert "ix_attendance_status_date" in attendance_indexes


def test_employee_pagination_returns_correct_page_metadata(api_client):
    for code in ("EMP0029", "EMP0027", "EMP0028"):
        create_employee(api_client, code)

    page_one = api_client.get("/employees", params={"page": 1, "page_size": 2})
    page_two = api_client.get("/employees", params={"page": 2, "page_size": 2})

    assert page_one.status_code == 200, page_one.text
    assert page_two.status_code == 200, page_two.text
    assert page_one.json()["total"] == 3
    assert page_one.json()["page"] == 1
    assert page_one.json()["page_size"] == 2
    assert [item["emp_code"] for item in page_one.json()["items"]] == [
        "EMP0027",
        "EMP0028",
    ]
    assert [item["emp_code"] for item in page_two.json()["items"]] == ["EMP0029"]


def test_employee_pagination_rejects_page_size_over_100(api_client):
    response = api_client.get("/employees", params={"page_size": 101})

    assert response.status_code == 422


def test_attendance_list_pagination_and_timestamp_serialization(api_client):
    create_employee(api_client, "EMP0022")
    create_employee(api_client, "EMP0023")

    for code in ("EMP0022", "EMP0023"):
        response = api_client.post(
            "/attendance/punch-in",
            json={
                "emp_code": code,
                "punched_at": epoch_ms("2026-10-12T04:00:00+00:00"),
                "status": "PRESENT",
            },
        )
        assert response.status_code == 201, response.text
        assert isinstance(response.json()["punch_in"], int)

    response = api_client.get("/attendance", params={"page": 1, "page_size": 1})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 1
    assert len(body["items"]) == 1
    assert isinstance(body["items"][0]["punch_in"], int)


def test_explain_attendance_list_returns_execution_stats(api_client):
    create_employee(api_client, "EMP0026")
    insert_attendance("EMP0026", "2026-10-12")

    response = api_client.get(
        "/admin/explain/attendance_list",
        params={"date_from": "2026-10-01", "date_to": "2026-10-31"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == "attendance_list"
    assert body["collection"] == "attendance_logs"
    explain = body["explain"]
    assert "queryPlanner" in explain
    assert "executionStats" in explain
    assert "totalDocsExamined" in explain["executionStats"]
    assert "totalKeysExamined" in explain["executionStats"]

    used_indexes = collect_index_names(explain)
    assert "ix_attendance_date_employee" in used_indexes


def test_explain_rejects_unsupported_endpoint(api_client):
    response = api_client.get("/admin/explain/not_a_real_endpoint")

    assert response.status_code == 422
    assert response.json()["detail"] == "Unsupported endpoint"


def test_employee_created_at_is_date_in_db_and_epoch_ms_in_response(api_client):
    response = api_client.post(
        "/employees",
        json=employee_payload("EMP0024"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body["created_at"], int)

    stored = main.db.employees.find_one({"emp_code": "EMP0024"})
    assert isinstance(stored["created_at"], datetime)


def test_attendance_punch_in_is_date_in_db_and_epoch_ms_in_response(api_client):
    create_employee(api_client, "EMP0025")
    timestamp = epoch_ms("2026-10-12T04:00:00+00:00")

    response = api_client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP0025",
            "punched_at": timestamp,
            "status": "PRESENT",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["punch_in"] == timestamp

    stored = main.db.attendance_logs.find_one({"emp_code": "EMP0025"})
    assert isinstance(stored["punch_in"], datetime)
