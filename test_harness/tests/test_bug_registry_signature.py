from __future__ import annotations

from check_bug_registry_regression import classify_bug


CRASH_SIGNATURE = {
    "schema_version": 1,
    "kind": "crash",
    "returncode": -1073741819,
    "phase": "",
    "exception_code": "0xC0000005",
    "sdk_error_code": None,
    "validation_failures": [],
    "topology_failures": [],
    "message_signature": "",
}


def bug() -> dict[str, object]:
    return {
        "fingerprint": "fp",
        "bug_id": "bug",
        "expected_failure_signature": CRASH_SIGNATURE,
        "expected": {"returncode": -1073741819},
        "paths": {"replay_recipe": "recipe.json"},
    }


def test_bug_registry_rejects_different_nonzero_failure() -> None:
    result = classify_bug(
        bug(),
        {
            "returncode": 1,
            "timed_out": False,
            "stderr": "schema validation failed",
            "artifact_dir": "",
        },
    )

    assert result["status"] == "changed_failure"
    assert result["signature_match_reason"].startswith("kind_changed")


def test_bug_registry_accepts_same_windows_exception() -> None:
    result = classify_bug(
        bug(),
        {
            "returncode": -1073741819,
            "timed_out": False,
            "stderr": "access violation",
            "artifact_dir": "",
        },
    )

    assert result["status"] == "still_failing"
    assert result["signature_match_reason"] == "same_crash"
