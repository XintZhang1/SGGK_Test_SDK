from __future__ import annotations

import json
from pathlib import Path

from probe_topotrack_crashes import classify_probe, selection_reason


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def result(artifact_dir: Path, returncode: int) -> dict[str, object]:
    return {
        "artifact_dir": str(artifact_dir),
        "returncode": returncode,
        "timed_out": False,
    }


def test_validation_failure_is_selected_for_isolated_topotrack_capture(tmp_path: Path) -> None:
    recipe = tmp_path / "failure.json"
    write_json(recipe, {"case_id": "failure", "topo_track": True})

    assert (
        selection_reason(
            {
                "recipe": str(recipe),
                "case_id": "failure",
                "returncode": 2,
                "artifact_dir": str(tmp_path / "case"),
            }
        )
        == "selected"
    )


def test_capture_crash_is_separated_from_existing_oracle_failure(tmp_path: Path) -> None:
    capture_dir = tmp_path / "capture"
    disabled_dir = tmp_path / "disabled"
    write_json(disabled_dir / "report/validation.json", {"ok": False, "failures": ["oracle"]})
    record = {"status": "written", "case_id": "failure", "source_returncode": 2}

    classified = classify_probe(
        record,
        result(capture_dir, -1073741819),
        result(disabled_dir, 2),
    )

    assert classified["classification"] == "topotrack_instrumentation_crash"
    assert classified["evidence_quality"] == "diagnostic_not_causal_proof"


def test_nonzero_case_can_still_produce_topotrack_evidence(tmp_path: Path) -> None:
    capture_dir = tmp_path / "capture"
    disabled_dir = tmp_path / "disabled"
    write_json(
        capture_dir / "report/topo_track_summary.json",
        {
            "item_count": 8,
            "ancestor_count": 11,
            "resolved_ancestor_count": 9,
        },
    )
    write_json(capture_dir / "report/validation.json", {"ok": False, "failures": ["oracle"]})
    write_json(disabled_dir / "report/validation.json", {"ok": False, "failures": ["oracle"]})

    classified = classify_probe(
        {"status": "written", "case_id": "failure", "source_returncode": 2},
        result(capture_dir, 2),
        result(disabled_dir, 2),
    )

    assert classified["classification"] == "topotrack_capture_available_with_failure"
    assert classified["capture_topotrack"]["item_count"] == 8


def test_noncrash_topotrack_failure_with_clean_disabled_control_is_excluded(
    tmp_path: Path,
) -> None:
    capture_dir = tmp_path / "capture"
    disabled_dir = tmp_path / "disabled"
    write_json(
        capture_dir / "report/topo_track_summary.json",
        {"item_count": 2, "ancestor_count": 3, "resolved_ancestor_count": 2},
    )
    write_json(capture_dir / "report/validation.json", {"ok": False, "failures": ["capture"]})
    write_json(disabled_dir / "report/validation.json", {"ok": True, "failures": []})

    classified = classify_probe(
        {"status": "written", "case_id": "failure", "source_returncode": 2},
        result(capture_dir, 2),
        result(disabled_dir, 0),
    )

    assert classified["classification"] == "topotrack_only_modeling_ok"
