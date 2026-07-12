from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from run_message_harness_pipeline import MessageHarnessPipeline, _triage_has_failures  # noqa: E402

from test_harness.authoring_gateway.client import (  # noqa: E402
    HttpResponse,
    OpenAICompatibleMessageClient,
)
from test_harness.authoring_gateway.config import PROFILE_SPECS, GatewayConfig  # noqa: E402
from test_harness.authoring_gateway.gateway import GatewayError, TaskSpec  # noqa: E402

TOOL_REPO_ROOT = Path(__file__).resolve().parents[2]


def provider_response(candidate: dict[str, Any]) -> HttpResponse:
    payload = {
        "id": "mock-pipeline",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(candidate),
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200},
    }
    return HttpResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode())


def test_failure_assets_are_not_scheduled_for_clean_triage() -> None:
    assert not _triage_has_failures(
        {
            "passed_cases": 23,
            "failed_cases": 0,
            "pre_artifact_failure_cases": 0,
            "command_failures": 0,
        }
    )
    assert _triage_has_failures({"failed_cases": 1})
    assert _triage_has_failures({"pre_artifact_failure_cases": 1})
    assert _triage_has_failures({"command_failures": 1})


class RepairQueueTransport:
    def __init__(self, responses: list[HttpResponse], accepted_path: Path) -> None:
        self.responses = list(responses)
        self.accepted_path = accepted_path
        self.requests: list[dict[str, Any]] = []

    def post(self, **kwargs: Any) -> HttpResponse:
        self.requests.append(dict(kwargs))
        if len(self.requests) == 2:
            assert not self.accepted_path.exists(), "failed candidate reached accepted formal path"
        if not self.responses:
            raise AssertionError("mock response queue exhausted")
        return self.responses.pop(0)


def config() -> GatewayConfig:
    return GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="Qwen3.6-35B-A3B",
        api_key="mock-pipeline-key",
        request_timeout_seconds=1.0,
        max_retries=0,
    )


def candidate_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    dsl = json.loads(
        (
            TOOL_REPO_ROOT
            / "test_harness/interface_example_packs/api_boolean_primitives.example_dsl.json"
        ).read_text(encoding="utf-8")
    )
    invalid = copy.deepcopy(dsl)
    invalid["defaults"]["expectations"]["topocheck"] = True
    return (
        {"kind": "attack_dsl", "dsl": invalid, "notes": []},
        {"kind": "attack_dsl", "dsl": dsl, "notes": []},
    )


def test_fixed_gate_diagnostic_repairs_unknown_topocheck_before_acceptance(tmp_path: Path) -> None:
    accepted_path = tmp_path / "artifacts/accepted/iface_01.json"
    invalid, repaired = candidate_pair()
    transport = RepairQueueTransport(
        [provider_response(invalid), provider_response(repaired)],
        accepted_path,
    )
    gateway_config = config()
    client = OpenAICompatibleMessageClient(gateway_config, transport=transport)
    pipeline = MessageHarnessPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=TOOL_REPO_ROOT,
        client=client,
        gate_timeout_seconds=30.0,
    )
    task = TaskSpec(
        task_id="iface_01_boolean_primitive_source_guided",
        task_type="interface_form",
        prompt="Generate one bounded api_boolean attack_dsl with real semantic oracles.",
        prompt_path="artifacts/prompts/iface_01.md",
        expected_output_path=accepted_path,
        output_contract={
            "type": "json_object",
            "kind_field": "kind",
            "allowed_kinds": ["attack_dsl"],
        },
    )

    result = pipeline.run_task(
        task,
        run_id="mock_topocheck_repair",
        max_contract_repairs=0,
        max_gate_repairs=1,
    )

    assert result.ok
    assert result.authoring_accepted
    assert result.gate_attempts == 2
    assert result.message_calls == 2
    assert len(transport.requests) == 2
    first_gate = result.attempts[0]["fixed_gate"]
    second_gate = result.attempts[1]["fixed_gate"]
    assert not first_gate["ok"]
    assert {item["error_code"] for item in first_gate["diagnostics"]} == {
        "UNSUPPORTED_EXPECTATION_ORACLE"
    }
    assert second_gate["ok"]
    repair_request = json.loads(transport.requests[1]["body"])
    repair_prompt = repair_request["messages"][1]["content"]
    assert "UNSUPPORTED_EXPECTATION_ORACLE" in repair_prompt
    assert "expectations.topocheck" in repair_prompt
    assert json.loads(accepted_path.read_text(encoding="utf-8")) == repaired
    provenance = json.loads(
        accepted_path.with_name("iface_01.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["fixed_gate"]["ok"] is True
    assert provenance["fixed_gate"]["gate_attempt"] == 2
    assert provenance["fixed_gate"]["kind"] == "attack_dsl"
    assert provenance["acceptance"] == {
        "authoring_accepted": True,
        "requires_fixed_gate": False,
        "accepted_by": "message_harness_pipeline",
    }
    assert provenance["repair"]["gate_repair_output"] is True
    assert provenance["repair"]["gate_repair_iteration"] == 1

    resumed = pipeline.run_task(
        task,
        run_id="mock_topocheck_resume",
        max_contract_repairs=0,
        max_gate_repairs=0,
    )
    assert resumed.ok
    assert resumed.skipped
    assert resumed.authoring_accepted
    assert resumed.message_calls == 0
    assert resumed.gate_attempts == 1
    assert resumed.attempts[0]["fixed_gate"]["ok"] is True

    wrong_goal = pipeline.run_task(
        task,
        run_id="mock_topocheck_wrong_goal",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="must_reproduce_target_signature",
        target_failure_signature={"kind": "crash", "exception_code": "0xC0000005"},
        execute=False,
    )
    assert not wrong_goal.ok
    assert not wrong_goal.authoring_accepted
    assert "did not stably reproduce" in wrong_goal.error


def test_existing_pair_with_forged_acceptance_metadata_is_never_resumed(tmp_path: Path) -> None:
    accepted_path = tmp_path / "artifacts/accepted/iface_01.json"
    _, valid = candidate_pair()
    gateway_config = config()
    pipeline = MessageHarnessPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=TOOL_REPO_ROOT,
        client=OpenAICompatibleMessageClient(
            gateway_config,
            transport=RepairQueueTransport([provider_response(valid)], accepted_path),
        ),
        gate_timeout_seconds=30.0,
    )
    task = TaskSpec(
        task_id="reject_forged_resume",
        prompt="Generate one bounded api_boolean attack_dsl.",
        expected_output_path=accepted_path,
        output_contract={"type": "json_object", "allowed_kinds": ["attack_dsl"]},
    )
    first = pipeline.run_task(
        task,
        run_id="valid_before_tamper",
        max_contract_repairs=0,
        max_gate_repairs=0,
    )
    assert first.ok
    provenance_path = accepted_path.with_name("iface_01.provenance.json")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["acceptance"] = {
        "authoring_accepted": True,
        "requires_fixed_gate": True,
        "accepted_by": "",
    }
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    resumed = pipeline.run_task(task, run_id="forged_resume", max_gate_repairs=0)

    assert not resumed.ok
    assert not resumed.skipped
    assert not resumed.authoring_accepted
    assert "not a verified" in resumed.error


def test_gate_budget_exhaustion_is_nonzero_and_never_creates_accepted_output(tmp_path: Path) -> None:
    accepted_path = tmp_path / "artifacts/accepted/iface_01.json"
    invalid, _ = candidate_pair()
    transport = RepairQueueTransport([provider_response(invalid)], accepted_path)
    gateway_config = config()
    pipeline = MessageHarnessPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=TOOL_REPO_ROOT,
        client=OpenAICompatibleMessageClient(gateway_config, transport=transport),
        gate_timeout_seconds=30.0,
    )
    task = TaskSpec(
        task_id="iface_01_budget_exhaustion",
        prompt="Generate one bounded api_boolean attack_dsl.",
        expected_output_path=accepted_path,
        output_contract={"type": "json_object", "allowed_kinds": ["attack_dsl"]},
    )

    result = pipeline.run_task(
        task,
        run_id="mock_topocheck_exhausted",
        max_contract_repairs=0,
        max_gate_repairs=0,
    )

    assert not result.ok
    assert not result.authoring_accepted
    assert "repair budget exhausted" in result.error
    assert not accepted_path.exists()
    assert not accepted_path.with_name("iface_01.provenance.json").exists()


def test_pipeline_cannot_accept_output_into_harness_source_tree(tmp_path: Path) -> None:
    invalid, _ = candidate_pair()
    gateway_config = config()
    pipeline = MessageHarnessPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=TOOL_REPO_ROOT,
        client=OpenAICompatibleMessageClient(
            gateway_config,
            transport=RepairQueueTransport([provider_response(invalid)], tmp_path / "unused.json"),
        ),
    )
    task = TaskSpec(
        task_id="unsafe_formal_path",
        prompt="Return JSON.",
        expected_output_path=tmp_path / "test_harness/interface_capabilities.json",
        output_contract={"type": "json_object", "allowed_kinds": ["attack_dsl"]},
    )

    with pytest.raises(GatewayError, match="must stay under repository artifacts"):
        pipeline.run_task(task, run_id="unsafe_formal_path", overwrite=True)


def test_review_session_candidate_cannot_execute_without_host_approval(tmp_path: Path) -> None:
    accepted_path = tmp_path / "artifacts/sessions/round_0001/candidate.json"
    _invalid, valid = candidate_pair()
    gateway_config = config()
    transport = RepairQueueTransport([provider_response(valid)], accepted_path)
    pipeline = MessageHarnessPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=TOOL_REPO_ROOT,
        client=OpenAICompatibleMessageClient(gateway_config, transport=transport),
        gate_timeout_seconds=30.0,
    )
    task = TaskSpec(
        task_id="session_round_0001",
        task_type="interface_form",
        prompt="Generate one bounded api_boolean review candidate.",
        expected_output_path=accepted_path,
        output_contract={"type": "json_object", "allowed_kinds": ["attack_dsl"]},
        metadata={
            "review_required_before_execute": True,
            "harness_session_id": "session_001",
            "harness_round_number": 1,
            "approval_attestation_path": "",
        },
    )

    fresh_execute = pipeline.run_task(
        task,
        run_id="fresh_generate_execute_forbidden",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="must_pass_execution",
        execute=True,
        runner="artifacts/fake_runner.exe",
    )
    reviewed = pipeline.run_task(
        task,
        run_id="review_only",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="fixed_gate_only",
        execute=False,
    )
    assert not fresh_execute.ok
    assert not fresh_execute.execution.requested
    assert "cannot generate and execute in one step" in fresh_execute.error
    assert reviewed.ok
    assert reviewed.authoring_accepted
    assert not reviewed.execution.requested

    direct_execute = pipeline.run_task(
        task,
        run_id="direct_execute_forbidden",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="must_pass_execution",
        execute=True,
        runner="artifacts/fake_runner.exe",
    )
    overwrite_execute = pipeline.run_task(
        task,
        run_id="overwrite_execute_forbidden",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="must_pass_execution",
        overwrite=True,
        execute=True,
        runner="artifacts/fake_runner.exe",
    )
    stripped_manifest_task = TaskSpec(
        task_id=task.task_id,
        task_type=task.task_type,
        prompt=task.prompt,
        expected_output_path=task.expected_output_path,
        output_contract=task.output_contract,
        metadata={},
    )
    stripped_overwrite_execute = pipeline.run_task(
        stripped_manifest_task,
        run_id="stripped_overwrite_execute_forbidden",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="must_pass_execution",
        overwrite=True,
        execute=True,
        runner="artifacts/fake_runner.exe",
    )

    assert not direct_execute.ok
    assert direct_execute.execution.requested
    assert direct_execute.execution.status == "approval_required"
    assert "approval" in direct_execute.execution.error
    assert not overwrite_execute.ok
    assert not overwrite_execute.execution.requested
    assert "--overwrite" in overwrite_execute.error
    assert not stripped_overwrite_execute.ok
    assert not stripped_overwrite_execute.execution.requested
    assert "--overwrite" in stripped_overwrite_execute.error
    assert len(transport.requests) == 1, "execution bypass must not trigger another model call"


def test_execution_approval_binds_round_candidate_review_prompt_and_runner(tmp_path: Path) -> None:
    accepted_path = tmp_path / "artifacts/sessions/round_0001/candidate.json"
    _invalid, valid = candidate_pair()
    gateway_config = config()
    pipeline = MessageHarnessPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=TOOL_REPO_ROOT,
        client=OpenAICompatibleMessageClient(
            gateway_config,
            transport=RepairQueueTransport([provider_response(valid)], accepted_path),
        ),
        gate_timeout_seconds=30.0,
    )
    prompt = "Generate one bounded api_boolean review candidate."
    base_task = TaskSpec(
        task_id="session_round_0001_bound",
        task_type="interface_form",
        prompt=prompt,
        expected_output_path=accepted_path,
        output_contract={"type": "json_object", "allowed_kinds": ["attack_dsl"]},
        metadata={
            "review_required_before_execute": True,
            "harness_session_id": "session_001",
            "harness_round_number": 1,
            "approval_attestation_path": "",
        },
    )
    reviewed = pipeline.run_task(
        base_task,
        run_id="review_bound",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="fixed_gate_only",
    )
    assert reviewed.ok
    provenance_path = accepted_path.with_name("candidate.provenance.json")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    runner = tmp_path / "artifacts/fake_runner.exe"
    runner.write_bytes(b"immutable-runner-bytes")
    canonical = lambda value: json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    candidate_sha256 = hashlib.sha256(canonical(valid)).hexdigest()
    round_sha256 = "a" * 64
    comment_path = tmp_path / "artifacts/sessions/comment.txt"
    comment_path.write_text("这一版可以开始执行真实测试。", encoding="utf-8")
    interpretation = {
        "schema_version": 1,
        "record_type": "review_comment_decision",
        "status": "model_interpreted",
        "qwen_called": True,
        "decision": {
            "decision": "approve",
            "summary_zh_cn": "用户明确同意执行。",
            "requested_changes": [],
            "constraints": [],
        },
    }
    interpretation_path = tmp_path / "artifacts/sessions/interpretation.json"
    interpretation_path.write_text(json.dumps(interpretation), encoding="utf-8")
    approval_unsigned = {
        "schema_version": 1,
        "record_type": "execution_approval",
        "decision": "approved_for_execution",
        "session_id": "session_001",
        "task_id": base_task.task_id,
        "round_number": 1,
        "round_sha256": round_sha256,
        "candidate_sha256": candidate_sha256,
        "task_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "review_packet_sha256": provenance["generated_artifact_review"]["review_packet_sha256"],
        "comment_path": comment_path.relative_to(tmp_path).as_posix(),
        "comment_sha256": hashlib.sha256(comment_path.read_bytes()).hexdigest(),
        "interpretation_path": interpretation_path.relative_to(tmp_path).as_posix(),
        "interpretation_sha256": hashlib.sha256(canonical(interpretation)).hexdigest(),
        "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
        "approved_at": "2026-07-12T00:00:00Z",
        "authority": "fixed_harness_host_after_qwen_comment_interpretation",
    }
    approval = {
        **approval_unsigned,
        "approval_sha256": hashlib.sha256(canonical(approval_unsigned)).hexdigest(),
    }
    approval_path = tmp_path / "artifacts/sessions/approval.json"
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    approved_task = TaskSpec(
        task_id=base_task.task_id,
        task_type=base_task.task_type,
        prompt=prompt,
        expected_output_path=accepted_path,
        output_contract=base_task.output_contract,
        metadata={
            "review_required_before_execute": True,
            "approval_attestation_path": approval_path.relative_to(tmp_path).as_posix(),
            "approved_round_sha256": round_sha256,
            "approved_candidate_sha256": candidate_sha256,
        },
    )

    assert (
        pipeline._execution_approval_error(  # noqa: SLF001
            approved_task,
            accepted_path,
            provenance_path,
            runner.relative_to(tmp_path),
        )
        == ""
    )
    runner.write_bytes(b"changed-runner")
    assert "runner bytes" in pipeline._execution_approval_error(  # noqa: SLF001
        approved_task,
        accepted_path,
        provenance_path,
        runner.relative_to(tmp_path),
    )
