#!/usr/bin/env python3
"""Run the fully automatic Message API -> fixed gate -> execution pipeline.

Model responses are candidates, never accepted harness inputs.  Each candidate
is first staged by the SDK-free authoring gateway, normalized, and checked by a
kind-specific deterministic tool.  Structured gate diagnostics are fed back to
the Message API within a bounded repair budget.  Only a gate-passing candidate
is promoted to the manifest's formal output path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_harness.authoring_gateway.client import (  # noqa: E402
    CompletionOptions,
    OpenAICompatibleMessageClient,
    canonical_json_bytes,
)
from test_harness.authoring_gateway.config import (  # noqa: E402
    PROFILE_SPECS,
    ConfigError,
    GatewayConfig,
    load_gateway_config,
)
from test_harness.authoring_gateway.gateway import (  # noqa: E402
    AuthoringGateway,
    GatewayError,
    TaskSpec,
    load_manifest_tasks,
)


class PipelineError(ValueError):
    """The pipeline cannot safely continue."""


def _safe_id(value: str) -> str:
    result = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in value)
    safe = result.strip("._-") or "task"
    if len(safe) <= 96 and safe == value:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:80]}_{digest}"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, _json_bytes(value))


def _inside(root: Path, value: str | Path, *, label: str) -> Path:
    raw = Path(value)
    resolved = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise PipelineError(f"{label} must stay inside repository root: {value}") from exc
    return resolved


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "<truncated>\n" + text[-limit:]


def _diagnostic(
    code: str,
    path: str,
    message: str,
    repair_hint: str,
    *,
    expected_shape: Any | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "severity": "error",
        "error_code": code,
        "path": path,
        "message": message,
        "repair_hint": repair_hint,
    }
    if expected_shape is not None:
        result["expected_shape"] = expected_shape
    return result


def _load_diagnostics(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        loaded = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    raw = loaded.get("diagnostics") if isinstance(loaded, dict) else None
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _triage_has_failures(summary: Any) -> bool:
    if not isinstance(summary, dict):
        return False
    return any(
        isinstance(summary.get(key), int) and summary[key] > 0
        for key in ("failed_cases", "pre_artifact_failure_cases", "command_failures")
    )


@dataclass
class CommandRecord:
    name: str
    argv: list[str]
    returncode: int
    elapsed_seconds: float
    stdout_tail: str
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "argv": self.argv,
            "returncode": self.returncode,
            "ok": self.ok,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


@dataclass
class FixedGateResult:
    ok: bool
    kind: str
    gate_root: str
    normalized_path: str = ""
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    commands: list[CommandRecord] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "ok": self.ok,
            "kind": self.kind,
            "gate_root": self.gate_root,
            "normalized_path": self.normalized_path,
            "diagnostic_count": len(self.diagnostics),
            "diagnostics": self.diagnostics,
            "commands": [item.as_dict() for item in self.commands],
            "artifacts": self.artifacts,
        }


@dataclass
class ExecutionResult:
    requested: bool
    ok: bool
    status: str
    commands: list[CommandRecord] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    error: str = ""
    candidate_cause: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "ok": self.ok,
            "status": self.status,
            "commands": [item.as_dict() for item in self.commands],
            "artifacts": self.artifacts,
            "error": self.error,
            "candidate_cause": self.candidate_cause,
        }


@dataclass
class PipelineTaskResult:
    ok: bool
    task_id: str
    run_id: str
    authoring_accepted: bool = False
    gate_attempts: int = 0
    message_calls: int = 0
    accepted_path: str = ""
    provenance_path: str = ""
    staging_path: str = ""
    skipped: bool = False
    error: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    execution: ExecutionResult = field(
        default_factory=lambda: ExecutionResult(False, True, "not_requested")
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "authoring_accepted": self.authoring_accepted,
            "gate_attempts": self.gate_attempts,
            "message_calls": self.message_calls,
            "accepted_path": self.accepted_path,
            "provenance_path": self.provenance_path,
            "staging_path": self.staging_path,
            "skipped": self.skipped,
            "error": self.error,
            "attempts": self.attempts,
            "execution": self.execution.as_dict(),
        }


@dataclass
class PipelineBatchResult:
    ok: bool
    run_id: str
    manifest_path: str
    staging_path: str
    results: list[PipelineTaskResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "ok": self.ok,
            "run_id": self.run_id,
            "manifest_path": self.manifest_path,
            "staging_path": self.staging_path,
            "task_count": len(self.results),
            "passed": sum(item.ok and not item.skipped for item in self.results),
            "skipped": sum(item.skipped for item in self.results),
            "failed": sum(not item.ok for item in self.results),
            "authoring_accepted": sum(item.authoring_accepted for item in self.results),
            "message_calls": sum(item.message_calls for item in self.results),
            "gate_repairs": sum(max(0, item.gate_attempts - 1) for item in self.results),
            "execution_requested": sum(item.execution.requested for item in self.results),
            "execution_passed": sum(
                item.execution.requested and item.execution.ok for item in self.results
            ),
            "execution_failed": sum(
                item.execution.requested and not item.execution.ok for item in self.results
            ),
            "errors": self.errors,
            "results": [item.as_dict() for item in self.results],
        }


class FixedGateRunner:
    """Invoke only fixed, typed harness validation tools with ``shell=False``."""

    def __init__(
        self,
        *,
        repo_root: Path,
        tool_repo_root: Path,
        timeout_seconds: float = 120.0,
        python_executable: str = sys.executable,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.tool_repo_root = tool_repo_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.python_executable = python_executable

    def _tool(self, name: str) -> str:
        path = self.tool_repo_root / "test_harness" / "tools" / name
        if not path.is_file():
            raise PipelineError(f"fixed gate tool is missing: {path}")
        return str(path)

    def _run(self, name: str, argv: Sequence[str]) -> CommandRecord:
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                list(argv),
                cwd=self.tool_repo_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
            return CommandRecord(
                name,
                list(argv),
                completed.returncode,
                time.perf_counter() - started,
                _tail(completed.stdout),
                _tail(completed.stderr),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandRecord(
                name,
                list(argv),
                124,
                time.perf_counter() - started,
                _tail(stdout),
                _tail(stderr or f"fixed gate exceeded {self.timeout_seconds} seconds"),
            )

    def run(self, candidate_path: Path, task: TaskSpec, gate_root: Path) -> FixedGateResult:
        gate_root.mkdir(parents=True, exist_ok=True)
        normalized_path = gate_root / "normalized.json"
        normalize_diagnostics = gate_root / "normalize_diagnostics.json"
        normalize = self._run(
            "normalize_model_output",
            [
                self.python_executable,
                self._tool("normalize_model_output.py"),
                str(candidate_path),
                "--request-id",
                task.task_id,
                "--out",
                str(normalized_path),
                "--diagnostics",
                str(normalize_diagnostics),
            ],
        )
        normalize_report = _read_json(normalize_diagnostics) if normalize_diagnostics.is_file() else {}
        kind = str(normalize_report.get("kind") or "invalid") if isinstance(normalize_report, dict) else "invalid"
        diagnostics = _load_diagnostics(normalize_diagnostics)
        artifacts = {"normalize_diagnostics": _relative(self.repo_root, normalize_diagnostics)}
        result = FixedGateResult(
            normalize.ok and bool(normalize_report.get("ok")),
            kind,
            _relative(self.repo_root, gate_root),
            _relative(self.repo_root, normalized_path) if normalized_path.is_file() else "",
            diagnostics,
            [normalize],
            artifacts,
        )
        if not result.ok:
            if not result.diagnostics:
                result.diagnostics.append(
                    _diagnostic(
                        "MODEL_OUTPUT_NORMALIZATION_FAILED",
                        str(candidate_path),
                        normalize.stderr_tail or normalize.stdout_tail or "normalizer failed",
                        "Return one supported output kind using the exact documented JSON shape.",
                    )
                )
            return self._finish(result, gate_root)

        if kind == "attack_dsl":
            self._gate_dsl(result, normalized_path, gate_root)
        elif kind == "flat_recipe":
            self._gate_recipe(result, normalized_path, gate_root)
        elif kind == "cluster_seed":
            self._gate_cluster_seed(result, normalized_path, gate_root)
        elif kind == "needs_harness_extension":
            self._gate_extension(result, normalized_path, gate_root)
        elif kind == "campaign_request":
            self._gate_campaign(result, normalized_path, task, gate_root)
        else:
            result.ok = False
            result.diagnostics.append(
                _diagnostic(
                    "UNSUPPORTED_FIXED_GATE_KIND",
                    "$.kind",
                    f"No fixed acceptance gate exists for output kind {kind!r}.",
                    "Return a kind declared by the task output_contract.",
                )
            )
        return self._finish(result, gate_root)

    def _finish(self, result: FixedGateResult, gate_root: Path) -> FixedGateResult:
        result.ok = result.ok and not any(item.get("severity") == "error" for item in result.diagnostics)
        summary_path = gate_root / "fixed_gate_report.json"
        result.artifacts["fixed_gate_report"] = _relative(self.repo_root, summary_path)
        _write_json(summary_path, result.as_dict())
        return result

    def _gate_dsl(self, result: FixedGateResult, normalized_path: Path, gate_root: Path) -> None:
        report_path = gate_root / "compile_report.json"
        diagnostics_path = gate_root / "compile_diagnostics.json"
        command = self._run(
            "compile_attack_dsl_check",
            [
                self.python_executable,
                self._tool("compile_attack_dsl.py"),
                str(normalized_path),
                "--check",
                "--model-asset-policy",
                "--report",
                str(report_path),
                "--model-diagnostics",
                str(diagnostics_path),
            ],
        )
        result.commands.append(command)
        result.artifacts.update(
            {
                "compile_report": _relative(self.repo_root, report_path),
                "compile_diagnostics": _relative(self.repo_root, diagnostics_path),
            }
        )
        result.diagnostics.extend(_load_diagnostics(diagnostics_path))
        result.ok = result.ok and command.ok
        if not command.ok and not _load_diagnostics(diagnostics_path):
            result.diagnostics.append(
                _diagnostic(
                    "ATTACK_DSL_FIXED_GATE_FAILED",
                    str(normalized_path),
                    command.stderr_tail or command.stdout_tail or "DSL compiler check failed",
                    "Repair the DSL using the compiler's structured constraints.",
                )
            )

    def _gate_recipe(self, result: FixedGateResult, normalized_path: Path, gate_root: Path) -> None:
        diagnostics_path = gate_root / "recipe_diagnostics.json"
        command = self._run(
            "validate_recipe",
            [
                self.python_executable,
                self._tool("validate_recipe.py"),
                str(normalized_path),
                "--check-assets",
                "--model-asset-policy",
                "--model-diagnostics",
                str(diagnostics_path),
            ],
        )
        result.commands.append(command)
        result.artifacts["recipe_diagnostics"] = _relative(self.repo_root, diagnostics_path)
        result.diagnostics.extend(_load_diagnostics(diagnostics_path))
        result.ok = result.ok and command.ok
        if not command.ok and not _load_diagnostics(diagnostics_path):
            result.diagnostics.append(
                _diagnostic(
                    "FLAT_RECIPE_FIXED_GATE_FAILED",
                    str(normalized_path),
                    command.stderr_tail or command.stdout_tail or "recipe validation failed",
                    "Repair the recipe using only supported API fields and available input assets.",
                )
            )

    def _gate_cluster_seed(self, result: FixedGateResult, normalized_path: Path, gate_root: Path) -> None:
        expanded_path = gate_root / "expanded_cluster_dsl.json"
        expand = self._run(
            "expand_cluster_seed",
            [
                self.python_executable,
                self._tool("build_source_guided_cluster.py"),
                str(normalized_path),
                "--out",
                str(expanded_path),
            ],
        )
        result.commands.append(expand)
        result.artifacts["expanded_cluster_dsl"] = _relative(self.repo_root, expanded_path)
        if not expand.ok:
            result.ok = False
            result.diagnostics.append(
                _diagnostic(
                    "CLUSTER_SEED_EXPANSION_FAILED",
                    str(normalized_path),
                    expand.stderr_tail or expand.stdout_tail or "cluster seed expansion failed",
                    "Repair the cluster_seed fields so fixed code can expand it into bounded attack DSL.",
                )
            )
            return
        self._gate_dsl(result, expanded_path, gate_root)

    def _gate_extension(self, result: FixedGateResult, normalized_path: Path, gate_root: Path) -> None:
        report_path = gate_root / "extension_report.json"
        diagnostics_path = gate_root / "extension_diagnostics.json"
        command = self._run(
            "validate_harness_extension",
            [
                self.python_executable,
                self._tool("validate_harness_extension.py"),
                str(normalized_path),
                "--report",
                str(report_path),
                "--model-diagnostics",
                str(diagnostics_path),
            ],
        )
        result.commands.append(command)
        result.artifacts.update(
            {
                "extension_report": _relative(self.repo_root, report_path),
                "extension_diagnostics": _relative(self.repo_root, diagnostics_path),
            }
        )
        result.diagnostics.extend(_load_diagnostics(diagnostics_path))
        result.ok = result.ok and command.ok

    def _gate_campaign(
        self,
        result: FixedGateResult,
        normalized_path: Path,
        task: TaskSpec,
        gate_root: Path,
    ) -> None:
        loaded = _read_json(normalized_path)
        profile_id = loaded.get("profile_id") if isinstance(loaded, dict) else None
        if not isinstance(profile_id, str) or profile_id not in task.allowed_campaign_profiles:
            result.ok = False
            result.diagnostics.append(
                _diagnostic(
                    "CAMPAIGN_PROFILE_NOT_ALLOWED",
                    "$.profile_id",
                    f"Campaign profile {profile_id!r} is not allowed by this task.",
                    "Choose a profile_id from allowed_campaign_profiles.",
                    expected_shape=sorted(task.allowed_campaign_profiles),
                )
            )
        report_path = gate_root / "campaign_gate.json"
        result.artifacts["campaign_gate"] = _relative(self.repo_root, report_path)
        _write_json(
            report_path,
            {
                "ok": result.ok,
                "profile_id": profile_id,
                "allowed_profile_ids": sorted(task.allowed_campaign_profiles),
            },
        )


class MessageHarnessPipeline:
    """Coordinate candidate generation, deterministic gates, repair, and execution."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        repo_root: str | Path,
        staging_root: str | Path = "artifacts/message_harness_pipeline",
        client: OpenAICompatibleMessageClient | None = None,
        tool_repo_root: str | Path | None = None,
        gate_timeout_seconds: float = 120.0,
        max_repair_prompt_chars: int = 220_000,
    ) -> None:
        self.config = config
        self.repo_root = Path(repo_root).resolve()
        if not self.repo_root.is_dir():
            raise PipelineError(f"repository root does not exist: {self.repo_root}")
        self.staging_root = _inside(self.repo_root, staging_root, label="staging_root")
        artifacts_root = (self.repo_root / "artifacts").resolve()
        try:
            self.staging_root.relative_to(artifacts_root)
        except ValueError as exc:
            raise PipelineError("pipeline staging_root must stay under repository artifacts/") from exc
        self.client = client or OpenAICompatibleMessageClient(config)
        self.gateway = AuthoringGateway(
            config,
            repo_root=self.repo_root,
            staging_root=self.staging_root / "gateway",
            client=self.client,
        )
        self.gates = FixedGateRunner(
            repo_root=self.repo_root,
            tool_repo_root=Path(tool_repo_root or REPO_ROOT),
            timeout_seconds=gate_timeout_seconds,
        )
        if max_repair_prompt_chars <= 0:
            raise PipelineError("max_repair_prompt_chars must be positive")
        self.max_repair_prompt_chars = max_repair_prompt_chars

    def new_run_id(self) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}_{uuid.uuid4().hex[:8]}"

    def _repair_prompt(
        self,
        task: TaskSpec,
        candidate: Mapping[str, Any],
        gate: FixedGateResult,
        iteration: int,
    ) -> str:
        diagnostics = [
            {
                key: item[key]
                for key in ("severity", "error_code", "path", "message", "repair_hint", "expected_shape")
                if key in item
            }
            for item in gate.diagnostics
        ]
        candidate_text = json.dumps(candidate, indent=2, ensure_ascii=False, sort_keys=True)
        diagnostics_text = json.dumps(diagnostics, indent=2, ensure_ascii=False, sort_keys=True)
        suffix = f"""

## Automatic fixed-gate repair {iteration}

The previous Message API JSON passed the transport/output contract but failed
the deterministic harness gate. Return one corrected complete JSON object only.
Do not explain the repair and do not return markdown. Preserve the requested
test intent and real semantic oracles, but use only supported fields.

Fixed gate kind: {gate.kind}

Structured diagnostics:
```json
{diagnostics_text}
```

Previous candidate:
```json
{candidate_text}
```
"""
        if len(task.prompt) + len(suffix) <= self.max_repair_prompt_chars:
            return task.prompt + suffix
        fixed = suffix[-min(len(suffix), 120_000) :]
        budget = max(0, self.max_repair_prompt_chars - len(fixed) - 64)
        return task.prompt[:budget] + "\n<original-prompt-truncated>\n" + fixed

    def _accepted_pair_ok(self, task: TaskSpec, output_path: Path, provenance_path: Path) -> bool:
        pair_ok, _ = self.gateway._verify_existing_pair(task, output_path, provenance_path)  # noqa: SLF001
        if not pair_ok:
            return False
        provenance = _read_json(provenance_path)
        fixed_gate = provenance.get("fixed_gate") if isinstance(provenance, dict) else None
        return isinstance(fixed_gate, dict) and fixed_gate.get("ok") is True

    def _promote_accepted(
        self,
        task: TaskSpec,
        candidate_path: Path,
        candidate_provenance_path: Path,
        gate: FixedGateResult,
        output_path: Path,
        provenance_path: Path,
        *,
        run_id: str,
        gate_attempt: int,
        overwrite: bool,
    ) -> None:
        if not overwrite and (output_path.exists() or provenance_path.exists()):
            raise PipelineError(f"accepted formal output already exists: {output_path}")
        candidate = _read_json(candidate_path)
        source_provenance = _read_json(candidate_provenance_path)
        if not isinstance(candidate, dict) or not isinstance(source_provenance, dict):
            raise PipelineError("candidate and candidate provenance must be JSON objects")
        candidate_hash = _sha256_bytes(canonical_json_bytes(candidate))
        formal_provenance = dict(source_provenance)
        source_repair = (
            dict(source_provenance.get("repair"))
            if isinstance(source_provenance.get("repair"), dict)
            else {}
        )
        source_repair.update(
            {
                "gate_repair_output": gate_attempt > 1,
                "gate_repair_iteration": max(0, gate_attempt - 1),
            }
        )
        formal_provenance.update(
            {
                "schema_version": 3,
                "request_id": task.task_id,
                "source_path": task.prompt_path or task.manifest_path,
                "description": "Message API candidate passed its fixed kind-specific harness gate.",
                "output_path": _relative(self.repo_root, output_path),
                "saved_at": _utc_iso(),
                "run_id": run_id,
                "candidate_sha256": candidate_hash,
                "repair": source_repair,
                "acceptance": {
                    "authoring_accepted": True,
                    "requires_fixed_gate": False,
                    "accepted_by": "message_harness_pipeline",
                },
                "fixed_gate": {
                    "ok": True,
                    "kind": gate.kind,
                    "gate_attempt": gate_attempt,
                    "report_path": gate.artifacts.get("fixed_gate_report", ""),
                    "normalized_path": gate.normalized_path,
                    "normalized_sha256": (
                        _sha256_bytes(
                            _inside(
                                self.repo_root,
                                gate.normalized_path,
                                label="normalized_path",
                            ).read_bytes()
                        )
                        if gate.normalized_path
                        else ""
                    ),
                    "diagnostic_count": len(gate.diagnostics),
                },
                "boundary": {
                    "model_calls": True,
                    "direct_api_calls": True,
                    "runs_sdk": False,
                    "executes_commands": False,
                    "applies_patches": False,
                    "commits_changes": False,
                    "wired_into_harness": False,
                    "production_flow": "message_api_fixed_gate_repair_atomic_acceptance",
                },
            }
        )
        self.gateway._promote(  # noqa: SLF001
            candidate,
            formal_provenance,
            output_path,
            provenance_path,
            overwrite=overwrite,
        )

    def run_task(
        self,
        task: TaskSpec,
        *,
        run_id: str | None = None,
        completion_options: CompletionOptions | None = None,
        max_contract_repairs: int = 1,
        max_gate_repairs: int = 2,
        overwrite: bool = False,
        execute: bool = False,
        runner: str | Path = "",
        jobs: int = 1,
        timeout_seconds: float = 120.0,
        campaign_dataset: str | Path = "",
    ) -> PipelineTaskResult:
        if not 0 <= max_contract_repairs <= 3 or not 0 <= max_gate_repairs <= 3:
            raise PipelineError("contract and fixed-gate repair budgets must be between 0 and 3")
        run_id = _safe_id(run_id or self.new_run_id())
        task_root = self.staging_root / run_id / _safe_id(task.task_id)
        _, output_path, provenance_path = self.gateway._task_paths(task, run_id)  # noqa: SLF001
        result = PipelineTaskResult(
            False,
            task.task_id,
            run_id,
            staging_path=_relative(self.repo_root, task_root),
        )
        if output_path.exists() or provenance_path.exists():
            if not overwrite and self._accepted_pair_ok(task, output_path, provenance_path):
                result.authoring_accepted = True
                result.skipped = True
                result.accepted_path = _relative(self.repo_root, output_path)
                result.provenance_path = _relative(self.repo_root, provenance_path)
                revalidate_root = task_root / "gates" / "existing_accepted"
                existing_gate = self.gates.run(output_path, task, revalidate_root)
                result.gate_attempts = 1
                result.attempts.append(
                    {
                        "existing_accepted": True,
                        "fixed_gate": existing_gate.as_dict(),
                    }
                )
                if not existing_gate.ok:
                    result.error = "existing accepted output fails the current fixed gate"
                    self._write_task_summary(task_root, result)
                    return result
                result.execution = self._execute(
                    task,
                    existing_gate,
                    task_root,
                    execute=execute,
                    runner=runner,
                    jobs=jobs,
                    timeout_seconds=timeout_seconds,
                    campaign_dataset=campaign_dataset,
                )
                result.ok = result.execution.ok
                if not result.ok:
                    result.error = result.execution.error or "accepted candidate execution failed"
                self._write_task_summary(task_root, result)
                return result
            if not overwrite:
                result.error = "existing formal output is not a verified fixed-gate accepted pair; use --overwrite"
                return result

        options = completion_options or CompletionOptions(response_mode="auto")
        prompt = task.prompt
        last_gate: FixedGateResult | None = None
        for gate_attempt in range(1, max_gate_repairs + 2):
            candidate_root = task_root / "candidates" / f"gate_attempt_{gate_attempt:02d}"
            candidate_path = candidate_root / "candidate.json"
            candidate_task = TaskSpec(
                task_id=f"{task.task_id}__candidate_{gate_attempt:02d}",
                prompt=prompt,
                expected_output_path=candidate_path,
                output_contract=task.output_contract,
                task_type=task.task_type,
                prompt_path=task.prompt_path,
                manifest_path=task.manifest_path,
                allowed_campaign_profiles=task.allowed_campaign_profiles,
                metadata=task.metadata,
            )
            gateway_result = self.gateway.run_task(
                candidate_task,
                run_id=run_id,
                completion_options=options,
                max_repairs=max_contract_repairs,
                overwrite=False,
            )
            result.message_calls += gateway_result.attempts
            result.gate_attempts = gate_attempt
            attempt_record: dict[str, Any] = {
                "gate_attempt": gate_attempt,
                "gateway": gateway_result.as_dict(),
                "candidate_path": _relative(self.repo_root, candidate_path),
            }
            if not gateway_result.ok:
                result.attempts.append(attempt_record)
                result.error = gateway_result.error or "Message API candidate generation failed"
                self._write_task_summary(task_root, result)
                return result
            candidate = _read_json(candidate_path)
            if not isinstance(candidate, dict):
                result.attempts.append(attempt_record)
                result.error = "staged gateway candidate is not a JSON object"
                self._write_task_summary(task_root, result)
                return result
            gate_root = task_root / "gates" / f"gate_attempt_{gate_attempt:02d}"
            last_gate = self.gates.run(candidate_path, task, gate_root)
            attempt_record["fixed_gate"] = last_gate.as_dict()
            result.attempts.append(attempt_record)
            if last_gate.ok:
                candidate_provenance = candidate_path.with_name("candidate.provenance.json")
                try:
                    self._promote_accepted(
                        task,
                        candidate_path,
                        candidate_provenance,
                        last_gate,
                        output_path,
                        provenance_path,
                        run_id=run_id,
                        gate_attempt=gate_attempt,
                        overwrite=overwrite,
                    )
                except (OSError, PipelineError, GatewayError, json.JSONDecodeError) as exc:
                    result.error = f"accepted promotion failed: {exc}"
                    self._write_task_summary(task_root, result)
                    return result
                result.accepted_path = _relative(self.repo_root, output_path)
                result.provenance_path = _relative(self.repo_root, provenance_path)
                result.authoring_accepted = True
                result.execution = self._execute(
                    task,
                    last_gate,
                    task_root,
                    execute=execute,
                    runner=runner,
                    jobs=jobs,
                    timeout_seconds=timeout_seconds,
                    campaign_dataset=campaign_dataset,
                )
                # SDK/oracle failures never trigger model repair and never undo
                # authoring acceptance.  They do make the requested end-to-end
                # command nonzero after triage/replay/bug assets are collected.
                result.ok = result.authoring_accepted and result.execution.ok
                if not result.ok:
                    result.error = result.execution.error or "accepted candidate execution failed"
                self._write_task_summary(task_root, result)
                return result
            if gate_attempt > max_gate_repairs:
                result.error = "fixed harness gate repair budget exhausted"
                self._write_task_summary(task_root, result)
                return result
            prompt = self._repair_prompt(task, candidate, last_gate, gate_attempt)
        result.error = "fixed harness gate repair budget exhausted"
        if last_gate is not None:
            result.attempts.append({"fixed_gate": last_gate.as_dict()})
        self._write_task_summary(task_root, result)
        return result

    def _write_task_summary(self, task_root: Path, result: PipelineTaskResult) -> None:
        _write_json(task_root / "task_summary.json", result.as_dict())

    def _execute(
        self,
        task: TaskSpec,
        gate: FixedGateResult,
        task_root: Path,
        *,
        execute: bool,
        runner: str | Path,
        jobs: int,
        timeout_seconds: float,
        campaign_dataset: str | Path,
    ) -> ExecutionResult:
        if not execute:
            return ExecutionResult(False, True, "not_requested")
        if gate.kind == "needs_harness_extension":
            return ExecutionResult(True, True, "not_applicable_extension_request")
        if jobs <= 0 or timeout_seconds <= 0:
            return ExecutionResult(True, False, "invalid_execution_options", error="jobs and timeout must be positive")
        runner_path = _inside(self.repo_root, runner, label="runner") if runner else Path()
        if not runner or not runner_path.is_file():
            return ExecutionResult(True, False, "runner_missing", error="--execute requires an existing --runner")
        execution_root = task_root / "execution"
        execution_root.mkdir(parents=True, exist_ok=True)
        if gate.kind == "campaign_request":
            return self._execute_campaign(
                task,
                gate,
                execution_root,
                runner_path,
                campaign_dataset,
            )
        commands: list[CommandRecord] = []
        recipe_input = _inside(self.repo_root, gate.normalized_path, label="normalized_path")
        if gate.kind in {"attack_dsl", "cluster_seed"}:
            dsl_path = recipe_input
            if gate.kind == "cluster_seed":
                dsl_path = _inside(
                    self.repo_root,
                    gate.artifacts.get("expanded_cluster_dsl", ""),
                    label="expanded_cluster_dsl",
                )
            recipe_input = execution_root / "compiled_recipes"
            compile_report = execution_root / "compile_report.json"
            compile_diagnostics = execution_root / "compile_diagnostics.json"
            compile_command = self.gates._run(  # noqa: SLF001
                "compile_attack_dsl",
                [
                    self.gates.python_executable,
                    self.gates._tool("compile_attack_dsl.py"),  # noqa: SLF001
                    str(dsl_path),
                    "--out",
                    str(recipe_input),
                    "--model-asset-policy",
                    "--report",
                    str(compile_report),
                    "--model-diagnostics",
                    str(compile_diagnostics),
                ],
            )
            commands.append(compile_command)
            if not compile_command.ok:
                return ExecutionResult(
                    True,
                    False,
                    "compile_failed",
                    commands,
                    {
                        "compile_report": _relative(self.repo_root, compile_report),
                        "compile_diagnostics": _relative(self.repo_root, compile_diagnostics),
                    },
                    "accepted DSL unexpectedly failed materialization",
                )
        cases_root = execution_root / "cases"
        triage_root = execution_root / "triage"
        run_command = self.gates._run(  # noqa: SLF001
            "run_recipes",
            [
                self.gates.python_executable,
                self.gates._tool("run_recipes.py"),  # noqa: SLF001
                "--runner",
                str(runner_path),
                "--recipe",
                str(recipe_input),
                "--out",
                str(cases_root),
                "--timeout",
                str(timeout_seconds),
                "--jobs",
                str(jobs),
                "--hash-recipes",
                "--triage-out",
                str(triage_root),
            ],
        )
        commands.append(run_command)
        replay_root = execution_root / "replay"
        bundle_root = execution_root / "failure_bundles"
        draft_path = execution_root / "bug_record_drafts.json"
        registry_root = execution_root / "bug_registry"
        seeds_path = triage_root / "regression_seeds.json"
        triage_summary = triage_root / "triage_summary.json"
        seed_payload = _read_json(seeds_path) if seeds_path.is_file() else []
        has_replay_seeds = isinstance(seed_payload, list) and bool(seed_payload)
        has_triage_failures = _triage_has_failures(
            _read_json(triage_summary) if triage_summary.is_file() else {}
        )
        asset_commands_ok = True
        execution_artifacts = {
            "cases": _relative(self.repo_root, cases_root),
            "triage": _relative(self.repo_root, triage_root),
        }
        if has_replay_seeds:
            replay_command = self.gates._run(  # noqa: SLF001
                "replay_regression_seeds",
                [
                    self.gates.python_executable,
                    self.gates._tool("replay_regression_seeds.py"),  # noqa: SLF001
                    "--runner",
                    str(runner_path),
                    "--seeds",
                    str(seeds_path),
                    "--out",
                    str(replay_root),
                    "--retries",
                    "3",
                    "--timeout",
                    str(timeout_seconds),
                ],
            )
            commands.append(replay_command)
            asset_commands_ok = asset_commands_ok and replay_command.ok
            execution_artifacts["replay"] = _relative(self.repo_root, replay_root)
        if has_triage_failures:
            bundle_argv = [
                self.gates.python_executable,
                self.gates._tool("export_failure_bundles.py"),  # noqa: SLF001
                "--triage",
                str(triage_root),
                "--out",
                str(bundle_root),
            ]
            if (replay_root / "replay_summary.json").is_file():
                bundle_argv.extend(["--replay", str(replay_root)])
            bundle_command = self.gates._run("export_failure_bundles", bundle_argv)  # noqa: SLF001
            commands.append(bundle_command)
            asset_commands_ok = asset_commands_ok and bundle_command.ok
            draft_argv = [
                self.gates.python_executable,
                self.gates._tool("export_bug_record_drafts.py"),  # noqa: SLF001
                "--triage",
                str(triage_root),
                "--out",
                str(draft_path),
                "--bug-prefix",
                "message_pipeline_candidate",
            ]
            if (replay_root / "replay_summary.json").is_file():
                draft_argv.extend(["--replay", str(replay_root)])
            if (bundle_root / "bundle_index.json").is_file():
                draft_argv.extend(["--bundle-index", str(bundle_root)])
            draft_command = self.gates._run("export_bug_record_drafts", draft_argv)  # noqa: SLF001
            commands.append(draft_command)
            asset_commands_ok = asset_commands_ok and draft_command.ok
            registry_argv = [
                self.gates.python_executable,
                self.gates._tool("collect_bug_registry.py"),  # noqa: SLF001
                "--triage",
                str(triage_root),
                "--out",
                str(registry_root),
            ]
            if (replay_root / "replay_summary.json").is_file():
                registry_argv.extend(["--replay", str(replay_root)])
            if (bundle_root / "bundle_index.json").is_file():
                registry_argv.extend(["--bundle-index", str(bundle_root)])
            registry_command = self.gates._run("collect_bug_registry", registry_argv)  # noqa: SLF001
            commands.append(registry_command)
            asset_commands_ok = asset_commands_ok and registry_command.ok
            execution_artifacts.update(
                {
                    "failure_bundles": _relative(self.repo_root, bundle_root),
                    "bug_record_drafts": _relative(self.repo_root, draft_path),
                    "bug_registry": _relative(self.repo_root, registry_root),
                }
            )
        execution_ok = run_command.ok and not has_triage_failures and asset_commands_ok
        if execution_ok:
            status = "passed"
            error = ""
            candidate_cause = ""
        elif has_triage_failures and asset_commands_ok:
            status = "sdk_or_oracle_failures_triaged"
            error = "SDK/oracle tests returned nonzero; inspect triage, replay, and bug assets"
            candidate_cause = "oracle_or_sdk_requires_classification"
        else:
            status = "failure_asset_pipeline_error"
            error = "test or failure-asset command returned nonzero"
            candidate_cause = "test_or_infrastructure_requires_classification"
        return ExecutionResult(
            True,
            execution_ok,
            status,
            commands,
            execution_artifacts,
            error,
            candidate_cause,
        )

    def _execute_campaign(
        self,
        task: TaskSpec,
        gate: FixedGateResult,
        execution_root: Path,
        runner_path: Path,
        campaign_dataset: str | Path,
    ) -> ExecutionResult:
        if not campaign_dataset:
            return ExecutionResult(
                True,
                False,
                "campaign_dataset_missing",
                error="campaign_request execution requires --campaign-dataset",
            )
        dataset_path = _inside(self.repo_root, campaign_dataset, label="campaign_dataset")
        if not dataset_path.exists():
            return ExecutionResult(True, False, "campaign_dataset_missing", error=str(dataset_path))
        tools_path = str(self.gates.tool_repo_root / "test_harness" / "tools")
        if tools_path not in sys.path:
            sys.path.insert(0, tools_path)
        from campaign_profiles import CampaignRequestError, resolve_campaign_argv

        request = _read_json(_inside(self.repo_root, gate.normalized_path, label="normalized_path"))
        try:
            argv = resolve_campaign_argv(
                request,
                allowed_profiles=dict(task.allowed_campaign_profiles),
                bindings={
                    "runner": _relative(self.repo_root, runner_path),
                    "dataset": _relative(self.repo_root, dataset_path),
                    "out": _relative(self.repo_root, execution_root / "campaign"),
                },
            )
        except CampaignRequestError as exc:
            return ExecutionResult(True, False, "campaign_binding_invalid", error=str(exc))
        command = self.gates._run("run_campaign_profile", argv)  # noqa: SLF001
        return ExecutionResult(
            True,
            command.ok,
            "passed" if command.ok else "campaign_failure",
            [command],
            {"campaign": _relative(self.repo_root, execution_root / "campaign")},
            "" if command.ok else "fixed campaign profile returned nonzero",
        )

    def run_manifest(
        self,
        manifest_path: str | Path,
        *,
        run_id: str | None = None,
        task_ids: Iterable[str] = (),
        completion_options: CompletionOptions | None = None,
        max_contract_repairs: int = 1,
        max_gate_repairs: int = 2,
        overwrite: bool = False,
        continue_on_error: bool = True,
        execute: bool = False,
        runner: str | Path = "",
        jobs: int = 1,
        timeout_seconds: float = 120.0,
        campaign_dataset: str | Path = "",
    ) -> PipelineBatchResult:
        resolved_manifest = _inside(self.repo_root, manifest_path, label="manifest_path")
        run_id = _safe_id(run_id or self.new_run_id())
        run_root = self.staging_root / run_id
        selected = {item for item in task_ids if item}
        errors: list[str] = []
        try:
            tasks = load_manifest_tasks(resolved_manifest, self.repo_root)
        except (OSError, json.JSONDecodeError, GatewayError) as exc:
            batch = PipelineBatchResult(
                False,
                run_id,
                _relative(self.repo_root, resolved_manifest),
                _relative(self.repo_root, run_root),
                errors=[str(exc)],
            )
            _write_json(run_root / "pipeline_summary.json", batch.as_dict())
            return batch
        if selected:
            known = {task.task_id for task in tasks}
            missing = sorted(selected - known)
            if missing:
                errors.append(f"unknown task ids: {missing}")
            tasks = [task for task in tasks if task.task_id in selected]
        results: list[PipelineTaskResult] = []
        for task in tasks:
            try:
                task_result = self.run_task(
                    task,
                    run_id=run_id,
                    completion_options=completion_options,
                    max_contract_repairs=max_contract_repairs,
                    max_gate_repairs=max_gate_repairs,
                    overwrite=overwrite,
                    execute=execute,
                    runner=runner,
                    jobs=jobs,
                    timeout_seconds=timeout_seconds,
                    campaign_dataset=campaign_dataset,
                )
            except (OSError, PipelineError, GatewayError, json.JSONDecodeError) as exc:
                task_result = PipelineTaskResult(
                    False,
                    task.task_id,
                    run_id,
                    staging_path=_relative(self.repo_root, run_root / _safe_id(task.task_id)),
                    error=str(exc),
                )
            results.append(task_result)
            if not task_result.ok and not continue_on_error:
                break
        batch = PipelineBatchResult(
            not errors and bool(results) and all(item.ok for item in results),
            run_id,
            _relative(self.repo_root, resolved_manifest),
            _relative(self.repo_root, run_root),
            results,
            errors,
        )
        _write_json(run_root / "pipeline_summary.json", batch.as_dict())
        return batch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="model_prompt_pack/model_task_manifest.json")
    parser.add_argument("--profile", required=True, choices=sorted(PROFILE_SPECS))
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--run-id", default="")
    parser.add_argument("--staging-root", default="artifacts/message_harness_pipeline")
    parser.add_argument("--response-mode", choices=("auto", "json_schema", "json_object", "none"), default="auto")
    parser.add_argument("--thinking-mode", choices=("omit", "enabled", "disabled"), default="omit")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-contract-repairs", type=int, default=1)
    parser.add_argument("--max-gate-repairs", type=int, default=2)
    parser.add_argument("--max-retries", type=int)
    parser.add_argument("--request-timeout", type=float)
    parser.add_argument("--backoff-base", type=float)
    parser.add_argument("--max-retry-delay", type=float)
    parser.add_argument("--response-bytes-limit", type=int)
    parser.add_argument("--gate-timeout", type=float, default=120.0)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--runner", default="")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--campaign-dataset", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.execute and not args.runner:
            raise PipelineError("--execute requires --runner")
        config = load_gateway_config(
            args.profile,
            request_timeout_seconds=args.request_timeout,
            max_retries=args.max_retries,
            backoff_base_seconds=args.backoff_base,
            max_retry_delay_seconds=args.max_retry_delay,
            response_bytes_limit=args.response_bytes_limit,
        )
        options = CompletionOptions(
            response_mode=args.response_mode,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            thinking_mode=args.thinking_mode,
            seed=args.seed,
        )
        pipeline = MessageHarnessPipeline(
            config,
            repo_root=REPO_ROOT,
            staging_root=args.staging_root,
            gate_timeout_seconds=args.gate_timeout,
        )
        result = pipeline.run_manifest(
            args.manifest,
            run_id=args.run_id or None,
            task_ids=args.task_id,
            completion_options=options,
            max_contract_repairs=args.max_contract_repairs,
            max_gate_repairs=args.max_gate_repairs,
            overwrite=args.overwrite,
            continue_on_error=not args.stop_on_error,
            execute=args.execute,
            runner=args.runner,
            jobs=args.jobs,
            timeout_seconds=args.timeout,
            campaign_dataset=args.campaign_dataset,
        )
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return 0 if result.ok else 1
    except (ConfigError, PipelineError, GatewayError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
