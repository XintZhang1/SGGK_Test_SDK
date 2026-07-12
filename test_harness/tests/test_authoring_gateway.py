from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from test_harness.authoring_gateway.client import (
    HttpResponse,
    OpenAICompatibleMessageClient,
    TransportError,
)
from test_harness.authoring_gateway.config import PROFILE_SPECS, ConfigError, GatewayConfig, load_gateway_config
from test_harness.authoring_gateway.gateway import AuthoringGateway, GatewayError, TaskSpec

API_KEY = "test-api-key-never-persist"


class QueueTransport:
    def __init__(self, *items: HttpResponse | Exception) -> None:
        self.items = list(items)
        self.requests: list[dict[str, Any]] = []

    def post(self, **kwargs: Any) -> HttpResponse:
        self.requests.append(dict(kwargs))
        if not self.items:
            raise AssertionError("mock transport queue is empty")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def provider_response(
    content: str,
    *,
    status: int = 200,
    finish_reason: str = "stop",
    reasoning_content: str = "",
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    payload = {
        "id": "mock-completion",
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning_content,
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    return HttpResponse(status, headers or {"content-type": "application/json"}, json.dumps(payload).encode())


def raw_provider_response(body: bytes, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status, {"content-type": "application/json"}, body)


def error_response(status: int, message: str, *, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status,
        headers or {"content-type": "application/json"},
        json.dumps({"error": {"message": message}}).encode(),
    )


def make_config(*, retries: int = 0, profile: str = "intranet") -> GatewayConfig:
    return GatewayConfig(
        profile=PROFILE_SPECS[profile],
        base_url="https://message-api.invalid/v1",
        model="Qwen3.6-35B-A3B",
        api_key=API_KEY,
        request_timeout_seconds=0.1,
        max_retries=retries,
        backoff_base_seconds=0.001,
        max_retry_delay_seconds=0.01,
    )


def flat_candidate(case_id: str = "message_api_smoke") -> dict[str, Any]:
    return {
        "kind": "flat_recipe",
        "recipe": {"case_id": case_id, "api": "api_boolean", "expectations": {"result_bodies": {"min": 1}}},
        "notes": [],
    }


def flat_task(
    repo: Path,
    *,
    task_id: str = "task_one",
    output: str = "artifacts/model_outputs/task_one.json",
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        prompt="Return one bounded flat_recipe JSON object.",
        prompt_path="artifacts/prompts/task.md",
        expected_output_path=repo / output,
        output_contract={"type": "json_object", "kind_field": "kind", "allowed_kinds": ["flat_recipe"]},
    )


def campaign_profiles() -> dict[str, Any]:
    return {
        "abc_boolean_mass_recut": {
            "run_profile_id": "abc_boolean_mass_recut",
            "args_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "target_cases",
                    "preset",
                    "shard_count",
                    "shard_index",
                    "jobs",
                    "timeout_seconds",
                    "resume",
                ],
                "properties": {
                    "target_cases": {"type": "integer", "minimum": 1, "maximum": 100000},
                    "preset": {"type": "string", "enum": ["smoke", "standard", "stress"]},
                    "shard_count": {"type": "integer", "minimum": 1, "maximum": 128},
                    "shard_index": {"type": "integer", "minimum": 0, "maximum": 127},
                    "jobs": {"type": "integer", "minimum": 1, "maximum": 64},
                    "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 3600},
                    "resume": {"type": "boolean"},
                },
            },
            "defaults": {"preset": "smoke"},
        }
    }


def campaign_task(repo: Path, *, output: str = "artifacts/model_outputs/campaign.json") -> TaskSpec:
    return TaskSpec(
        task_id="campaign",
        prompt="Return one typed bounded campaign_request.",
        expected_output_path=repo / output,
        output_contract={
            "type": "json_object",
            "kind_field": "kind",
            "allowed_kinds": ["campaign_request"],
        },
        allowed_campaign_profiles=campaign_profiles(),
    )


def test_gateway_refuses_model_output_outside_artifacts(tmp_path: Path) -> None:
    gateway = make_gateway(tmp_path, QueueTransport(provider_response(json.dumps(flat_candidate()))))
    task = flat_task(
        tmp_path,
        output="test_harness/interface_capabilities.json",
    )

    with pytest.raises(GatewayError, match="must stay under repository artifacts"):
        gateway.run_task(task, run_id="unsafe_output", overwrite=True)


def make_gateway(
    repo: Path,
    transport: QueueTransport,
    *,
    retries: int = 0,
    sleeper: Any = lambda _delay: None,
    profile: str = "intranet",
) -> AuthoringGateway:
    config = make_config(retries=retries, profile=profile)
    client = OpenAICompatibleMessageClient(
        config,
        transport=transport,
        sleeper=sleeper,
        random_source=lambda: 0.0,
    )
    return AuthoringGateway(config, repo_root=repo, client=client)


def all_artifact_text(repo: Path) -> str:
    parts: list[str] = []
    for path in (repo / "artifacts").rglob("*"):
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_success_stages_hashes_and_atomically_promotes_without_reasoning(tmp_path: Path) -> None:
    candidate = flat_candidate()
    hidden_reasoning = f"private chain of thought {API_KEY}"
    transport = QueueTransport(
        provider_response(json.dumps(candidate), reasoning_content=hidden_reasoning)
    )
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(flat_task(tmp_path), run_id="success", max_repairs=1)

    assert result.ok and result.attempts == 1
    formal = tmp_path / result.promoted_path
    assert json.loads(formal.read_text()) == candidate
    sidecar = tmp_path / result.provenance_path
    provenance = json.loads(sidecar.read_text())
    assert provenance["source_type"] == "intranet_message_api"
    assert provenance["boundary"] == {
        "applies_patches": False,
        "commits_changes": False,
        "direct_api_calls": True,
        "model_calls": True,
        "production_flow": "message_api_contract_candidate_staging",
        "runs_sdk": False,
        "wired_into_harness": False,
    }
    assert provenance["acceptance"] == {
        "authoring_accepted": False,
        "requires_fixed_gate": True,
        "accepted_by": "",
    }
    attempt = tmp_path / "artifacts/authoring_gateway/success/task_one/attempt_01"
    assert {
        "request_manifest.json",
        "raw_response.json",
        "candidate.json",
        "contract_report.json",
        "provenance.json",
        "hashes.json",
    }.issubset({item.name for item in attempt.iterdir()})
    raw = json.loads((attempt / "raw_response.json").read_text())
    assert raw["reasoning_content_chars"] == len(hidden_reasoning)
    assert hidden_reasoning not in all_artifact_text(tmp_path)
    assert API_KEY not in all_artifact_text(tmp_path)


def test_401_fails_without_retry_or_formal_output_and_redacts_secret(tmp_path: Path) -> None:
    transport = QueueTransport(error_response(401, f"bad credential {API_KEY}"))
    gateway = make_gateway(tmp_path, transport, retries=2)

    result = gateway.run_task(flat_task(tmp_path), run_id="unauthorized")

    assert not result.ok
    assert result.attempts == 1
    assert len(transport.requests) == 1
    assert not (tmp_path / "artifacts/model_outputs/task_one.json").exists()
    assert API_KEY not in result.error
    assert API_KEY not in all_artifact_text(tmp_path)


@pytest.mark.parametrize("status", [429, 500])
def test_retryable_status_retries_then_succeeds(tmp_path: Path, status: int) -> None:
    delays: list[float] = []
    candidate = flat_candidate(f"retry_{status}")
    transport = QueueTransport(
        error_response(status, "retry", headers={"retry-after": "0"}),
        provider_response(json.dumps(candidate)),
    )
    gateway = make_gateway(tmp_path, transport, retries=1, sleeper=delays.append)

    result = gateway.run_task(flat_task(tmp_path), run_id=f"retry_{status}")

    assert result.ok and len(transport.requests) == 2
    assert delays == [0.0]


def test_timeout_retries_are_bounded_and_fail_nonzero_result(tmp_path: Path) -> None:
    transport = QueueTransport(TransportError("timeout"), TransportError("timeout"))
    gateway = make_gateway(tmp_path, transport, retries=1)

    result = gateway.run_task(flat_task(tmp_path), run_id="timeout")

    assert not result.ok
    assert result.attempts == 1
    assert len(transport.requests) == 2
    assert "transport failed" in result.error


def test_invalid_message_json_gets_one_diagnostic_repair_attempt(tmp_path: Path) -> None:
    candidate = flat_candidate("json_repaired")
    transport = QueueTransport(
        provider_response("```json\n{not-valid}\n```"),
        provider_response(json.dumps(candidate)),
    )
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(flat_task(tmp_path), run_id="json_repair", max_repairs=1)

    assert result.ok and result.attempts == 2
    second_request = json.loads(transport.requests[1]["body"])
    repair_prompt = second_request["messages"][1]["content"]
    assert "MESSAGE_API_OUTPUT_INVALID" in repair_prompt
    assert "choices[0].message.content" in repair_prompt
    assert (tmp_path / "artifacts/authoring_gateway/json_repair/task_one/attempt_02/candidate.json").is_file()


def test_contract_error_gets_one_repair_attempt(tmp_path: Path) -> None:
    wrong = {"kind": "flat_recipe", "recipe": {"case_id": "missing_api"}}
    repaired = flat_candidate("contract_repaired")
    transport = QueueTransport(
        provider_response(json.dumps(wrong)),
        provider_response(json.dumps(repaired)),
    )
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(flat_task(tmp_path), run_id="contract_repair", max_repairs=1)

    assert result.ok and result.attempts == 2
    second_request = json.loads(transport.requests[1]["body"])
    assert "FLAT_RECIPE_API_MISSING" in second_request["messages"][1]["content"]


def test_typed_campaign_request_is_validated_and_promoted(tmp_path: Path) -> None:
    candidate = {
        "kind": "campaign_request",
        "profile_id": "abc_boolean_mass_recut",
        "args": {
            "target_cases": 1000,
            "preset": "standard",
            "shard_count": 4,
            "shard_index": 1,
            "jobs": 8,
            "timeout_seconds": 120.0,
            "resume": True,
        },
        "notes": [],
        "expected_artifacts": ["campaign_summary"],
    }
    transport = QueueTransport(provider_response(json.dumps(candidate)))
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(campaign_task(tmp_path), run_id="campaign_ok")

    assert result.ok
    assert json.loads((tmp_path / result.promoted_path).read_text()) == candidate


def test_campaign_request_rejects_commands_unknown_args_and_out_of_range(tmp_path: Path) -> None:
    candidate = {
        "kind": "campaign_request",
        "profile_id": "abc_boolean_mass_recut",
        "command": "python arbitrary.py",
        "args": {
            "target_cases": 1000000,
            "preset": "unbounded",
            "shard_count": 2,
            "shard_index": 2,
            "jobs": 8,
            "timeout_seconds": 120,
            "resume": False,
            "dataset": "arbitrary",
        },
        "notes": [],
        "expected_artifacts": [],
    }
    transport = QueueTransport(provider_response(json.dumps(candidate)))
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(campaign_task(tmp_path), run_id="campaign_bad", max_repairs=0)

    assert not result.ok
    codes = {item["error_code"] for item in result.diagnostics}
    assert {
        "FREEFORM_COMMAND_FIELD_FORBIDDEN",
        "CAMPAIGN_REQUEST_FIELDS_UNKNOWN",
        "CAMPAIGN_ARGS_EXECUTION_FIELD_FORBIDDEN",
        "CAMPAIGN_ARGS_UNKNOWN",
        "CAMPAIGN_ARG_ABOVE_MAXIMUM",
        "CAMPAIGN_ARG_ENUM_INVALID",
        "CAMPAIGN_SHARD_RANGE_INVALID",
    }.issubset(codes)


def test_truncated_completion_is_never_promoted_and_can_be_repaired(tmp_path: Path) -> None:
    candidate = flat_candidate("truncation_repaired")
    transport = QueueTransport(
        provider_response('{"kind":"flat_recipe"', finish_reason="length"),
        provider_response(json.dumps(candidate)),
    )
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(flat_task(tmp_path), run_id="truncated", max_repairs=1)

    assert result.ok and result.attempts == 2
    first_report = json.loads(
        (tmp_path / "artifacts/authoring_gateway/truncated/task_one/attempt_01/contract_report.json").read_text()
    )
    assert not first_report["ok"]


def test_json_schema_rejection_downgrades_to_json_object_in_same_attempt(tmp_path: Path) -> None:
    candidate = flat_candidate("schema_downgrade")
    transport = QueueTransport(
        error_response(400, "response_format json_schema is not supported"),
        provider_response(json.dumps(candidate)),
    )
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(flat_task(tmp_path), run_id="schema_downgrade")

    assert result.ok and result.attempts == 1
    first = json.loads(transport.requests[0]["body"])
    second = json.loads(transport.requests[1]["body"])
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}


def test_reasoning_content_is_never_used_as_candidate(tmp_path: Path) -> None:
    transport = QueueTransport(
        provider_response("", reasoning_content=json.dumps(flat_candidate("reasoning_only")))
    )
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(flat_task(tmp_path), run_id="reasoning_only", max_repairs=0)

    assert not result.ok
    assert not (tmp_path / "artifacts/model_outputs/task_one.json").exists()
    assert "message.content is empty" in result.error


def test_empty_message_content_can_use_bounded_repair(tmp_path: Path) -> None:
    candidate = flat_candidate("empty_repaired")
    transport = QueueTransport(
        provider_response(""),
        provider_response(json.dumps(candidate)),
    )
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(flat_task(tmp_path), run_id="empty_repair", max_repairs=1)

    assert result.ok and result.attempts == 2
    second_request = json.loads(transport.requests[1]["body"])
    assert "MESSAGE_API_OUTPUT_INVALID" in second_request["messages"][1]["content"]


def test_invalid_utf8_provider_envelope_is_rejected(tmp_path: Path) -> None:
    body = b'{"choices":[{"message":{"content":"bad\xff"}}]}'
    gateway = make_gateway(tmp_path, QueueTransport(raw_provider_response(body)))

    result = gateway.run_task(flat_task(tmp_path), run_id="invalid_utf8", max_repairs=0)

    assert not result.ok
    assert "no choices[0]" in result.error
    assert not (tmp_path / "artifacts/model_outputs/task_one.json").exists()


@pytest.mark.parametrize(
    "content",
    [
        '[{"kind":"flat_recipe"}]',
        '{"kind":"flat_recipe","kind":"attack_dsl"}',
        '{"kind":"flat_recipe","value":NaN}',
    ],
)
def test_message_content_requires_strict_json_object(tmp_path: Path, content: str) -> None:
    transport = QueueTransport(provider_response(content))
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(flat_task(tmp_path), run_id="strict_json", max_repairs=0)

    assert not result.ok
    assert "not exact JSON" in result.error
    assert not (tmp_path / "artifacts/model_outputs/task_one.json").exists()


def test_manifest_batch_runs_each_task_and_writes_summary(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "artifacts/model_prompt_pack/prompts"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "one.md").write_text("first prompt", encoding="utf-8")
    (prompt_dir / "two.md").write_text("second prompt", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "tasks": [
            {
                "task_type": "interface_form",
                "task_id": "one",
                "prompt_path": "artifacts/model_prompt_pack/prompts/one.md",
                "expected_output_path": "artifacts/model_outputs/one.json",
                "output_contract": {"type": "json_object", "allowed_kinds": ["flat_recipe"]},
            },
            {
                "task_type": "interface_form",
                "task_id": "two",
                "prompt_path": "artifacts/model_prompt_pack/prompts/two.md",
                "expected_output_path": "artifacts/model_outputs/two.json",
                "output_contract": {"type": "json_object", "allowed_kinds": ["flat_recipe"]},
            },
        ],
    }
    manifest_path = tmp_path / "artifacts/model_prompt_pack/model_task_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    transport = QueueTransport(
        provider_response(json.dumps(flat_candidate("one"))),
        provider_response(json.dumps(flat_candidate("two"))),
    )
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_manifest(manifest_path, run_id="batch")

    assert result.ok and len(result.results) == 2
    assert (tmp_path / "artifacts/model_outputs/one.json").is_file()
    assert (tmp_path / "artifacts/model_outputs/two.json").is_file()
    summary = json.loads((tmp_path / "artifacts/authoring_gateway/batch/run_summary.json").read_text())
    assert summary["passed"] == 2 and summary["failed"] == 0


def test_orphan_or_mismatched_formal_pair_never_reports_successful_skip(tmp_path: Path) -> None:
    task = flat_task(tmp_path)
    output = task.expected_output_path
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps(flat_candidate("orphan")), encoding="utf-8")
    gateway = make_gateway(tmp_path, QueueTransport())

    orphan = gateway.run_task(task, run_id="orphan")

    assert not orphan.ok and not orphan.skipped
    assert "pair is incomplete" in orphan.error
    provenance = output.with_name(f"{output.stem}.provenance.json")
    provenance.write_text(
        json.dumps({"candidate_sha256": "wrong", "output_path": output.relative_to(tmp_path).as_posix()}),
        encoding="utf-8",
    )

    mismatch = gateway.run_task(task, run_id="mismatch")

    assert not mismatch.ok and not mismatch.skipped
    assert "candidate_sha256" in mismatch.error


def test_provider_usage_and_object_keys_cannot_persist_api_key(tmp_path: Path) -> None:
    candidate = flat_candidate("secret_usage")
    response = json.loads(provider_response(json.dumps(candidate)).body)
    response[API_KEY] = "top-level provider extension"
    response["usage"]["provider_echo"] = API_KEY
    transport = QueueTransport(raw_provider_response(json.dumps(response).encode()))
    gateway = make_gateway(tmp_path, transport)

    result = gateway.run_task(flat_task(tmp_path), run_id="secret_usage")

    assert result.ok
    assert API_KEY not in all_artifact_text(tmp_path)


def test_secret_with_json_escape_characters_is_never_staged_as_candidate(tmp_path: Path) -> None:
    secret = 'quote"inside'
    config = make_config()
    config = GatewayConfig(
        profile=config.profile,
        base_url=config.base_url,
        model=config.model,
        api_key=secret,
        request_timeout_seconds=config.request_timeout_seconds,
        max_retries=0,
    )
    candidate = flat_candidate("escaped_secret")
    candidate["notes"] = [secret]
    transport = QueueTransport(provider_response(json.dumps(candidate)))
    client = OpenAICompatibleMessageClient(config, transport=transport)
    gateway = AuthoringGateway(config, repo_root=tmp_path, client=client)

    result = gateway.run_task(flat_task(tmp_path), run_id="escaped_secret", max_repairs=0)

    assert not result.ok
    attempt = tmp_path / "artifacts/authoring_gateway/escaped_secret/task_one/attempt_01"
    assert not (attempt / "candidate.json").exists()
    assert secret not in all_artifact_text(tmp_path)


def test_intranet_profile_never_falls_back_to_siliconflow_configuration() -> None:
    siliconflow_only = {
        "SILICONFLOW_BASE_URL": "https://api.siliconflow.example/v1",
        "SILICONFLOW_MODEL": "Qwen3.6-35B-A3B",
        "SILICONFLOW_API_KEY": API_KEY,
    }
    with pytest.raises(ConfigError, match="SGGK_QWEN_BASE_URL"):
        load_gateway_config("intranet", environ=siliconflow_only)
    explicit = load_gateway_config("siliconflow-test", environ=siliconflow_only)
    assert explicit.profile.provenance_source_type == "siliconflow_test_message_api"
    assert explicit.model == "Qwen3.6-35B-A3B"


def test_siliconflow_profile_requires_https() -> None:
    with pytest.raises(ConfigError, match="must use https"):
        load_gateway_config(
            "siliconflow-test",
            environ={
                "SILICONFLOW_BASE_URL": "http://api.siliconflow.example/v1",
                "SILICONFLOW_MODEL": "Qwen3.6-35B-A3B",
                "SILICONFLOW_API_KEY": API_KEY,
            },
        )


def test_gateway_source_has_no_process_sdk_patch_or_git_imports() -> None:
    package_root = Path(__file__).resolve().parents[1] / "authoring_gateway"
    forbidden = {"subprocess", "sggk", "git", "pygit2", "runner"}
    for path in package_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        unsafe = (imported & forbidden) | {name for name in imported if "patch" in name.lower()}
        assert not unsafe, f"{path} imports an execution surface: {unsafe}"
