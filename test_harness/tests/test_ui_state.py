from __future__ import annotations

import json

import pytest

from test_harness.ui.state import active_session_root, read_artifact, round_overview, session_snapshot


def write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _make_review_round(repo_root, session_root, round_number=1) -> None:
    review_root = session_root / "rounds" / f"{round_number:04d}"
    rel = lambda p: p.relative_to(repo_root).as_posix()
    write_json(
        review_root / "candidate" / "candidate.json",
        {"kind": "attack_dsl", "dsl": {"cases": [{"case_id": "a"}, {"case_id": "b"}, {"case_id": "c"}]}},
    )
    packet_path = review_root / "pipeline" / "run_x" / "review" / "review_packet.json"
    write_json(
        packet_path,
        {
            "review_summary": {
                "purpose_zh_cn": "验证布尔运算结果体与体积关系",
                "risk_summary_zh_cn": "覆盖容差边界与空结果",
                "expected_behavior_zh_cn": "每个预期转成可测 oracle",
                "oracles": ["result_bodies", "clash", "distance"],
            },
            "generation": {"output_kind": "attack_dsl", "candidate_id": "candidate_02_test_design"},
            "machine_verification": {"fixed_gate_ok": True, "authoring_accepted": True},
        },
    )
    write_json(
        review_root / "round_manifest.json",
        {
            "round_number": round_number,
            "candidate_path": rel(review_root / "candidate" / "candidate.json"),
            "review_packet_path": rel(packet_path),
            "fixed_review_report_path": rel(review_root / "pipeline" / "run_x" / "review" / "review_report.zh-CN.md"),
        },
    )


def test_ui_state_projects_events_and_text_artifacts(tmp_path) -> None:
    root = tmp_path / "artifacts" / "harness_sessions" / "session-1"
    write_json(tmp_path / "artifacts/harness_sessions/active.json", {"session_id": "session-1"})
    write_json(
        root / "session.json",
        {"session_id": "session-1", "public_function": "api_boolean", "state": "awaiting_comment", "current_round": 1},
    )
    write_json(
        root / "events/000001.json",
        {"sequence": 1, "event_type": "PUBLIC_FUNCTION_RESOLVED", "recorded_at": "2026-01-01T00:00:00Z", "payload": {}},
    )
    write_json(
        root / "events/000002.json",
        {"sequence": 2, "event_type": "ROUND_READY_FOR_REVIEW", "recorded_at": "2026-01-01T00:00:01Z", "payload": {}},
    )
    (root / "rounds/0001/candidate").mkdir(parents=True)
    (root / "rounds/0001/candidate/test.cpp").write_text("int main() {}", encoding="utf-8")
    (root / "rounds/0001/candidate/blob.bin").write_bytes(b"secret")

    state = session_snapshot(tmp_path)

    assert state["session"]["public_function"] == "api_boolean"
    assert state["stages"][1]["status"] == "done"
    assert state["stages"][2]["status"] == "done"
    assert [item["path"] for item in state["artifacts"]] == [
        "events/000001.json",
        "events/000002.json",
        "rounds/0001/candidate/test.cpp",
        "session.json",
    ]
    assert read_artifact(active_session_root(tmp_path), "rounds/0001/candidate/test.cpp")["content"] == "int main() {}"


def test_ui_artifact_reader_blocks_path_traversal(tmp_path) -> None:
    root = tmp_path / "session"
    root.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        read_artifact(root, "../secret.txt")


def test_ui_state_groups_artifacts_and_exposes_primary_user_paths(tmp_path) -> None:
    session_id = "session-2"
    root = tmp_path / "artifacts" / "harness_sessions" / session_id
    session_prefix = f"artifacts/harness_sessions/{session_id}"
    write_json(tmp_path / "artifacts/harness_sessions/active.json", {"session_id": session_id})
    write_json(
        root / "session.json",
        {
            "session_id": session_id,
            "public_function": "api_boolean",
            "state": "completed",
            "current_round": 2,
            "current_review_report_path": (
                f"{session_prefix}/rounds/0002/review/第2轮测试方案审查.zh-CN.md"
            ),
            "current_execution_attempt_path": f"{session_prefix}/execution/round_0002/attempt_0001",
            "final_report_path": (
                f"{session_prefix}/execution/round_0002/attempt_0001/final_report.zh-CN.md"
            ),
        },
    )
    (root / "rounds/0002/review").mkdir(parents=True)
    (root / "rounds/0002/review/第2轮测试方案审查.zh-CN.md").write_text(
        "# 第 2 轮方案", encoding="utf-8"
    )
    write_json(root / "rounds/0002/candidate/candidate.json", {"kind": "attack_dsl"})
    (root / "rounds/0002/prompt").mkdir(parents=True)
    (root / "rounds/0002/prompt/authoring_prompt.md").write_text("prompt", encoding="utf-8")
    (root / "rounds/0002/comments/comment-1").mkdir(parents=True)
    (root / "rounds/0002/comments/comment-1/user_comment.txt").write_text("批准", encoding="utf-8")
    execution_root = root / "execution/round_0002/attempt_0001"
    write_json(execution_root / "execution_result.json", {"ok": True})
    (execution_root / "final_report.zh-CN.md").write_text("# 通过", encoding="utf-8")

    state = session_snapshot(tmp_path)
    artifacts = {item["path"]: item for item in state["artifacts"]}
    summary = state["artifact_summary"]

    assert summary["tone"] == "success"
    assert summary["title"] == "真实 SDK 测试已通过"
    assert [action["path"] for action in summary["actions"]] == [
        "execution/round_0002/attempt_0001/final_report.zh-CN.md",
        "rounds/0002/candidate/candidate.json",
        "execution/round_0002/attempt_0001/execution_result.json",
    ]
    assert artifacts["execution/round_0002/attempt_0001/final_report.zh-CN.md"]["group"] == "reports"
    assert artifacts["rounds/0002/candidate/candidate.json"]["label"] == "模型生成的测试方案"
    assert artifacts["rounds/0002/candidate/candidate.json"]["group"] == "proposal"
    assert artifacts["execution/round_0002/attempt_0001/execution_result.json"]["group"] == "execution"
    assert artifacts["rounds/0002/comments/comment-1/user_comment.txt"]["group"] == "review"
    assert artifacts["rounds/0002/prompt/authoring_prompt.md"]["group"] == "details"
    assert all(artifacts[action["path"]]["featured"] for action in summary["actions"])


def test_ui_state_does_not_feature_stale_final_report_during_a_new_review(tmp_path) -> None:
    session_id = "session-3"
    root = tmp_path / "artifacts" / "harness_sessions" / session_id
    prefix = f"artifacts/harness_sessions/{session_id}"
    write_json(tmp_path / "artifacts/harness_sessions/active.json", {"session_id": session_id})
    write_json(
        root / "session.json",
        {
            "session_id": session_id,
            "state": "awaiting_comment",
            "current_round": 2,
            "current_review_report_path": f"{prefix}/rounds/0002/review/第2轮测试方案审查.zh-CN.md",
            "final_report_path": f"{prefix}/execution/round_0001/attempt_0001/final_report.zh-CN.md",
        },
    )
    (root / "rounds/0002/review").mkdir(parents=True)
    (root / "rounds/0002/review/第2轮测试方案审查.zh-CN.md").write_text("new", encoding="utf-8")
    write_json(root / "rounds/0002/candidate/candidate.json", {"kind": "attack_dsl"})
    (root / "execution/round_0001/attempt_0001").mkdir(parents=True)
    (root / "execution/round_0001/attempt_0001/final_report.zh-CN.md").write_text("old", encoding="utf-8")

    summary = session_snapshot(tmp_path)["artifact_summary"]

    assert summary["actions"][0]["path"] == "rounds/0002/review/第2轮测试方案审查.zh-CN.md"
    assert all(action["path"] != "execution/round_0001/attempt_0001/final_report.zh-CN.md" for action in summary["actions"])


def test_round_overview_summarizes_reviewable_round(tmp_path) -> None:
    session_id = "session-round"
    session_root = tmp_path / "artifacts" / "harness_sessions" / session_id
    write_json(tmp_path / "artifacts/harness_sessions/active.json", {"session_id": session_id})
    write_json(
        session_root / "session.json",
        {"session_id": session_id, "state": "awaiting_comment", "current_round": 1},
    )
    _make_review_round(tmp_path, session_root, round_number=1)

    overview = session_snapshot(tmp_path)["round_overview"]

    assert overview["available"] is True
    assert overview["round_number"] == 1
    assert overview["tone"] == "ready"
    assert overview["candidate_kind"] == "attack_dsl"
    assert overview["case_count"] == 3
    assert overview["oracle_count"] == 3
    assert overview["gate_ok"] is True
    assert overview["authoring_accepted"] is True
    assert overview["purpose"] == "验证布尔运算结果体与体积关系"
    assert overview["risk"] == "覆盖容差边界与空结果"
    assert overview["candidate_path"] == "rounds/0001/candidate/candidate.json"
    assert overview["failure_reason"] == ""


def test_round_overview_explains_generation_failure(tmp_path) -> None:
    session_id = "session-fail"
    session_root = tmp_path / "artifacts" / "harness_sessions" / session_id
    write_json(tmp_path / "artifacts/harness_sessions/active.json", {"session_id": session_id})
    write_json(
        session_root / "session.json",
        {
            "session_id": session_id,
            "state": "generation_failed",
            "current_round": 0,
            "last_error": "no candidate passed the fixed gate: contract rejected",
        },
    )

    overview = round_overview(tmp_path, session_root, {"state": "generation_failed", "current_round": 0, "last_error": "no candidate passed the fixed gate: contract rejected"})

    assert overview["available"] is False
    assert overview["tone"] == "error"
    assert overview["headline"] == "测试方案生成未完成"
    assert overview["failure_reason"] == "no candidate passed the fixed gate: contract rejected"
    assert overview["next_hint"]
