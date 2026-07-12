"""Immutable review-session orchestration for one public SGGK function.

The ordinary user supplies only a public-function name and, after the first
review packet exists, natural-language comments.  Forms, task identifiers,
round identifiers, hashes, approval attestations, runner bindings, and Message
API parameters are host-owned implementation details.

This module deliberately separates three authorities:

* Qwen proposes and revises test artifacts through the Message API;
* fixed host code validates, hashes, stores, and interprets state transitions;
* the SDK runner is not reachable until a hash-bound approval attestation has
  been created for the latest immutable round.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from test_harness.authoring_gateway.config import PROFILE_SPECS
from test_harness.orchestration.source_discovery import (
    SourceDiscoveryError,
    discover_function_definitions,
    discover_header_declarations,
    path_identity,
)

PUBLIC_FUNCTION_RE = re.compile(
    r"^(?:~?[A-Za-z_][A-Za-z0-9_]*)(?:::~?[A-Za-z_][A-Za-z0-9_]*)*$"
)
TERMINAL_STATES = frozenset({"completed", "rejected", "generation_failed"})
COMMENTABLE_STATES = frozenset({"awaiting_comment", "execution_failed", "completed"})
TRANSIENT_STATES = frozenset({"created", "generating", "interpreting_comment", "executing"})
STALE_LOCK_GRACE_SECONDS = 30.0
APPROVAL_PATTERNS = (
    re.compile(r"(?:批准|同意|确认|可以|请|现在).{0,10}(?:执行|运行|开始测试|实测)"),
    re.compile(r"(?:开始|继续)(?:执行|运行|测试)"),
    re.compile(r"(?i)\b(?:approve(?:d)?|go\s+ahead|start|please)\b.{0,24}\b(?:execute|run|test)\b"),
)
EXECUTION_DENIAL_PATTERNS = (
    re.compile(
        r"(?:不要|不可以|不能|不可|不许|禁止|拒绝|暂不|暂缓|停止|取消|先不|先别|别|勿|"
        r"尚未|还未|没有|未|不)"
        r".{0,16}(?:执行|运行|开始测试|实测|测试)"
    ),
    re.compile(
        r"(?i)\b(?:do\s+not|don['’]?t|must\s+not|should\s+not|cannot|can['’]?t|"
        r"never|not\s+yet|not|hold\s+off|stop)\b.{0,32}\b(?:execute|run|test|start)\b"
    ),
)
EXECUTION_QUESTION_PATTERNS = (
    re.compile(
        r"(?:是否|能否|可否|要不要|能不能|可不可以|是不是).{0,16}"
        r"(?:执行|运行|开始测试|实测|测试)"
    ),
    re.compile(
        r"(?:执行|运行|开始测试|实测|测试).{0,12}(?:吗|么|呢|行不行|可以吗|[?？])"
    ),
    re.compile(
        r"(?i)\b(?:can|could|should|may|would|will|do|does|is|are)\b.{0,40}"
        r"\b(?:execute|run|test|start)\b[^?]{0,24}\?"
    ),
    re.compile(r"(?i)\b(?:execute|run|test|start)\b[^?]{0,24}\?"),
)
REVISION_PATTERNS = (
    re.compile(r"(?:增加|新增|补充|修改|调整|删除|移除|替换|改成|改为|再加|需要改)"),
    re.compile(r"(?i)\b(?:add|change|revise|modify|remove|replace|adjust)\b"),
)
SENSITIVE_OUTLINE_KEYS = frozenset(
    {
        "command",
        "commands",
        "argv",
        "env",
        "cwd",
        "runner",
        "executable",
        "api_key",
        "authorization",
        "password",
        "secret",
        "token",
        "credential",
        "source_file",
        "sdk_source_refs",
        "path",
    }
)
class WorkflowError(ValueError):
    """A workflow transition cannot be completed safely."""


class WorkflowRuntime(Protocol):
    """Provider/runner boundary used by the deterministic session engine."""

    def generate(
        self,
        *,
        manifest_path: Path,
        run_id: str,
        staging_root: Path,
    ) -> Mapping[str, Any]: ...

    def interpret_comment(
        self,
        *,
        comment: str,
        session: Mapping[str, Any],
        round_record: Mapping[str, Any],
        subject_outline: Mapping[str, Any],
        output_dir: Path,
    ) -> Mapping[str, Any]: ...

    def execute(
        self,
        *,
        manifest_path: Path,
        run_id: str,
        staging_root: Path,
        runner_path: Path | None,
    ) -> Mapping[str, Any]: ...


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


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
    _atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _write_text(path: Path, value: str) -> None:
    _atomic_write(path, value.encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise WorkflowError(f"JSON root must be an object: {path}")
    return value


def _safe_id(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "_.-" else "_" for char in value)
    return safe.strip("._-") or "task"


def _repo_relative(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise WorkflowError(f"path escapes repository: {path}") from exc


def _repo_path(repo_root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise WorkflowError(f"{label} is missing")
    raw = Path(value)
    path = raw.resolve() if raw.is_absolute() else (repo_root / raw).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise WorkflowError(f"{label} escapes repository") from exc
    return path


def _extract_api_name(public_function: str, known_apis: Sequence[str]) -> str:
    leaf = public_function.rsplit("::", 1)[-1]
    if leaf in known_apis:
        return leaf
    if public_function in known_apis:
        return public_function
    return leaf


def _header_declarations(
    public_function: str,
    sdk_dir: Path | None,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    include_root = sdk_dir / "include" if sdk_dir is not None else None
    try:
        return discover_header_declarations(public_function, include_root, limit=limit)
    except SourceDiscoveryError as exc:
        raise WorkflowError(str(exc)) from exc


def _source_occurrences(
    public_function: str,
    source_root: Path | None,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    try:
        return discover_function_definitions(public_function, source_root, limit=limit)
    except SourceDiscoveryError as exc:
        raise WorkflowError(str(exc)) from exc


def resolve_public_function(
    public_function: str,
    capabilities: Mapping[str, Any],
    *,
    sdk_dir: Path | None = None,
    source_root: Path | None = None,
    expose_declarations: bool = True,
) -> dict[str, Any]:
    """Resolve one user function name into host-owned route evidence."""

    value = public_function.strip()
    if not PUBLIC_FUNCTION_RE.fullmatch(value):
        raise WorkflowError(
            "public function must be an identifier or namespace-qualified identifier; "
            "do not provide a path, command, or full signature"
        )
    apis_raw = capabilities.get("apis")
    apis = apis_raw if isinstance(apis_raw, Mapping) else {}
    target_api = _extract_api_name(value, tuple(str(key) for key in apis))
    capability = apis.get(target_api)
    builtin = isinstance(capability, Mapping) and target_api != "needs_harness_extension"
    declarations = _header_declarations(value, sdk_dir) if expose_declarations else []
    source_occurrences = _source_occurrences(value, source_root) if expose_declarations else []
    route = (
        "checked_plugin_form"
        if builtin and isinstance(capability.get("plugin"), Mapping)
        else "builtin_form" if builtin else "extension_backlog"
    )
    if builtin and not bool(capability.get("runner_recipe_api", False)):
        route = "extension_backlog"
    unsigned = {
        "schema_version": 1,
        "requested_public_function": value,
        "resolved_api": target_api,
        "route": route,
        "capability_available": builtin,
        "declarations": declarations,
        "source_occurrences": source_occurrences,
    }
    return {**unsigned, "resolution_sha256": _sha256_json(unsigned)}


def _tolerance_focus(target_api: str) -> list[str]:
    if "boolean" in target_api:
        return ["exact_contact", "geom_tol", "topo_tol", "generated_topology"]
    if "offset2d" in target_api:
        return ["distance_tolerance", "connectivity", "curve_degeneration"]
    if "offset" in target_api:
        return ["distance_tolerance", "positive_offset", "analytic_extent"]
    if "import" in target_api or "roundtrip" in target_api:
        return ["roundtrip_drift", "import_topology"]
    return ["strict_recipe_schema"]


def build_internal_form(
    resolution: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    """Create the broad internal intent envelope that Qwen turns into cases."""

    target_api = str(resolution.get("resolved_api") or "needs_harness_extension")
    apis = capabilities.get("apis") if isinstance(capabilities.get("apis"), Mapping) else {}
    raw = apis.get(target_api)
    capability = raw if isinstance(raw, Mapping) else {}
    builders = [str(item) for item in capability.get("supported_body_builders", []) if item]
    oracles = [str(item) for item in capability.get("supported_oracles", []) if item]
    if not oracles:
        oracles = ["topocheck"]
    body_required = [str(item) for item in capability.get("body_required", []) if item]
    geometry: dict[str, Any] = {
        "family": "qwen_risk_driven",
        "parameter_notes": (
            "Qwen must choose runnable nominal, negative, degenerate, tolerance-boundary, "
            "and large-coordinate variants from the fixed Harness capabilities."
        ),
    }
    if builders:
        geometry["target_builder"] = builders[0]
        if "tool" in body_required or len(builders) > 1:
            geometry["tool_builder"] = builders[1] if len(builders) > 1 else builders[0]
    declarations = resolution.get("declarations")
    source_refs: list[str] = []
    if isinstance(declarations, list):
        for item in declarations:
            if not isinstance(item, Mapping):
                continue
            source_refs.append(
                f"{item.get('function_ref_id')}:{item.get('header')}:{item.get('line')}:"
                f"{item.get('declaration')}"
            )
    return {
        "request_id": request_id,
        "owner": "harness_session_host",
        "target_api": target_api,
        "sdk_source_refs": source_refs,
        "test_goal": (
            "由 Qwen 依据接口能力、声明和固定示例自动设计可执行的风险驱动测试；"
            "普通用户不负责选择 builder、oracle、容差、用例数量或执行参数。"
        ),
        "risk_summary": (
            "覆盖正常语义、非法输入、退化输入、容差两侧、生成拓扑、结果为空、"
            "大坐标与重复执行确定性；未知能力必须明确提出最小 Harness 扩展。"
        ),
        "geometry": geometry,
        "tolerance_focus": _tolerance_focus(target_api),
        "oracles": oracles,
        "expected_behavior": (
            "Qwen 必须把每个预期转成可测 oracle，不得只检查 API 返回状态；"
            "不确定的 SDK 语义必须在审查报告中标为待确认假设。"
        ),
        "case_count": 12,
        "run_profile": "matrix",
        "input_assets": {},
        "notes": (
            "这是 Harness 自动创建的内部 IR，不是用户表单。Qwen 可在固定能力边界内"
            "决定完整用例设计，宿主负责门禁、哈希、审查轮次和执行。"
        ),
    }


def _sanitize_outline(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "<depth-limited>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered in SENSITIVE_OUTLINE_KEYS or lowered.endswith("_path"):
                continue
            else:
                result[key] = _sanitize_outline(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize_outline(item, depth=depth + 1) for item in value[:300]]
    if isinstance(value, str):
        if re.fullmatch(r"[0-9A-Fa-f]{64}", value):
            return "<host-bound-hash>"
        if re.search(r"(?i)\b(?:https?|ftp|ssh)://", value):
            return "<host-managed-location>"
        if re.search(r"(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/])", value):
            return "<host-managed-location>"
        if re.search(r"(?:[A-Za-z0-9_.-]+[\\/])+(?:[A-Za-z0-9_.-]+)", value):
            return "<host-managed-location>"
        if re.search(
            r"(?i)(?:^|[\s`'\"])(?:powershell|pwsh|cmd(?:\.exe)?|bash|sh\s+-c|curl|wget|git|python(?:\.exe)?|node|npm|cmake|ninja)(?:\s|$)",
            value,
        ):
            return "<host-managed-instruction>"
        if len(value) > 4000:
            return value[:4000] + "<truncated>"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _bounded_subject_outline(value: Mapping[str, Any], *, limit: int = 28_000) -> dict[str, Any]:
    """Keep enough semantic context for comments while excluding huge payloads."""

    outline = dict(value)
    if len(_canonical_json_bytes(outline)) <= limit:
        return outline
    candidate = outline.get("candidate") if isinstance(outline.get("candidate"), Mapping) else {}
    kind = str(candidate.get("kind") or "")
    raw_cases: list[Any] = []
    source_review: Mapping[str, Any] = {}
    if kind == "attack_dsl":
        dsl = candidate.get("dsl") if isinstance(candidate.get("dsl"), Mapping) else {}
        raw_cases = list(dsl.get("cases")) if isinstance(dsl.get("cases"), list) else []
        source_review = (
            dsl.get("source_review") if isinstance(dsl.get("source_review"), Mapping) else {}
        )
    elif kind == "flat_recipe":
        recipe = candidate.get("recipe") if isinstance(candidate.get("recipe"), Mapping) else {}
        raw_cases = [recipe]
        source_review = (
            recipe.get("source_review")
            if isinstance(recipe.get("source_review"), Mapping)
            else {}
        )
    elif isinstance(candidate.get("source_review"), Mapping):
        source_review = candidate["source_review"]
    compact_cases: list[dict[str, Any]] = []
    for raw in raw_cases[:64]:
        if not isinstance(raw, Mapping):
            continue
        compact_cases.append(
            {
                key: _sanitize_outline(raw.get(key))
                for key in (
                    "case_id",
                    "api",
                    "variant",
                    "hypothesis",
                    "target_kind",
                    "tool_kind",
                    "expectations",
                    "sweeps",
                    "paired_sweeps",
                )
                if key in raw
            }
        )
    plan = outline.get("internal_plan") if isinstance(outline.get("internal_plan"), Mapping) else {}
    compact = {
        "target": outline.get("target"),
        "resolved_api": outline.get("resolved_api"),
        "route": outline.get("route"),
        "plan_summary": {
            key: plan.get(key)
            for key in ("target_api", "test_goal", "risk_summary", "geometry", "oracles", "tolerance_focus")
            if key in plan
        },
        "candidate_summary": {
            "kind": kind,
            "notes": candidate.get("notes", []),
            "cases": compact_cases,
            "source_review": {
                key: _sanitize_outline(source_review.get(key))
                for key in (
                    "summary",
                    "risky_branches",
                    "failure_hypotheses",
                    "test_enhancements",
                )
                if key in source_review
            },
        },
        "machine_verification": outline.get("machine_verification", {}),
        "previous_interpretation": outline.get("previous_interpretation", {}),
        "outline_compacted": True,
    }
    encoded = _canonical_json_bytes(compact)
    if len(encoded) > limit:
        compact["candidate_summary"]["cases"] = [
            {
                key: case.get(key)
                for key in ("case_id", "api", "variant", "hypothesis")
                if key in case
            }
            for case in compact_cases[:32]
        ]
        compact["candidate_summary"]["notes"] = []
        source_summary = compact["candidate_summary"].get("source_review")
        if isinstance(source_summary, dict):
            for key in ("risky_branches", "failure_hypotheses", "test_enhancements"):
                if isinstance(source_summary.get(key), list):
                    source_summary[key] = source_summary[key][:8]
    if len(_canonical_json_bytes(compact)) > limit:
        compact["candidate_summary"]["cases"] = compact["candidate_summary"]["cases"][:8]
        compact["plan_summary"] = {
            "target_api": compact["plan_summary"].get("target_api"),
            "oracles": compact["plan_summary"].get("oracles", []),
        }
    return compact


class _WorkspaceLock(AbstractContextManager["_WorkspaceLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def __enter__(self) -> _WorkspaceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        for _attempt in range(3):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                if not self._remove_if_stale():
                    raise WorkflowError(
                        "another Harness session operation is running; wait for it to finish before retrying"
                    ) from exc
        if descriptor is None:
            raise WorkflowError("could not acquire the Harness workspace lock safely")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()} started_at={_utc_now()}\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise
        self.acquired = True
        return self

    def _remove_if_stale(self) -> bool:
        """Reclaim dead-owner locks and conservatively aged incomplete records."""

        try:
            text = self.path.read_text(encoding="utf-8", errors="strict")
        except FileNotFoundError:
            return True
        except (OSError, UnicodeError):
            return self._quarantine_expired_invalid_lock()
        match = re.fullmatch(
            r"pid=(\d+) started_at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\n",
            text,
        )
        if match is None:
            return self._quarantine_expired_invalid_lock()
        pid = int(match.group(1))
        if pid <= 0:
            return self._quarantine_expired_invalid_lock()
        if self._pid_is_alive(pid):
            return False
        return self._quarantine_lock("dead_owner")

    def _quarantine_expired_invalid_lock(self) -> bool:
        try:
            age = datetime.now(UTC).timestamp() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if age < STALE_LOCK_GRACE_SECONDS:
            return False
        return self._quarantine_lock("invalid_owner")

    def _quarantine_lock(self, reason: str) -> bool:
        quarantine = self.path.parent / ".stale_locks"
        try:
            quarantine.mkdir(parents=True, exist_ok=True)
            destination = quarantine / (
                f"{self.path.name}.{reason}.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}."
                f"{uuid.uuid4().hex[:8]}"
            )
            os.replace(self.path, destination)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid == os.getpid():
            return True
        if os.name == "nt":
            # ``os.kill(pid, 0)`` is not a portable liveness probe on Windows.
            # Query the process handle without requesting mutation rights.
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process_query_limited_information = 0x1000
            still_active = 259
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                # Access denied proves that a process object exists; every other
                # failure means there is no live owner we can safely identify.
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
        self.acquired = False


@dataclass(frozen=True)
class SessionPaths:
    repo_root: Path
    sessions_root: Path
    session_root: Path

    @property
    def session_file(self) -> Path:
        return self.session_root / "session.json"

    @property
    def events_root(self) -> Path:
        return self.session_root / "events"

    def round_root(self, number: int) -> Path:
        return self.session_root / "rounds" / f"{number:04d}"


class HarnessWorkflow:
    """Deterministic session state machine backed by a Message API runtime."""

    def __init__(
        self,
        runtime: WorkflowRuntime,
        *,
        repo_root: str | Path,
        capabilities_path: str | Path = "test_harness/interface_capabilities.json",
        sessions_root: str | Path = "artifacts/harness_sessions",
        profile: str = "intranet",
        sdk_dir: str | Path | None = None,
        source_root: str | Path | None = None,
        runner_path: str | Path | None = None,
    ) -> None:
        self.runtime = runtime
        self.repo_root = Path(repo_root).resolve()
        self.capabilities_path = _repo_path(
            self.repo_root, str(capabilities_path), label="capabilities_path"
        )
        base_capabilities = _read_json(self.capabilities_path)
        import sys

        tools_root = self.repo_root / "test_harness" / "tools"
        if str(tools_root) not in sys.path:
            sys.path.insert(0, str(tools_root))
        from plugin_catalog import merge_capabilities

        self.capabilities = merge_capabilities(
            base_capabilities,
            self.repo_root / "test_harness" / "api_plugins",
        )
        self.sessions_root = _repo_path(self.repo_root, str(sessions_root), label="sessions_root")
        artifacts_root = (self.repo_root / "artifacts").resolve()
        try:
            self.sessions_root.relative_to(artifacts_root)
        except ValueError as exc:
            raise WorkflowError("sessions_root must stay under repository artifacts/") from exc
        self.profile = profile.strip()
        profile_spec = PROFILE_SPECS.get(self.profile)
        if profile_spec is None:
            raise WorkflowError(f"unknown workflow provider profile: {self.profile!r}")
        self.profile_category = profile_spec.category
        runtime_profile = str(getattr(runtime, "provider_profile", "") or "")
        runtime_category = str(getattr(runtime, "provider_profile_category", "") or "")
        if runtime_profile and runtime_profile != self.profile:
            raise WorkflowError(
                "workflow profile does not match the Message API runtime provider profile"
            )
        if runtime_category and runtime_category != self.profile_category:
            raise WorkflowError(
                "workflow profile category does not match the Message API runtime category"
            )
        self.sdk_dir = Path(sdk_dir).resolve() if sdk_dir else None
        if self.sdk_dir is not None and not self.sdk_dir.is_dir():
            raise WorkflowError(f"SDK directory does not exist: {self.sdk_dir}")
        self.source_root = Path(source_root).resolve() if source_root else None
        if self.source_root is not None and not self.source_root.is_dir():
            raise WorkflowError(f"source root does not exist: {self.source_root}")
        if self.profile != "intranet":
            self.sdk_dir = None
            self.source_root = None
        self.sdk_dir_identity = path_identity(self.sdk_dir)
        self.source_root_identity = path_identity(self.source_root)
        self.runner_path = Path(runner_path).resolve() if runner_path else None
        if self.runner_path is not None:
            try:
                self.runner_path.relative_to(self.repo_root)
            except ValueError as exc:
                raise WorkflowError(
                    "SGGK runner must stay inside the repository so approval and execution use the same path policy"
                ) from exc
        self.active_path = self.sessions_root / "active.json"
        self.lock_path = self.sessions_root / ".workflow.lock"

    def _paths(self, session_id: str) -> SessionPaths:
        root = (self.sessions_root / _safe_id(session_id)).resolve()
        try:
            root.relative_to(self.sessions_root.resolve())
        except ValueError as exc:
            raise WorkflowError("session id escapes session root") from exc
        return SessionPaths(self.repo_root, self.sessions_root, root)

    def _load_active(self) -> tuple[dict[str, Any], SessionPaths]:
        if not self.active_path.is_file():
            raise WorkflowError("no active Harness session; run start <public-function> first")
        pointer = _read_json(self.active_path)
        paths = self._paths(str(pointer.get("session_id") or ""))
        if not paths.session_file.is_file():
            raise WorkflowError("active session pointer is stale")
        session = _read_json(paths.session_file)
        if session.get("session_id") != pointer.get("session_id"):
            raise WorkflowError("active session identity mismatch")
        self._verify_event_head(session, paths)
        return session, paths

    def _assert_session_provider(self, session: Mapping[str, Any]) -> None:
        session_profile = str(session.get("provider_profile") or session.get("profile") or "")
        session_category = str(session.get("provider_profile_category") or "")
        if session_profile != self.profile:
            raise WorkflowError(
                "active session belongs to a different Message API provider profile"
            )
        if session_category and session_category != self.profile_category:
            raise WorkflowError(
                "active session belongs to a different Message API profile category"
            )
        if (
            session.get("data_classification") == "proprietary_source"
            and self.profile_category != "intranet"
        ):
            raise WorkflowError(
                "proprietary source session cannot continue on an external Message API profile"
            )
        if str(session.get("source_root_identity") or "") != self.source_root_identity:
            raise WorkflowError(
                "active session source root changed; restart the API review with the original source root"
            )
        if str(session.get("sdk_dir_identity") or "") != self.sdk_dir_identity:
            raise WorkflowError(
                "active session SDK directory changed; restart the API review with the original SDK directory"
            )

    @staticmethod
    def _verify_event_head(
        session: Mapping[str, Any],
        paths: SessionPaths,
        *,
        allow_uncommitted_tail: bool = False,
    ) -> None:
        sequence = int(session.get("event_sequence") or 0)
        head = str(session.get("event_head_sha256") or "")
        event_files = sorted(paths.events_root.glob("*.json"))
        expected_names = {f"{number:06d}.json" for number in range(1, sequence + 1)}
        actual_names = {path.name for path in event_files}
        if sequence == 0:
            if head:
                raise WorkflowError("session has an invalid empty event head")
            if actual_names and not allow_uncommitted_tail:
                raise WorkflowError("session contains an uncommitted event file")
            return
        if not expected_names.issubset(actual_names):
            raise WorkflowError("session contains missing committed event files")
        if actual_names != expected_names and not allow_uncommitted_tail:
            raise WorkflowError("session contains missing or uncommitted event files")
        previous = ""
        for number in range(1, sequence + 1):
            event_path = paths.events_root / f"{number:06d}.json"
            if not event_path.is_file():
                raise WorkflowError(f"session event {number} is missing")
            event = _read_json(event_path)
            supplied_hash = event.get("event_sha256")
            unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            if event.get("sequence") != number:
                raise WorkflowError(f"session event {number} sequence mismatch")
            if event.get("previous_event_sha256") != previous:
                raise WorkflowError(f"session event {number} previous hash mismatch")
            if event.get("payload_sha256") != _sha256_json(payload):
                raise WorkflowError(f"session event {number} payload hash mismatch")
            if supplied_hash != _sha256_json(unsigned):
                raise WorkflowError(f"session event {number} self-hash mismatch")
            previous = str(supplied_hash)
        if previous != head:
            raise WorkflowError("session event head hash mismatch")

    def _load_active_for_update(self) -> tuple[dict[str, Any], SessionPaths]:
        """Load committed state and recover only provably interrupted operations."""

        if not self.active_path.is_file():
            raise WorkflowError("no active Harness session; run start <public-function> first")
        pointer = _read_json(self.active_path)
        paths = self._paths(str(pointer.get("session_id") or ""))
        if not paths.session_file.is_file():
            raise WorkflowError("active session pointer is stale")
        session = _read_json(paths.session_file)
        if session.get("session_id") != pointer.get("session_id"):
            raise WorkflowError("active session identity mismatch")
        self._recover_uncommitted_event_tail(session, paths)
        self._verify_event_head(session, paths)
        self._recover_transient_state(session, paths)
        return session, paths

    def _recover_uncommitted_event_tail(
        self,
        session: Mapping[str, Any],
        paths: SessionPaths,
    ) -> None:
        self._verify_event_head(session, paths, allow_uncommitted_tail=True)
        sequence = int(session.get("event_sequence") or 0)
        expected = {f"{number:06d}.json" for number in range(1, sequence + 1)}
        extras = sorted(path for path in paths.events_root.glob("*.json") if path.name not in expected)
        if not extras:
            return
        quarantine = (
            paths.session_root
            / "recovery"
            / "uncommitted_events"
            / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"
        )
        quarantine.mkdir(parents=True, exist_ok=False)
        for source in extras:
            os.replace(source, quarantine / source.name)

    def _recover_transient_state(self, session: dict[str, Any], paths: SessionPaths) -> None:
        state = str(session.get("state") or "")
        if state not in TRANSIENT_STATES:
            return
        configured = str(session.get("recovery_state") or "")
        if state == "executing":
            recovered = "execution_failed"
        elif configured in COMMENTABLE_STATES | {"generation_failed"}:
            recovered = configured
        elif state in {"created", "generating"} and int(session.get("current_round") or 0) == 0:
            recovered = "generation_failed"
        else:
            recovered = "awaiting_comment"

        artifact_raw = session.get("recovery_artifact_path")
        quarantined_path = ""
        if isinstance(artifact_raw, str) and artifact_raw:
            artifact = _repo_path(self.repo_root, artifact_raw, label="recovery_artifact_path")
            try:
                artifact.relative_to(paths.session_root.resolve())
            except ValueError as exc:
                raise WorkflowError("recovery artifact escapes the active session") from exc
            if artifact.exists():
                quarantine = paths.session_root / "recovery" / "interrupted_artifacts"
                quarantine.mkdir(parents=True, exist_ok=True)
                destination = quarantine / f"{artifact.name}_{uuid.uuid4().hex[:12]}"
                os.replace(artifact, destination)
                quarantined_path = _repo_relative(self.repo_root, destination)

        session["state"] = recovered
        session["last_error"] = (
            f"recovered interrupted Harness state {state!r}; no in-flight operation was resumed automatically"
        )
        session["recovery_state"] = ""
        session["recovery_artifact_path"] = ""
        self._event(
            session,
            paths,
            "INTERRUPTED_OPERATION_RECOVERED",
            {
                "previous_state": state,
                "recovered_state": recovered,
                "quarantined_artifact_path": quarantined_path,
            },
        )
        self._save_session(session, paths)

    def _save_session(self, session: dict[str, Any], paths: SessionPaths) -> None:
        session["updated_at"] = _utc_now()
        _write_json(paths.session_file, session)
        _write_json(
            self.active_path,
            {
                "schema_version": 1,
                "session_id": session["session_id"],
                "updated_at": session["updated_at"],
            },
        )

    def _event(
        self,
        session: dict[str, Any],
        paths: SessionPaths,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        sequence = int(session.get("event_sequence") or 0) + 1
        previous = str(session.get("event_head_sha256") or "")
        body = {
            "schema_version": 1,
            "sequence": sequence,
            "event_type": event_type,
            "previous_event_sha256": previous,
            "payload_sha256": _sha256_json(payload),
            "recorded_at": _utc_now(),
            "payload": dict(payload),
        }
        event_hash = _sha256_json(body)
        event = {**body, "event_sha256": event_hash}
        _write_json(paths.events_root / f"{sequence:06d}.json", event)
        session["event_sequence"] = sequence
        session["event_head_sha256"] = event_hash

    def _new_session(self, public_function: str) -> tuple[dict[str, Any], SessionPaths]:
        if self.active_path.is_file():
            active, _ = self._load_active_for_update()
            if str(active.get("state")) not in TERMINAL_STATES:
                raise WorkflowError(
                    "an active session is still waiting for review; comment on or reject it before starting another"
                )
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        session_id = f"{stamp}_{_safe_id(public_function)[-48:]}_{uuid.uuid4().hex[:8]}"
        paths = self._paths(session_id)
        paths.session_root.mkdir(parents=True, exist_ok=False)
        session = {
            "schema_version": 1,
            "session_id": session_id,
            "public_function": public_function,
            "profile": self.profile,
            "provider_profile": self.profile,
            "provider_profile_category": self.profile_category,
            "data_classification": "public_interface",
            "source_root_identity": self.source_root_identity,
            "sdk_dir_identity": self.sdk_dir_identity,
            "state": "created",
            "current_round": 0,
            "current_round_sha256": "",
            "approved_round": 0,
            "approval_path": "",
            "execution_manifest_path": "",
            "execution_manifest_sha256": "",
            "execution_attempt": 0,
            "current_execution_attempt_path": "",
            "recovery_state": "",
            "recovery_artifact_path": "",
            "final_report_path": "",
            "event_sequence": 0,
            "event_head_sha256": "",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }
        self._event(session, paths, "SESSION_CREATED", {"public_function": public_function})
        self._save_session(session, paths)
        return session, paths

    def start(self, public_function: str) -> dict[str, Any]:
        """Resolve an API and generate the first immutable review round."""

        with _WorkspaceLock(self.lock_path):
            value = public_function.strip()
            # Validate before creating any artifact directory.
            resolution = resolve_public_function(
                value,
                self.capabilities,
                sdk_dir=self.sdk_dir,
                source_root=self.source_root,
                expose_declarations=self.profile == "intranet",
            )
            session, paths = self._new_session(value)
            resolution_path = paths.session_root / "resolution" / "round_0001.json"
            _write_json(resolution_path, resolution)
            session["state"] = "generating"
            session["recovery_state"] = "generation_failed"
            session["recovery_artifact_path"] = _repo_relative(
                self.repo_root, paths.round_root(1)
            )
            self._event(
                session,
                paths,
                "PUBLIC_FUNCTION_RESOLVED",
                {
                    "resolved_api": resolution["resolved_api"],
                    "route": resolution["route"],
                    "resolution_sha256": resolution["resolution_sha256"],
                },
            )
            self._save_session(session, paths)
            return self._generate_round(session, paths, resolution, previous=None, interpretation=None)

    def _build_round_files(
        self,
        session: Mapping[str, Any],
        paths: SessionPaths,
        resolution: Mapping[str, Any],
        number: int,
        previous: Mapping[str, Any] | None,
        interpretation: Mapping[str, Any] | None,
    ) -> tuple[Path, Path, Path, dict[str, Any]]:
        # These modules retain direct imports for their standalone CLI.  Import
        # lazily so the ordinary workflow remains cheap and testable.
        import sys

        tools_root = self.repo_root / "test_harness" / "tools"
        if str(tools_root) not in sys.path:
            sys.path.insert(0, str(tools_root))
        from test_harness.tools.build_api_test_task import build_task, validate_form
        from test_harness.tools.build_model_prompt_pack import (
            campaign_profiles_for,
            contract_for,
            interface_prompt,
            source_prompt,
        )

        round_root = paths.round_root(number)
        round_root.mkdir(parents=True, exist_ok=False)
        task_id = _safe_id(f"{session['session_id']}_r{number:04d}")
        form = build_internal_form(
            resolution,
            self.capabilities,
            request_id=task_id,
        )
        form_path = round_root / "internal" / "api_test_form.json"
        _write_json(form_path, form)
        errors, warnings = validate_form(form)
        if errors:
            raise WorkflowError("host-generated internal form is invalid: " + "; ".join(errors))
        task = build_task(form_path, form, warnings)
        expected_output = round_root / "candidate" / "candidate.json"
        prompt = interface_prompt(task, form, _repo_relative(self.repo_root, expected_output))
        preferred = (task.get("api_guidance") or {}).get("preferred_format")
        output_contract = contract_for(preferred)
        task_type = "interface_form"
        source_metadata: dict[str, Any] = {
            "provider_profile": self.profile,
            "provider_profile_category": self.profile_category,
        }
        if (
            session.get("data_classification") == "proprietary_source"
            or bool(form.get("sdk_source_refs"))
        ):
            source_metadata.update(
                {
                    "data_classification": "proprietary_source",
                    "allowed_profile_categories": ["intranet"],
                }
            )
        occurrences = resolution.get("source_occurrences")
        if (
            self.source_root is not None
            and isinstance(occurrences, list)
            and occurrences
        ):
            from test_harness.authoring_gateway.source_evidence import (
                build_source_contract_from_ranges,
                read_source,
            )

            source_ranges: list[dict[str, Any]] = []
            source_excerpts: list[dict[str, Any]] = []
            source_cache: dict[Path, list[str]] = {}
            for occurrence in occurrences:
                if (
                    not isinstance(occurrence, Mapping)
                    or occurrence.get("definition_kind") != "function_definition"
                ):
                    raise WorkflowError("resolved source occurrence is not a function definition")
                try:
                    source_path = (
                        self.source_root / str(occurrence.get("relative_path") or "")
                    ).resolve(strict=True)
                    source_path.relative_to(self.source_root.resolve(strict=True))
                except (OSError, ValueError) as exc:
                    raise WorkflowError("resolved source occurrence escapes source root") from exc
                if source_path not in source_cache:
                    _data, source_cache[source_path] = read_source(source_path)
                source_lines = source_cache[source_path]
                start = int(occurrence.get("line_start") or 0)
                end = int(occurrence.get("line_end") or 0)
                if not (1 <= start <= end <= len(source_lines)):
                    raise WorkflowError("resolved source definition range is invalid")
                source_ranges.append(
                    {
                        "source_path": source_path,
                        "line_start": start,
                        "line_end": end,
                    }
                )
                source_excerpts.append(
                    {
                        "path": str(occurrence["relative_path"]),
                        "start_line": start,
                        "end_line": end,
                        "signature": str(occurrence.get("signature") or ""),
                        "text": "\n".join(source_lines[start - 1 : end]),
                    }
                )
            finding = {
                "id": _safe_id(f"finding_{task_id}"),
                "severity": "review",
                "suggested_attack_family": form["target_api"],
                "summary": (
                    "Analyze all bound public-function definitions and overload branches, then convert "
                    "at least two falsifiable failure hypotheses into generated cases."
                ),
            }
            source_contract, host_bindings = build_source_contract_from_ranges(
                task_id=task_id,
                finding=finding,
                source_root=self.source_root,
                source_ranges=source_ranges,
            )
            output_contract = contract_for(preferred, source_task=True)
            source_task = {
                "task_id": task_id,
                "model_prompt": prompt,
                "finding": finding,
                "source_contract": source_contract,
                "source_excerpts": source_excerpts,
                "output_contract": output_contract,
                "post_generation_checks": [
                    "bind every source branch to at least two hypotheses",
                    "bind every enhancement to exact generated case IDs",
                    "revalidate source bytes before acceptance and execution",
                ],
            }
            prompt = source_prompt(
                source_task,
                _repo_relative(self.repo_root, expected_output),
            )
            task_type = "source_attack"
            source_metadata.update(
                {
                    "data_classification": "proprietary_source",
                    "allowed_profile_categories": ["intranet"],
                    "source_contract": source_contract,
                    "host_source_bindings": host_bindings,
                }
            )
        if previous is not None and interpretation is not None:
            previous_candidate = _repo_path(
                self.repo_root,
                previous.get("candidate_path"),
                label="previous candidate path",
            )
            previous_value = _read_json(previous_candidate)
            decision = interpretation.get("decision")
            decision_value = decision if isinstance(decision, Mapping) else {}
            revision_context = {
                "user_comment": interpretation.get("user_comment", ""),
                "qwen_interpretation": decision_value,
                "previous_candidate": previous_value,
                "rules": {
                    "return_complete_replacement": True,
                    "preserve_unmentioned_valid_coverage": True,
                    "do_not_execute": True,
                    "all_changes_require_new_review_round": True,
                },
            }
            prompt += (
                "\n\n# Immutable review revision\n\n"
                "Produce a complete replacement candidate for the next review round. "
                "Apply the interpreted requested changes, preserve valid unmentioned coverage, "
                "and do not return a patch or execution instruction.\n\n```json\n"
                + json.dumps(revision_context, indent=2, ensure_ascii=False)
                + "\n```\n"
            )
        prompt_path = round_root / "prompt" / "authoring_prompt.md"
        _write_text(prompt_path, prompt)
        manifest = {
            "schema_version": 1,
            "generated_at": _utc_now(),
            "run_tag": f"{session['session_id']}_round_{number:04d}",
            "task_count": 1,
            "tasks": [
                {
                    "task_type": task_type,
                    "task_id": task_id,
                    "request_id": task_id,
                    "form_path": _repo_relative(self.repo_root, form_path),
                    "prompt_path": _repo_relative(self.repo_root, prompt_path),
                    "expected_output_path": _repo_relative(self.repo_root, expected_output),
                    "output_contract": output_contract,
                    "target_api": form["target_api"],
                    "interface_family": task.get("interface_family", ""),
                    "run_profile_id": task.get("run_profile_id", ""),
                    "allowed_campaign_profiles": campaign_profiles_for(task),
                    "review_required_before_execute": True,
                    "harness_session_id": session["session_id"],
                    "harness_round_number": number,
                    "approval_attestation_path": "",
                    **source_metadata,
                }
            ],
        }
        manifest_path = round_root / "prompt" / "model_task_manifest.json"
        _write_json(manifest_path, manifest)
        return round_root, manifest_path, expected_output, form

    def _generate_round(
        self,
        session: dict[str, Any],
        paths: SessionPaths,
        resolution: Mapping[str, Any],
        *,
        previous: Mapping[str, Any] | None,
        interpretation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        self._assert_session_provider(session)
        number = int(session.get("current_round") or 0) + 1
        resolution_path = paths.session_root / "resolution" / f"round_{number:04d}.json"
        if not resolution_path.is_file() or _read_json(resolution_path) != dict(resolution):
            raise WorkflowError("current round resolution artifact is missing or inconsistent")
        round_root, manifest_path, expected_output, form = self._build_round_files(
            session, paths, resolution, number, previous, interpretation
        )
        manifest_value = _read_json(manifest_path)
        manifest_tasks = manifest_value.get("tasks")
        manifest_task = (
            manifest_tasks[0]
            if isinstance(manifest_tasks, list)
            and len(manifest_tasks) == 1
            and isinstance(manifest_tasks[0], Mapping)
            else {}
        )
        data_classification = str(manifest_task.get("data_classification") or "public_interface")
        allowed_profile_categories = manifest_task.get("allowed_profile_categories")
        if data_classification == "proprietary_source":
            if allowed_profile_categories != ["intranet"] or self.profile_category != "intranet":
                raise WorkflowError(
                    "proprietary round data must remain bound to the intranet profile category"
                )
            session["data_classification"] = "proprietary_source"
        run_id = _safe_id(f"{session['session_id']}_review_r{number:04d}")
        try:
            result = dict(
                self.runtime.generate(
                    manifest_path=manifest_path,
                    run_id=run_id,
                    staging_root=round_root / "pipeline",
                )
            )
        except Exception as exc:  # noqa: BLE001 - persist recovery state at runtime boundary
            result = {"ok": False, "error": str(exc)}
        _write_json(round_root / "pipeline" / "generation_result.json", result)
        if result.get("ok") is not True or not expected_output.is_file():
            session["state"] = "generation_failed"
            session["recovery_state"] = ""
            session["recovery_artifact_path"] = ""
            session["last_error"] = str(result.get("error") or result.get("errors") or "generation failed")
            failure_report = paths.session_root / "generation_failure_report.zh-CN.md"
            _write_text(
                failure_report,
                "\n".join(
                    [
                        "# SGGK 测试方案生成未完成",
                        "",
                        f"- Public function：`{session['public_function']}`",
                        f"- 失败阶段：第 {number} 轮 Message API 生成或固定门禁",
                        f"- 错误摘要：{session['last_error']}",
                        "- SDK 真实执行：未发生",
                        "",
                        "修复 endpoint 配置或门禁问题后，可重新 start 同一接口；旧失败证据会保留。",
                        "",
                    ]
                ),
            )
            session["final_report_path"] = _repo_relative(self.repo_root, failure_report)
            self._event(
                session,
                paths,
                "ROUND_GENERATION_FAILED",
                {"round_number": number, "error": session["last_error"]},
            )
            self._save_session(session, paths)
            raise WorkflowError(session["last_error"])
        batch_results = result.get("results")
        task_result = batch_results[0] if isinstance(batch_results, list) and batch_results else {}
        if not isinstance(task_result, Mapping) or task_result.get("authoring_accepted") is not True:
            raise WorkflowError("pipeline did not produce a fixed-gate accepted candidate")
        candidate = _read_json(expected_output)
        candidate_sha256 = _sha256_json(candidate)
        provenance_path = expected_output.with_name(f"{expected_output.stem}.provenance.json")
        if not provenance_path.is_file():
            raise WorkflowError("fixed-gate accepted candidate has no provenance sidecar")
        review_packet_path = _repo_path(
            self.repo_root, task_result.get("review_packet_path"), label="review_packet_path"
        )
        review_report_path = _repo_path(
            self.repo_root, task_result.get("review_report_path"), label="review_report_path"
        )
        subject_outline = _bounded_subject_outline(_sanitize_outline(
            {
                "target": session["public_function"],
                "resolved_api": resolution.get("resolved_api"),
                "route": resolution.get("route"),
                "internal_plan": form,
                "candidate": candidate,
                "machine_verification": {
                    "authoring_accepted": task_result.get("authoring_accepted"),
                    "selection_policy": task_result.get("selection_policy"),
                    "candidate_count": task_result.get("candidate_count"),
                    "execution_requested": False,
                },
                "previous_interpretation": (
                    {
                        "user_comment": interpretation.get("user_comment", ""),
                        "decision": interpretation.get("decision", {}),
                    }
                    if isinstance(interpretation, Mapping)
                    else {}
                ),
            }
        ))
        _write_json(round_root / "review" / "review_subject_digest.json", subject_outline)
        record_unsigned = {
            "schema_version": 1,
            "session_id": session["session_id"],
            "provider_profile": self.profile,
            "provider_profile_category": self.profile_category,
            "data_classification": data_classification,
            "allowed_profile_categories": (
                list(allowed_profile_categories)
                if isinstance(allowed_profile_categories, list)
                else []
            ),
            "round_number": number,
            "task_id": str(task_result.get("task_id") or ""),
            "run_id": str(task_result.get("run_id") or run_id),
            "resolution_path": _repo_relative(self.repo_root, resolution_path),
            "resolution_sha256": _sha256_json(dict(resolution)),
            "manifest_path": _repo_relative(self.repo_root, manifest_path),
            "manifest_sha256": _sha256_file(manifest_path),
            "candidate_path": _repo_relative(self.repo_root, expected_output),
            "candidate_sha256": candidate_sha256,
            "provenance_path": _repo_relative(self.repo_root, provenance_path),
            "provenance_sha256": _sha256_file(provenance_path),
            "review_packet_path": _repo_relative(self.repo_root, review_packet_path),
            "review_packet_sha256": _sha256_file(review_packet_path),
            "fixed_review_report_path": _repo_relative(self.repo_root, review_report_path),
            "fixed_review_report_sha256": _sha256_file(review_report_path),
            "subject_digest_path": _repo_relative(
                self.repo_root, round_root / "review" / "review_subject_digest.json"
            ),
            "subject_digest_sha256": _sha256_json(subject_outline),
            "previous_round_sha256": str(previous.get("round_sha256") or "") if previous else "",
            "interpretation_sha256": (
                _sha256_json(interpretation) if isinstance(interpretation, Mapping) else ""
            ),
            "created_at": _utc_now(),
            "state": "awaiting_comment",
        }
        round_record = {**record_unsigned, "round_sha256": _sha256_json(record_unsigned)}
        round_record_path = round_root / "round_manifest.json"
        _write_json(round_record_path, round_record)
        user_report = round_root / "review" / f"第{number}轮测试方案审查.zh-CN.md"
        self._write_round_report(
            user_report,
            session=session,
            resolution=resolution,
            round_record=round_record,
            form=form,
            candidate=candidate,
            interpretation=interpretation,
        )
        round_record["user_review_report_path"] = _repo_relative(self.repo_root, user_report)
        round_record["user_review_report_sha256"] = _sha256_file(user_report)
        # The final hash binds the user report too.  Recompute once, then write
        # the immutable manifest as the round commit marker.
        without_hash = {key: value for key, value in round_record.items() if key != "round_sha256"}
        round_record["round_sha256"] = _sha256_json(without_hash)
        _write_json(round_record_path, round_record)
        session["state"] = "awaiting_comment"
        session["recovery_state"] = ""
        session["recovery_artifact_path"] = ""
        session["current_round"] = number
        session["current_round_sha256"] = round_record["round_sha256"]
        session["current_review_report_path"] = round_record["user_review_report_path"]
        session["last_error"] = ""
        self._event(
            session,
            paths,
            "ROUND_READY_FOR_REVIEW",
            {
                "round_number": number,
                "round_sha256": round_record["round_sha256"],
                "candidate_sha256": candidate_sha256,
            },
        )
        self._save_session(session, paths)
        return self.status_payload(session)

    @staticmethod
    def _write_round_report(
        path: Path,
        *,
        session: Mapping[str, Any],
        resolution: Mapping[str, Any],
        round_record: Mapping[str, Any],
        form: Mapping[str, Any],
        candidate: Mapping[str, Any],
        interpretation: Mapping[str, Any] | None,
    ) -> None:
        number = int(round_record["round_number"])
        decision = interpretation.get("decision") if isinstance(interpretation, Mapping) else {}
        decision = decision if isinstance(decision, Mapping) else {}
        changes = decision.get("requested_changes") if isinstance(decision.get("requested_changes"), list) else []
        lines = [
            f"# 第 {number} 轮 SGGK 测试方案审查",
            "",
            "> 本轮只完成 Message API 生成和固定机器门禁，尚未调用 SGGK SDK 真实执行。",
            "> 任务 ID、轮次、候选 ID 和完整性哈希均由 Harness 管理，用户无需填写。",
            "",
            "## 1. 本轮目标",
            "",
            f"- Public function：`{session['public_function']}`",
            f"- Harness API：`{resolution.get('resolved_api')}`",
            f"- 自动路由：`{resolution.get('route')}`",
            "- 当前结论：等待用户自然语言评论；明确同意执行后才会实测。",
            "",
        ]
        if interpretation:
            lines.extend(
                [
                    "## 2. 上一轮用户意见与 Qwen 理解",
                    "",
                    f"- 用户原始意见：{interpretation.get('user_comment', '')}",
                    f"- Qwen 语义判断：`{decision.get('decision', '')}`",
                    f"- Qwen 中文解释：{decision.get('summary_zh_cn', '')}",
                    "- 本轮采纳项：",
                    "",
                ]
            )
            if changes:
                for item in changes:
                    if isinstance(item, Mapping):
                        description = item.get("change_zh_cn") or item.get("instruction") or item
                        lines.append(f"  - [{item.get('scope', 'other')}] {description}")
            else:
                lines.append("  - 无结构性修改；保留上一轮有效设计。")
            lines.append("")
            next_index = 3
        else:
            next_index = 2
        lines.extend(
            [
                f"## {next_index}. Harness 自动形成的内部测试意图",
                "",
                "```json",
                json.dumps(form, indent=2, ensure_ascii=False),
                "```",
                "",
                f"## {next_index + 1}. Qwen 生成的完整候选",
                "",
                "下列 JSON 是固定门禁已经接受、但尚未执行的完整候选。字段保持原样，便于逐项复核。",
                "",
                "```json",
                json.dumps(candidate, indent=2, ensure_ascii=False),
                "```",
                "",
                f"## {next_index + 2}. 机器门禁与审查证据",
                "",
                f"- 候选类型：`{candidate.get('kind', '')}`",
                f"- 固定审查包：`{round_record.get('review_packet_path', '')}`",
                f"- 固定中文报告：`{round_record.get('fixed_review_report_path', '')}`",
                "- SDK 真实执行：`未开始`",
                "",
                f"## {next_index + 3}. 用户下一步",
                "",
                "只需要提交一句自然语言评论，例如：",
                "",
                "```powershell",
                '.\\harness.ps1 comment "第二个用例增加大坐标和 topo_tol 两侧扰动。"',
                '.\\harness.ps1 comment "这一版可以开始执行。"',
                "```",
                "",
                "任何要求修改的评论都会先生成下一轮审查，不会在同一轮修改后直接执行。",
                "",
            ]
        )
        _write_text(path, "\n".join(lines))

    def _load_round(self, session: Mapping[str, Any], paths: SessionPaths) -> dict[str, Any]:
        number = int(session.get("current_round") or 0)
        if number < 1:
            raise WorkflowError("active session has no review round")
        path = paths.round_root(number) / "round_manifest.json"
        record = _read_json(path)
        actual = _sha256_json({key: value for key, value in record.items() if key != "round_sha256"})
        if actual != record.get("round_sha256") or actual != session.get("current_round_sha256"):
            raise WorkflowError("latest review round hash mismatch")
        for path_key, hash_key in (
            ("resolution_path", "resolution_sha256"),
            ("manifest_path", "manifest_sha256"),
            ("candidate_path", "candidate_sha256"),
            ("provenance_path", "provenance_sha256"),
            ("review_packet_path", "review_packet_sha256"),
            ("fixed_review_report_path", "fixed_review_report_sha256"),
            ("subject_digest_path", "subject_digest_sha256"),
            ("user_review_report_path", "user_review_report_sha256"),
        ):
            artifact = _repo_path(self.repo_root, record.get(path_key), label=path_key)
            if not artifact.is_file():
                raise WorkflowError(f"latest review artifact is missing: {path_key}")
            if path_key in {"resolution_path", "candidate_path", "subject_digest_path"}:
                actual_hash = _sha256_json(_read_json(artifact))
            else:
                actual_hash = _sha256_file(artifact)
            if actual_hash != record.get(hash_key):
                raise WorkflowError(f"latest review artifact changed: {path_key}")
        return record

    def comment(self, comment: str) -> dict[str, Any]:
        """Interpret one natural-language comment and perform the safe transition."""

        with _WorkspaceLock(self.lock_path):
            session, paths = self._load_active_for_update()
            self._assert_session_provider(session)
            if str(session.get("state")) not in COMMENTABLE_STATES:
                raise WorkflowError(f"current session state does not accept comments: {session.get('state')}")
            previous_state = str(session.get("state") or "awaiting_comment")
            round_record = self._load_round(session, paths)
            text = comment.strip()
            if not text:
                raise WorkflowError("comment must not be empty")
            comment_key = _sha256_json(
                {"round_sha256": round_record["round_sha256"], "comment": comment}
            )
            comment_root = paths.round_root(int(round_record["round_number"])) / "comments" / comment_key
            completed_path = comment_root / "completed.json"
            if completed_path.is_file():
                return _read_json(completed_path)
            comment_root.mkdir(parents=True, exist_ok=True)
            _write_text(comment_root / "user_comment.txt", comment)
            subject_outline = _read_json(
                _repo_path(
                    self.repo_root,
                    round_record["subject_digest_path"],
                    label="subject_digest_path",
                )
            )
            session["state"] = "interpreting_comment"
            session["recovery_state"] = previous_state
            session["recovery_artifact_path"] = _repo_relative(self.repo_root, comment_root)
            self._event(
                session,
                paths,
                "COMMENT_RECEIVED",
                {
                    "round_number": round_record["round_number"],
                    "comment_sha256": _sha256_bytes(comment.encode("utf-8")),
                },
            )
            self._save_session(session, paths)
            try:
                interpretation = dict(
                    self.runtime.interpret_comment(
                        comment=comment,
                        session=session,
                        round_record=round_record,
                        subject_outline=subject_outline,
                        output_dir=comment_root,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - restore commentable state
                session["state"] = previous_state
                session["recovery_state"] = ""
                session["recovery_artifact_path"] = ""
                session["last_error"] = str(exc)
                self._event(
                    session,
                    paths,
                    "COMMENT_INTERPRETATION_FAILED",
                    {
                        "round_number": round_record["round_number"],
                        "error": str(exc),
                    },
                )
                self._save_session(session, paths)
                raise WorkflowError(f"Qwen comment interpretation failed: {exc}") from exc
            decision = interpretation.get("decision")
            if not isinstance(decision, Mapping):
                raise WorkflowError("Qwen comment interpretation has no decision object")
            decision_name = str(decision.get("decision") or "")
            interpretation["user_comment"] = comment
            _write_json(comment_root / "interpretation.json", interpretation)
            session["recovery_state"] = ""
            session["recovery_artifact_path"] = ""
            session["last_error"] = ""
            self._event(
                session,
                paths,
                "COMMENT_INTERPRETED",
                {
                    "round_number": round_record["round_number"],
                    "decision": decision_name,
                    "interpretation_sha256": _sha256_json(interpretation),
                },
            )
            if decision_name == "revise":
                if int(session.get("approved_round") or 0) > 0:
                    self._event(
                        session,
                        paths,
                        "EXECUTION_APPROVAL_INVALIDATED",
                        {
                            "previous_approved_round": session["approved_round"],
                            "reason": "a later natural-language comment requested revision",
                        },
                    )
                    session["approved_round"] = 0
                    session["approval_path"] = ""
                    session["execution_manifest_path"] = ""
                    session["execution_manifest_sha256"] = ""
                next_round = int(session.get("current_round") or 0) + 1
                try:
                    resolution = resolve_public_function(
                        str(session["public_function"]),
                        self.capabilities,
                        sdk_dir=self.sdk_dir,
                        source_root=self.source_root,
                        expose_declarations=self.profile_category == "intranet",
                    )
                except Exception as exc:  # noqa: BLE001 - persist a recoverable review state
                    session["state"] = "awaiting_comment"
                    session["recovery_state"] = ""
                    session["recovery_artifact_path"] = ""
                    session["last_error"] = str(exc)
                    self._event(
                        session,
                        paths,
                        "PUBLIC_FUNCTION_RERESOLUTION_FAILED",
                        {"round_number": next_round, "error": str(exc)},
                    )
                    self._save_session(session, paths)
                    raise WorkflowError(f"source re-resolution failed: {exc}") from exc
                resolution_path = (
                    paths.session_root / "resolution" / f"round_{next_round:04d}.json"
                )
                _write_json(resolution_path, resolution)
                self._event(
                    session,
                    paths,
                    "PUBLIC_FUNCTION_RERESOLVED",
                    {
                        "round_number": next_round,
                        "resolved_api": resolution["resolved_api"],
                        "route": resolution["route"],
                        "resolution_sha256": resolution["resolution_sha256"],
                    },
                )
                session["state"] = "generating"
                session["recovery_state"] = "awaiting_comment"
                session["recovery_artifact_path"] = _repo_relative(
                    self.repo_root,
                    paths.round_root(next_round),
                )
                self._save_session(session, paths)
                payload = self._generate_round(
                    session,
                    paths,
                    resolution,
                    previous=round_record,
                    interpretation=interpretation,
                )
            elif decision_name == "approve":
                payload = self._approve_and_execute(
                    session, paths, round_record, interpretation, comment_root
                )
            elif decision_name == "reject":
                session["state"] = "rejected"
                report = paths.session_root / "final_rejection_report.zh-CN.md"
                _write_text(
                    report,
                    "\n".join(
                        [
                            "# SGGK 测试任务已拒绝",
                            "",
                            f"- 接口：`{session['public_function']}`",
                            f"- 用户意见：{comment}",
                            f"- Qwen 理解：{decision.get('summary_zh_cn', '')}",
                            "- SDK 执行：未发生",
                            "",
                        ]
                    ),
                )
                session["final_report_path"] = _repo_relative(self.repo_root, report)
                self._event(session, paths, "SESSION_REJECTED", {"round_number": round_record["round_number"]})
                self._save_session(session, paths)
                payload = self.status_payload(session)
            elif decision_name == "question":
                session["state"] = "awaiting_comment"
                answer_path = comment_root / "qwen_answer.zh-CN.md"
                _write_text(
                    answer_path,
                    "\n".join(
                        [
                            "# Qwen 对本轮评论的理解",
                            "",
                            f"- 用户评论：{comment}",
                            f"- 回答/说明：{decision.get('summary_zh_cn', '')}",
                            "",
                            "当前候选未改变，也未执行。可以继续提交自然语言评论。",
                            "",
                        ]
                    ),
                )
                self._event(
                    session,
                    paths,
                    "QUESTION_ANSWERED",
                    {"round_number": round_record["round_number"], "answer_sha256": _sha256_file(answer_path)},
                )
                self._save_session(session, paths)
                payload = self.status_payload(session)
                payload["answer_path"] = _repo_relative(self.repo_root, answer_path)
            else:
                raise WorkflowError(f"unsupported Qwen review decision: {decision_name}")
            _write_json(completed_path, payload)
            return payload

    @staticmethod
    def _explicit_execution_approval(comment: str) -> bool:
        if any(pattern.search(comment) for pattern in (
            *EXECUTION_DENIAL_PATTERNS,
            *EXECUTION_QUESTION_PATTERNS,
        )):
            return False
        return any(pattern.search(comment) for pattern in APPROVAL_PATTERNS) and not any(
            pattern.search(comment) for pattern in REVISION_PATTERNS
        )

    def _approval_record(
        self,
        session: Mapping[str, Any],
        round_record: Mapping[str, Any],
        interpretation: Mapping[str, Any],
        comment_root: Path,
        runner_path: Path | None,
        execution_manifest_path: Path,
        execution_manifest_sha256: str,
    ) -> dict[str, Any]:
        runner_hash = _sha256_file(runner_path) if runner_path and runner_path.is_file() else ""
        reviewed_manifest_path = _repo_path(
            self.repo_root, round_record["manifest_path"], label="manifest"
        )
        reviewed_manifest = _read_json(reviewed_manifest_path)
        reviewed_tasks = reviewed_manifest.get("tasks")
        if (
            not isinstance(reviewed_tasks, list)
            or len(reviewed_tasks) != 1
            or not isinstance(reviewed_tasks[0], Mapping)
        ):
            raise WorkflowError("reviewed manifest no longer contains exactly one task")
        prompt_path = _repo_path(
            self.repo_root, reviewed_tasks[0].get("prompt_path"), label="prompt_path"
        )
        unsigned = {
            "schema_version": 1,
            "record_type": "execution_approval",
            "decision": "approved_for_execution",
            "session_id": session["session_id"],
            "task_id": round_record["task_id"],
            "round_number": round_record["round_number"],
            "round_sha256": round_record["round_sha256"],
            "candidate_sha256": round_record["candidate_sha256"],
            "reviewed_manifest_sha256": round_record["manifest_sha256"],
            "execution_manifest_path": _repo_relative(
                self.repo_root, execution_manifest_path
            ),
            "execution_manifest_sha256": execution_manifest_sha256,
            "task_prompt_sha256": _sha256_bytes(prompt_path.read_bytes()),
            "review_packet_sha256": round_record["review_packet_sha256"],
            "comment_path": _repo_relative(self.repo_root, comment_root / "user_comment.txt"),
            "comment_sha256": _sha256_bytes(str(interpretation["user_comment"]).encode("utf-8")),
            "interpretation_path": _repo_relative(
                self.repo_root, comment_root / "interpretation.json"
            ),
            "interpretation_sha256": _sha256_json(interpretation),
            "runner_sha256": runner_hash,
            "approved_at": _utc_now(),
            "authority": "fixed_harness_host_after_qwen_comment_interpretation",
        }
        return {**unsigned, "approval_sha256": _sha256_json(unsigned)}

    def _verify_execution_binding(
        self,
        session: Mapping[str, Any],
        round_record: Mapping[str, Any],
        manifest_path: Path,
    ) -> dict[str, Any]:
        approval_path = _repo_path(
            self.repo_root, session.get("approval_path"), label="approval_path"
        )
        approval = _read_json(approval_path)
        unsigned = {key: value for key, value in approval.items() if key != "approval_sha256"}
        if _sha256_json(unsigned) != approval.get("approval_sha256"):
            raise WorkflowError("approval attestation changed")
        manifest_relative = _repo_relative(self.repo_root, manifest_path)
        manifest_sha256 = _sha256_file(manifest_path)
        if (
            approval.get("session_id") != session.get("session_id")
            or approval.get("task_id") != round_record.get("task_id")
            or approval.get("round_number") != round_record.get("round_number")
            or approval.get("round_sha256") != round_record.get("round_sha256")
            or approval.get("candidate_sha256") != round_record.get("candidate_sha256")
            or approval.get("reviewed_manifest_sha256") != round_record.get("manifest_sha256")
        ):
            raise WorkflowError("execution approval is not bound to the latest immutable round")
        if (
            approval.get("execution_manifest_path") != manifest_relative
            or session.get("execution_manifest_path") != manifest_relative
            or approval.get("execution_manifest_sha256") != manifest_sha256
            or session.get("execution_manifest_sha256") != manifest_sha256
        ):
            raise WorkflowError("execution manifest changed after approval")
        manifest = _read_json(manifest_path)
        tasks = manifest.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
            raise WorkflowError("execution manifest no longer contains exactly one task")
        task = tasks[0]
        if (
            task.get("task_id") != round_record.get("task_id")
            or task.get("harness_session_id") != session.get("session_id")
            or task.get("harness_round_number") != round_record.get("round_number")
            or task.get("approved_round_sha256") != round_record.get("round_sha256")
            or task.get("approved_candidate_sha256") != round_record.get("candidate_sha256")
            or task.get("approval_attestation_path")
            != _repo_relative(self.repo_root, approval_path)
        ):
            raise WorkflowError("execution manifest task binding changed after approval")
        return approval

    def _approve_and_execute(
        self,
        session: dict[str, Any],
        paths: SessionPaths,
        round_record: dict[str, Any],
        interpretation: dict[str, Any],
        comment_root: Path,
    ) -> dict[str, Any]:
        comment = str(interpretation.get("user_comment") or "")
        if not self._explicit_execution_approval(comment):
            session["state"] = "awaiting_comment"
            note = comment_root / "approval_not_explicit.zh-CN.md"
            _write_text(
                note,
                "# 尚未开始执行\n\nQwen 将评论理解为批准，但宿主未检测到明确的“同意执行”语义。"
                "请明确评论“这一版可以开始执行”。\n",
            )
            self._event(
                session,
                paths,
                "AMBIGUOUS_APPROVAL_REJECTED",
                {"round_number": round_record["round_number"]},
            )
            self._save_session(session, paths)
            payload = self.status_payload(session)
            payload["notice_path"] = _repo_relative(self.repo_root, note)
            return payload
        # Re-read all bound artifacts immediately before approval.
        current = self._load_round(session, paths)
        if current["round_sha256"] != round_record["round_sha256"]:
            raise WorkflowError("latest round changed while approval was being interpreted")
        manifest_path = _repo_path(
            self.repo_root, round_record["manifest_path"], label="manifest_path"
        )
        manifest = _read_json(manifest_path)
        tasks = manifest.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
            raise WorkflowError("round manifest no longer contains exactly one task")
        approval_tag = _safe_id(comment_root.name)[:20]
        approval_path = (
            paths.session_root
            / "approval"
            / f"round_{int(round_record['round_number']):04d}_{approval_tag}.json"
        )
        execution_input = (
            paths.session_root
            / "approval"
            / "execution_input"
            / f"round_{int(round_record['round_number']):04d}_{approval_tag}"
        )
        execution_candidate = execution_input / "candidate.json"
        execution_provenance = execution_input / "candidate.provenance.json"
        reviewed_candidate = _read_json(
            _repo_path(
                self.repo_root,
                round_record["candidate_path"],
                label="candidate_path",
            )
        )
        reviewed_provenance = _read_json(
            _repo_path(
                self.repo_root,
                round_record["provenance_path"],
                label="provenance_path",
            )
        )
        # The lower pipeline re-attests provenance after execution.  Execute a
        # host-created copy so the reviewed round remains byte-for-byte
        # immutable and can still receive later comments or retries.
        reviewed_provenance["output_path"] = _repo_relative(
            self.repo_root, execution_candidate
        )
        _write_json(execution_provenance, reviewed_provenance)
        _write_json(execution_candidate, reviewed_candidate)
        tasks[0]["expected_output_path"] = _repo_relative(
            self.repo_root, execution_candidate
        )
        tasks[0]["approval_attestation_path"] = _repo_relative(self.repo_root, approval_path)
        tasks[0]["approved_round_sha256"] = round_record["round_sha256"]
        tasks[0]["approved_candidate_sha256"] = round_record["candidate_sha256"]
        execution_manifest_path = (
            paths.session_root
            / "approval"
            / f"round_{int(round_record['round_number']):04d}_{approval_tag}.execution_manifest.json"
        )
        # Keep the reviewed manifest immutable.  Execution receives a host-only
        # copy whose sole additions are the bound approval fields above.
        _write_json(execution_manifest_path, manifest)
        execution_manifest_sha256 = _sha256_file(execution_manifest_path)
        approval = self._approval_record(
            session,
            round_record,
            interpretation,
            comment_root,
            self.runner_path,
            execution_manifest_path,
            execution_manifest_sha256,
        )
        _write_json(approval_path, approval)
        session["approved_round"] = round_record["round_number"]
        session["approval_path"] = _repo_relative(self.repo_root, approval_path)
        session["execution_manifest_path"] = _repo_relative(
            self.repo_root, execution_manifest_path
        )
        session["execution_manifest_sha256"] = execution_manifest_sha256
        self._event(
            session,
            paths,
            "EXECUTION_APPROVED",
            {
                "round_number": round_record["round_number"],
                "approval_sha256": approval["approval_sha256"],
                "execution_manifest_sha256": execution_manifest_sha256,
            },
        )
        return self._run_approved_execution(
            session, paths, round_record, execution_manifest_path
        )

    def _run_approved_execution(
        self,
        session: dict[str, Any],
        paths: SessionPaths,
        round_record: Mapping[str, Any],
        manifest_path: Path,
    ) -> dict[str, Any]:
        self._verify_execution_binding(session, round_record, manifest_path)
        attempt = int(session.get("execution_attempt") or 0) + 1
        execution_root = (
            paths.session_root
            / "execution"
            / f"round_{int(round_record['round_number']):04d}"
            / f"attempt_{attempt:04d}"
        )
        run_id = _safe_id(
            f"{session['session_id']}_execute_r{int(round_record['round_number']):04d}_a{attempt:04d}"
        )
        session["execution_attempt"] = attempt
        session["current_execution_attempt_path"] = _repo_relative(
            self.repo_root, execution_root
        )
        session["state"] = "executing"
        session["recovery_state"] = "execution_failed"
        session["recovery_artifact_path"] = ""
        self._event(
            session,
            paths,
            "EXECUTION_ATTEMPT_STARTED",
            {
                "round_number": round_record["round_number"],
                "attempt": attempt,
                "run_id": run_id,
                "execution_manifest_sha256": session["execution_manifest_sha256"],
            },
        )
        self._save_session(session, paths)
        try:
            result = dict(
                self.runtime.execute(
                    manifest_path=manifest_path,
                    run_id=run_id,
                    staging_root=execution_root / "pipeline",
                    runner_path=self.runner_path,
                )
            )
        except Exception as exc:  # noqa: BLE001 - persist execution failure evidence
            result = {"ok": False, "error": str(exc), "results": []}
        _write_json(execution_root / "execution_result.json", result)
        # The executed formal candidate must still be the approved bytes.
        candidate_path = _repo_path(
            self.repo_root, round_record["candidate_path"], label="candidate_path"
        )
        if _sha256_json(_read_json(candidate_path)) != round_record["candidate_sha256"]:
            raise WorkflowError("executed candidate no longer matches the approved candidate")
        task_results = result.get("results")
        task_result = task_results[0] if isinstance(task_results, list) and task_results else {}
        execution = task_result.get("execution") if isinstance(task_result, Mapping) else {}
        execution = execution if isinstance(execution, Mapping) else {}
        passed = result.get("ok") is True and execution.get("requested") is True and execution.get("ok") is True
        session["state"] = "completed" if passed else "execution_failed"
        session["recovery_state"] = ""
        session["recovery_artifact_path"] = ""
        session["last_error"] = "" if passed else str(
            (task_result.get("error") if isinstance(task_result, Mapping) else "")
            or result.get("error")
            or result.get("errors")
            or execution.get("error")
            or "execution did not reach a passing SDK result"
        )
        report = execution_root / "final_report.zh-CN.md"
        self._write_final_report(
            report,
            session=session,
            round_record=round_record,
            result=result,
            task_result=task_result if isinstance(task_result, Mapping) else {},
            passed=passed,
        )
        session["final_report_path"] = _repo_relative(self.repo_root, report)
        self._event(
            session,
            paths,
            "EXECUTION_COMPLETED" if passed else "EXECUTION_FAILED",
            {
                "round_number": round_record["round_number"],
                "attempt": attempt,
                "execution_result_path": _repo_relative(
                    self.repo_root, execution_root / "execution_result.json"
                ),
                "execution_result_sha256": _sha256_file(execution_root / "execution_result.json"),
                "final_report_sha256": _sha256_file(report),
            },
        )
        self._save_session(session, paths)
        return self.status_payload(session)

    @staticmethod
    def _write_final_report(
        path: Path,
        *,
        session: Mapping[str, Any],
        round_record: Mapping[str, Any],
        result: Mapping[str, Any],
        task_result: Mapping[str, Any],
        passed: bool,
    ) -> None:
        execution = task_result.get("execution") if isinstance(task_result.get("execution"), Mapping) else {}
        lines = [
            "# SGGK Harness 最终测试报告",
            "",
            f"- Public function：`{session['public_function']}`",
            f"- 批准轮次：第 `{round_record['round_number']}` 轮",
            f"- 总体结果：`{'通过' if passed else '执行未完成/失败'}`",
            f"- 执行状态：`{execution.get('status', '')}`",
            f"- 固定门禁接受：`{task_result.get('authoring_accepted', False)}`",
            f"- SDK 执行已请求：`{execution.get('requested', False)}`",
            f"- SDK 执行通过：`{execution.get('ok', False)}`",
            "",
            "## 失败或诊断摘要",
            "",
            f"- Pipeline：{task_result.get('error') or result.get('errors') or '无'}",
            f"- Execution：{execution.get('error') or '无'}",
            "",
            "## 可复核证据",
            "",
            f"- 本轮审查报告：`{round_record.get('user_review_report_path', '')}`",
            f"- 审查包：`{round_record.get('review_packet_path', '')}`",
            f"- 正式候选：`{round_record.get('candidate_path', '')}`",
            f"- 执行 staging：`{result.get('staging_path', '')}`",
            "",
        ]
        _write_text(path, "\n".join(lines))

    def retry(self) -> dict[str, Any]:
        """Retry an unchanged, previously approved round after execution failure."""

        with _WorkspaceLock(self.lock_path):
            session, paths = self._load_active_for_update()
            self._assert_session_provider(session)
            if session.get("state") != "execution_failed":
                raise WorkflowError("retry is available only after an approved execution failure")
            round_record = self._load_round(session, paths)
            if int(session.get("approved_round") or 0) != int(round_record["round_number"]):
                raise WorkflowError("latest round is not the approved round; submit a new approval comment")
            manifest_path = _repo_path(
                self.repo_root,
                session.get("execution_manifest_path"),
                label="execution_manifest_path",
            )
            self._verify_execution_binding(session, round_record, manifest_path)
            self._event(
                session,
                paths,
                "EXECUTION_RETRY_STARTED",
                {
                    "round_number": round_record["round_number"],
                    "next_attempt": int(session.get("execution_attempt") or 0) + 1,
                },
            )
            return self._run_approved_execution(session, paths, round_record, manifest_path)

    def status(self) -> dict[str, Any]:
        session, _paths = self._load_active()
        return self.status_payload(session)

    @staticmethod
    def status_payload(session: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "public_function": session.get("public_function", ""),
            "state": session.get("state", ""),
            "current_round": session.get("current_round", 0),
            "review_report_path": session.get("current_review_report_path", ""),
            "final_report_path": session.get("final_report_path", ""),
            "last_error": session.get("last_error", ""),
        }

    def show(self) -> Path:
        session, _paths = self._load_active()
        value = session.get("final_report_path") if session.get("state") in TERMINAL_STATES else session.get(
            "current_review_report_path"
        )
        return _repo_path(self.repo_root, value, label="report_path")
