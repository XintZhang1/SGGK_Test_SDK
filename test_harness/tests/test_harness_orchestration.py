from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_harness.authoring_gateway.client import (  # noqa: E402
    HttpResponse,
    OpenAICompatibleMessageClient,
)
from test_harness.authoring_gateway.config import PROFILE_SPECS, GatewayConfig  # noqa: E402
from test_harness.authoring_gateway.review_comment import ReviewCommentContext  # noqa: E402
from test_harness.orchestration.runtime import MessageApiRuntime  # noqa: E402
from test_harness.orchestration.__main__ import _OfflineRuntime  # noqa: E402
from test_harness.orchestration.workflow import (  # noqa: E402
    HarnessWorkflow,
    WorkflowError,
    resolve_public_function,
)


class FakeRuntime:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.generate_calls = 0
        self.interpret_calls = 0
        self.execute_calls = 0
        self.execute_outcomes: list[bool] = []
        self.execution_requests: list[dict[str, Any]] = []

    def generate(
        self,
        *,
        manifest_path: Path,
        run_id: str,
        staging_root: Path,
    ) -> dict[str, Any]:
        self.generate_calls += 1
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        task = manifest["tasks"][0]
        output = self.repo_root / task["expected_output_path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        candidate = {
            "kind": "attack_dsl",
            "dsl": {
                "version": 1,
                "cases": [
                    {
                        "case_id": f"round_{self.generate_calls}_nominal",
                        "api": "api_boolean",
                        "hypothesis": "nominal and tolerance-boundary behavior",
                    },
                    {
                        "case_id": f"round_{self.generate_calls}_boundary",
                        "api": "api_boolean",
                        "hypothesis": "topo_tol boundary behavior",
                    },
                ],
            },
            "notes": ["Qwen-generated review candidate"],
        }
        output.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        provenance = output.with_name(f"{output.stem}.provenance.json")
        provenance.write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
        review_root = staging_root / run_id / task["task_id"] / "review"
        review_root.mkdir(parents=True, exist_ok=True)
        packet = review_root / "review_packet.json"
        report = review_root / "review_report.zh-CN.md"
        packet.write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
        report.write_text("# 固定审查报告\n", encoding="utf-8")

        def rel(path: Path) -> str:
            return path.relative_to(self.repo_root).as_posix()

        return {
            "ok": True,
            "run_id": run_id,
            "results": [
                {
                    "task_id": task["task_id"],
                    "run_id": run_id,
                    "authoring_accepted": True,
                    "selection_policy": "fixed_gate_only",
                    "candidate_count": 3,
                    "review_packet_path": rel(packet),
                    "review_report_path": rel(report),
                }
            ],
        }

    def interpret_comment(
        self,
        *,
        comment: str,
        session: dict[str, Any],
        round_record: dict[str, Any],
        subject_outline: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        self.interpret_calls += 1
        assert subject_outline["candidate"]["dsl"]["cases"][1]["case_id"]
        if "增加" in comment:
            decision = {
                "decision": "revise",
                "summary_zh_cn": "需要增加大坐标边界。",
                "requested_changes": [
                    {
                        "scope": "cases",
                        "instruction": "增加大坐标边界",
                        "priority": "high",
                    }
                ],
                "constraints": [],
            }
        else:
            decision = {
                "decision": "approve",
                "summary_zh_cn": "用户同意当前轮次。",
                "requested_changes": [],
                "constraints": [],
            }
        return {"schema_version": 1, "decision": decision}

    def execute(
        self,
        *,
        manifest_path: Path,
        run_id: str,
        staging_root: Path,
        runner_path: Path | None,
    ) -> dict[str, Any]:
        self.execute_calls += 1
        self.execution_requests.append(
            {
                "manifest_path": manifest_path,
                "run_id": run_id,
                "staging_root": staging_root,
                "runner_path": runner_path,
            }
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        task = manifest["tasks"][0]
        assert task["approval_attestation_path"]
        assert task["approved_candidate_sha256"]
        passed = self.execute_outcomes.pop(0) if self.execute_outcomes else True
        return {
            "ok": passed,
            "staging_path": staging_root.relative_to(self.repo_root).as_posix(),
            "results": [
                {
                    "authoring_accepted": True,
                    "error": "" if passed else "simulated SDK execution failure",
                    "execution": {
                        "requested": True,
                        "ok": passed,
                        "status": "passed" if passed else "failed",
                        "error": "" if passed else "simulated SDK execution failure",
                    },
                }
            ],
        }


def make_workflow(tmp_path: Path) -> tuple[HarnessWorkflow, FakeRuntime]:
    capabilities = tmp_path / "test_harness" / "interface_capabilities.json"
    capabilities.parent.mkdir(parents=True)
    capabilities.write_bytes((REPO_ROOT / "test_harness/interface_capabilities.json").read_bytes())
    (tmp_path / "artifacts").mkdir()
    runtime = FakeRuntime(tmp_path)
    workflow = HarnessWorkflow(runtime, repo_root=tmp_path, profile="intranet")
    return workflow, runtime


def test_read_only_runtime_exposes_provider_identity_without_api_config() -> None:
    runtime = _OfflineRuntime("intranet")

    assert runtime.provider_profile == "intranet"
    assert runtime.provider_profile_category == "intranet"
    with pytest.raises(WorkflowError, match="unavailable in read-only mode"):
        runtime.generate()


def test_start_and_revision_need_only_function_and_comment(tmp_path: Path) -> None:
    workflow, runtime = make_workflow(tmp_path)

    started = workflow.start("api_boolean")

    assert started["state"] == "awaiting_comment"
    assert started["current_round"] == 1
    assert runtime.generate_calls == 1
    assert runtime.execute_calls == 0
    first_report = tmp_path / started["review_report_path"]
    assert "尚未调用 SGGK SDK 真实执行" in first_report.read_text(encoding="utf-8")

    revised = workflow.comment("增加大坐标和 topo_tol 两侧的复杂边界。")

    assert revised["state"] == "awaiting_comment"
    assert revised["current_round"] == 2
    assert runtime.generate_calls == 2
    assert runtime.execute_calls == 0
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    assert (session_root / "rounds/0001/round_manifest.json").is_file()
    assert (session_root / "rounds/0002/round_manifest.json").is_file()


def test_ambiguous_approval_never_executes_but_explicit_comment_does(tmp_path: Path) -> None:
    workflow, runtime = make_workflow(tmp_path)
    started = workflow.start("api_boolean")
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    round_record = json.loads(
        (session_root / "rounds/0001/round_manifest.json").read_text(encoding="utf-8")
    )
    reviewed_manifest = tmp_path / round_record["manifest_path"]
    reviewed_provenance = tmp_path / round_record["provenance_path"]
    manifest_bytes = reviewed_manifest.read_bytes()
    provenance_bytes = reviewed_provenance.read_bytes()

    ambiguous = workflow.comment("看起来不错。")

    assert ambiguous["state"] == "awaiting_comment"
    assert runtime.execute_calls == 0
    assert ambiguous["notice_path"]

    completed = workflow.comment("我明确同意当前方案，可以开始执行真实测试。")

    assert completed["state"] == "completed"
    assert runtime.execute_calls == 1
    assert completed["final_report_path"]
    assert reviewed_manifest.read_bytes() == manifest_bytes
    assert reviewed_provenance.read_bytes() == provenance_bytes
    final_report = tmp_path / completed["final_report_path"]
    assert "总体结果：`通过`" in final_report.read_text(encoding="utf-8")

    revised_after_execution = workflow.comment("增加一个新的容差边界用例。")

    assert revised_after_execution["state"] == "awaiting_comment"
    assert revised_after_execution["current_round"] == started["current_round"] + 1
    session = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    assert session["approved_round"] == 0


@pytest.mark.parametrize(
    "comment",
    [
        "请不要执行。",
        "现在不可以运行测试。",
        "现在不执行这一版。",
        "不可以开始执行这一版。",
        "Do not execute this version.",
        "Please do not run the tests yet.",
    ],
)
def test_negated_execution_comment_never_authorizes_execution(
    tmp_path: Path,
    comment: str,
) -> None:
    workflow, runtime = make_workflow(tmp_path)
    workflow.start("api_boolean")

    result = workflow.comment(comment)

    assert result["state"] == "awaiting_comment"
    assert result["notice_path"]
    assert runtime.execute_calls == 0


@pytest.mark.parametrize(
    "comment",
    [
        "现在可以执行了吗？",
        "请问是否可以开始测试？",
        "Please run this version?",
        "Could you please run this version?",
    ],
)
def test_execution_question_never_authorizes_execution(
    tmp_path: Path,
    comment: str,
) -> None:
    workflow, runtime = make_workflow(tmp_path)
    workflow.start("api_boolean")

    result = workflow.comment(comment)

    assert result["state"] == "awaiting_comment"
    assert result["notice_path"]
    assert runtime.execute_calls == 0


def test_status_and_show_do_not_expose_internal_identifiers(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    started = workflow.start("sggk::api_boolean")

    status = workflow.status()
    report = workflow.show()

    assert status == started
    assert report == tmp_path / status["review_report_path"]
    assert "session_id" not in status
    assert "round_sha256" not in status
    assert "task_id" not in status


def test_review_comment_receives_safe_semantic_subject_outline(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    workflow.start("api_boolean")
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    round_record = json.loads(
        (session_root / "rounds/0001/round_manifest.json").read_text(encoding="utf-8")
    )
    subject = json.loads(
        (tmp_path / round_record["subject_digest_path"]).read_text(encoding="utf-8")
    )

    context = ReviewCommentContext(
        task_id=round_record["task_id"],
        run_id=round_record["run_id"],
        round_number=1,
        subject_sha256=round_record["subject_digest_sha256"],
        subject_outline=subject,
        target="api_boolean",
    )

    assert context.as_dict()["subject_outline"]["candidate"]["dsl"]["cases"][1]["case_id"]


def test_resolver_distinguishes_plugin_extension_and_header_overloads(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk"
    header = sdk / "include/sggk/modeling/public_api.h"
    header.parent.mkdir(parents=True)
    header.write_text(
        "namespace sggk {\n"
        "Result api_new(Body a);\n"
        "Result api_new(Body a, Body b);\n"
        "}\n",
        encoding="utf-8",
    )
    capabilities = {
        "apis": {
            "api_plugin": {
                "runner_recipe_api": True,
                "plugin": {"api": "api_plugin"},
            }
        }
    }

    plugin = resolve_public_function("api_plugin", capabilities)
    extension = resolve_public_function("sggk::api_new", capabilities, sdk_dir=sdk)

    assert plugin["route"] == "checked_plugin_form"
    assert extension["route"] == "extension_backlog"
    assert len(extension["declarations"]) == 2
    assert extension["declarations"][0]["function_ref_id"] != extension["declarations"][1][
        "function_ref_id"
    ]
    with pytest.raises(WorkflowError, match="do not provide a path"):
        resolve_public_function("../api_new", capabilities)


def test_intranet_source_root_is_automatically_bound_but_external_is_not(tmp_path: Path) -> None:
    capabilities = tmp_path / "test_harness/interface_capabilities.json"
    capabilities.parent.mkdir(parents=True)
    capabilities.write_bytes((REPO_ROOT / "test_harness/interface_capabilities.json").read_bytes())
    (tmp_path / "artifacts").mkdir()
    source_root = tmp_path / "sdk_source"
    source = source_root / "modeling/boolean_api.cpp"
    source.parent.mkdir(parents=True)
    source.write_text(
        "Result api_boolean(Body target, Body tool) {\n"
        "  if (!target || !tool) return InvalidInput();\n"
        "  return RunBoolean(target, tool);\n"
        "}\n",
        encoding="utf-8",
    )
    intranet_runtime = FakeRuntime(tmp_path)
    intranet = HarnessWorkflow(
        intranet_runtime,
        repo_root=tmp_path,
        profile="intranet",
        source_root=source_root,
    )

    intranet.start("api_boolean")
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    manifest = json.loads(
        (session_root / "rounds/0001/prompt/model_task_manifest.json").read_text(encoding="utf-8")
    )
    prompt = (session_root / "rounds/0001/prompt/authoring_prompt.md").read_text(
        encoding="utf-8"
    )

    assert manifest["tasks"][0]["task_type"] == "source_attack"
    assert manifest["tasks"][0]["data_classification"] == "proprietary_source"
    assert manifest["tasks"][0]["source_contract"]["source_refs"]
    assert "if (!target || !tool)" in prompt

def test_source_resolution_ignores_text_occurrences_and_binds_all_definitions(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sdk_source"
    source = source_root / "modeling/api.cpp"
    source.parent.mkdir(parents=True)
    source.write_text(
        'const char* name = "api_boolean";\n'
        "Result helper(Body a, Body b) { return api_boolean(a, b); }\n"
        "Result api_boolean(Body a) { return RunBoolean(a); }\n"
        "Result api_boolean(Body a, Body b) { return RunBoolean(a, b); }\n",
        encoding="utf-8",
    )

    resolution = resolve_public_function(
        "api_boolean",
        {"apis": {}},
        source_root=source_root,
    )

    assert len(resolution["source_occurrences"]) == 2
    assert [item["match_line"] for item in resolution["source_occurrences"]] == [3, 4]
    assert all(
        item["definition_kind"] == "function_definition"
        for item in resolution["source_occurrences"]
    )


def test_source_revision_rediscovers_current_definition(tmp_path: Path) -> None:
    capabilities = tmp_path / "test_harness/interface_capabilities.json"
    capabilities.parent.mkdir(parents=True)
    capabilities.write_bytes((REPO_ROOT / "test_harness/interface_capabilities.json").read_bytes())
    (tmp_path / "artifacts").mkdir()
    source_root = tmp_path / "sdk_source"
    source = source_root / "boolean.cpp"
    source.parent.mkdir()
    source.write_text(
        "Result api_boolean(Body a, Body b) {\n"
        "  if (!a) return InvalidInput();\n"
        "  return RunBoolean(a, b);\n"
        "}\n",
        encoding="utf-8",
    )
    workflow = HarnessWorkflow(
        FakeRuntime(tmp_path),
        repo_root=tmp_path,
        profile="intranet",
        source_root=source_root,
    )
    workflow.start("api_boolean")
    source.write_text(
        "// implementation moved during review\n"
        "Result api_boolean(Body a, Body b) {\n"
        "  if (!b) return InvalidTool();\n"
        "  return RunBoolean(a, b);\n"
        "}\n",
        encoding="utf-8",
    )

    revised = workflow.comment("增加 tool 为空时的边界覆盖。")

    assert revised["current_round"] == 2
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    second_prompt = (session_root / "rounds/0002/prompt/authoring_prompt.md").read_text(
        encoding="utf-8"
    )
    second_resolution = json.loads(
        (session_root / "resolution/round_0002.json").read_text(encoding="utf-8")
    )
    assert "if (!b) return InvalidTool();" in second_prompt
    assert second_resolution["source_occurrences"][0]["match_line"] == 2


def test_mutating_action_rejects_changed_source_root(tmp_path: Path) -> None:
    capabilities = tmp_path / "test_harness/interface_capabilities.json"
    capabilities.parent.mkdir(parents=True)
    capabilities.write_bytes((REPO_ROOT / "test_harness/interface_capabilities.json").read_bytes())
    (tmp_path / "artifacts").mkdir()
    first_root = tmp_path / "first_source"
    second_root = tmp_path / "second_source"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "api.cpp").write_text(
        "Result api_boolean(Body a, Body b) { return RunBoolean(a, b); }\n",
        encoding="utf-8",
    )
    first = HarnessWorkflow(
        FakeRuntime(tmp_path),
        repo_root=tmp_path,
        profile="intranet",
        source_root=first_root,
    )
    first.start("api_boolean")
    switched = HarnessWorkflow(
        FakeRuntime(tmp_path),
        repo_root=tmp_path,
        profile="intranet",
        source_root=second_root,
    )

    with pytest.raises(WorkflowError, match="source root changed"):
        switched.comment("增加一个新的边界用例。")


def test_workflow_rejects_runner_outside_repository(tmp_path: Path) -> None:
    capabilities = tmp_path / "repo" / "test_harness/interface_capabilities.json"
    capabilities.parent.mkdir(parents=True)
    capabilities.write_bytes((REPO_ROOT / "test_harness/interface_capabilities.json").read_bytes())
    (tmp_path / "repo/artifacts").mkdir()
    outside_runner = tmp_path / "runner.exe"
    outside_runner.write_bytes(b"runner")

    with pytest.raises(WorkflowError, match="runner must stay inside the repository"):
        HarnessWorkflow(
            FakeRuntime(tmp_path / "repo"),
            repo_root=tmp_path / "repo",
            profile="intranet",
            runner_path=outside_runner,
        )


def test_header_evidence_is_intranet_classified_even_without_source_implementation(
    tmp_path: Path,
) -> None:
    capabilities = tmp_path / "test_harness/interface_capabilities.json"
    capabilities.parent.mkdir(parents=True)
    capabilities.write_bytes((REPO_ROOT / "test_harness/interface_capabilities.json").read_bytes())
    (tmp_path / "artifacts").mkdir()
    sdk = tmp_path / "sdk"
    header = sdk / "include/sggk/modeling/public_api.h"
    header.parent.mkdir(parents=True)
    header.write_text("Result api_boolean(Body target, Body tool);\n", encoding="utf-8")
    runtime = FakeRuntime(tmp_path)
    workflow = HarnessWorkflow(
        runtime,
        repo_root=tmp_path,
        profile="intranet",
        sdk_dir=sdk,
    )

    workflow.start("api_boolean")
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    manifest = json.loads(
        (session_root / "rounds/0001/prompt/model_task_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    task = manifest["tasks"][0]

    assert task["task_type"] == "interface_form"
    assert task["data_classification"] == "proprietary_source"
    assert task["allowed_profile_categories"] == ["intranet"]
    assert task["provider_profile"] == "intranet"
    assert task["provider_profile_category"] == "intranet"


def test_session_rejects_tampered_earlier_event(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    workflow.start("api_boolean")
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    first_event = session_root / "events/000001.json"
    event = json.loads(first_event.read_text(encoding="utf-8"))
    event["payload"]["public_function"] = "api_tampered"
    first_event.write_text(json.dumps(event), encoding="utf-8")

    with pytest.raises(WorkflowError, match="payload hash mismatch"):
        workflow.status()


def test_round_loader_rejects_tampered_reviewed_manifest(tmp_path: Path) -> None:
    workflow, runtime = make_workflow(tmp_path)
    workflow.start("api_boolean")
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    round_record = json.loads(
        (session_root / "rounds/0001/round_manifest.json").read_text(encoding="utf-8")
    )
    manifest_path = tmp_path / round_record["manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tasks"][0]["task_type"] = "interface_form"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(WorkflowError, match="latest review artifact changed: manifest_path"):
        workflow.comment("please execute this approved test")

    assert runtime.interpret_calls == 0
    assert runtime.execute_calls == 0


def test_retry_rejects_tampered_execution_manifest(tmp_path: Path) -> None:
    workflow, runtime = make_workflow(tmp_path)
    runtime.execute_outcomes = [False]
    workflow.start("api_boolean")
    failed = workflow.comment("please execute this approved test")
    assert failed["state"] == "execution_failed"

    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    session = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    manifest_path = tmp_path / session["execution_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tasks"][0]["task_type"] = "interface_form"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(WorkflowError, match="execution manifest changed after approval"):
        workflow.retry()

    assert runtime.execute_calls == 1


def test_retry_uses_isolated_attempt_roots_and_preserves_prior_result(tmp_path: Path) -> None:
    workflow, runtime = make_workflow(tmp_path)
    runtime.execute_outcomes = [False, True]
    workflow.start("api_boolean")

    first = workflow.comment("please execute this approved test")
    second = workflow.retry()

    assert first["state"] == "execution_failed"
    assert second["state"] == "completed"
    assert runtime.execute_calls == 2
    first_request, second_request = runtime.execution_requests
    assert first_request["run_id"] != second_request["run_id"]
    assert first_request["staging_root"] != second_request["staging_root"]
    assert first_request["staging_root"].parent.name == "attempt_0001"
    assert second_request["staging_root"].parent.name == "attempt_0002"
    first_result = json.loads(
        (first_request["staging_root"].parent / "execution_result.json").read_text(
            encoding="utf-8"
        )
    )
    second_result = json.loads(
        (second_request["staging_root"].parent / "execution_result.json").read_text(
            encoding="utf-8"
        )
    )
    assert first_result["ok"] is False
    assert second_result["ok"] is True
    assert (tmp_path / second["final_report_path"]).parent.name == "attempt_0002"


def test_dead_lock_and_interrupted_execution_are_recovered_without_auto_run(
    tmp_path: Path,
) -> None:
    workflow, runtime = make_workflow(tmp_path)
    workflow.start("api_boolean")
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    session_path = session_root / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["state"] = "executing"
    session["recovery_state"] = "execution_failed"
    session_path.write_text(json.dumps(session), encoding="utf-8")
    orphan_event = session_root / "events/999999.json"
    orphan_event.write_text(json.dumps({"uncommitted": True}), encoding="utf-8")
    lock_path = tmp_path / "artifacts/harness_sessions/.workflow.lock"
    lock_path.write_text("pid=999999 started_at=2020-01-01T00:00:00Z\n", encoding="utf-8")

    recovered = workflow.comment("looks fine")

    assert recovered["state"] == "awaiting_comment"
    assert runtime.execute_calls == 0
    assert not lock_path.exists()
    assert not orphan_event.exists()
    assert list((session_root / "recovery/uncommitted_events").rglob("999999.json"))
    events = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((session_root / "events").glob("*.json"))]
    assert any(event["event_type"] == "INTERRUPTED_OPERATION_RECOVERED" for event in events)


def test_expired_empty_lock_from_pre_owner_crash_is_quarantined(tmp_path: Path) -> None:
    workflow, runtime = make_workflow(tmp_path)
    workflow.start("api_boolean")
    lock_path = tmp_path / "artifacts/harness_sessions/.workflow.lock"
    lock_path.write_bytes(b"")
    os.utime(lock_path, (1, 1))

    result = workflow.comment("looks fine")

    assert result["state"] == "awaiting_comment"
    assert runtime.execute_calls == 0
    assert not lock_path.exists()
    assert list((lock_path.parent / ".stale_locks").glob(".workflow.lock.invalid_owner.*"))


def test_live_lock_is_never_reclaimed_even_when_old(tmp_path: Path) -> None:
    workflow, runtime = make_workflow(tmp_path)
    workflow.start("api_boolean")
    lock_path = tmp_path / "artifacts/harness_sessions/.workflow.lock"
    lock_path.write_text(f"pid={os.getpid()} started_at=2020-01-01T00:00:00Z\n", encoding="utf-8")
    os.utime(lock_path, (1, 1))

    with pytest.raises(WorkflowError, match="another Harness session operation is running"):
        workflow.comment("looks fine")

    assert runtime.interpret_calls == 0
    assert lock_path.is_file()


class QueueTransport:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self.candidates = list(candidates)
        self.calls = 0

    def post(self, **_kwargs: Any) -> HttpResponse:
        self.calls += 1
        if not self.candidates:
            raise AssertionError("mock Message API response queue exhausted")
        candidate = self.candidates.pop(0)
        payload = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(candidate, ensure_ascii=False)},
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},
        }
        return HttpResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode())


def test_real_message_runtime_generates_review_and_interprets_question(tmp_path: Path) -> None:
    capabilities = tmp_path / "test_harness/interface_capabilities.json"
    capabilities.parent.mkdir(parents=True)
    capabilities.write_bytes((REPO_ROOT / "test_harness/interface_capabilities.json").read_bytes())
    (tmp_path / "artifacts").mkdir()
    dsl = json.loads(
        (
            REPO_ROOT
            / "test_harness/interface_example_packs/api_boolean_primitives.example_dsl.json"
        ).read_text(encoding="utf-8")
    )
    generated = {"kind": "attack_dsl", "dsl": dsl, "notes": ["完整固定门禁候选"]}
    question = {
        "decision": "question",
        "summary_zh_cn": "当前方案覆盖基本布尔和语义 Oracle，但尚未执行真实 SDK。",
        "requested_changes": [],
        "constraints": [],
    }
    transport = QueueTransport([generated, question])
    config = GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="Qwen3.6-35B-A3B",
        api_key="test-key",
        max_retries=0,
    )
    client = OpenAICompatibleMessageClient(config, transport=transport)
    runtime = MessageApiRuntime(
        repo_root=tmp_path,
        profile="intranet",
        config=config,
        client=client,
        candidate_count=1,
        candidate_parallelism=1,
    )
    workflow = HarnessWorkflow(runtime, repo_root=tmp_path, profile="intranet")

    started = workflow.start("api_boolean")
    answered = workflow.comment("这一轮目前覆盖了什么，还缺少什么？")

    assert started["state"] == "awaiting_comment"
    assert answered["state"] == "awaiting_comment"
    assert answered["answer_path"]
    assert "尚未执行真实 SDK" in (tmp_path / answered["answer_path"]).read_text(encoding="utf-8")
    assert transport.calls == 2

