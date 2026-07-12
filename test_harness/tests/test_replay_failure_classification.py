from __future__ import annotations

import json
from pathlib import Path

from replay_regression_seeds import build_replay_recipe, classify_attempts


EXPECTED = {"kind": "crash", "exception_code": "0xC0000005"}


def attempt(returncode: int, matches: bool | None) -> dict[str, object]:
    return {"returncode": returncode, "matches_expected": matches}


def test_all_matching_attempts_are_stable_same_failure() -> None:
    attempts = [attempt(-1073741819, True), attempt(-1073741819, True)]

    assert classify_attempts(attempts, EXPECTED) == "stable_same_failure"


def test_nonzero_changed_failure_is_not_stable_reproduction() -> None:
    attempts = [attempt(1, False), attempt(1, False)]

    assert classify_attempts(attempts, EXPECTED) == "changed_failure"


def test_missing_crash_phase_is_unverified_not_changed() -> None:
    attempts = [
        {
            "returncode": -1073741819,
            "matches_expected": False,
            "match_reason": "crash_phase_unobserved",
        },
        {
            "returncode": -1073741819,
            "matches_expected": False,
            "match_reason": "crash_phase_unobserved",
        },
    ]

    assert classify_attempts(attempts, EXPECTED) == "unverified_failure"


def test_mixed_match_is_flaky_same_failure() -> None:
    attempts = [attempt(-1073741819, True), attempt(0, False)]

    assert classify_attempts(attempts, EXPECTED) == "flaky_same_failure"


def test_legacy_nonzero_without_predicate_is_unverified() -> None:
    attempts = [attempt(2, None), attempt(2, None)]

    assert classify_attempts(attempts, {}) == "unverified_failure"


def test_replay_rebinds_stale_loaded_sgt_to_frozen_artifact(tmp_path: Path) -> None:
    recipe_path = tmp_path / "recipe.json"
    frozen_target = tmp_path / "target.sgt"
    frozen_target.write_bytes(b"fixture")
    recipe_path.write_text(
        json.dumps(
            {
                "case_id": "old",
                "api": "api_boolean",
                "target_kind": "loaded_sgt",
                "target_source_file": str(tmp_path / "missing.sgt"),
                "tool_kind": "solid_sphere",
            }
        ),
        encoding="utf-8",
    )
    seed = {
        "recipe_paths": [str(recipe_path)],
        "artifact_inputs": {"target_sgt": str(frozen_target)},
    }

    replay, reason = build_replay_recipe(seed, "replay")

    assert reason == ""
    assert replay is not None
    assert replay["target_source_file"] == str(frozen_target)
