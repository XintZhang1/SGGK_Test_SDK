"""Production Message API and SDK execution runtime for review sessions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from test_harness.authoring_gateway.client import CompletionOptions, OpenAICompatibleMessageClient
from test_harness.authoring_gateway.config import GatewayConfig, load_gateway_config
from test_harness.authoring_gateway.review_comment import (
    ReviewCommentContext,
    build_review_comment_task,
    finalize_review_comment_response,
    normalize_review_comment_candidate,
    validate_review_comment_response,
)
from test_harness.tools.run_message_harness_pipeline import MessageHarnessPipeline

from .workflow import WorkflowError, _safe_id, _write_json


class MessageApiRuntime:
    """Use one configured OpenAI-compatible Message API for every model step."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        profile: str,
        config: GatewayConfig | None = None,
        client: OpenAICompatibleMessageClient | None = None,
        candidate_count: int = 3,
        candidate_parallelism: int = 3,
        max_tokens: int = 32_768,
        thinking_mode: str | None = None,
        jobs: int = 1,
        execution_timeout_seconds: float = 180.0,
        campaign_dataset: str | Path = "",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        requested_profile = profile.strip()
        self.config = config or load_gateway_config(requested_profile)
        if self.config.profile.name != requested_profile:
            raise WorkflowError(
                "runtime profile does not match the supplied GatewayConfig provider profile"
            )
        self.provider_profile = self.config.profile.name
        self.provider_profile_category = self.config.profile.category
        self.client = client or OpenAICompatibleMessageClient(self.config)
        if not 1 <= candidate_count <= 8:
            raise WorkflowError("candidate_count must be between 1 and 8")
        if not 1 <= candidate_parallelism <= 8:
            raise WorkflowError("candidate_parallelism must be between 1 and 8")
        if max_tokens <= 0 or jobs <= 0 or execution_timeout_seconds <= 0:
            raise WorkflowError("runtime token, job, and timeout budgets must be positive")
        self.candidate_count = candidate_count
        self.candidate_parallelism = min(candidate_parallelism, candidate_count)
        self.max_tokens = max_tokens
        self.thinking_mode = thinking_mode or self.config.profile.default_thinking_mode
        if self.thinking_mode not in {"omit", "enabled", "disabled"}:
            raise WorkflowError("thinking_mode must be omit, enabled, or disabled")
        self.jobs = jobs
        self.execution_timeout_seconds = execution_timeout_seconds
        self.campaign_dataset = campaign_dataset

    def _pipeline(self, staging_root: Path) -> MessageHarnessPipeline:
        return MessageHarnessPipeline(
            self.config,
            repo_root=self.repo_root,
            staging_root=staging_root,
            client=self.client,
        )

    def _authoring_options(self) -> CompletionOptions:
        return CompletionOptions(
            response_mode="auto",
            temperature=0.2,
            max_tokens=self.max_tokens,
            thinking_mode=self.thinking_mode,
            stream=self.config.profile.default_stream,
        )

    def generate(
        self,
        *,
        manifest_path: Path,
        run_id: str,
        staging_root: Path,
    ) -> Mapping[str, Any]:
        result = self._pipeline(staging_root).run_manifest(
            manifest_path,
            run_id=run_id,
            completion_options=self._authoring_options(),
            max_contract_repairs=1,
            max_gate_repairs=2,
            candidate_count=self.candidate_count,
            candidate_parallelism=self.candidate_parallelism,
            selection_goal="fixed_gate_only",
            overwrite=False,
            continue_on_error=False,
            execute=False,
        )
        payload = result.as_dict()
        if not payload.get("ok"):
            task_errors = [
                str(item.get("error") or "").strip()
                for item in payload.get("results", [])
                if isinstance(item, Mapping) and str(item.get("error") or "").strip()
            ]
            batch_errors = [
                str(item).strip()
                for item in payload.get("errors", [])
                if str(item).strip()
            ]
            payload["error"] = (task_errors + batch_errors + ["generation failed"])[0]
        return payload

    def interpret_comment(
        self,
        *,
        comment: str,
        session: Mapping[str, Any],
        round_record: Mapping[str, Any],
        subject_outline: Mapping[str, Any],
        output_dir: Path,
    ) -> Mapping[str, Any]:
        session_profile = str(session.get("provider_profile") or session.get("profile") or "")
        session_category = str(session.get("provider_profile_category") or "")
        if session_profile != self.provider_profile:
            raise WorkflowError(
                "active review session belongs to a different Message API provider profile"
            )
        if session_category and session_category != self.provider_profile_category:
            raise WorkflowError(
                "active review session belongs to a different Message API profile category"
            )
        classification = str(
            round_record.get("data_classification")
            or session.get("data_classification")
            or ""
        )
        allowed_categories = round_record.get("allowed_profile_categories")
        if classification == "proprietary_source":
            if allowed_categories != ["intranet"]:
                raise WorkflowError(
                    "proprietary review context has no valid intranet-only profile policy"
                )
            if self.provider_profile_category != "intranet":
                raise WorkflowError(
                    "proprietary review context cannot be sent to an external Message API profile"
                )
        elif (
            isinstance(allowed_categories, list)
            and allowed_categories
            and self.provider_profile_category not in allowed_categories
        ):
            raise WorkflowError(
                "review context is not allowed for the configured Message API profile category"
            )
        context = ReviewCommentContext(
            task_id=str(round_record["task_id"]),
            run_id=str(round_record["run_id"]),
            round_number=int(round_record["round_number"]),
            subject_sha256=str(round_record["subject_digest_sha256"]),
            subject_outline=subject_outline,
            target=_safe_id(str(session.get("public_function") or "target")),
            current_status="awaiting_natural_language_comment",
        )
        task = build_review_comment_task(comment, context)
        task_record = task.as_dict()
        _write_json(output_dir / "message_task.json", task_record)
        response_schema = task_record["message_api"]["response_format"]["json_schema"]["schema"]
        prompt = task.user_prompt
        last_error = ""
        for attempt in range(1, 3):
            completion = self.client.create_completion(
                system_prompt=task.system_prompt,
                user_prompt=prompt,
                options=CompletionOptions(
                    response_mode="auto",
                    response_schema=response_schema,
                    schema_name="sggk_review_comment_response",
                    temperature=0.0,
                    max_tokens=4096,
                    thinking_mode=self.thinking_mode,
                    stream=self.config.profile.default_stream,
                ),
            )
            metadata = {
                "ok": completion.ok,
                "candidate_source": completion.candidate_source,
                "reasoning_content_sha256": completion.reasoning_content_sha256,
                "reasoning_content_chars": completion.reasoning_content_chars,
                "final_mode": completion.final_mode,
                "finish_reason": completion.finish_reason,
                "usage": completion.usage,
                "error": completion.error,
                "error_kind": completion.error_kind,
                "events": completion.events,
                "provider_responses": completion.response_records,
            }
            _write_json(output_dir / f"message_attempt_{attempt:02d}.json", metadata)
            if completion.candidate is not None:
                normalized_candidate, normalization_notes = normalize_review_comment_candidate(
                    completion.candidate
                )
                if normalization_notes:
                    _write_json(
                        output_dir / f"normalization_attempt_{attempt:02d}.json",
                        {
                            "attempt": attempt,
                            "notes": list(normalization_notes),
                        },
                    )
                validation = validate_review_comment_response(normalized_candidate, task)
                _write_json(
                    output_dir / f"validation_attempt_{attempt:02d}.json",
                    validation.as_dict(),
                )
                if validation.ok:
                    finalized = finalize_review_comment_response(normalized_candidate, task)
                    finalized["provider"] = self.config.public_metadata()
                    finalized["message_attempts"] = attempt
                    _write_json(output_dir / "comment_decision.json", finalized)
                    return finalized
                last_error = "; ".join(
                    f"{item.path}: {item.message}" for item in validation.diagnostics[:8]
                )
            else:
                last_error = completion.error or "Message API returned no JSON decision"
                last_status = (
                    completion.response_records[-1].get("status")
                    if completion.response_records
                    else None
                )
                repairable_completion = (
                    isinstance(last_status, int)
                    and 200 <= last_status < 300
                    and "refusal" not in completion.error
                    and completion.finish_reason != "content_filter"
                    and completion.error_kind
                    not in {
                        "provider_error",
                        "stream_candidate_too_large",
                        "stream_event_too_large",
                        "stream_refusal_too_large",
                        "stream_wire_too_large",
                    }
                )
                if not repairable_completion:
                    raise WorkflowError(
                        f"model review comment interpretation failed: {last_error}"
                    )
            prompt = (
                task.user_prompt
                + "\n\nThe prior response failed deterministic validation. Return one complete "
                "schema-valid JSON object only. Diagnostics:\n"
                + last_error[:4000]
            )
        raise WorkflowError(f"model review comment interpretation failed: {last_error}")

    def execute(
        self,
        *,
        manifest_path: Path,
        run_id: str,
        staging_root: Path,
        runner_path: Path | None,
    ) -> Mapping[str, Any]:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        tasks = manifest.get("tasks") if isinstance(manifest, dict) else None
        task = tasks[0] if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) else {}
        output_raw = task.get("expected_output_path")
        output_path = (
            (self.repo_root / str(output_raw)).resolve()
            if isinstance(output_raw, str) and output_raw
            else Path()
        )
        candidate: dict[str, Any] = {}
        if output_path.is_file():
            loaded = json.loads(output_path.read_text(encoding="utf-8-sig"))
            candidate = loaded if isinstance(loaded, dict) else {}
        selection_goal = (
            "adapter_build_pass"
            if candidate.get("kind") == "api_plugin_candidate"
            else "must_pass_execution"
        )
        result = self._pipeline(staging_root).run_manifest(
            manifest_path,
            run_id=run_id,
            completion_options=self._authoring_options(),
            max_contract_repairs=0,
            max_gate_repairs=0,
            candidate_count=1,
            candidate_parallelism=1,
            selection_goal=selection_goal,
            overwrite=False,
            continue_on_error=False,
            execute=True,
            runner=str(runner_path or ""),
            jobs=self.jobs,
            timeout_seconds=self.execution_timeout_seconds,
            campaign_dataset=self.campaign_dataset,
        )
        return result.as_dict()


__all__ = ["MessageApiRuntime"]
