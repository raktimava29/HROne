import pytest
from pydantic import ValidationError

from app import main


def employee_payload(**overrides):
    payload = {
        "emp_code": "EMP9201",
        "name": "Shift Test",
        "email": "emp9201@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    payload.update(overrides)
    return payload


def test_correction_strings_are_not_silently_trimmed():
    correction = main.AttendanceCorrectionIn.model_validate({
        "reason": " valid reason ",
        "regularized_by": " hr.admin ",
    })

    assert correction.reason == " valid reason "
    assert correction.regularized_by == " hr.admin "


def test_employee_rejects_identical_shift_times():
    with pytest.raises(ValidationError):
        main.EmployeeIn.model_validate(
            employee_payload(
                shift_start="09:30",
                shift_end="09:30",
            )
        )


def test_employee_accepts_overnight_shift():
    employee = main.EmployeeIn.model_validate(
        employee_payload(
            shift_start="22:00",
            shift_end="06:00",
        )
    )

    assert employee.shift_start == "22:00"
    assert employee.shift_end == "06:00"