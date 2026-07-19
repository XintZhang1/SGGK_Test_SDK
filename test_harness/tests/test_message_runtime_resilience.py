from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from test_harness.authoring_gateway.client import CompletionResult
from test_harness.authoring_gateway.config import GatewayConfig, PROFILE_SPECS
from test_harness.orchestration.runtime import MessageApiRuntime
from test_harness.orchestration.workflow import WorkflowError


def _config() -> GatewayConfig:
    return GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="zai-org/GLM-5.2",
        api_key="test-key",
        max_retries=2,
    )


class _BatchResult:
    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "errors": [],
            "results": [
                {
                    "ok": False,
                    "error": (
                        "no independent candidate satisfied selection_goal=fixed_gate_only: "
                        "Message API timed out after 300 seconds"
                    ),
                }
            ],
        }


class _FailingPipeline:
    def run_manifest(self, *_args: Any, **_kwargs: Any) -> _BatchResult:
        return _BatchResult()


def test_generation_promotes_nested_task_error_to_top_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MessageApiRuntime(
        repo_root=tmp_path,
        profile="intranet",
        config=_config(),
        candidate_count=1,
        candidate_parallelism=1,
    )
    monkeypatch.setattr(runtime, "_pipeline", lambda _staging_root: _FailingPipeline())

    result = runtime.generate(
        manifest_path=tmp_path / "manifest.json",
        run_id="timeout",
        staging_root=tmp_path / "staging",
    )

    assert "timed out after 300 seconds" in result["error"]
    assert result["error"] != "generation failed"


class _TimeoutClient:
    def __init__(self) -> None:
        self.calls = 0

    def create_completion(self, **_kwargs: Any) -> CompletionResult:
        self.calls += 1
        return CompletionResult(
            ok=False,
            error="Message API timed out after 300 seconds; the timeout was not retried",
            error_kind="transport_timeout",
        )


def test_review_comment_timeout_does_not_trigger_validation_repair(
    tmp_path: Path,
) -> None:
    client = _TimeoutClient()
    runtime = MessageApiRuntime(
        repo_root=tmp_path,
        profile="intranet",
        config=_config(),
        client=client,
        candidate_count=1,
        candidate_parallelism=1,
    )
    output_dir = tmp_path / "comment"

    with pytest.raises(WorkflowError, match="timed out after 300 seconds"):
        runtime.interpret_comment(
            comment="请解释当前候选。",
            session={
                "provider_profile": "intranet",
                "provider_profile_category": "intranet",
                "public_function": "api_boolean",
                "data_classification": "public_interface",
            },
            round_record={
                "task_id": "review_task",
                "run_id": "review_run",
                "round_number": 1,
                "subject_digest_sha256": "a" * 64,
                "data_classification": "public_interface",
                "allowed_profile_categories": ["intranet"],
            },
            subject_outline={"candidate": {"kind": "attack_dsl"}},
            output_dir=output_dir,
        )

    assert client.calls == 1
    assert (output_dir / "message_attempt_01.json").is_file()
    assert not (output_dir / "message_attempt_02.json").exists()
