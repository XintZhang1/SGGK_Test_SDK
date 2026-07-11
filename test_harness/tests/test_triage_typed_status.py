from __future__ import annotations

import json
from pathlib import Path

from triage_artifacts import classify_case


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_expected_nonzero_typed_status_is_not_api_failure(tmp_path: Path) -> None:
    case = tmp_path / "offset2d_expected_diagnostic"
    write_json(case / "manifest.json", {"case_id": case.name, "api": "api_offset2d"})
    typed = {
        "status_semantics": "offset2d_status_enum",
        "expected_status": "CanNotConnect",
        "actual_status": "CanNotConnect",
        "expected_status_matched": True,
        "test_outcome_succeeded": True,
    }
    write_json(
        case / "report" / "status.json",
        {"succeeded": False, "error_code": 3, "error_message": "CanNotConnect", **typed},
    )
    write_json(case / "report" / "validation.json", {"ok": True, "failures": [], **typed})

    result = classify_case(case, {"returncode": 0, "timed_out": False}, 1, 1)

    assert result["failed"] is False
    assert "api_failed" not in result["reasons"]
    assert "api_error" not in result["reasons"]
    assert result["status_semantic_success"] is True


def test_unmatched_typed_status_remains_failure(tmp_path: Path) -> None:
    case = tmp_path / "offset2d_wrong_status"
    write_json(case / "manifest.json", {"case_id": case.name, "api": "api_offset2d"})
    write_json(
        case / "report" / "status.json",
        {
            "succeeded": False,
            "error_code": 3,
            "status_semantics": "offset2d_status_enum",
            "expected_status": "Success",
            "actual_status": "CanNotConnect",
            "expected_status_matched": False,
            "test_outcome_succeeded": False,
        },
    )
    write_json(case / "report" / "validation.json", {"ok": False, "failures": ["status mismatch"]})

    result = classify_case(case, {"returncode": 2, "timed_out": False}, 1, 1)

    assert result["failed"] is True
    assert "api_failed" in result["reasons"]
    assert "api_error" in result["reasons"]
