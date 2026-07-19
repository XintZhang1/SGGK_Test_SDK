from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_harness.tools.compare_nx_sggk_boolean import classify


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sggk_case(root: Path, *, succeeded: bool = True, volume: float = 100.0, area: float = 50.0) -> Path:
    case = root / "case"
    _write_json(
        case / "report" / "status.json",
        {"succeeded": succeeded, "result_body_count": 1, "error_code": 0, "error_message": ""},
    )
    _write_json(
        case / "report" / "properties.json",
        {"bodies": [{"property_ok": True, "area": area, "volume": volume}]},
    )
    _write_json(case / "report" / "validation.json", {"ok": True})
    return case


def _nx_boolean(
    root: Path,
    *,
    boolean_ok: bool = True,
    import_ok: bool = True,
    closed: bool = True,
    volume: float = 100.0,
    area: float = 50.0,
    body_count: int = 1,
) -> Path:
    bodies = [
        {
            "index": index,
            "is_solid": True,
            "closed": closed,
            "free_edge_count": 0 if closed else 2,
            "measurement_ok": True,
            "area": area / body_count,
            "abs_volume": volume / body_count,
        }
        for index in range(body_count)
    ]
    path = root / "nx_boolean.json"
    _write_json(
        path,
        {
            "status": "completed" if import_ok else "import_failed",
            "import": {
                "target": {"ok": import_ok, "body_count": 1, "solid_body_count": 1},
                "tool": {"ok": import_ok, "body_count": 1, "solid_body_count": 1},
            },
            "boolean": {"ok": boolean_ok, "operation": "subtract", "error_message": ""},
            "measurement": {
                "ok": True,
                "body_count": body_count,
                "total_area": area,
                "total_abs_volume": volume,
                "all_solid_closed": closed,
                "bodies": bodies,
            },
        },
    )
    return path


def _nx_sggk_result(
    root: Path,
    name: str = "nx_sggk_result.json",
    *,
    import_ok: bool = True,
    closed: bool = True,
    volume: float = 100.0,
    area: float = 50.0,
    measurement_ok: bool = True,
) -> Path:
    path = root / name
    _write_json(
        path,
        {
            "status": "completed" if import_ok else "import_failed",
            "import": {"ok": import_ok, "body_count": 1},
            "measurement": {
                "ok": measurement_ok,
                "body_count": 1,
                "total_area": area,
                "total_abs_volume": volume,
                "bodies": [
                    {
                        "is_solid": True,
                        "closed": closed,
                        "free_edge_count": 0 if closed else 3,
                        "measurement_ok": measurement_ok,
                        "area": area,
                        "abs_volume": volume,
                    }
                ],
            }
        },
    )
    return path


def test_both_correct_when_valid_and_agreeing(tmp_path: Path) -> None:
    result = classify(
        _sggk_case(tmp_path),
        _nx_boolean(tmp_path),
        [_nx_sggk_result(tmp_path)],
    )
    assert result["verdict"] == "both_correct"
    assert result["signals"]["sggk_valid"] is True
    assert result["signals"]["parasolid_valid"] is True
    assert result["signals"]["measurements_agree"] is True


def test_sggk_correct_when_parasolid_boolean_fails(tmp_path: Path) -> None:
    result = classify(
        _sggk_case(tmp_path),
        _nx_boolean(tmp_path, boolean_ok=False, closed=False, volume=0.0, area=0.0, body_count=0),
        [_nx_sggk_result(tmp_path)],
    )
    assert result["verdict"] == "sggk_correct"


def test_parasolid_correct_when_sggk_result_not_closed(tmp_path: Path) -> None:
    result = classify(
        _sggk_case(tmp_path),
        _nx_boolean(tmp_path),
        [_nx_sggk_result(tmp_path, closed=False)],
    )
    assert result["verdict"] == "parasolid_correct"


def test_both_wrong_when_neither_closed(tmp_path: Path) -> None:
    result = classify(
        _sggk_case(tmp_path),
        _nx_boolean(tmp_path, boolean_ok=False, closed=False, volume=0.0, area=0.0, body_count=0),
        [_nx_sggk_result(tmp_path, closed=False)],
    )
    assert result["verdict"] == "both_wrong"


def test_inconclusive_when_sggk_result_measurement_missing(tmp_path: Path) -> None:
    result = classify(_sggk_case(tmp_path), _nx_boolean(tmp_path), [])
    assert result["verdict"] == "inconclusive"


def test_inconclusive_when_both_valid_but_volumes_disagree(tmp_path: Path) -> None:
    result = classify(
        _sggk_case(tmp_path, volume=100.0),
        _nx_boolean(tmp_path, volume=100.0),
        [_nx_sggk_result(tmp_path, volume=180.0)],
    )
    assert result["verdict"] == "inconclusive"
    assert result["checks"]["volume_agree"]["ok"] is False


def test_body_count_disagreement_marks_inconclusive(tmp_path: Path) -> None:
    result = classify(
        _sggk_case(tmp_path),
        _nx_boolean(tmp_path, body_count=2),
        [_nx_sggk_result(tmp_path)],
    )
    assert result["verdict"] == "inconclusive"
    assert result["checks"]["body_count_agree"]["ok"] is False


def test_inconclusive_when_parasolid_cannot_import_large_coordinates(tmp_path: Path) -> None:
    result = classify(
        _sggk_case(tmp_path),
        _nx_boolean(tmp_path, import_ok=False, boolean_ok=False, closed=False, volume=0.0, area=0.0, body_count=0),
        [_nx_sggk_result(tmp_path, import_ok=False, closed=False, volume=0.0, area=0.0, measurement_ok=False)],
    )
    assert result["verdict"] == "inconclusive"
    assert result["signals"]["parasolid_available"] is False
    assert any("无法导入" in reason for reason in result["reasons"])


def test_parasolid_correct_when_sggk_api_fails_and_result_unmeasurable(tmp_path: Path) -> None:
    result = classify(
        _sggk_case(tmp_path, succeeded=False),
        _nx_boolean(tmp_path),
        [_nx_sggk_result(tmp_path, import_ok=False, closed=False, volume=0.0, area=0.0, measurement_ok=False)],
    )
    assert result["verdict"] == "parasolid_correct"
