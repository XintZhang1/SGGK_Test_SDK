from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_harness.orchestration.session_memory import gather_prior_review_memory  # noqa: E402
from test_harness.tests.test_harness_orchestration import make_workflow  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_prior_session(
    sessions_root: Path,
    session_id: str,
    public_function: str,
    *,
    state: str = "rejected",
    updated_at: str = "2026-07-17T16:00:00Z",
) -> None:
    session_root = sessions_root / session_id
    _write_json(
        session_root / "session.json",
        {
            "schema_version": 1,
            "session_id": session_id,
            "public_function": public_function,
            "state": state,
            "created_at": "2026-07-17T12:00:00Z",
            "updated_at": updated_at,
            "current_round": 1,
        },
    )
    comment_root = session_root / "rounds" / "0001" / "comments" / ("a" * 64)
    comment_root.mkdir(parents=True, exist_ok=True)
    (comment_root / "user_comment.txt").write_text("请增加大坐标和近容差边界的用例。", encoding="utf-8")
    _write_json(
        comment_root / "comment_decision.json",
        {
            "decision": {
                "decision": "revise",
                "summary_zh_cn": "需要补充大坐标与近容差边界。",
                "requested_changes": [
                    {
                        "scope": "cases",
                        "instruction": "增加 1e6 大坐标与 +/- topo_tol 边界用例。",
                        "priority": "high",
                    }
                ],
                "constraints": ["保留固定门禁语义。"],
            }
        },
    )


def test_gather_memory_collects_prior_feedback(tmp_path: Path) -> None:
    sessions_root = tmp_path / "artifacts" / "harness_sessions"
    _write_prior_session(sessions_root, "20260717T120000Z_api_boolean_aaaa1111", "api_boolean")

    memory = gather_prior_review_memory(sessions_root, "api_boolean")

    assert memory["enabled"] is True
    assert memory["session_count"] == 1
    assert memory["comment_count"] == 1
    feedback = memory["sessions"][0]["feedback"][0]
    assert feedback["decision"] == "revise"
    assert "大坐标" in feedback["user_comment"]
    assert feedback["requested_changes"][0]["scope"] == "cases"
    assert feedback["constraints"] == ["保留固定门禁语义。"]


def test_gather_memory_ignores_other_functions_and_excludes_current(tmp_path: Path) -> None:
    sessions_root = tmp_path / "artifacts" / "harness_sessions"
    _write_prior_session(sessions_root, "20260717T120000Z_api_boolean_aaaa1111", "api_boolean")
    _write_prior_session(sessions_root, "20260717T120000Z_step_import_bbbb2222", "step_import")

    other = gather_prior_review_memory(sessions_root, "api_offset2d")
    assert other["enabled"] is False

    excluded = gather_prior_review_memory(
        sessions_root,
        "api_boolean",
        exclude_session_id="20260717T120000Z_api_boolean_aaaa1111",
    )
    assert excluded["enabled"] is False


def test_gather_memory_strips_sensitive_text(tmp_path: Path) -> None:
    sessions_root = tmp_path / "artifacts" / "harness_sessions"
    session_root = sessions_root / "20260717T120000Z_api_boolean_cccc3333"
    _write_json(
        session_root / "session.json",
        {
            "schema_version": 1,
            "session_id": "20260717T120000Z_api_boolean_cccc3333",
            "public_function": "api_boolean",
            "state": "rejected",
            "updated_at": "2026-07-17T16:00:00Z",
        },
    )
    comment_root = session_root / "rounds" / "0001" / "comments" / ("b" * 64)
    comment_root.mkdir(parents=True, exist_ok=True)
    (comment_root / "user_comment.txt").write_text("用 sk-abcdef123456 这个 key 跑 C:\\temp\\x.step", encoding="utf-8")
    _write_json(
        comment_root / "comment_decision.json",
        {
            "decision": {
                "decision": "revise",
                "summary_zh_cn": "正常摘要。",
                "requested_changes": [
                    {"scope": "cases", "instruction": "参考 C:\\temp\\secret.step 的参数。", "priority": "low"},
                    {"scope": "cases", "instruction": "增加正常用例。", "priority": "high"},
                ],
                "constraints": [],
            }
        },
    )

    memory = gather_prior_review_memory(sessions_root, "api_boolean")

    feedback = memory["sessions"][0]["feedback"][0]
    assert feedback["user_comment"] == ""
    assert len(feedback["requested_changes"]) == 1
    assert feedback["requested_changes"][0]["instruction"] == "增加正常用例。"


def _new_session_form(tmp_path: Path, prior_session_id: str) -> dict:
    sessions_root = tmp_path / "artifacts" / "harness_sessions"
    candidates = [
        path
        for path in sessions_root.glob("*_api_boolean_*")
        if path.name != prior_session_id and (path / "rounds/0001/internal/api_test_form.json").is_file()
    ]
    assert len(candidates) == 1
    return json.loads((candidates[0] / "rounds/0001/internal/api_test_form.json").read_text(encoding="utf-8"))


def test_round_one_form_injects_prior_memory_by_default(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    sessions_root = tmp_path / "artifacts" / "harness_sessions"
    prior_id = "20260717T120000Z_api_boolean_aaaa1111"
    _write_prior_session(sessions_root, prior_id, "api_boolean")

    workflow.start("api_boolean")

    form = _new_session_form(tmp_path, prior_id)
    assert "prior_review_memory" in form
    assert form["prior_review_memory"]["enabled"] is True
    assert "memory_note" in form


def test_round_one_form_omits_memory_when_disabled(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    sessions_root = tmp_path / "artifacts" / "harness_sessions"
    prior_id = "20260717T120000Z_api_boolean_aaaa1111"
    _write_prior_session(sessions_root, prior_id, "api_boolean")

    workflow.start("api_boolean", use_memory=False)

    form = _new_session_form(tmp_path, prior_id)
    assert "prior_review_memory" not in form
    assert "memory_note" not in form
