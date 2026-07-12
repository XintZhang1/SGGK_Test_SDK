from __future__ import annotations

from failure_predicate import build_failure_signature, signatures_match


def test_nonzero_schema_error_does_not_match_access_violation() -> None:
    crash = build_failure_signature(
        returncode=-1073741819,
        stderr="crash during invoke_api",
        run_state={"last_phase": "invoke_api"},
    )
    schema_error = build_failure_signature(returncode=1, stderr="unsupported api")

    matched, reason = signatures_match(crash, schema_error)

    assert crash["kind"] == "crash"
    assert crash["exception_code"] == "0xC0000005"
    assert matched is False
    assert reason.startswith("kind_changed")


def test_same_sdk_error_requires_same_error_code() -> None:
    expected = build_failure_signature(
        returncode=2,
        status={"succeeded": False, "error_code": 1001, "error_message": "boolean failed"},
    )
    changed = build_failure_signature(
        returncode=2,
        status={"succeeded": False, "error_code": 1002, "error_message": "boolean failed"},
    )

    assert signatures_match(expected, changed) == (False, "sdk_error_code_changed")


def test_oracle_signature_is_preserved_by_superset() -> None:
    expected = build_failure_signature(
        returncode=2,
        validation={"ok": False, "failures": ["volume relation failed at body 1"]},
    )
    observed = build_failure_signature(
        returncode=2,
        validation={
            "ok": False,
            "failures": ["volume relation failed at body 9", "extra diagnostic"],
        },
    )

    matched, reason = signatures_match(expected, observed)

    assert matched is True
    assert reason == "same_oracle_failure"


def test_timeout_only_matches_timeout() -> None:
    expected = build_failure_signature(returncode=124, timed_out=True)
    observed = build_failure_signature(returncode=124, timed_out=True)

    assert signatures_match(expected, observed) == (True, "same_timeout")


def test_crash_with_missing_observed_phase_is_unverified() -> None:
    expected = {"kind": "crash", "exception_code": "0xC0000005", "phase": "topocheck"}
    observed = {"kind": "crash", "exception_code": "0xC0000005", "phase": ""}

    assert signatures_match(expected, observed) == (False, "crash_phase_unobserved")


def test_matched_typed_status_enum_is_not_sdk_error() -> None:
    typed = {
        "status_semantics": "offset2d_status_enum",
        "expected_status": "CanNotConnect",
        "actual_status": "CanNotConnect",
        "expected_status_matched": True,
        "test_outcome_succeeded": True,
    }

    signature = build_failure_signature(
        returncode=0,
        status={"succeeded": False, "error_code": 3, **typed},
        validation={"ok": True, **typed},
    )

    assert signature["kind"] == "pass"
    assert signature["sdk_error_code"] is None


def test_artifact_evidence_overrides_stale_launch_phase() -> None:
    oracle = build_failure_signature(
        returncode=2,
        validation={"ok": False, "failures": ["result count mismatch"]},
        run_state={"phase": "completed", "last_phase": "launching"},
    )
    topology = build_failure_signature(
        returncode=2,
        topo_check={"bodies": [{"ok": False, "error_code": 7, "error_string": "bad"}]},
        run_state={"last_phase": "launching"},
    )
    sdk_error = build_failure_signature(
        returncode=2,
        status={"succeeded": False, "error_code": 19},
        run_state={"last_phase": "launching"},
    )

    assert oracle["phase"] == "oracle"
    assert topology["phase"] == "topocheck"
    assert sdk_error["phase"] == "invoke_api"
