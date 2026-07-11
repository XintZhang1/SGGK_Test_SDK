from __future__ import annotations

import json
from pathlib import Path

from triage_artifacts import (
    build_failure_groups,
    build_regression_seeds,
    classify_case,
    classify_command_failure,
    iter_case_dirs,
)


def test_pre_status_crash_becomes_replayable_regression_seed(tmp_path: Path) -> None:
    recipe_path = tmp_path / "crash_recipe.json"
    recipe_path.write_text(
        json.dumps({"case_id": "crash_case", "api": "api_boolean"}),
        encoding="utf-8",
    )
    record = {
        "recipe": str(recipe_path),
        "case_id": "crash_case",
        "artifact_dir": "",
        "returncode": -1073741819,
        "timed_out": False,
        "stderr": "access violation while invoke_api",
    }

    failure = classify_command_failure(record)
    groups = build_failure_groups([failure])
    seeds = build_regression_seeds(groups)

    assert failure["failure_signature"]["kind"] == "crash"
    assert failure["failure_signature"]["exception_code"] == "0xC0000005"
    assert groups[0]["count"] == 1
    assert seeds[0]["recipe_paths"] == [str(recipe_path)]
    assert seeds[0]["failure_signature"]["kind"] == "crash"


def test_run_state_only_crash_is_discovered_without_command_summary(tmp_path: Path) -> None:
    case_dir = tmp_path / "crash_case"
    case_dir.mkdir()
    (case_dir / "run_state.json").write_text(
        json.dumps(
            {
                "case_id": "crash_case",
                "api": "api_boolean",
                "recipe_path": "recipe.json",
                "returncode": -1073741819,
                "timed_out": False,
                "last_phase": "invoke_api",
            }
        ),
        encoding="utf-8",
    )

    discovered = iter_case_dirs([tmp_path])
    result = classify_case(discovered[0], None, 1, 1)

    assert discovered == [case_dir.resolve()]
    assert result["failed"] is True
    assert result["api"] == "api_boolean"
    assert result["failure_signature"]["kind"] == "crash"
    assert result["failure_signature"]["phase"] == "invoke_api"
