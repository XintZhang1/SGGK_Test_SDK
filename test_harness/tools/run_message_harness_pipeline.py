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
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    is_source_task_type,
    load_manifest_tasks,
)
from test_harness.authoring_gateway.source_evidence import (  # noqa: E402
    generated_case_ids,
    sha256_json,
    validate_source_review,
    verify_current_source,
)
from test_harness.tools.failure_predicate import signatures_match  # noqa: E402
from test_harness.tools.generated_artifact_review import (  # noqa: E402
    ReviewPacketError,
    verify_review_attestation,
    write_review_packet,
)


class PipelineError(ValueError):
    """The pipeline cannot safely continue."""


AUTHORING_SUBAGENT_ROLES: dict[str, str] = {
    "implementation": (
        "Act as the implementation subagent. Produce a complete, conservative candidate that "
        "uses only documented fields and can pass the deterministic harness gate."
    ),
    "test_design": (
        "Act as the test-design subagent. Preserve the requested API intent while emphasizing "
        "executable inputs, meaningful semantic oracles, and reproducible boundary cases."
    ),
    "adversarial_review": (
        "Act as the adversarial-review subagent. Produce an independently authored complete "
        "candidate that targets likely edge conditions without inventing unsupported fields."
    ),
    "minimal_reproducer": (
        "Act as the minimal-reproducer subagent. Prefer the smallest complete candidate that "
        "still exercises the requested API behavior and semantic oracle."
    ),
}

SELECTION_GOALS = {
    "auto",
    "fixed_gate_only",
    "must_pass_execution",
    "must_reproduce_target_signature",
    "extension_backlog",
    "adapter_build_pass",
}

ORACLE_FIELD_KINDS = {
    "result_bodies": "result_body_count",
    "result_edges": "result_edge_count",
    "result_vertices": "result_vertex_count",
    "result_paths": "result_path_count",
    "total_length": "metric_total_length",
    "total_area": "metric_total_area",
    "total_volume": "metric_total_volume",
    "total_abs_volume": "metric_total_abs_volume",
    "boolean_volume_relation": "boolean_volume_relation",
    "boolean_bbox_relation": "boolean_bbox_relation",
    "point_relations": "point_relation",
    "face_point_relations": "face_point_relation",
    "clash_checks": "clash_check",
    "distance_checks": "distance_check",
    "plane_extreme_checks": "plane_extreme_check",
    "topocheck": "topocheck",
    "expected_status": "typed_status",
}

BUG_INVESTIGATOR_ROLES = {
    "reproduction_analyst",
    "topology_analyst",
    "source_analyst",
    "skeptical_oracle_analyst",
}


def _authoring_role_prompt(prompt: str, role_id: str, candidate_index: int) -> str:
    instruction = AUTHORING_SUBAGENT_ROLES[role_id]
    return (
        f"{prompt}\n\n"
        f"## Parallel authoring subagent {candidate_index}: {role_id}\n\n"
        f"{instruction}\n"
        "Work independently from other candidates. Return the same single JSON output contract "
        "requested above; do not discuss or rank other candidates."
    )


def _candidate_roles(candidate_count: int, requested: Sequence[str]) -> list[str]:
    if not 1 <= candidate_count <= 8:
        raise PipelineError("candidate_count must be between 1 and 8")
    role_ids = list(requested) or list(AUTHORING_SUBAGENT_ROLES)
    unknown = sorted(set(role_ids) - set(AUTHORING_SUBAGENT_ROLES))
    if unknown:
        raise PipelineError(f"unknown authoring subagent roles: {unknown}")
    return [role_ids[index % len(role_ids)] for index in range(candidate_count)]


def _candidate_shape_metrics(value: Any, kind: str) -> dict[str, Any]:
    oracle_kinds: set[str] = set()
    oracle_checks = 0

    def visit(item: Any) -> None:
        nonlocal oracle_checks
        if isinstance(item, dict):
            for key, child in item.items():
                oracle_kind = ORACLE_FIELD_KINDS.get(key)
                if oracle_kind is not None and child not in (None, False, "", [], {}):
                    oracle_kinds.add(oracle_kind)
                    oracle_checks += len(child) if isinstance(child, list) else 1
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    if kind == "flat_recipe":
        case_count = 1
    elif isinstance(value, dict) and isinstance(value.get("cases"), list):
        case_count = len(value["cases"])
    elif isinstance(value, dict) and isinstance(value.get("variants"), list):
        case_count = len(value["variants"])
    else:
        case_count = 0
    return {
        "requested_case_coverage": min(case_count, 10_000),
        "distinct_oracle_kinds": len(oracle_kinds),
        "oracle_checks": min(oracle_checks, 10_000),
        "oracle_kinds": sorted(oracle_kinds),
    }


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
class CandidateBranchResult:
    candidate_id: str
    candidate_index: int
    role_id: str
    gate_ok: bool = False
    message_calls: int = 0
    gate_attempts: int = 0
    candidate_path: str = ""
    candidate_provenance_path: str = ""
    candidate_sha256: str = ""
    duplicate_of: str = ""
    score: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    fixed_gate: FixedGateResult | None = field(default=None, repr=False)
    execution: ExecutionResult = field(
        default_factory=lambda: ExecutionResult(False, True, "not_evaluated")
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_index": self.candidate_index,
            "role_id": self.role_id,
            "gate_ok": self.gate_ok,
            "message_calls": self.message_calls,
            "gate_attempts": self.gate_attempts,
            "candidate_path": self.candidate_path,
            "candidate_provenance_path": self.candidate_provenance_path,
            "candidate_sha256": self.candidate_sha256,
            "duplicate_of": self.duplicate_of,
            "score": self.score,
            "error": self.error,
            "attempts": self.attempts,
            "fixed_gate": self.fixed_gate.as_dict() if self.fixed_gate is not None else {},
            "execution": self.execution.as_dict(),
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
    review_packet_path: str = ""
    review_packet_sha256: str = ""
    review_report_path: str = ""
    review_report_sha256: str = ""
    review_status: str = ""
    staging_path: str = ""
    candidate_count: int = 1
    selected_candidate_id: str = ""
    selected_role_id: str = ""
    selection_policy: str = "fixed_gate_then_execution_pass"
    candidates: list[dict[str, Any]] = field(default_factory=list)
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
            "review_packet_path": self.review_packet_path,
            "review_packet_sha256": self.review_packet_sha256,
            "review_report_path": self.review_report_path,
            "review_report_sha256": self.review_report_sha256,
            "review_status": self.review_status,
            "staging_path": self.staging_path,
            "candidate_count": self.candidate_count,
            "selected_candidate_id": self.selected_candidate_id,
            "selected_role_id": self.selected_role_id,
            "selection_policy": self.selection_policy,
            "candidates": self.candidates,
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
            "candidate_branches": sum(item.candidate_count for item in self.results),
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

    def _run(
        self,
        name: str,
        argv: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> CommandRecord:
        started = time.perf_counter()
        command_timeout = timeout_seconds or self.timeout_seconds
        try:
            completed = subprocess.run(
                list(argv),
                cwd=self.tool_repo_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=command_timeout,
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
                _tail(stderr or f"fixed command exceeded {command_timeout} seconds"),
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
        elif kind == "api_plugin_candidate":
            self._gate_plugin_candidate(result, normalized_path, task, gate_root)
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
        if is_source_task_type(task.task_type):
            self._gate_source_review(result, normalized_path, task, gate_root)
        return self._finish(result, gate_root)

    def _gate_source_review(
        self,
        result: FixedGateResult,
        normalized_path: Path,
        task: TaskSpec,
        gate_root: Path,
    ) -> None:
        contract = task.metadata.get("source_contract")
        bindings = task.metadata.get("host_source_bindings")
        normalized = _read_json(normalized_path) if normalized_path.is_file() else {}
        review = normalized.get("source_review") if isinstance(normalized, dict) else None
        errors: list[str] = []
        current_refs: list[dict[str, Any]] = []
        case_ids: list[str] = []
        if not isinstance(contract, dict):
            errors.append("source task metadata is missing the host-issued source_contract")
        if not isinstance(bindings, list):
            errors.append("source task metadata is missing host_source_bindings")
        if isinstance(contract, dict) and isinstance(bindings, list):
            current_errors, current_refs = verify_current_source(contract, bindings)
            errors.extend(current_errors)
        if not isinstance(review, dict):
            errors.append("source output must include source_review for every candidate kind")
        case_source = normalized
        if result.kind == "cluster_seed":
            expanded_raw = result.artifacts.get("expanded_cluster_dsl", "")
            try:
                expanded_path = _inside(self.repo_root, expanded_raw, label="expanded_cluster_dsl")
                case_source = _read_json(expanded_path) if expanded_path.is_file() else {}
            except (PipelineError, OSError, json.JSONDecodeError):
                case_source = {}
        if isinstance(case_source, dict):
            case_ids = generated_case_ids(result.kind, case_source)
        if not case_ids:
            errors.append("source-guided output must expose at least one stable generated case ID")
        if isinstance(review, dict) and isinstance(contract, dict):
            errors.extend(validate_source_review(review, contract, case_ids))

        for message in errors:
            result.diagnostics.append(
                _diagnostic(
                    "SOURCE_REVIEW_BINDING_FAILED",
                    "$.source_review",
                    message,
                    (
                        "Copy the host-issued task/finding/contract/reference values exactly and "
                        "connect every branch, hypothesis, enhancement, and generated case by ID."
                    ),
                )
            )
        if errors:
            result.ok = False
        attestation = {
            "schema_version": 1,
            "ok": not errors,
            "task_id": task.task_id,
            "source_contract_sha256": (
                contract.get("source_contract_sha256") if isinstance(contract, dict) else ""
            ),
            "source_review_sha256": sha256_json(review) if isinstance(review, dict) else "",
            "validated_source_refs": current_refs,
            "generated_case_ids": case_ids,
            "error_count": len(errors),
            "errors": errors,
        }
        attestation_path = gate_root / "source_review_attestation.json"
        _write_json(attestation_path, attestation)
        result.artifacts.update(
            {
                "source_review_attestation": _relative(self.repo_root, attestation_path),
                "source_review_attestation_sha256": _sha256_bytes(attestation_path.read_bytes()),
                "source_contract_sha256": str(attestation["source_contract_sha256"] or ""),
                "source_review_sha256": str(attestation["source_review_sha256"] or ""),
            }
        )

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

    def _gate_plugin_candidate(
        self,
        result: FixedGateResult,
        normalized_path: Path,
        task: TaskSpec,
        gate_root: Path,
    ) -> None:
        materialized_root = gate_root / "materialized"
        report_path = gate_root / "plugin_candidate_report.json"
        diagnostics_path = gate_root / "plugin_candidate_diagnostics.json"
        contract_path = gate_root / "trusted_adaptation_contract.json"
        contract = task.metadata.get("adaptation_contract")
        contract_sha256 = task.metadata.get("adaptation_contract_sha256")
        identity_report_path = gate_root / "adaptation_identity_report.json"
        result.artifacts["adaptation_identity_report"] = _relative(
            self.repo_root,
            identity_report_path,
        )
        if (
            task.task_type != "api_adaptation"
            or not isinstance(contract, dict)
            or not isinstance(contract_sha256, str)
            or len(contract_sha256) != 64
        ):
            result.ok = False
            message = (
                "api_plugin_candidate requires a host-generated api_adaptation task with "
                "adaptation_contract and adaptation_contract_sha256 metadata"
            )
            result.diagnostics.append(
                _diagnostic(
                    "API_ADAPTATION_CONTRACT_MISSING",
                    "$.task.metadata.adaptation_contract",
                    message,
                    "Only the review-session resolver may issue a discovery-bound adaptation contract.",
                )
            )
            _write_json(identity_report_path, {"schema_version": 1, "ok": False, "errors": [message]})
            return
        _write_json(contract_path, contract)
        result.artifacts["trusted_adaptation_contract"] = _relative(self.repo_root, contract_path)
        command = self._run(
            "materialize_api_plugin_candidate",
            [
                self.python_executable,
                self._tool("materialize_api_plugin_candidate.py"),
                str(normalized_path),
                "--out",
                str(materialized_root),
                "--report",
                str(report_path),
                "--model-diagnostics",
                str(diagnostics_path),
                "--expected-contract",
                str(contract_path),
                "--expected-contract-sha256",
                contract_sha256,
            ],
        )
        result.commands.append(command)
        result.artifacts.update(
            {
                "plugin_candidate_report": _relative(self.repo_root, report_path),
                "plugin_candidate_diagnostics": _relative(self.repo_root, diagnostics_path),
                "materialized_plugins": _relative(self.repo_root, materialized_root / "plugins"),
            }
        )
        result.diagnostics.extend(_load_diagnostics(diagnostics_path))
        report = _read_json(report_path) if report_path.is_file() else {}
        _write_json(
            identity_report_path,
            {
                "schema_version": 1,
                "ok": bool(report.get("ok")) and report.get("identity_bound") is True,
                "target_api": contract.get("target_api"),
                "adaptation_contract_sha256": contract_sha256,
                "intake_sha256": contract.get("intake_sha256"),
                "candidate_api": report.get("api"),
                "errors": report.get("errors", []),
            },
        )
        materialized_plugin = report.get("materialized_plugin") if isinstance(report, dict) else ""
        if isinstance(materialized_plugin, str) and materialized_plugin:
            result.artifacts["materialized_plugin"] = _relative(
                self.repo_root,
                _inside(self.repo_root, materialized_plugin, label="materialized_plugin"),
            )
        result.ok = result.ok and command.ok and bool(report.get("ok"))

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
        enable_bug_investigation: bool = False,
        bug_source_roots: Sequence[str | Path] = (),
        bug_investigator_roles: Sequence[str] = (),
        bug_investigator_parallelism: int = 4,
        bug_investigation_max_rounds: int = 16,
        bug_investigation_max_tool_calls: int = 32,
        bug_investigation_max_tokens: int = 16_384,
        reduce_failure_candidates: bool = False,
        reduction_limit: int = 3,
        reduction_max_trials: int = 32,
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
        self.enable_bug_investigation = enable_bug_investigation
        self.bug_source_roots = [Path(path).resolve() for path in bug_source_roots]
        missing_source_roots = [str(path) for path in self.bug_source_roots if not path.is_dir()]
        if missing_source_roots:
            raise PipelineError(f"bug source roots do not exist: {missing_source_roots}")
        self.bug_investigator_roles = list(bug_investigator_roles)
        unknown_bug_roles = sorted(set(self.bug_investigator_roles) - BUG_INVESTIGATOR_ROLES)
        if unknown_bug_roles:
            raise PipelineError(f"unknown bug investigator roles: {unknown_bug_roles}")
        if not 1 <= bug_investigator_parallelism <= 8:
            raise PipelineError("bug_investigator_parallelism must be between 1 and 8")
        if not 1 <= bug_investigation_max_rounds <= 64:
            raise PipelineError("bug_investigation_max_rounds must be between 1 and 64")
        if not 1 <= bug_investigation_max_tool_calls <= 128:
            raise PipelineError("bug_investigation_max_tool_calls must be between 1 and 128")
        if bug_investigation_max_tokens <= 0:
            raise PipelineError("bug_investigation_max_tokens must be positive")
        self.bug_investigator_parallelism = bug_investigator_parallelism
        self.bug_investigation_max_rounds = bug_investigation_max_rounds
        self.bug_investigation_max_tool_calls = bug_investigation_max_tool_calls
        self.bug_investigation_max_tokens = bug_investigation_max_tokens
        if reduction_limit < 0:
            raise PipelineError("reduction_limit must be >= 0")
        if reduction_max_trials <= 0:
            raise PipelineError("reduction_max_trials must be positive")
        self.reduce_failure_candidates = reduce_failure_candidates
        self.reduction_limit = reduction_limit
        self.reduction_max_trials = reduction_max_trials

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
        if not isinstance(provenance, dict) or provenance.get("schema_version") != 3:
            return False
        if provenance.get("request_id") != task.task_id:
            return False
        if provenance.get("source_type") != self.config.profile.provenance_source_type:
            return False
        if provenance.get("profile") != self.config.profile.name:
            return False
        if provenance.get("interface") != "openai_compatible_chat_completions_message_content_json":
            return False
        if provenance.get("model") != self.config.model:
            return False
        if not all(
            isinstance(provenance.get(key), str) and provenance.get(key)
            for key in ("run_id", "prompt_sha256", "message_content_sha256", "candidate_sha256")
        ):
            return False
        if provenance.get("task_prompt_sha256") != _sha256_bytes(task.prompt.encode("utf-8")):
            return False
        acceptance = provenance.get("acceptance")
        if acceptance != {
            "authoring_accepted": True,
            "requires_fixed_gate": False,
            "accepted_by": "message_harness_pipeline",
        }:
            return False
        boundary = provenance.get("boundary")
        if not isinstance(boundary, dict) or not all(
            (
                boundary.get("model_calls") is True,
                boundary.get("direct_api_calls") is True,
                boundary.get("production_flow")
                == "message_api_fixed_gate_repair_atomic_acceptance",
            )
        ):
            return False
        fixed_gate = provenance.get("fixed_gate") if isinstance(provenance, dict) else None
        if not isinstance(fixed_gate, dict) or fixed_gate.get("ok") is not True:
            return False
        review = provenance.get("generated_artifact_review")
        if not isinstance(review, dict):
            return False
        review_ok, _review_reason = verify_review_attestation(
            self.repo_root,
            review,
            expected_candidate_sha256=str(provenance.get("candidate_sha256") or ""),
        )
        if not review_ok:
            return False
        if is_source_task_type(task.task_type):
            if self.config.profile.category != "intranet":
                return False
            contract = task.metadata.get("source_contract")
            bindings = task.metadata.get("host_source_bindings")
            source_attestation = provenance.get("source_review_attestation")
            if (
                not isinstance(contract, dict)
                or not isinstance(bindings, list)
                or not isinstance(source_attestation, dict)
                or source_attestation.get("ok") is not True
            ):
                return False
            current_errors, current_refs = verify_current_source(contract, bindings)
            if current_errors:
                return False
            attestation_path_raw = source_attestation.get("path")
            try:
                attestation_path = _inside(
                    self.repo_root,
                    str(attestation_path_raw or ""),
                    label="source_review_attestation.path",
                )
                if (
                    not attestation_path.is_file()
                    or _sha256_bytes(attestation_path.read_bytes())
                    != source_attestation.get("sha256")
                ):
                    return False
                attestation = _read_json(attestation_path)
            except (PipelineError, OSError, json.JSONDecodeError):
                return False
            if not isinstance(attestation, dict) or attestation.get("ok") is not True:
                return False
            if attestation.get("task_id") != task.task_id:
                return False
            if attestation.get("validated_source_refs") != current_refs:
                return False
            if (
                attestation.get("source_contract_sha256")
                != contract.get("source_contract_sha256")
                or source_attestation.get("source_contract_sha256")
                != contract.get("source_contract_sha256")
                or source_attestation.get("source_review_sha256")
                != attestation.get("source_review_sha256")
            ):
                return False
        report_path_raw = fixed_gate.get("report_path")
        if not isinstance(report_path_raw, str) or not report_path_raw:
            return False
        try:
            report_path = _inside(self.repo_root, report_path_raw, label="fixed_gate.report_path")
            report = _read_json(report_path) if report_path.is_file() else {}
        except (PipelineError, OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(report, dict)
            and report.get("ok") is True
            and report.get("kind") == fixed_gate.get("kind")
            and report.get("normalized_path") == fixed_gate.get("normalized_path")
        )

    def _execution_review_required(
        self,
        task: TaskSpec,
        provenance_path: Path,
    ) -> bool:
        """All Message-authored candidates require session-host approval."""

        del task, provenance_path
        return True

    def _execution_approval_error(
        self,
        task: TaskSpec,
        output_path: Path,
        provenance_path: Path,
        runner: str | Path,
    ) -> str:
        """Verify the host approval bound to an orchestrated review round.

        A model decision is never execution authority.  The approval file is
        emitted by the session host only after a natural-language comment has
        been interpreted as an explicit approval.  Direct callers of this
        lower-level pipeline therefore cannot bypass review with ``--execute``.
        """

        provenance = _read_json(provenance_path) if provenance_path.is_file() else {}
        if not self._execution_review_required(task, provenance_path):
            return ""
        raw_path = task.metadata.get("approval_attestation_path")
        if not isinstance(raw_path, str) or not raw_path:
            return "reviewed session task requires a host execution approval attestation"
        try:
            approval_path = _inside(self.repo_root, raw_path, label="approval_attestation_path")
            artifacts_root = (self.repo_root / "artifacts").resolve()
            approval_path.relative_to(artifacts_root)
        except (PipelineError, ValueError):
            return "execution approval attestation must stay under repository artifacts/"
        if not approval_path.is_file():
            return "execution approval attestation is missing"
        try:
            approval = _read_json(approval_path)
            candidate = _read_json(output_path)
        except (OSError, json.JSONDecodeError):
            return "execution approval or accepted candidate is unreadable"
        if not isinstance(approval, dict) or not isinstance(candidate, dict):
            return "execution approval and accepted candidate must be JSON objects"
        unsigned = {key: value for key, value in approval.items() if key != "approval_sha256"}
        if approval.get("approval_sha256") != _sha256_bytes(canonical_json_bytes(unsigned)):
            return "execution approval self-hash mismatch"
        if (
            approval.get("schema_version") != 1
            or approval.get("record_type") != "execution_approval"
            or approval.get("decision") != "approved_for_execution"
        ):
            return "execution approval has an invalid fixed decision contract"
        if approval.get("task_id") != task.task_id:
            return "execution approval task identity mismatch"
        candidate_sha256 = _sha256_bytes(canonical_json_bytes(candidate))
        if approval.get("candidate_sha256") != candidate_sha256:
            return "execution approval candidate hash mismatch"
        if task.metadata.get("approved_candidate_sha256") != candidate_sha256:
            return "execution manifest is not bound to the approved candidate"
        round_sha256 = task.metadata.get("approved_round_sha256")
        if not isinstance(round_sha256, str) or len(round_sha256) != 64:
            return "execution manifest has no valid approved round hash"
        if approval.get("round_sha256") != round_sha256:
            return "execution approval round hash mismatch"
        if approval.get("task_prompt_sha256") != _sha256_bytes(task.prompt.encode("utf-8")):
            return "execution approval prompt hash mismatch"
        review = provenance.get("generated_artifact_review") if isinstance(provenance, dict) else {}
        if not isinstance(review, dict) or approval.get("review_packet_sha256") != review.get(
            "review_packet_sha256"
        ):
            return "execution approval review packet hash mismatch"
        for path_key, hash_key, canonical in (
            ("comment_path", "comment_sha256", False),
            ("interpretation_path", "interpretation_sha256", True),
        ):
            raw_evidence_path = approval.get(path_key)
            expected_evidence_hash = approval.get(hash_key)
            if not isinstance(raw_evidence_path, str) or not raw_evidence_path:
                return f"execution approval has no valid {path_key}"
            if not isinstance(expected_evidence_hash, str) or len(expected_evidence_hash) != 64:
                return f"execution approval has no valid {hash_key}"
            try:
                evidence_path = _inside(
                    self.repo_root,
                    raw_evidence_path,
                    label=path_key,
                )
                evidence_path.relative_to((self.repo_root / "artifacts").resolve())
            except (PipelineError, ValueError):
                return f"execution approval {path_key} must stay under artifacts/"
            if not evidence_path.is_file():
                return f"execution approval evidence is missing: {path_key}"
            if canonical:
                try:
                    evidence = _read_json(evidence_path)
                except (OSError, json.JSONDecodeError):
                    return "execution comment interpretation is unreadable"
                actual_evidence_hash = (
                    _sha256_bytes(canonical_json_bytes(evidence))
                    if isinstance(evidence, dict)
                    else ""
                )
                decision = evidence.get("decision") if isinstance(evidence, dict) else {}
                if (
                    not isinstance(decision, dict)
                    or decision.get("decision") != "approve"
                    or evidence.get("record_type") != "review_comment_decision"
                    or evidence.get("status") != "model_interpreted"
                    or evidence.get("qwen_called") is not True
                ):
                    return "execution comment interpretation is not a validated Qwen approval decision"
            else:
                actual_evidence_hash = _sha256_bytes(evidence_path.read_bytes())
            if actual_evidence_hash != expected_evidence_hash:
                return f"execution approval {hash_key} mismatch"
        runner_hash = str(approval.get("runner_sha256") or "")
        if runner:
            try:
                runner_path = _inside(self.repo_root, runner, label="runner")
            except PipelineError:
                return "approved runner escapes the repository"
            if not runner_path.is_file():
                return "approved runner is missing"
            if not runner_hash or _sha256_bytes(runner_path.read_bytes()) != runner_hash:
                return "runner bytes do not match the execution approval"
        elif runner_hash:
            return "execution approval names runner bytes but execution omitted the runner"
        return ""

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
        candidate_id: str,
        role_id: str,
        candidate_count: int,
        selection_goal: str,
        selection_reason: str,
        candidate_pool: Sequence[Mapping[str, Any]],
        selected_execution: ExecutionResult,
        generated_review: Mapping[str, Any],
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
        adaptation_contract = (
            task.metadata.get("adaptation_contract")
            if isinstance(task.metadata.get("adaptation_contract"), dict)
            else None
        )
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
                "task_prompt_sha256": _sha256_bytes(task.prompt.encode("utf-8")),
                "api_adaptation": (
                    {
                        "identity_bound": True,
                        "target_api": adaptation_contract.get("target_api"),
                        "adapter_archetype": adaptation_contract.get("adapter_archetype"),
                        "intake_sha256": adaptation_contract.get("intake_sha256"),
                        "function_signature_sha256": adaptation_contract.get(
                            "function_signature_sha256"
                        ),
                        "adaptation_contract_sha256": task.metadata.get(
                            "adaptation_contract_sha256"
                        ),
                        "identity_report_path": gate.artifacts.get(
                            "adaptation_identity_report",
                            "",
                        ),
                    }
                    if gate.kind == "api_plugin_candidate" and adaptation_contract is not None
                    else None
                ),
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
                "candidate_selection": {
                    "policy": selection_goal,
                    "score_algorithm": "sggk_candidate_score_v1",
                    "candidate_id": candidate_id,
                    "role_id": role_id,
                    "candidate_count": candidate_count,
                    "selection_reason": selection_reason,
                    "independent_branches": candidate_count > 1,
                    "pool": [dict(item) for item in candidate_pool],
                    "execution": {
                        "requested": selected_execution.requested,
                        "ok": selected_execution.ok,
                        "status": selected_execution.status,
                        "artifacts": dict(selected_execution.artifacts),
                    },
                },
                "generated_artifact_review": dict(generated_review),
                "execution_approval_policy": {
                    "required": True,
                    "harness_session_id": str(task.metadata.get("harness_session_id") or ""),
                    "harness_round_number": int(task.metadata.get("harness_round_number") or 0),
                    "approval_authority": "fixed_harness_session_host",
                },
                "source_review_attestation": (
                    {
                        "ok": True,
                        "path": gate.artifacts.get("source_review_attestation", ""),
                        "sha256": gate.artifacts.get("source_review_attestation_sha256", ""),
                        "source_contract_sha256": gate.artifacts.get(
                            "source_contract_sha256", ""
                        ),
                        "source_review_sha256": gate.artifacts.get(
                            "source_review_sha256", ""
                        ),
                    }
                    if is_source_task_type(task.task_type)
                    else None
                ),
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
        if gate.kind == "api_plugin_candidate" and selected_execution.requested:
            if not self._plugin_execution_attested(selected_execution):
                raise PipelineError("selected API plugin lacks a complete hash-bound build attestation")
            formal_provenance["api_plugin_build_attestation"] = {
                "ok": True,
                "attested_at": _utc_iso(),
                "attestation_run_id": run_id,
                **dict(selected_execution.artifacts),
            }
        self.gateway._promote(  # noqa: SLF001
            candidate,
            formal_provenance,
            output_path,
            provenance_path,
            overwrite=overwrite,
        )

    def _plugin_execution_attested(self, execution: ExecutionResult) -> bool:
        if not execution.requested or not execution.ok:
            return False
        artifacts = execution.artifacts
        required_hashes = (
            "plugin_build_report_sha256",
            "runner_sha256",
            "runtime_registry_sha256",
            "sdk_identity_sha256",
            "semantic_sha256",
        )
        if any(
            not isinstance(artifacts.get(key), str) or len(artifacts[key]) != 64
            for key in required_hashes
        ):
            return False
        report_raw = artifacts.get("plugin_build_report")
        if not isinstance(report_raw, str) or not report_raw:
            return False
        try:
            report_path = _inside(self.repo_root, report_raw, label="plugin_build_report")
            if (
                not report_path.is_file()
                or _sha256_bytes(report_path.read_bytes())
                != artifacts["plugin_build_report_sha256"]
            ):
                return False
            report = _read_json(report_path)
        except (OSError, PipelineError, json.JSONDecodeError):
            return False
        sdk_identity = report.get("sdk_identity") if isinstance(report, dict) else None
        semantic_hashes = report.get("semantic_hashes") if isinstance(report, dict) else None
        return bool(
            isinstance(report, dict)
            and report.get("ok") is True
            and report.get("stable_semantic_evidence") is True
            and report.get("smoke_replays") == 3
            and report.get("runner_sha256") == artifacts["runner_sha256"]
            and report.get("runtime_registry_sha256")
            == artifacts["runtime_registry_sha256"]
            and isinstance(sdk_identity, dict)
            and sdk_identity.get("sha256") == artifacts["sdk_identity_sha256"]
            and isinstance(semantic_hashes, list)
            and len(semantic_hashes) == 3
            and len(set(semantic_hashes)) == 1
            and semantic_hashes[0] == artifacts["semantic_sha256"]
        )

    def _reattest_existing_acceptance(
        self,
        *,
        task: TaskSpec,
        output_path: Path,
        provenance_path: Path,
        gate: FixedGateResult,
        execution: ExecutionResult,
        run_id: str,
        selection_goal: str,
    ) -> None:
        candidate = _read_json(output_path)
        provenance = _read_json(provenance_path)
        if not isinstance(candidate, dict) or not isinstance(provenance, dict):
            raise PipelineError("existing accepted pair is not a pair of JSON objects")
        if provenance.get("request_id") != task.task_id:
            raise PipelineError("existing accepted provenance request_id does not match the task")
        previous_provenance_sha256 = _sha256_bytes(canonical_json_bytes(provenance))
        normalized_sha256 = (
            _sha256_bytes(
                _inside(
                    self.repo_root,
                    gate.normalized_path,
                    label="reattest.normalized_path",
                ).read_bytes()
            )
            if gate.normalized_path
            else ""
        )
        selection = (
            dict(provenance.get("candidate_selection"))
            if isinstance(provenance.get("candidate_selection"), dict)
            else {}
        )
        selection.update(
            {
                "previous_policy": selection.get("policy", ""),
                "policy": selection_goal,
                "candidate_id": "existing_accepted",
                "role_id": "revalidation",
                "candidate_count": 0,
                "selection_reason": (
                    "existing Message API candidate revalidated against the current fixed gate "
                    "and selection goal"
                ),
                "independent_branches": False,
                "execution": {
                    "requested": execution.requested,
                    "ok": execution.ok,
                    "status": execution.status,
                    "artifacts": dict(execution.artifacts),
                },
            }
        )
        attested_at = _utc_iso()
        provenance.update(
            {
                "fixed_gate": {
                    "ok": True,
                    "kind": gate.kind,
                    "gate_attempt": 1,
                    "revalidation": True,
                    "report_path": gate.artifacts.get("fixed_gate_report", ""),
                    "normalized_path": gate.normalized_path,
                    "normalized_sha256": normalized_sha256,
                    "diagnostic_count": len(gate.diagnostics),
                },
                "candidate_selection": selection,
                "latest_attestation": {
                    "schema_version": 1,
                    "attested_at": attested_at,
                    "attestation_run_id": run_id,
                    "selection_policy": selection_goal,
                    "previous_provenance_sha256": previous_provenance_sha256,
                    "fixed_gate_report": gate.artifacts.get("fixed_gate_report", ""),
                    "execution_artifacts": dict(execution.artifacts),
                },
            }
        )
        if is_source_task_type(task.task_type):
            provenance["source_review_attestation"] = {
                "ok": True,
                "path": gate.artifacts.get("source_review_attestation", ""),
                "sha256": gate.artifacts.get("source_review_attestation_sha256", ""),
                "source_contract_sha256": gate.artifacts.get(
                    "source_contract_sha256", ""
                ),
                "source_review_sha256": gate.artifacts.get("source_review_sha256", ""),
            }
        if gate.kind == "api_plugin_candidate" and selection_goal == "adapter_build_pass":
            if not self._plugin_execution_attested(execution):
                raise PipelineError("API plugin execution lacks a complete hash-bound build attestation")
            provenance["api_plugin_build_attestation"] = {
                "ok": True,
                "attested_at": attested_at,
                "attestation_run_id": run_id,
                **dict(execution.artifacts),
            }
        self.gateway._promote(  # noqa: SLF001
            candidate,
            provenance,
            output_path,
            provenance_path,
            overwrite=True,
        )

    def _run_candidate_branch(
        self,
        task: TaskSpec,
        task_root: Path,
        run_id: str,
        options: CompletionOptions,
        *,
        candidate_index: int,
        role_id: str,
        max_contract_repairs: int,
        max_gate_repairs: int,
    ) -> CandidateBranchResult:
        candidate_id = f"candidate_{candidate_index:02d}_{role_id}"
        branch = CandidateBranchResult(candidate_id, candidate_index, role_id)
        branch_prompt = _authoring_role_prompt(task.prompt, role_id, candidate_index)
        branch_task = TaskSpec(
            task_id=task.task_id,
            prompt=branch_prompt,
            expected_output_path=task.expected_output_path,
            output_contract=task.output_contract,
            task_type=task.task_type,
            prompt_path=task.prompt_path,
            manifest_path=task.manifest_path,
            allowed_campaign_profiles=task.allowed_campaign_profiles,
            metadata=task.metadata,
        )
        branch_options = options
        if options.seed is not None:
            branch_options = replace(options, seed=options.seed + candidate_index - 1)
        prompt = branch_prompt
        try:
            for gate_attempt in range(1, max_gate_repairs + 2):
                candidate_root = (
                    task_root
                    / "candidates"
                    / candidate_id
                    / f"gate_attempt_{gate_attempt:02d}"
                )
                candidate_path = candidate_root / "candidate.json"
                candidate_task = TaskSpec(
                    task_id=f"{task.task_id}__{candidate_id}__attempt_{gate_attempt:02d}",
                    prompt=prompt,
                    expected_output_path=candidate_path,
                    output_contract=task.output_contract,
                    task_type=task.task_type,
                    prompt_path=task.prompt_path,
                    manifest_path=task.manifest_path,
                    allowed_campaign_profiles=task.allowed_campaign_profiles,
                    metadata={
                        **dict(task.metadata),
                        "authoring_subagent": {
                            "candidate_id": candidate_id,
                            "candidate_index": candidate_index,
                            "role_id": role_id,
                        },
                    },
                )
                gateway_result = self.gateway.run_task(
                    candidate_task,
                    run_id=run_id,
                    completion_options=branch_options,
                    max_repairs=max_contract_repairs,
                    overwrite=False,
                )
                branch.message_calls += gateway_result.attempts
                branch.gate_attempts = gate_attempt
                branch.candidate_path = _relative(self.repo_root, candidate_path)
                branch.candidate_provenance_path = _relative(
                    self.repo_root,
                    candidate_path.with_name("candidate.provenance.json"),
                )
                attempt_record: dict[str, Any] = {
                    "candidate_id": candidate_id,
                    "candidate_index": candidate_index,
                    "role_id": role_id,
                    "gate_attempt": gate_attempt,
                    "gateway": gateway_result.as_dict(),
                    "candidate_path": branch.candidate_path,
                }
                if not gateway_result.ok:
                    branch.attempts.append(attempt_record)
                    branch.error = gateway_result.error or "Message API candidate generation failed"
                    return branch
                candidate = _read_json(candidate_path)
                if not isinstance(candidate, dict):
                    branch.attempts.append(attempt_record)
                    branch.error = "staged gateway candidate is not a JSON object"
                    return branch
                branch.candidate_sha256 = _sha256_bytes(canonical_json_bytes(candidate))
                gate_root = (
                    task_root
                    / "gates"
                    / candidate_id
                    / f"gate_attempt_{gate_attempt:02d}"
                )
                fixed_gate = self.gates.run(candidate_path, task, gate_root)
                branch.fixed_gate = fixed_gate
                attempt_record["fixed_gate"] = fixed_gate.as_dict()
                branch.attempts.append(attempt_record)
                if fixed_gate.ok:
                    branch.gate_ok = True
                    return branch
                if gate_attempt > max_gate_repairs:
                    branch.error = "fixed harness gate repair budget exhausted"
                    return branch
                prompt = self._repair_prompt(branch_task, candidate, fixed_gate, gate_attempt)
        except (OSError, PipelineError, GatewayError, json.JSONDecodeError, ValueError) as exc:
            branch.error = str(exc)
            return branch
        branch.error = "fixed harness gate repair budget exhausted"
        return branch

    def _score_candidate(
        self,
        branch: CandidateBranchResult,
        *,
        eligible: bool,
        signature_match: bool = False,
        stable_replay_count: int = 0,
    ) -> tuple[int, int, int, int, int, int, int, str]:
        normalized: Any = {}
        normalized_bytes = 0
        if branch.fixed_gate is not None and branch.fixed_gate.normalized_path:
            normalized_path = _inside(
                self.repo_root,
                branch.fixed_gate.normalized_path,
                label="normalized_path",
            )
            if normalized_path.is_file():
                normalized_bytes = normalized_path.stat().st_size
                normalized = _read_json(normalized_path)
        metrics = _candidate_shape_metrics(
            normalized,
            branch.fixed_gate.kind if branch.fixed_gate is not None else "",
        )
        warning_count = sum(
            item.get("severity") == "warning"
            for item in (branch.fixed_gate.diagnostics if branch.fixed_gate is not None else [])
        )
        execution_ok = branch.execution.requested and branch.execution.ok
        branch.score = {
            "algorithm": "sggk_candidate_score_v1",
            "eligible": eligible,
            "execution_ok": execution_ok,
            "target_signature_match": signature_match,
            "stable_replay_count": stable_replay_count,
            **metrics,
            "warning_count": warning_count,
            "gate_repairs": max(0, branch.gate_attempts - 1),
            "normalized_bytes": normalized_bytes,
            "tie_break_sha256": branch.candidate_sha256,
        }
        # Ascending tuple: eligibility/execution/coverage are negated while
        # warnings, repair count, size, and the canonical hash remain natural.
        return (
            -int(eligible),
            -int(execution_ok or signature_match),
            -int(metrics["requested_case_coverage"]),
            -int(metrics["distinct_oracle_kinds"]),
            -int(metrics["oracle_checks"]),
            warning_count + max(0, branch.gate_attempts - 1),
            normalized_bytes,
            branch.candidate_sha256,
        )

    def _matches_target_signature(
        self,
        execution: ExecutionResult,
        target_signature: Mapping[str, Any],
    ) -> tuple[bool, int]:
        triage_path = execution.artifacts.get("triage", "")
        if not triage_path:
            return False, 0
        seeds_path = _inside(self.repo_root, triage_path, label="triage") / "regression_seeds.json"
        seeds = _read_json(seeds_path) if seeds_path.is_file() else []
        expected = canonical_json_bytes(dict(target_signature))
        matched_fingerprints: set[str] = set()
        for seed in seeds if isinstance(seeds, list) else []:
            if not isinstance(seed, dict) or not isinstance(seed.get("failure_signature"), dict):
                continue
            if canonical_json_bytes(seed["failure_signature"]) == expected:
                fingerprint = str(seed.get("fingerprint") or "")
                if fingerprint:
                    matched_fingerprints.add(fingerprint)
        if not matched_fingerprints:
            return False, 0
        replay_path = execution.artifacts.get("replay", "")
        replay_summary_path = (
            _inside(self.repo_root, replay_path, label="replay") / "replay_summary.json"
            if replay_path
            else Path()
        )
        replay = _read_json(replay_summary_path) if replay_path and replay_summary_path.is_file() else {}
        stable_count = 0
        if isinstance(replay, dict):
            for item in replay.get("results", []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("fingerprint") or "") not in matched_fingerprints:
                    continue
                expected_signature = item.get("expected_failure_signature")
                attempts = item.get("attempts")
                declared_attempt_count = item.get("attempt_count")
                if (
                    item.get("status") != "stable_same_failure"
                    or not isinstance(expected_signature, dict)
                    or canonical_json_bytes(expected_signature) != expected
                    or not isinstance(attempts, list)
                    or len(attempts) < 3
                    or not isinstance(declared_attempt_count, int)
                    or isinstance(declared_attempt_count, bool)
                    or declared_attempt_count != len(attempts)
                ):
                    continue
                if not all(
                    isinstance(attempt, dict)
                    and attempt.get("matches_expected") is True
                    and isinstance(attempt.get("failure_signature"), dict)
                    and signatures_match(
                        target_signature,
                        attempt["failure_signature"],
                    )[0]
                    for attempt in attempts
                ):
                    continue
                stable_count = max(stable_count, len(attempts))
        return stable_count > 0, stable_count

    def _evaluate_candidate_pool(
        self,
        task: TaskSpec,
        task_root: Path,
        branches: Sequence[CandidateBranchResult],
        *,
        execute: bool,
        runner: str | Path,
        jobs: int,
        timeout_seconds: float,
        campaign_dataset: str | Path,
        selection_goal: str,
        target_failure_signature: Mapping[str, Any],
    ) -> tuple[CandidateBranchResult | None, str]:
        gated = [
            branch
            for branch in branches
            if branch.gate_ok and branch.fixed_gate is not None and not branch.duplicate_of
        ]
        if not gated:
            return None, "no candidate passed the fixed gate"
        if selection_goal == "extension_backlog":
            candidates = [
                branch
                for branch in gated
                if branch.fixed_gate is not None
                and branch.fixed_gate.kind == "needs_harness_extension"
            ]
            for branch in gated:
                branch.execution = ExecutionResult(False, True, "not_requested")
            if not candidates:
                return None, "extension_backlog requires a needs_harness_extension candidate"
            ranked = sorted(
                candidates,
                key=lambda item: self._score_candidate(item, eligible=True),
            )
            return ranked[0], "highest deterministic score for extension backlog intake"

        if selection_goal == "fixed_gate_only":
            for branch in gated:
                branch.execution = ExecutionResult(False, True, "not_requested")
            ranked = sorted(
                gated,
                key=lambda item: self._score_candidate(item, eligible=True),
            )
            return ranked[0], "highest deterministic fixed-gate candidate score"

        if not execute:
            return None, f"selection_goal={selection_goal} requires SDK execution"
        if selection_goal == "must_reproduce_target_signature" and not target_failure_signature:
            return None, "must_reproduce_target_signature requires a target failure signature"

        ranked: list[tuple[tuple[int, int, int, int, int, int, int, str], CandidateBranchResult]] = []
        for branch in gated:
            if branch.fixed_gate is None:
                continue
            if branch.fixed_gate.kind == "needs_harness_extension":
                branch.execution = ExecutionResult(
                    True,
                    False,
                    "adaptation_required",
                    error="needs_harness_extension is not an executable adapter",
                )
                self._score_candidate(branch, eligible=False)
                continue
            branch.execution = self._execute(
                task,
                branch.fixed_gate,
                task_root / "candidate_executions" / branch.candidate_id,
                execute=True,
                runner=runner,
                jobs=jobs,
                timeout_seconds=timeout_seconds,
                campaign_dataset=campaign_dataset,
            )
            signature_match = False
            stable_replay_count = 0
            if selection_goal == "must_reproduce_target_signature":
                signature_match, stable_replay_count = self._matches_target_signature(
                    branch.execution,
                    target_failure_signature,
                )
                eligible = signature_match and stable_replay_count > 0
            elif selection_goal == "adapter_build_pass":
                eligible = (
                    branch.fixed_gate.kind == "api_plugin_candidate"
                    and self._plugin_execution_attested(branch.execution)
                )
            else:
                eligible = branch.execution.ok
            ranked.append(
                (
                    self._score_candidate(
                        branch,
                        eligible=eligible,
                        signature_match=signature_match,
                        stable_replay_count=stable_replay_count,
                    ),
                    branch,
                )
            )
        passing = sorted(item for item in ranked if item[1].score.get("eligible"))
        if not passing:
            return None, f"no independent candidate satisfied selection_goal={selection_goal}"
        selected = passing[0][1]
        if selection_goal == "must_reproduce_target_signature":
            reason = "highest deterministic score among stable target-signature reproducers"
        else:
            reason = "highest deterministic score among SDK/oracle execution passes"
        return selected, reason

    def run_task(
        self,
        task: TaskSpec,
        *,
        run_id: str | None = None,
        completion_options: CompletionOptions | None = None,
        max_contract_repairs: int = 1,
        max_gate_repairs: int = 2,
        candidate_count: int = 1,
        candidate_parallelism: int = 1,
        authoring_roles: Sequence[str] = (),
        selection_goal: str = "auto",
        target_failure_signature: Mapping[str, Any] | None = None,
        overwrite: bool = False,
        execute: bool = False,
        runner: str | Path = "",
        jobs: int = 1,
        timeout_seconds: float = 120.0,
        campaign_dataset: str | Path = "",
    ) -> PipelineTaskResult:
        if not 0 <= max_contract_repairs <= 3 or not 0 <= max_gate_repairs <= 3:
            raise PipelineError("contract and fixed-gate repair budgets must be between 0 and 3")
        roles = _candidate_roles(candidate_count, authoring_roles)
        if not 1 <= candidate_parallelism <= 8:
            raise PipelineError("candidate_parallelism must be between 1 and 8")
        if selection_goal not in SELECTION_GOALS:
            raise PipelineError(f"selection_goal must be one of {sorted(SELECTION_GOALS)}")
        resolved_selection_goal = selection_goal
        if resolved_selection_goal == "auto":
            resolved_selection_goal = "must_pass_execution" if execute else "fixed_gate_only"
        target_signature = dict(target_failure_signature or {})
        run_id = _safe_id(run_id or self.new_run_id())
        task_root = self.staging_root / run_id / _safe_id(task.task_id)
        _, output_path, provenance_path = self.gateway._task_paths(task, run_id)  # noqa: SLF001
        result = PipelineTaskResult(
            False,
            task.task_id,
            run_id,
            staging_path=_relative(self.repo_root, task_root),
            candidate_count=candidate_count,
            selection_policy=resolved_selection_goal,
        )
        if execute and self._execution_review_required(task, provenance_path):
            if overwrite:
                result.error = (
                    "reviewed session tasks cannot execute with --overwrite; approval is bound "
                    "to the unchanged fixed-gate candidate"
                )
                self._write_task_summary(task_root, result)
                return result
            if not output_path.is_file() or not provenance_path.is_file():
                result.error = (
                    "reviewed session tasks cannot generate and execute in one step; "
                    "generate a fixed-gate review round, submit a comment, and bind host approval first"
                )
                self._write_task_summary(task_root, result)
                return result
        if is_source_task_type(task.task_type) and self.config.profile.category != "intranet":
            result.error = (
                "source_attack tasks contain source excerpts and are restricted to the intranet profile; "
                "external simulator profiles are forbidden"
            )
            self._write_task_summary(task_root, result)
            return result
        if output_path.exists() or provenance_path.exists():
            if not overwrite and self._accepted_pair_ok(task, output_path, provenance_path):
                result.skipped = True
                result.candidate_count = 0
                result.selected_candidate_id = "existing_accepted"
                result.selected_role_id = "revalidation"
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
                provenance = _read_json(provenance_path)
                prior_gate = provenance.get("fixed_gate") if isinstance(provenance, dict) else {}
                current_normalized_sha256 = (
                    _sha256_bytes(
                        _inside(
                            self.repo_root,
                            existing_gate.normalized_path,
                            label="existing_gate.normalized_path",
                        ).read_bytes()
                    )
                    if existing_gate.normalized_path
                    else ""
                )
                if (
                    not isinstance(prior_gate, dict)
                    or prior_gate.get("kind") != existing_gate.kind
                    or prior_gate.get("normalized_sha256") != current_normalized_sha256
                ):
                    result.error = "existing accepted output no longer matches its recorded fixed-gate identity"
                    self._write_task_summary(task_root, result)
                    return result
                approval_error = (
                    self._execution_approval_error(task, output_path, provenance_path, runner)
                    if execute
                    else ""
                )
                if approval_error:
                    result.execution = ExecutionResult(
                        True,
                        False,
                        "approval_required",
                        error=approval_error,
                        candidate_cause="missing_or_stale_execution_approval",
                    )
                else:
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
                selection_ok = False
                selection_error = ""
                if resolved_selection_goal == "fixed_gate_only":
                    selection_ok = result.execution.ok
                elif resolved_selection_goal == "extension_backlog":
                    selection_ok = existing_gate.kind == "needs_harness_extension"
                    selection_error = "existing accepted output is not a harness-extension backlog item"
                elif resolved_selection_goal == "adapter_build_pass":
                    selection_ok = (
                        existing_gate.kind == "api_plugin_candidate"
                        and self._plugin_execution_attested(result.execution)
                    )
                    selection_error = result.execution.error or (
                        "existing accepted output did not pass the API plugin build/runtime gate"
                    )
                elif resolved_selection_goal == "must_pass_execution":
                    selection_ok = result.execution.requested and result.execution.ok
                    selection_error = result.execution.error or (
                        "existing accepted output did not pass requested SDK execution"
                    )
                elif resolved_selection_goal == "must_reproduce_target_signature":
                    signature_match, stable_replays = self._matches_target_signature(
                        result.execution,
                        target_signature,
                    )
                    selection_ok = (
                        result.execution.requested and signature_match and stable_replays > 0
                    )
                    selection_error = result.execution.error or (
                        "existing accepted output did not stably reproduce the requested immutable signature"
                    )
                if selection_ok:
                    try:
                        self._reattest_existing_acceptance(
                            task=task,
                            output_path=output_path,
                            provenance_path=provenance_path,
                            gate=existing_gate,
                            execution=result.execution,
                            run_id=run_id,
                            selection_goal=resolved_selection_goal,
                        )
                    except (OSError, PipelineError, GatewayError, json.JSONDecodeError) as exc:
                        selection_ok = False
                        selection_error = f"existing acceptance re-attestation failed: {exc}"
                result.authoring_accepted = selection_ok
                result.ok = selection_ok
                if not selection_ok:
                    result.error = selection_error or result.execution.error or "accepted candidate selection failed"
                self._write_task_summary(task_root, result)
                return result
            if not overwrite:
                result.error = "existing formal output is not a verified fixed-gate accepted pair; use --overwrite"
                return result

        options = completion_options or CompletionOptions(response_mode="auto")
        branches: list[CandidateBranchResult] = []

        def generate(index: int, role_id: str) -> CandidateBranchResult:
            return self._run_candidate_branch(
                task,
                task_root,
                run_id,
                options,
                candidate_index=index,
                role_id=role_id,
                max_contract_repairs=max_contract_repairs,
                max_gate_repairs=max_gate_repairs,
            )

        if candidate_count == 1 or candidate_parallelism == 1:
            branches = [generate(index, role_id) for index, role_id in enumerate(roles, 1)]
        else:
            workers = min(candidate_count, candidate_parallelism)
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="sggk-authoring-subagent",
            ) as executor:
                futures = {
                    executor.submit(generate, index, role_id): index
                    for index, role_id in enumerate(roles, 1)
                }
                for future in concurrent.futures.as_completed(futures):
                    index = futures[future]
                    try:
                        branches.append(future.result())
                    except Exception as exc:  # pragma: no cover - defensive thread boundary
                        role_id = roles[index - 1]
                        branches.append(
                            CandidateBranchResult(
                                f"candidate_{index:02d}_{role_id}",
                                index,
                                role_id,
                                error=f"authoring subagent failed: {exc}",
                            )
                        )
            branches.sort(key=lambda item: item.candidate_index)

        seen_candidates: dict[str, str] = {}
        for branch in sorted(branches, key=lambda item: item.candidate_index):
            if not branch.gate_ok or not branch.candidate_sha256:
                continue
            duplicate_of = seen_candidates.get(branch.candidate_sha256)
            if duplicate_of:
                branch.duplicate_of = duplicate_of
                branch.execution = ExecutionResult(
                    False,
                    False,
                    "duplicate_candidate",
                    error=f"canonical candidate duplicates {duplicate_of}",
                )
                self._score_candidate(branch, eligible=False)
            else:
                seen_candidates[branch.candidate_sha256] = branch.candidate_id

        result.message_calls = sum(branch.message_calls for branch in branches)
        result.attempts = [attempt for branch in branches for attempt in branch.attempts]
        selected, selection_reason = self._evaluate_candidate_pool(
            task,
            task_root,
            branches,
            execute=execute,
            runner=runner,
            jobs=jobs,
            timeout_seconds=timeout_seconds,
            campaign_dataset=campaign_dataset,
            selection_goal=resolved_selection_goal,
            target_failure_signature=target_signature,
        )
        result.candidates = [branch.as_dict() for branch in branches]
        if selected is None or selected.fixed_gate is None:
            result.gate_attempts = max((branch.gate_attempts for branch in branches), default=0)
            branch_errors = [branch.error for branch in branches if branch.error]
            result.error = selection_reason
            if branch_errors:
                result.error += ": " + " | ".join(branch_errors[:4])
            self._write_task_summary(task_root, result)
            return result

        result.selected_candidate_id = selected.candidate_id
        result.selected_role_id = selected.role_id
        result.gate_attempts = selected.gate_attempts
        candidate_path = _inside(self.repo_root, selected.candidate_path, label="candidate_path")
        candidate_provenance = _inside(
            self.repo_root,
            selected.candidate_provenance_path,
            label="candidate_provenance_path",
        )
        try:
            result.accepted_path = _relative(self.repo_root, output_path)
            result.provenance_path = _relative(self.repo_root, provenance_path)
            result.execution = selected.execution
            review_result = result.as_dict()
            review_result["authoring_accepted"] = True
            generated_review = write_review_packet(
                repo_root=self.repo_root,
                task_root=task_root,
                task_context={
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "prompt_path": task.prompt_path,
                    "manifest_path": task.manifest_path,
                    "metadata": dict(task.metadata),
                },
                result=review_result,
                candidate_path=candidate_path,
                planned_output_path=result.accepted_path,
            )
            result.review_packet_path = str(generated_review["review_packet_path"])
            result.review_packet_sha256 = str(generated_review["review_packet_sha256"])
            result.review_report_path = str(generated_review["review_report_path"])
            result.review_report_sha256 = str(generated_review["review_report_sha256"])
            result.review_status = str(generated_review["review_status"])
            self._promote_accepted(
                task,
                candidate_path,
                candidate_provenance,
                selected.fixed_gate,
                output_path,
                provenance_path,
                run_id=run_id,
                gate_attempt=selected.gate_attempts,
                candidate_id=selected.candidate_id,
                role_id=selected.role_id,
                candidate_count=candidate_count,
                selection_goal=resolved_selection_goal,
                selection_reason=selection_reason,
                candidate_pool=[
                    {
                        "candidate_id": branch.candidate_id,
                        "role_id": branch.role_id,
                        "candidate_sha256": branch.candidate_sha256,
                        "duplicate_of": branch.duplicate_of,
                        "gate_ok": branch.gate_ok,
                        "execution_status": branch.execution.status,
                        "score": branch.score,
                    }
                    for branch in branches
                ],
                selected_execution=selected.execution,
                generated_review=generated_review,
                overwrite=overwrite,
            )
        except (OSError, PipelineError, GatewayError, ReviewPacketError, json.JSONDecodeError) as exc:
            result.error = f"accepted promotion failed: {exc}"
            self._write_task_summary(task_root, result)
            return result
        result.authoring_accepted = True
        result.execution = selected.execution
        # A target-signature reproducer is intentionally a failing SDK/oracle
        # execution. Its success condition is exact signature + stable replay,
        # already recorded by the fixed host score, not execution.ok.
        if resolved_selection_goal == "must_reproduce_target_signature":
            result.ok = bool(
                result.authoring_accepted
                and selected.score.get("target_signature_match")
                and int(selected.score.get("stable_replay_count") or 0) > 0
            )
        else:
            result.ok = result.authoring_accepted and result.execution.ok
        if not result.ok:
            result.error = result.execution.error or "accepted candidate execution failed"
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
            return ExecutionResult(
                True,
                False,
                "adaptation_required",
                error="needs_harness_extension is intake evidence, not a runnable API adapter",
                candidate_cause="harness_extension_required",
            )
        if jobs <= 0 or timeout_seconds <= 0:
            return ExecutionResult(True, False, "invalid_execution_options", error="jobs and timeout must be positive")
        execution_root = task_root / "execution"
        execution_root.mkdir(parents=True, exist_ok=True)
        if gate.kind == "api_plugin_candidate":
            return self._execute_plugin_candidate(gate, execution_root, timeout_seconds)
        runner_path = _inside(self.repo_root, runner, label="runner") if runner else Path()
        if not runner or not runner_path.is_file():
            return ExecutionResult(True, False, "runner_missing", error="--execute requires an existing --runner")
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
        reduction_root = execution_root / "reductions"
        qualification_root = execution_root / "qualification"
        topotrack_probe_root = execution_root / "topotrack_probe"
        bundle_root = execution_root / "failure_bundles"
        draft_path = execution_root / "bug_record_drafts.json"
        registry_root = execution_root / "failure_registry"
        investigation_root = execution_root / "bug_investigation"
        triage_summary = triage_root / "triage_summary.json"
        has_triage_failures = _triage_has_failures(
            _read_json(triage_summary) if triage_summary.is_file() else {}
        )
        asset_commands_ok = True
        eligible_triage_root = qualification_root / "eligible_triage"
        eligible_failure_count = 0
        execution_artifacts = {
            "cases": _relative(self.repo_root, cases_root),
            "triage": _relative(self.repo_root, triage_root),
        }
        if has_triage_failures:
            qualification_command = self.gates._run(  # noqa: SLF001
                "qualify_failures",
                [
                    self.gates.python_executable,
                    self.gates._tool("qualify_failures.py"),  # noqa: SLF001
                    "--triage",
                    str(triage_root),
                    "--out",
                    str(qualification_root),
                ],
            )
            commands.append(qualification_command)
            asset_commands_ok = asset_commands_ok and qualification_command.ok
            execution_artifacts["qualification"] = _relative(self.repo_root, qualification_root)
            qualification_summary = qualification_root / "qualification_summary.json"
            if qualification_command.ok and qualification_summary.is_file():
                qualification_payload = _read_json(qualification_summary)
                if isinstance(qualification_payload, dict):
                    eligible_failure_count = int(
                        qualification_payload.get("eligible_group_count") or 0
                    )
            topotrack_probe_command = self.gates._run(  # noqa: SLF001
                "probe_topotrack_crashes",
                [
                    self.gates.python_executable,
                    self.gates._tool("probe_topotrack_crashes.py"),  # noqa: SLF001
                    "--runner",
                    str(runner_path),
                    "--summary",
                    str(cases_root / "recipe_summary.json"),
                    "--out",
                    str(topotrack_probe_root),
                    "--timeout",
                    str(timeout_seconds),
                    "--jobs",
                    str(jobs),
                ],
                timeout_seconds=max(self.gates.timeout_seconds, timeout_seconds * 4),
            )
            commands.append(topotrack_probe_command)
            asset_commands_ok = asset_commands_ok and topotrack_probe_command.ok
            execution_artifacts["topotrack_probe"] = _relative(
                self.repo_root,
                topotrack_probe_root,
            )
        seeds_path = eligible_triage_root / "regression_seeds.json"
        seed_payload = _read_json(seeds_path) if seeds_path.is_file() else []
        has_replay_seeds = isinstance(seed_payload, list) and bool(seed_payload)
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
        replay_summary_path = replay_root / "replay_summary.json"
        if self.reduce_failure_candidates and replay_summary_path.is_file():
            reduction_command = self.gates._run(  # noqa: SLF001
                "reduce_replay_failures",
                [
                    self.gates.python_executable,
                    self.gates._tool("reduce_replay_failures.py"),  # noqa: SLF001
                    "--runner",
                    str(runner_path),
                    "--replay",
                    str(replay_summary_path),
                    "--out",
                    str(reduction_root),
                    "--limit",
                    str(self.reduction_limit),
                    "--max-trials",
                    str(self.reduction_max_trials),
                    "--timeout",
                    str(timeout_seconds),
                ],
                timeout_seconds=max(
                    self.gates.timeout_seconds,
                    timeout_seconds
                    * self.reduction_max_trials
                    * max(1, self.reduction_limit or eligible_failure_count)
                    + 120.0,
                ),
            )
            commands.append(reduction_command)
            # Return code 2 means the fixed reducer completed but one or more
            # attempted simplifications changed the signature. The original
            # three-replay recipe remains the canonical reproducer, so this is
            # evidence rather than a pipeline infrastructure failure.
            asset_commands_ok = asset_commands_ok and reduction_command.returncode in {0, 2}
            execution_artifacts["reductions"] = _relative(self.repo_root, reduction_root)
        if eligible_failure_count > 0:
            bundle_argv = [
                self.gates.python_executable,
                self.gates._tool("export_failure_bundles.py"),  # noqa: SLF001
                "--triage",
                str(eligible_triage_root),
                "--out",
                str(bundle_root),
            ]
            if (replay_root / "replay_summary.json").is_file():
                bundle_argv.extend(["--replay", str(replay_root)])
            if (reduction_root / "reduction_index.json").is_file():
                bundle_argv.extend(["--reductions", str(reduction_root)])
            if (topotrack_probe_root / "topotrack_probe_summary.json").is_file():
                bundle_argv.extend(["--topotrack-probe", str(topotrack_probe_root)])
            bundle_command = self.gates._run("export_failure_bundles", bundle_argv)  # noqa: SLF001
            commands.append(bundle_command)
            asset_commands_ok = asset_commands_ok and bundle_command.ok
            draft_argv = [
                self.gates.python_executable,
                self.gates._tool("export_bug_record_drafts.py"),  # noqa: SLF001
                "--out",
                str(draft_path),
                "--bug-prefix",
                "message_pipeline_candidate",
            ]
            if (bundle_root / "bundle_index.json").is_file():
                draft_argv.extend(["--bundle-index", str(bundle_root)])
            draft_command = self.gates._run("export_bug_record_drafts", draft_argv)  # noqa: SLF001
            commands.append(draft_command)
            asset_commands_ok = asset_commands_ok and draft_command.ok
            registry_argv = [
                self.gates.python_executable,
                self.gates._tool("collect_failure_registry.py"),  # noqa: SLF001
                "--out",
                str(registry_root),
            ]
            if (bundle_root / "bundle_index.json").is_file():
                registry_argv.extend(["--bundle-index", str(bundle_root)])
            registry_command = self.gates._run("collect_failure_registry", registry_argv)  # noqa: SLF001
            commands.append(registry_command)
            asset_commands_ok = asset_commands_ok and registry_command.ok
            execution_artifacts.update(
                {
                    "failure_bundles": _relative(self.repo_root, bundle_root),
                    "bug_record_drafts": _relative(self.repo_root, draft_path),
                    "failure_registry": _relative(self.repo_root, registry_root),
                }
            )
            if self.enable_bug_investigation and (bundle_root / "bundle_index.json").is_file():
                investigation_argv = [
                    self.gates.python_executable,
                    self.gates._tool("run_bug_investigation.py"),  # noqa: SLF001
                    "--profile",
                    self.config.profile.name,
                    "--bundle-index",
                    str(bundle_root / "bundle_index.json"),
                    "--out",
                    str(investigation_root),
                    "--parallelism",
                    str(self.bug_investigator_parallelism),
                    "--max-rounds",
                    str(self.bug_investigation_max_rounds),
                    "--max-tool-calls",
                    str(self.bug_investigation_max_tool_calls),
                    "--max-tokens",
                    str(self.bug_investigation_max_tokens),
                ]
                for source_root in self.bug_source_roots:
                    investigation_argv.extend(["--source-root", str(source_root)])
                for role_id in self.bug_investigator_roles:
                    investigation_argv.extend(["--role", role_id])
                investigation_command = self.gates._run(  # noqa: SLF001
                    "run_bug_investigation",
                    investigation_argv,
                    timeout_seconds=max(
                        self.gates.timeout_seconds,
                        self.config.request_timeout_seconds
                        * self.bug_investigation_max_rounds
                        * max(1, len(self.bug_investigator_roles) or len(BUG_INVESTIGATOR_ROLES)),
                    ),
                )
                commands.append(investigation_command)
                asset_commands_ok = asset_commands_ok and investigation_command.ok
                execution_artifacts["bug_investigation"] = _relative(
                    self.repo_root,
                    investigation_root,
                )
        execution_ok = run_command.ok and not has_triage_failures and asset_commands_ok
        if execution_ok:
            status = "passed"
            error = ""
            candidate_cause = ""
        elif has_triage_failures and asset_commands_ok and eligible_failure_count == 0:
            status = "test_or_oracle_defects_qualified"
            error = "test/oracle execution failed, but deterministic qualification excluded every SDK bug candidate"
            candidate_cause = "test_generation_or_oracle_defect"
        elif has_triage_failures and asset_commands_ok:
            status = "sdk_or_oracle_failures_triaged"
            error = "SDK/oracle tests returned nonzero; inspect qualification, replay, and candidate failure assets"
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

    def _execute_plugin_candidate(
        self,
        gate: FixedGateResult,
        execution_root: Path,
        timeout_seconds: float,
    ) -> ExecutionResult:
        materialized = gate.artifacts.get("materialized_plugin", "")
        if not materialized:
            return ExecutionResult(
                True,
                False,
                "plugin_materialization_missing",
                error="fixed plugin gate did not produce a materialized plugin",
            )
        plugin_path = _inside(self.repo_root, materialized, label="materialized_plugin")
        build_root = execution_root / "plugin_build"
        command = self.gates._run(  # noqa: SLF001
            "build_api_plugin_candidate",
            [
                self.gates.python_executable,
                self.gates._tool("build_api_plugin_candidate.py"),  # noqa: SLF001
                "--plugin",
                str(plugin_path),
                "--out",
                str(build_root),
                "--smoke-replays",
                "3",
                "--timeout",
                str(max(120.0, timeout_seconds)),
            ],
            timeout_seconds=max(self.gates.timeout_seconds, max(120.0, timeout_seconds) * 6),
        )
        report_path = build_root / "plugin_build_report.json"
        report = _read_json(report_path) if report_path.is_file() else {}
        report_sha256 = _sha256_bytes(report_path.read_bytes()) if report_path.is_file() else ""
        required_hashes = (
            report.get("runner_sha256"),
            report.get("runtime_registry_sha256"),
            (report.get("sdk_identity") or {}).get("sha256")
            if isinstance(report.get("sdk_identity"), dict)
            else "",
        ) if isinstance(report, dict) else ()
        semantic_hashes = report.get("semantic_hashes") if isinstance(report, dict) else []
        ok = bool(
            command.ok
            and isinstance(report, dict)
            and report.get("ok") is True
            and report.get("stable_semantic_evidence") is True
            and report.get("smoke_replays") == 3
            and isinstance(semantic_hashes, list)
            and len(semantic_hashes) == 3
            and len(set(semantic_hashes)) == 1
            and all(isinstance(value, str) and len(value) == 64 for value in required_hashes)
        )
        return ExecutionResult(
            True,
            ok,
            "passed" if ok else "plugin_build_or_smoke_failed",
            [command],
            {
                "plugin_build": _relative(self.repo_root, build_root),
                "plugin_build_report": _relative(self.repo_root, report_path),
                "plugin_build_report_sha256": report_sha256,
                "runner_sha256": report.get("runner_sha256", "") if isinstance(report, dict) else "",
                "runtime_registry_sha256": (
                    report.get("runtime_registry_sha256", "") if isinstance(report, dict) else ""
                ),
                "sdk_identity_sha256": (
                    (report.get("sdk_identity") or {}).get("sha256", "")
                    if isinstance(report, dict) and isinstance(report.get("sdk_identity"), dict)
                    else ""
                ),
                "semantic_sha256": semantic_hashes[0] if ok else "",
            },
            "" if ok else "materialized API plugin failed isolated build/runtime/smoke gates",
            "" if ok else "harness_adapter_candidate_requires_repair",
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
        candidate_count: int = 1,
        candidate_parallelism: int = 1,
        authoring_roles: Sequence[str] = (),
        selection_goal: str = "auto",
        target_failure_signature: Mapping[str, Any] | None = None,
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
                    candidate_count=candidate_count,
                    candidate_parallelism=candidate_parallelism,
                    authoring_roles=authoring_roles,
                    selection_goal=selection_goal,
                    target_failure_signature=target_failure_signature,
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
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="Candidate output budget; default 32768 for every Message API endpoint profile",
    )
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-contract-repairs", type=int, default=1)
    parser.add_argument("--max-gate-repairs", type=int, default=2)
    parser.add_argument(
        "--candidate-count",
        type=int,
        default=0,
        help="Independent authoring subagents per task; default is 3 for every endpoint profile",
    )
    parser.add_argument(
        "--candidate-parallelism",
        type=int,
        default=0,
        help="Maximum concurrent Message API candidate branches; default is min(candidate-count, 3)",
    )
    parser.add_argument(
        "--authoring-role",
        action="append",
        choices=sorted(AUTHORING_SUBAGENT_ROLES),
        default=[],
        help="Role cycle for independent candidates; repeat to set an explicit role order",
    )
    parser.add_argument(
        "--selection-goal",
        choices=sorted(SELECTION_GOALS),
        default="auto",
        help="Fixed host-side candidate eligibility goal; the model cannot override it",
    )
    parser.add_argument(
        "--target-failure-signature",
        default="",
        help="Trusted JSON failure signature required by must_reproduce_target_signature",
    )
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
    parser.add_argument(
        "--analyze-bugs",
        action="store_true",
        help="After qualification/replay, run parallel candidate-only Qwen investigators on eligible bundles",
    )
    parser.add_argument(
        "--bug-source-root",
        action="append",
        default=[],
        help="Trusted read-only SDK source snapshot root for opaque source search tools",
    )
    parser.add_argument(
        "--bug-investigator-role",
        action="append",
        choices=sorted(BUG_INVESTIGATOR_ROLES),
        default=[],
    )
    parser.add_argument("--bug-investigator-parallelism", type=int, default=4)
    parser.add_argument("--bug-investigation-max-rounds", type=int, default=16)
    parser.add_argument("--bug-investigation-max-tool-calls", type=int, default=32)
    parser.add_argument(
        "--bug-investigation-max-tokens",
        type=int,
        default=0,
        help="Per-investigator output budget; default 32768 for every endpoint profile",
    )
    parser.add_argument(
        "--reduce-bug-candidates",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Minimize stable eligible failures before bundling; defaults on with --analyze-bugs",
    )
    parser.add_argument("--reduction-limit", type=int, default=3)
    parser.add_argument("--reduction-max-trials", type=int, default=32)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_gateway_config(
            args.profile,
            request_timeout_seconds=args.request_timeout,
            max_retries=args.max_retries,
            backoff_base_seconds=args.backoff_base,
            max_retry_delay_seconds=args.max_retry_delay,
            response_bytes_limit=args.response_bytes_limit,
        )
        authoring_max_tokens = args.max_tokens or 32_768
        investigation_max_tokens = args.bug_investigation_max_tokens or 32_768
        if authoring_max_tokens <= 0 or investigation_max_tokens <= 0:
            raise PipelineError("Message API token budgets must be positive")
        options = CompletionOptions(
            response_mode=args.response_mode,
            temperature=args.temperature,
            max_tokens=authoring_max_tokens,
            thinking_mode=args.thinking_mode,
            seed=args.seed,
        )
        candidate_count = args.candidate_count or 3
        candidate_parallelism = args.candidate_parallelism or min(candidate_count, 3)
        target_failure_signature: dict[str, Any] = {}
        if args.target_failure_signature:
            loaded_signature = _read_json(
                _inside(
                    REPO_ROOT,
                    args.target_failure_signature,
                    label="target_failure_signature",
                )
            )
            if not isinstance(loaded_signature, dict):
                raise PipelineError("target failure signature must be a JSON object")
            target_failure_signature = dict(
                loaded_signature.get("failure_signature", loaded_signature)
            )
        pipeline = MessageHarnessPipeline(
            config,
            repo_root=REPO_ROOT,
            staging_root=args.staging_root,
            gate_timeout_seconds=args.gate_timeout,
            enable_bug_investigation=args.analyze_bugs,
            bug_source_roots=args.bug_source_root,
            bug_investigator_roles=args.bug_investigator_role,
            bug_investigator_parallelism=args.bug_investigator_parallelism,
            bug_investigation_max_rounds=args.bug_investigation_max_rounds,
            bug_investigation_max_tool_calls=args.bug_investigation_max_tool_calls,
            bug_investigation_max_tokens=investigation_max_tokens,
            reduce_failure_candidates=(
                args.analyze_bugs
                if args.reduce_bug_candidates is None
                else args.reduce_bug_candidates
            ),
            reduction_limit=args.reduction_limit,
            reduction_max_trials=args.reduction_max_trials,
        )
        result = pipeline.run_manifest(
            args.manifest,
            run_id=args.run_id or None,
            task_ids=args.task_id,
            completion_options=options,
            max_contract_repairs=args.max_contract_repairs,
            max_gate_repairs=args.max_gate_repairs,
            candidate_count=candidate_count,
            candidate_parallelism=candidate_parallelism,
            authoring_roles=args.authoring_role,
            selection_goal=args.selection_goal,
            target_failure_signature=target_failure_signature,
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
