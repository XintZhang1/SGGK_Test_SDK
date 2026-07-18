from __future__ import annotations

import json

from test_harness.ui.state import execution_overview, session_snapshot


def write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


ATTEMPT_SUFFIX = "execution/round_0001/attempt_0001"


def _session(tmp_path, session_id="session-exec", **extra):
    root = tmp_path / "artifacts" / "harness_sessions" / session_id
    write_json(tmp_path / "artifacts/harness_sessions/active.json", {"session_id": session_id})
    prefix = f"artifacts/harness_sessions/{session_id}"
    session = {
        "session_id": session_id,
        "public_function": "api_combine_bodies",
        "state": "completed",
        "current_round": 1,
        "current_execution_attempt_path": f"{prefix}/{ATTEMPT_SUFFIX}",
    }
    session.update(extra)
    write_json(root / "session.json", session)
    return root, prefix


def _write_case(cases_root, case_id, *, succeeded, phase="oracle", error_message="", failures=None, timed_out=False):
    case_dir = cases_root / case_id
    write_json(
        case_dir / "report" / "status.json",
        {"succeeded": succeeded, "error_code": 0 if succeeded else 3, "error_message": error_message},
    )
    write_json(
        case_dir / "report" / "validation.json",
        {"ok": not failures, "failures": list(failures or []), "skipped_checks": []},
    )
    write_json(
        case_dir / "run_state.json",
        {
            "phase": "timed_out" if timed_out else "completed",
            "last_phase": phase,
            "returncode": 0 if succeeded else (124 if timed_out else 2),
            "timed_out": timed_out,
        },
    )
    return case_dir


def _write_full_execution(tmp_path, session_root, prefix, *, ok=False):
    attempt_root = session_root / ATTEMPT_SUFFIX
    cases_rel = f"{prefix}/{ATTEMPT_SUFFIX}/pipeline/runs/run_x/cases"
    triage_rel = f"{prefix}/{ATTEMPT_SUFFIX}/pipeline/runs/run_x/triage"
    write_json(
        attempt_root / "execution_result.json",
        {
            "ok": ok,
            "run_id": "run_x",
            "results": [
                {
                    "ok": ok,
                    "error": "",
                    "execution": {
                        "requested": True,
                        "ok": ok,
                        "status": "passed" if ok else "sdk_or_oracle_failures_triaged",
                        "candidate_cause": "" if ok else "oracle_or_sdk_requires_classification",
                        "error": "" if ok else "SDK/oracle tests returned nonzero; inspect qualification",
                        "commands": [
                            {
                                "name": "run_recipes",
                                "argv": ["python", "run_recipes.py", "--secret-flag"],
                                "returncode": 0,
                                "ok": True,
                                "elapsed_seconds": 1.25,
                                "stdout_tail": "secret stdout",
                                "stderr_tail": "",
                            },
                            {
                                "name": "qualify_failures",
                                "argv": ["python"],
                                "returncode": 0,
                                "ok": True,
                                "elapsed_seconds": 0.5,
                                "stdout_tail": "",
                                "stderr_tail": "",
                            },
                        ],
                        "artifacts": {"cases": cases_rel, "triage": triage_rel},
                    },
                }
            ],
            "errors": [],
        },
    )
    cases_root = tmp_path / cases_rel
    triage_root = tmp_path / triage_rel
    pass_dir = _write_case(cases_root, "case_pass", succeeded=True, phase="done")
    fail_dir = _write_case(
        cases_root,
        "case_fail",
        succeeded=False,
        phase="oracle",
        error_message="volume relation mismatch 体积关系不一致" + "x" * 300,
        failures=[f"oracle_failure_{index}" for index in range(12)],
    )
    timeout_dir = _write_case(
        cases_root, "case_timeout", succeeded=False, phase="modeling", timed_out=True
    )
    write_json(
        cases_root / "recipe_summary.json",
        {
            "total": 3,
            "executed": 3,
            "skipped": 0,
            "passed": 1,
            "failed": 2,
            "timed_out": 1,
            "results": [
                {
                    "recipe": "r1",
                    "case_id": "case_pass",
                    "artifact_dir": str(pass_dir),
                    "returncode": 0,
                    "elapsed_seconds": 0.4,
                    "timed_out": False,
                    "skipped": False,
                },
                {
                    "recipe": "r2",
                    "case_id": "case_fail",
                    "artifact_dir": str(fail_dir),
                    "returncode": 2,
                    "elapsed_seconds": 0.6,
                    "timed_out": False,
                    "skipped": False,
                },
                {
                    "recipe": "r3",
                    "case_id": "case_timeout",
                    "artifact_dir": str(timeout_dir),
                    "returncode": 124,
                    "elapsed_seconds": 9.9,
                    "timed_out": True,
                    "skipped": False,
                },
            ],
        },
    )
    write_json(
        triage_root / "triage_summary.json",
        {
            "total_cases": 3,
            "passed_cases": 1,
            "failed_cases": 2,
            "failures": [
                {"case_id": "case_fail", "reasons": ["oracle_validation_failed"], "api": "api_combine_bodies"},
                {"case_id": "case_timeout", "reasons": ["runner_timeout"], "api": "api_combine_bodies"},
            ],
            "failure_groups": [
                {
                    "fingerprint": "fp-oracle",
                    "count": 2,
                    "apis": ["api_combine_bodies"],
                    "reasons": ["oracle_validation_failed", "runner_timeout"],
                    "representative_case_id": "case_fail",
                    "representative_failure_signature": {
                        "kind": "oracle_failure",
                        "returncode": 2,
                        "phase": "oracle",
                        "exception_code": "",
                        "sdk_error_code": 3,
                        "validation_failures": ["volume mismatch"],
                        "topology_failures": [],
                    },
                }
            ],
        },
    )
    return cases_rel, triage_rel


def test_execution_overview_unavailable_without_session(tmp_path) -> None:
    overview = execution_overview(tmp_path, None, None)

    assert overview["available"] is False
    assert overview["cases"] == []
    assert overview["totals"] == {"total": 0, "passed": 0, "failed": 0, "timed_out": 0, "skipped": 0}


def test_execution_overview_unavailable_outside_execution_states(tmp_path) -> None:
    root, _prefix = _session(tmp_path, state="awaiting_comment")

    overview = session_snapshot(tmp_path)["execution_overview"]

    assert root is not None
    assert overview["available"] is False


def test_execution_overview_partial_result_during_execution(tmp_path) -> None:
    session_root, _prefix = _session(tmp_path, state="executing")
    write_json(
        session_root / ATTEMPT_SUFFIX / "execution_result.json",
        {"ok": False, "results": []},
    )

    overview = session_snapshot(tmp_path)["execution_overview"]

    assert overview["available"] is True
    assert overview["state"] == "executing"
    assert overview["ok"] is False
    assert overview["attempt_path"] == ATTEMPT_SUFFIX
    assert overview["execution_result_path"] == f"{ATTEMPT_SUFFIX}/execution_result.json"
    assert overview["commands"] == []
    assert overview["cases"] == []
    assert overview["failure_groups"] == []
    assert overview["totals"]["total"] == 0


def test_execution_overview_summarizes_completed_execution(tmp_path) -> None:
    session_root, prefix = _session(tmp_path)
    _write_full_execution(tmp_path, session_root, prefix)

    overview = session_snapshot(tmp_path)["execution_overview"]

    assert overview["available"] is True
    assert overview["ok"] is False
    assert overview["status"] == "sdk_or_oracle_failures_triaged"
    assert overview["candidate_cause"] == "oracle_or_sdk_requires_classification"
    assert "SDK/oracle tests returned nonzero" in overview["error"]
    assert overview["totals"] == {"total": 3, "passed": 1, "failed": 2, "timed_out": 1, "skipped": 0}
    assert overview["total_elapsed_seconds"] == 1.75
    assert [step["name"] for step in overview["commands"]] == ["run_recipes", "qualify_failures"]
    # Command steps never carry argv or captured output.
    assert all(set(step) == {"name", "returncode", "ok", "elapsed_seconds"} for step in overview["commands"])
    assert overview["cases_root"] == f"{ATTEMPT_SUFFIX}/pipeline/runs/run_x/cases"
    assert overview["triage_root"] == f"{ATTEMPT_SUFFIX}/pipeline/runs/run_x/triage"

    # Failed/timed-out cases come first, then the rest by case_id.
    assert [row["case_id"] for row in overview["cases"]] == ["case_fail", "case_timeout", "case_pass"]
    assert overview["cases_truncated"] is False
    fail_row = overview["cases"][0]
    assert fail_row["outcome"] == "fail"
    assert fail_row["returncode"] == 2
    assert fail_row["phase"] == "oracle"
    assert 0 < len(fail_row["error_message"]) <= 200
    assert fail_row["validation_failures"] == [f"oracle_failure_{index}" for index in range(8)]
    assert fail_row["triage_reasons"] == ["oracle_validation_failed"]
    assert fail_row["artifact_path"] == f"{ATTEMPT_SUFFIX}/pipeline/runs/run_x/cases/case_fail"
    timeout_row = overview["cases"][1]
    assert timeout_row["outcome"] == "timeout"
    assert timeout_row["timed_out"] is True
    assert timeout_row["triage_reasons"] == ["runner_timeout"]
    pass_row = overview["cases"][2]
    assert pass_row["outcome"] == "pass"
    assert pass_row["error_message"] == ""
    assert pass_row["validation_failures"] == []

    assert len(overview["failure_groups"]) == 1
    group = overview["failure_groups"][0]
    assert group["count"] == 2
    assert group["apis"] == ["api_combine_bodies"]
    assert group["representative_case_id"] == "case_fail"
    assert group["signature"] == {"kind": "oracle_failure", "phase": "oracle", "sdk_error_code": 3}
    assert overview["parasolid"] == {"ran": False}


def test_execution_overview_degrades_on_corrupt_and_missing_json(tmp_path) -> None:
    session_root, prefix = _session(tmp_path)
    attempt_root = session_root / ATTEMPT_SUFFIX
    (attempt_root / "pipeline/runs/run_x/cases").mkdir(parents=True)
    (attempt_root / "execution_result.json").parent.mkdir(parents=True, exist_ok=True)
    (attempt_root / "execution_result.json").write_text("{ not json", encoding="utf-8")
    cases_rel = f"{prefix}/{ATTEMPT_SUFFIX}/pipeline/runs/run_x/cases"
    write_json(
        attempt_root / "execution_result.json",
        {
            "ok": True,
            "results": [
                {
                    "execution": {
                        "requested": True,
                        "ok": True,
                        "status": "passed",
                        "commands": [],
                        "artifacts": {"cases": cases_rel, "triage": f"{prefix}/{ATTEMPT_SUFFIX}/missing_triage"},
                    }
                }
            ],
        },
    )
    (tmp_path / cases_rel / "recipe_summary.json").write_text("{ corrupt", encoding="utf-8")

    overview = session_snapshot(tmp_path)["execution_overview"]

    assert overview["available"] is True
    assert overview["status"] == "passed"
    assert overview["totals"] == {"total": 0, "passed": 0, "failed": 0, "timed_out": 0, "skipped": 0}
    assert overview["cases"] == []
    assert overview["failure_groups"] == []

    (attempt_root / "execution_result.json").write_text("{ still corrupt", encoding="utf-8")
    session = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    overview = execution_overview(tmp_path, session_root, session)
    assert overview["available"] is True
    assert overview["ok"] is None
    assert overview["status"] == ""


def test_execution_overview_bounds_the_case_list(tmp_path) -> None:
    session_root, prefix = _session(tmp_path)
    attempt_root = session_root / ATTEMPT_SUFFIX
    cases_rel = f"{prefix}/{ATTEMPT_SUFFIX}/pipeline/runs/run_x/cases"
    results = [
        {
            "recipe": f"r{index}",
            "case_id": f"case_{index:04d}",
            "artifact_dir": "",
            "returncode": 0 if index % 2 else 2,
            "elapsed_seconds": 0.1,
            "timed_out": False,
            "skipped": False,
        }
        for index in range(150)
    ]
    write_json(
        attempt_root / "execution_result.json",
        {
            "ok": False,
            "results": [
                {
                    "execution": {
                        "requested": True,
                        "ok": False,
                        "status": "x",
                        "commands": [],
                        "artifacts": {"cases": cases_rel},
                    }
                }
            ],
        },
    )
    write_json(
        tmp_path / cases_rel / "recipe_summary.json",
        {"total": 150, "passed": 75, "failed": 75, "timed_out": 0, "skipped": 0, "results": results},
    )

    overview = session_snapshot(tmp_path)["execution_overview"]

    assert overview["totals"]["total"] == 150
    assert len(overview["cases"]) == 100
    assert overview["cases_truncated"] is True
    assert all(row["outcome"] == "fail" for row in overview["cases"][:75])


def test_execution_overview_surfaces_parasolid_comparison(tmp_path) -> None:
    session_id = "session-exec"
    prefix = f"artifacts/harness_sessions/{session_id}"
    session_root, _ = _session(
        tmp_path,
        session_id=session_id,
        parasolid_comparison={
            "ran": True,
            "ok": True,
            "total": 3,
            "consistent": 2,
            "attention": 1,
            "verdict_counts": {"both_correct": 2, "sggk_only_issue": 1},
            "attention_cases": [
                {
                    "case_id": "case_fail",
                    "verdict": "inconclusive",
                    "cause_class": "volume_drift",
                    "reasons": ["体积差异超差"],
                },
                "not-a-mapping",
            ],
            "report_path": f"{prefix}/{ATTEMPT_SUFFIX}/parasolid_compare/parasolid_comparison.zh-CN.md",
            "batch_summary_path": f"{prefix}/{ATTEMPT_SUFFIX}/parasolid_compare/batch_summary.json",
        },
    )
    _write_full_execution(tmp_path, session_root, prefix, ok=True)

    overview = session_snapshot(tmp_path)["execution_overview"]

    parasolid = overview["parasolid"]
    assert parasolid["ran"] is True
    assert parasolid["consistent"] == 2
    assert parasolid["attention"] == 1
    assert parasolid["verdict_counts"] == {"both_correct": 2, "sggk_only_issue": 1}
    assert parasolid["report_path"] == f"{ATTEMPT_SUFFIX}/parasolid_compare/parasolid_comparison.zh-CN.md"
    assert parasolid["attention_cases"] == [
        {"case_id": "case_fail", "verdict": "inconclusive", "cause_class": "volume_drift"}
    ]


def test_execution_overview_bounds_parasolid_attention_cases(tmp_path) -> None:
    session_id = "session-exec"
    session_root, _prefix = _session(
        tmp_path,
        session_id=session_id,
        parasolid_comparison={
            "ran": True,
            "ok": True,
            "total": 30,
            "consistent": 0,
            "attention": 30,
            "verdict_counts": {"inconclusive": 30},
            "attention_cases": [
                {"case_id": f"case_{index:03d}", "verdict": "inconclusive", "cause_class": "volume_drift"}
                for index in range(30)
            ],
        },
    )
    _write_full_execution(tmp_path, session_root, _prefix, ok=True)

    overview = session_snapshot(tmp_path)["execution_overview"]

    attention_cases = overview["parasolid"]["attention_cases"]
    assert len(attention_cases) == 24
    assert attention_cases[0]["case_id"] == "case_000"
    assert attention_cases[-1]["case_id"] == "case_023"
