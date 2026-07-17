"""SDK-free candidate staging for configured Message API authoring tasks.

The gateway has one privilege: call an explicitly configured OpenAI-compatible
chat-completions endpoint and atomically stage JSON that passes the transport
output contract. It deliberately has no runner, fixed harness gate, patch,
shell, or Git hooks; only the message harness pipeline can accept a candidate.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import CompletionOptions, OpenAICompatibleMessageClient, canonical_json_bytes
from .config import GatewayConfig
from .contracts import ContractDiagnostic, ContractReport, response_schema_for_contract, validate_candidate

DEFAULT_SYSTEM_PROMPT = """You author bounded SGGK harness test descriptions.
Return exactly one JSON object in choices[0].message.content.
Do not use markdown fences or surrounding prose.
Never return credentials, provider configuration, shell commands, patches, or Git operations.
Follow the task's output_contract exactly; deterministic local code validates the result.
"""

SOURCE_TASK_TYPES = frozenset({"source_attack", "sggk_source_attack"})


class GatewayError(ValueError):
    """A task or manifest violates the gateway's fixed boundary."""


def is_source_task_type(task_type: str) -> bool:
    """Return whether a task type carries proprietary source evidence."""

    return str(task_type).strip() in SOURCE_TASK_TYPES


def _safe_id(value: str) -> str:
    result = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in value)
    return result.strip("._-") or "task"


def _storage_id(value: str, namespace: str) -> str:
    """Map long logical IDs to stable, bounded artifact path components.

    Short IDs (<32 chars) pass through unchanged so human-readable run/task
    names stay legible; longer ones collapse to ``<namespace>_<24hex>`` to keep
    directory paths well under Windows MAX_PATH. Mirrors the staging helper in
    ``run_message_harness_pipeline`` so gateway depths match the pipeline's.
    """

    safe = _safe_id(value)
    if len(safe) < 32:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{namespace}_{digest}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    if pretty:
        return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    return canonical_json_bytes(value)


def _redact(value: Any, secret_values: Iterable[str]) -> Any:
    secrets = tuple(item for item in secret_values if item)
    if isinstance(value, str):
        result = value
        for secret in secrets:
            result = result.replace(secret, "<redacted-secret>")
        return result
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            str(_redact(str(key), secrets)): _redact(item, secrets)
            for key, item in value.items()
        }
    return value


def _inside(root: Path, value: str | Path, *, label: str) -> Path:
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise GatewayError(f"{label} must stay inside repository root: {value}") from exc
    return resolved


def _repo_relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            # Minimal prefix/suffix to stay well under Windows MAX_PATH (260):
            # the staged dir is already deep, so embedding the full target name
            # (e.g. ".raw_response.json.<rand>.tmp") risks overflow. The parent
            # dir scopes the file; os.replace atomically promotes it.
            prefix=".~",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, _json_bytes(value))


def _file_hash(path: Path) -> str:
    return _sha256_bytes(path.read_bytes()) if path.is_file() else ""


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    prompt: str
    expected_output_path: Path
    output_contract: dict[str, Any]
    task_type: str = "single_task"
    prompt_path: str = ""
    manifest_path: str = ""
    allowed_campaign_profiles: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class GatewayRunResult:
    ok: bool
    task_id: str
    run_id: str
    attempts: int = 0
    promoted_path: str = ""
    provenance_path: str = ""
    staging_path: str = ""
    skipped: bool = False
    error: str = ""
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "attempts": self.attempts,
            "promoted_path": self.promoted_path,
            "provenance_path": self.provenance_path,
            "staging_path": self.staging_path,
            "skipped": self.skipped,
            "error": self.error,
            "diagnostics": self.diagnostics,
        }


@dataclass
class GatewayBatchResult:
    ok: bool
    run_id: str
    manifest_path: str
    staging_path: str
    results: list[GatewayRunResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "manifest_path": self.manifest_path,
            "staging_path": self.staging_path,
            "task_count": len(self.results),
            "passed": sum(item.ok and not item.skipped for item in self.results),
            "skipped": sum(item.skipped for item in self.results),
            "failed": sum(not item.ok for item in self.results),
            "errors": self.errors,
            "results": [item.as_dict() for item in self.results],
        }


class AuthoringGateway:
    """Run fixed JSON authoring tasks through one explicit provider profile."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        repo_root: str | Path,
        staging_root: str | Path = "artifacts/authoring_gateway",
        client: OpenAICompatibleMessageClient | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        clock: Callable[[], datetime] = _utc_now,
        max_prompt_chars: int = 250_000,
        max_repair_context_chars: int = 120_000,
    ) -> None:
        self.config = config
        self.repo_root = Path(repo_root).resolve()
        if not self.repo_root.is_dir():
            raise GatewayError(f"repository root does not exist: {self.repo_root}")
        self.artifacts_root = (self.repo_root / "artifacts").resolve()
        self.staging_root = _inside(self.repo_root, staging_root, label="staging_root")
        try:
            self.staging_root.relative_to(self.artifacts_root)
        except ValueError as exc:
            raise GatewayError("gateway staging_root must stay under repository artifacts/") from exc
        self.client = client or OpenAICompatibleMessageClient(config)
        self.system_prompt = system_prompt
        self.clock = clock
        if max_prompt_chars <= 0 or max_repair_context_chars <= 0:
            raise GatewayError("prompt limits must be positive")
        self.max_prompt_chars = max_prompt_chars
        self.max_repair_context_chars = max_repair_context_chars

    def new_run_id(self) -> str:
        stamp = self.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}_{uuid.uuid4().hex[:8]}"

    def _task_paths(self, task: TaskSpec, run_id: str) -> tuple[Path, Path, Path]:
        output_path = _inside(self.repo_root, task.expected_output_path, label="expected_output_path")
        try:
            output_path.relative_to(self.artifacts_root)
        except ValueError as exc:
            raise GatewayError("expected_output_path must stay under repository artifacts/") from exc
        if output_path.suffix.lower() != ".json" or output_path.name.endswith(".provenance.json"):
            raise GatewayError("expected_output_path must be a formal .json output, not a provenance sidecar")
        task_root = (
            self.staging_root
            / _storage_id(run_id, "run")
            / _storage_id(task.task_id, "task")
        )
        provenance_path = output_path.with_name(f"{output_path.stem}.provenance.json")
        return task_root, output_path, provenance_path

    @staticmethod
    def _completion_diagnostics(error: str, error_kind: str = "") -> list[ContractDiagnostic]:
        error_codes = {
            "transport_timeout": "MESSAGE_API_TIMEOUT",
            "transport_error": "MESSAGE_API_TRANSPORT_ERROR",
            "http_error": "MESSAGE_API_HTTP_ERROR",
            "provider_error": "MESSAGE_API_PROVIDER_ERROR",
            "stream_candidate_too_large": "MESSAGE_API_OUTPUT_TOO_LARGE",
            "stream_event_too_large": "MESSAGE_API_STREAM_EVENT_TOO_LARGE",
            "stream_incomplete": "MESSAGE_API_STREAM_INCOMPLETE",
            "stream_invalid_event": "MESSAGE_API_STREAM_INVALID",
            "stream_invalid_utf8": "MESSAGE_API_STREAM_INVALID",
            "stream_refusal_too_large": "MESSAGE_API_STREAM_REFUSAL_TOO_LARGE",
            "stream_wire_too_large": "MESSAGE_API_STREAM_TOO_LARGE",
        }
        return [
            ContractDiagnostic(
                "error",
                error_codes.get(error_kind, "MESSAGE_API_OUTPUT_INVALID"),
                "$.choices[0].message.content",
                error or "Message API completion did not yield an exact JSON object.",
                "Return exactly one JSON object in choices[0].message.content.",
            )
        ]

    def _repair_prompt(
        self,
        task: TaskSpec,
        prior_content: str,
        diagnostics: Sequence[Mapping[str, Any]],
        repair_iteration: int,
    ) -> str:
        prior = _redact(prior_content, self.config.secrets)
        if len(prior) > 60_000:
            prior = prior[:60_000] + "\n<previous-output-truncated>"
        diagnostic_text = json.dumps(
            _redact(list(diagnostics), self.config.secrets), indent=2, ensure_ascii=False
        )
        text = f"""{task.prompt}

## Deterministic repair request {repair_iteration}

The previous choices[0].message.content failed the fixed JSON/contract gate.
Return a corrected complete JSON object only. Do not explain the repair.

Previous message.content:
<previous-output>
{prior}
</previous-output>

Fixed diagnostics:
{diagnostic_text}
"""
        if len(text) > self.max_repair_context_chars:
            fixed_tail = text[-min(len(text), 70_000) :]
            prompt_budget = max(0, self.max_repair_context_chars - len(fixed_tail) - 80)
            text = task.prompt[:prompt_budget] + "\n<task-context-truncated-for-repair>\n" + fixed_tail
        return text

    def _request_manifest(
        self,
        task: TaskSpec,
        run_id: str,
        attempt: int,
        prompt: str,
        options: CompletionOptions,
        repair_parent: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "attempt": attempt,
            "repair": {
                "is_repair": attempt > 1,
                "iteration": attempt - 1,
                "parent_attempt": repair_parent,
            },
            "provider": self.config.public_metadata(),
            "message_contract": {
                "interface": "openai_compatible_chat_completions",
                "candidate_location": "choices[0].message.content",
                "candidate_encoding": "exact_json_object",
                "reasoning_content_policy": "hash_and_length_only_never_candidate",
            },
            "prompt": {
                "prompt_path": task.prompt_path,
                "manifest_path": task.manifest_path,
                "system_chars": len(self.system_prompt),
                "system_sha256": _sha256_text(self.system_prompt),
                "user_chars": len(prompt),
                "user_sha256": _sha256_text(prompt),
            },
            "output_contract": task.output_contract,
            "allowed_campaign_profiles": task.allowed_campaign_profiles,
            "response_options": {
                "response_mode": options.response_mode,
                "schema_name": options.schema_name,
                "response_schema": options.response_schema,
                "temperature": options.temperature,
                "max_tokens": options.max_tokens,
                "thinking_mode": options.thinking_mode,
                "stream": options.stream,
                "seed": options.seed,
            },
            "boundary": {
                "runs_sdk": False,
                "executes_commands": False,
                "applies_patches": False,
                "commits_changes": False,
            },
        }

    def _attempt_provenance(
        self,
        task: TaskSpec,
        run_id: str,
        attempt: int,
        prompt: str,
        completion: Any,
        report: ContractReport,
        output_path: Path,
        formal_provenance_path: Path,
        started_at: datetime,
        elapsed_seconds: float,
        promotion_complete: bool,
    ) -> dict[str, Any]:
        candidate_hash = (
            _sha256_bytes(canonical_json_bytes(completion.candidate))
            if isinstance(completion.candidate, dict)
            else ""
        )
        return {
            "schema_version": 1,
            "run_id": run_id,
            "task_id": task.task_id,
            "attempt": attempt,
            "started_at": _iso(started_at),
            "elapsed_seconds": round(elapsed_seconds, 6),
            "profile": self.config.profile.name,
            "source_type": self.config.profile.provenance_source_type,
            "model": self.config.model,
            "prompt_sha256": _sha256_text(prompt),
            "message_content_sha256": _sha256_text(completion.content) if completion.content else "",
            "message_content_chars": len(completion.content),
            "reasoning_content_sha256": completion.reasoning_content_sha256,
            "reasoning_content_chars": completion.reasoning_content_chars,
            "candidate_sha256": candidate_hash,
            "finish_reason": completion.finish_reason,
            "error_kind": completion.error_kind,
            "response_mode": completion.final_mode,
            "usage": completion.usage,
            "contract": report.as_dict(),
            "promotion": {
                "eligible": report.ok,
                "completed": promotion_complete,
                "output_path": _repo_relative(self.repo_root, output_path),
                "provenance_path": _repo_relative(self.repo_root, formal_provenance_path),
            },
            "boundary": {
                "model_calls": True,
                "direct_api_calls": True,
                "runs_sdk": False,
                "executes_commands": False,
                "applies_patches": False,
                "commits_changes": False,
                "wired_into_harness": False,
            },
        }

    def _formal_provenance(
        self,
        task: TaskSpec,
        run_id: str,
        attempt: int,
        prompt: str,
        completion: Any,
        output_path: Path,
        candidate_hash: str,
    ) -> dict[str, Any]:
        return _redact({
            "schema_version": 2,
            "request_id": task.task_id,
            "source_type": self.config.profile.provenance_source_type,
            "source_label": self.config.profile.name,
            "source_path": task.prompt_path or task.manifest_path,
            "description": (
                "Message API candidate passed the transport/output contract "
                "and still requires fixed harness gates."
            ),
            "output_path": _repo_relative(self.repo_root, output_path),
            "saved_at": _iso(self.clock()),
            "model": self.config.model,
            "interface": "openai_compatible_chat_completions_message_content_json",
            "profile": self.config.profile.name,
            "run_id": run_id,
            "attempt": attempt,
            "prompt_sha256": _sha256_text(prompt),
            "task_prompt_sha256": _sha256_text(task.prompt),
            "candidate_sha256": candidate_hash,
            "message_content_sha256": _sha256_text(completion.content),
            "reasoning_content_sha256": completion.reasoning_content_sha256,
            "reasoning_content_chars": completion.reasoning_content_chars,
            "usage": completion.usage,
            "repair": {
                "is_repair_output": attempt > 1,
                "iteration": max(0, attempt - 1),
                "parent_attempt": attempt - 1 if attempt > 1 else 0,
            },
            "acceptance": {
                "authoring_accepted": False,
                "requires_fixed_gate": True,
                "accepted_by": "",
            },
            "boundary": {
                "model_calls": True,
                "direct_api_calls": True,
                "runs_sdk": False,
                "applies_patches": False,
                "commits_changes": False,
                "wired_into_harness": False,
                "production_flow": "message_api_contract_candidate_staging",
            },
        }, self.config.secrets)

    def _verify_existing_pair(
        self,
        task: TaskSpec,
        output_path: Path,
        provenance_path: Path,
    ) -> tuple[bool, str]:
        if not output_path.exists() and not provenance_path.exists():
            return False, ""
        if not output_path.is_file() or not provenance_path.is_file():
            return False, (
                "formal output/provenance pair is incomplete; use --overwrite to replace it: "
                f"{output_path}"
            )
        try:
            candidate = json.loads(output_path.read_text(encoding="utf-8-sig"))
            provenance = json.loads(provenance_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return False, f"formal output/provenance pair is unreadable: {exc}"
        if not isinstance(candidate, dict) or not isinstance(provenance, dict):
            return False, "formal output and provenance must both be JSON objects"
        report = validate_candidate(
            candidate,
            task.output_contract,
            allowed_campaign_profiles=task.allowed_campaign_profiles,
            secret_values=self.config.secrets,
        )
        if not report.ok:
            codes = [item.error_code for item in report.diagnostics]
            return False, f"existing formal output fails the current contract: {codes}"
        candidate_hash = _sha256_bytes(canonical_json_bytes(candidate))
        if provenance.get("candidate_sha256") != candidate_hash:
            return False, "existing formal output does not match provenance candidate_sha256"
        if provenance.get("task_prompt_sha256") != _sha256_text(task.prompt):
            return False, "existing formal output was authored from a different task prompt"
        expected_relative = _repo_relative(self.repo_root, output_path)
        if provenance.get("output_path") != expected_relative:
            return False, "existing provenance output_path does not match the formal output"
        return True, ""

    def _write_attempt(
        self,
        attempt_root: Path,
        request_manifest: Mapping[str, Any],
        completion: Any,
        report: ContractReport,
        provenance: Mapping[str, Any],
    ) -> None:
        secret_values = self.config.secrets
        raw_response = {
            "ok": completion.ok,
            "error": completion.error,
            "error_kind": completion.error_kind,
            "candidate_source": completion.candidate_source,
            "message_content": completion.content,
            "message_content_sha256": _sha256_text(completion.content) if completion.content else "",
            "reasoning_content_sha256": completion.reasoning_content_sha256,
            "reasoning_content_chars": completion.reasoning_content_chars,
            "finish_reason": completion.finish_reason,
            "final_mode": completion.final_mode,
            "usage": completion.usage,
            "events": completion.events,
            "provider_responses": completion.response_records,
        }
        request_path = attempt_root / "request_manifest.json"
        response_path = attempt_root / "raw_response.json"
        report_path = attempt_root / "contract_report.json"
        provenance_path = attempt_root / "provenance.json"
        _write_json(request_path, _redact(dict(request_manifest), secret_values))
        _write_json(response_path, _redact(raw_response, secret_values))
        _write_json(report_path, _redact(report.as_dict(), secret_values))
        # Never persist a candidate containing a configured credential.
        secret_error = any(item.error_code == "SECRET_VALUE_DETECTED" for item in report.diagnostics)
        candidate_path = attempt_root / "candidate.json"
        if isinstance(completion.candidate, dict) and not secret_error:
            _write_json(candidate_path, completion.candidate)
        _write_json(provenance_path, _redact(dict(provenance), secret_values))
        hashes = {
            "algorithm": "sha256",
            "request_manifest.json": _file_hash(request_path),
            "raw_response.json": _file_hash(response_path),
            "contract_report.json": _file_hash(report_path),
            "candidate.json": _file_hash(candidate_path),
            "provenance.json": _file_hash(provenance_path),
        }
        _write_json(attempt_root / "hashes.json", hashes)

    def _promote(
        self,
        candidate: dict[str, Any],
        formal_provenance: Mapping[str, Any],
        output_path: Path,
        provenance_path: Path,
        *,
        overwrite: bool,
    ) -> None:
        if not overwrite and (output_path.exists() or provenance_path.exists()):
            raise GatewayError(f"formal output already exists: {output_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_temp: Path | None = None
        provenance_temp: Path | None = None
        try:
            for target, payload, label in (
                (output_path, _json_bytes(candidate), "output"),
                (provenance_path, _json_bytes(dict(formal_provenance)), "provenance"),
            ):
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=target.parent,
                    # Minimal prefix/suffix to stay under Windows MAX_PATH (260):
                    # the formal output lives under a deep staging dir, so
                    # embedding ".candidate.json.output.<rand>.tmp" can overflow.
                    # The parent dir scopes the file; os.replace promotes it.
                    prefix=f".~{label}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if target == output_path:
                    output_temp = temporary
                else:
                    provenance_temp = temporary
            # The formal JSON is the commit marker: publish provenance first.
            os.replace(provenance_temp, provenance_path)
            provenance_temp = None
            os.replace(output_temp, output_path)
            output_temp = None
        finally:
            if output_temp is not None:
                output_temp.unlink(missing_ok=True)
            if provenance_temp is not None:
                provenance_temp.unlink(missing_ok=True)

    def run_task(
        self,
        task: TaskSpec,
        *,
        run_id: str | None = None,
        completion_options: CompletionOptions | None = None,
        max_repairs: int = 1,
        overwrite: bool = False,
    ) -> GatewayRunResult:
        if not task.task_id.strip():
            raise GatewayError("task_id must be non-empty")
        classification = str(task.metadata.get("data_classification") or "")
        source_task = is_source_task_type(task.task_type)
        authored_profile = str(task.metadata.get("provider_profile") or "")
        authored_category = str(task.metadata.get("provider_profile_category") or "")
        if authored_profile and authored_profile != self.config.profile.name:
            raise GatewayError(
                "task provider_profile does not match the configured Message API profile"
            )
        if authored_category and authored_category != self.config.profile.category:
            raise GatewayError(
                "task provider_profile_category does not match the configured Message API category"
            )
        if source_task and classification != "proprietary_source":
            raise GatewayError(
                "source authoring tasks must declare data_classification=proprietary_source"
            )
        allowed_categories = task.metadata.get("allowed_profile_categories")
        if source_task and allowed_categories != ["intranet"]:
            raise GatewayError(
                "source authoring tasks must declare allowed_profile_categories=['intranet']"
            )
        if allowed_categories is not None:
            if (
                not isinstance(allowed_categories, list)
                or not allowed_categories
                or not all(isinstance(item, str) and item for item in allowed_categories)
            ):
                raise GatewayError("allowed_profile_categories must be a non-empty string array")
        if classification == "proprietary_source":
            if allowed_categories != ["intranet"]:
                raise GatewayError(
                    "proprietary source evidence must declare allowed_profile_categories=['intranet']"
                )
            if self.config.profile.category != "intranet":
                raise GatewayError(
                    "proprietary source evidence is restricted to the intranet profile category"
                )
        if (
            isinstance(allowed_categories, list)
            and self.config.profile.category not in allowed_categories
        ):
            raise GatewayError(
                "task data is not allowed for the configured Message API profile category"
            )
        if self.config.profile.category != "intranet" and (
            authored_profile != self.config.profile.name
            or authored_category != self.config.profile.category
        ):
            raise GatewayError(
                "external Message API tasks must be explicitly bound to the configured "
                "provider profile and category"
            )
        if self.config.profile.category != "intranet" and (
            classification != "public_interface"
            or allowed_categories != [self.config.profile.category]
        ):
            raise GatewayError(
                "external Message API tasks must declare data_classification=public_interface "
                "and the exact configured external profile category"
            )
        if not 0 <= max_repairs <= 3:
            raise GatewayError("max_repairs must be between 0 and 3")
        if not task.prompt.strip():
            raise GatewayError("task prompt must be non-empty")
        if len(task.prompt) > self.max_prompt_chars:
            raise GatewayError(
                f"task prompt exceeds gateway limit: {len(task.prompt)} > {self.max_prompt_chars} chars"
            )
        run_id = _safe_id(run_id or self.new_run_id())
        task_root, output_path, formal_provenance_path = self._task_paths(task, run_id)
        result = GatewayRunResult(
            ok=False,
            task_id=task.task_id,
            run_id=run_id,
            staging_path=_repo_relative(self.repo_root, task_root),
        )
        if not overwrite and (output_path.exists() or formal_provenance_path.exists()):
            pair_ok, pair_error = self._verify_existing_pair(task, output_path, formal_provenance_path)
            if not pair_ok:
                result.error = str(_redact(pair_error, self.config.secrets))
                return result
            result.ok = True
            result.skipped = True
            result.promoted_path = _repo_relative(self.repo_root, output_path)
            result.provenance_path = _repo_relative(self.repo_root, formal_provenance_path)
            return result

        options = completion_options or CompletionOptions(
            response_mode="auto",
            thinking_mode=self.config.profile.default_thinking_mode,
            stream=self.config.profile.default_stream,
        )
        if options.response_mode in {"auto", "json_schema"} and options.response_schema is None:
            options = replace(options, response_schema=response_schema_for_contract(task.output_contract))
        prompt = task.prompt
        prior_attempt = 0
        last_diagnostics: list[dict[str, Any]] = []
        for attempt in range(1, max_repairs + 2):
            attempt_root = task_root / f"attempt_{attempt:02d}"
            request_manifest = self._request_manifest(
                task,
                run_id,
                attempt,
                prompt,
                options,
                prior_attempt,
            )
            started_at = self.clock()
            started_perf = time.perf_counter()
            completion = self.client.create_completion(
                system_prompt=self.system_prompt,
                user_prompt=prompt,
                options=options,
            )
            elapsed = time.perf_counter() - started_perf
            if completion.candidate is None:
                report = ContractReport(
                    False,
                    diagnostics=self._completion_diagnostics(
                        completion.error,
                        completion.error_kind,
                    ),
                )
                diagnostics = report.as_dict()["diagnostics"]
            else:
                report = validate_candidate(
                    completion.candidate,
                    task.output_contract,
                    allowed_campaign_profiles=task.allowed_campaign_profiles,
                    secret_values=self.config.secrets,
                )
                diagnostics = report.as_dict()["diagnostics"]
            provenance = self._attempt_provenance(
                task,
                run_id,
                attempt,
                prompt,
                completion,
                report,
                output_path,
                formal_provenance_path,
                started_at,
                elapsed,
                False,
            )
            self._write_attempt(attempt_root, request_manifest, completion, report, provenance)
            result.attempts = attempt
            result.diagnostics = list(_redact(diagnostics, self.config.secrets))
            if report.ok and isinstance(completion.candidate, dict):
                candidate_hash = _sha256_bytes(canonical_json_bytes(completion.candidate))
                formal_provenance = self._formal_provenance(
                    task,
                    run_id,
                    attempt,
                    prompt,
                    completion,
                    output_path,
                    candidate_hash,
                )
                try:
                    self._promote(
                        completion.candidate,
                        formal_provenance,
                        output_path,
                        formal_provenance_path,
                        overwrite=overwrite,
                    )
                except (OSError, GatewayError) as exc:
                    result.error = str(_redact(f"atomic promotion failed: {exc}", self.config.secrets))
                    return result
                completed_provenance = dict(provenance)
                completed_provenance["promotion"] = dict(provenance["promotion"])
                completed_provenance["promotion"]["completed"] = True
                self._write_attempt(
                    attempt_root,
                    request_manifest,
                    completion,
                    report,
                    completed_provenance,
                )
                result.ok = True
                result.promoted_path = _repo_relative(self.repo_root, output_path)
                result.provenance_path = _repo_relative(self.repo_root, formal_provenance_path)
                return result

            last_diagnostics = list(diagnostics)
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
            repairable = completion.candidate is not None or repairable_completion
            if attempt > max_repairs or not repairable:
                result.error = str(
                    _redact(
                        completion.error or "candidate failed the fixed output contract",
                        self.config.secrets,
                    )
                )
                return result
            prior_attempt = attempt
            prompt = self._repair_prompt(task, completion.content, last_diagnostics, attempt)
        result.error = "repair budget exhausted"
        return result

    def run_manifest(
        self,
        manifest_path: str | Path,
        *,
        run_id: str | None = None,
        task_ids: Iterable[str] = (),
        completion_options: CompletionOptions | None = None,
        max_repairs: int = 1,
        overwrite: bool = False,
        continue_on_error: bool = True,
    ) -> GatewayBatchResult:
        resolved_manifest = _inside(self.repo_root, manifest_path, label="manifest_path")
        run_id = _safe_id(run_id or self.new_run_id())
        run_root = self.staging_root / run_id
        selected = {item for item in task_ids if item}
        errors: list[str] = []
        try:
            tasks = load_manifest_tasks(resolved_manifest, self.repo_root)
        except (OSError, json.JSONDecodeError, GatewayError) as exc:
            batch = GatewayBatchResult(
                False,
                run_id,
                _repo_relative(self.repo_root, resolved_manifest),
                _repo_relative(self.repo_root, run_root),
                errors=[str(exc)],
            )
            _write_json(run_root / "run_summary.json", batch.as_dict())
            return batch
        if selected:
            known = {task.task_id for task in tasks}
            missing = sorted(selected - known)
            if missing:
                errors.append(f"unknown task ids: {missing}")
            tasks = [task for task in tasks if task.task_id in selected]
        results: list[GatewayRunResult] = []
        for task in tasks:
            try:
                task_result = self.run_task(
                    task,
                    run_id=run_id,
                    completion_options=completion_options,
                    max_repairs=max_repairs,
                    overwrite=overwrite,
                )
            except (OSError, GatewayError) as exc:
                task_result = GatewayRunResult(
                    False,
                    task.task_id,
                    run_id,
                    staging_path=_repo_relative(
                        self.repo_root, self.staging_root / run_id / _safe_id(task.task_id)
                    ),
                    error=str(exc),
                )
            results.append(task_result)
            if not task_result.ok and not continue_on_error:
                break
        ok = not errors and bool(results) and all(item.ok for item in results)
        batch = GatewayBatchResult(
            ok,
            run_id,
            _repo_relative(self.repo_root, resolved_manifest),
            _repo_relative(self.repo_root, run_root),
            results,
            errors,
        )
        _write_json(run_root / "run_summary.json", batch.as_dict())
        return batch


def load_manifest_tasks(manifest_path: Path, repo_root: Path) -> list[TaskSpec]:
    loaded = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("tasks"), list):
        raise GatewayError("model prompt pack manifest must contain a tasks array")
    tasks: list[TaskSpec] = []
    seen_ids: set[str] = set()
    seen_outputs: set[Path] = set()
    for index, raw in enumerate(loaded["tasks"]):
        if not isinstance(raw, dict):
            raise GatewayError(f"manifest tasks[{index}] must be an object")
        task_id = str(raw.get("task_id") or raw.get("request_id") or "").strip()
        if not task_id:
            raise GatewayError(f"manifest tasks[{index}] is missing task_id")
        if task_id in seen_ids:
            raise GatewayError(f"duplicate manifest task_id: {task_id}")
        seen_ids.add(task_id)
        prompt_path = _inside(repo_root, str(raw.get("prompt_path") or ""), label="prompt_path")
        if not prompt_path.is_file():
            raise GatewayError(f"prompt file does not exist: {prompt_path}")
        output_path = _inside(
            repo_root,
            str(raw.get("expected_output_path") or ""),
            label="expected_output_path",
        )
        if output_path in seen_outputs:
            raise GatewayError(f"duplicate manifest expected_output_path: {output_path}")
        seen_outputs.add(output_path)
        contract = raw.get("output_contract")
        if not isinstance(contract, dict):
            raise GatewayError(f"manifest task {task_id} has no output_contract object")
        profiles_raw = raw.get("allowed_campaign_profiles", {})
        if not isinstance(profiles_raw, dict):
            raise GatewayError(f"manifest task {task_id} allowed_campaign_profiles must be an object")
        tasks.append(
            TaskSpec(
                task_id=task_id,
                task_type=str(raw.get("task_type") or "manifest_task"),
                prompt=prompt_path.read_text(encoding="utf-8-sig"),
                prompt_path=_repo_relative(repo_root, prompt_path),
                manifest_path=_repo_relative(repo_root, manifest_path),
                expected_output_path=output_path,
                output_contract=dict(contract),
                allowed_campaign_profiles=dict(profiles_raw),
                metadata={
                    key: value
                    for key, value in raw.items()
                    if key
                    not in {
                        "prompt_path",
                        "expected_output_path",
                        "output_contract",
                        "allowed_campaign_profiles",
                    }
                },
            )
        )
    if not tasks:
        raise GatewayError("model prompt pack manifest has no tasks")
    return tasks
