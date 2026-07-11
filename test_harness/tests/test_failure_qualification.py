from __future__ import annotations

import json
from pathlib import Path

from test_harness.tools.qualify_failures import qualify


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def failure_group(case_id: str, case_dir: Path, recipe: Path, fingerprint: str) -> dict[str, object]:
    return {
        "fingerprint": fingerprint,
        "count": 1,
        "reasons": ["runner_nonzero_exit", "validation_failed"],
        "apis": ["api_boolean"],
        "recipe_paths": [str(recipe)],
        "representative_case_id": case_id,
        "representative_case_dir": str(case_dir),
        "representative_validation_failures": ["result_body_count_below_min actual=0 min=1"],
        "representative_failure_signature": {
            "schema_version": 1,
            "kind": "oracle_failure",
            "returncode": 2,
            "phase": "oracle",
            "sdk_error_code": 0,
        },
    }


def test_disjoint_intersection_expectation_is_excluded_from_sdk_bug_candidates(tmp_path: Path) -> None:
    triage = tmp_path / "triage"
    case_dir = tmp_path / "cases/disjoint"
    recipe_path = tmp_path / "recipes/disjoint.json"
    write_json(
        recipe_path,
        {
            "case_id": "disjoint",
            "api": "api_boolean",
            "boolean_type": "INTERSECTION",
            "modeling_tol": 0.01,
            "expectations": {"result_bodies": {"min": 1}},
        },
    )
    write_json(
        case_dir / "report/input_properties.json",
        {
            "target": [{"bbox": {"min": [-10, -10, -10], "max": [10, 10, 10]}}],
            "tool": [{"bbox": {"min": [40, -5, -5], "max": [50, 5, 5]}}],
        },
    )
    group = failure_group("disjoint", case_dir, recipe_path, "fp_disjoint")
    write_json(
        triage / "triage_summary.json",
        {"failure_groups": [group], "failures": [{"fingerprint": "fp_disjoint"}]},
    )
    write_json(
        triage / "regression_seeds.json",
        [{"fingerprint": "fp_disjoint", "failure_signature": group["representative_failure_signature"]}],
    )

    report = qualify(triage, tmp_path / "qualification")

    assert report["eligible_group_count"] == 0
    record = report["records"][0]
    assert record["classification"] == "test_generation_defect"
    assert record["evidence"][0]["rule_id"] == "boolean_intersection_nonempty_vs_disjoint_bbox_v1"
    assert record["evidence"][0]["bbox_distance"] == 30.0
    eligible = json.loads(
        (tmp_path / "qualification/eligible_triage/regression_seeds.json").read_text()
    )
    assert eligible == []


def test_overlapping_intersection_remains_candidate_only_not_confirmed_bug(tmp_path: Path) -> None:
    triage = tmp_path / "triage"
    case_dir = tmp_path / "cases/overlap"
    recipe_path = tmp_path / "recipes/overlap.json"
    write_json(
        recipe_path,
        {
            "case_id": "overlap",
            "api": "api_boolean",
            "boolean_type": "INTERSECTION",
            "modeling_tol": 0.01,
            "expectations": {"result_bodies": {"min": 1}},
        },
    )
    write_json(
        case_dir / "report/input_properties.json",
        {
            "target": [{"bbox": {"min": [-10, -10, -10], "max": [10, 10, 10]}}],
            "tool": [{"bbox": {"min": [5, -5, -5], "max": [15, 5, 5]}}],
        },
    )
    group = failure_group("overlap", case_dir, recipe_path, "fp_overlap")
    write_json(
        triage / "triage_summary.json",
        {"failure_groups": [group], "failures": [{"fingerprint": "fp_overlap"}]},
    )
    write_json(
        triage / "regression_seeds.json",
        [{"fingerprint": "fp_overlap", "failure_signature": group["representative_failure_signature"]}],
    )

    report = qualify(triage, tmp_path / "qualification")

    assert report["eligible_group_count"] == 1
    record = report["records"][0]
    assert record["classification"] == "oracle_test_or_sdk_investigation_candidate"
    assert record["assessment_status"] == "candidate_only"
    assert record["eligible_for_bug_investigation"] is True
