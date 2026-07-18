#!/usr/bin/env python3
"""Build deterministic Chinese review packets for model-generated harness artifacts."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "test_harness/schemas/generated_artifact_review.schema.json"
SOURCE_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inc"}
CASE_EXTENSIONS = {".json", ".jsonl"}
MAX_DIRECTORY_FILES = 2_000


class ReviewPacketError(ValueError):
    """The deterministic review packet cannot be produced safely."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_strings(value: Any) -> list[str]:
    return [str(item) for item in _as_list(value) if isinstance(item, (str, int, float)) and str(item)]


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _repo_path(repo_root: Path, value: Any) -> Path | None:
    raw = _text(value)
    if not raw:
        return None
    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return resolved


def _display_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _artifact_role(path: Path, hint: str = "") -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    normalized_hint = hint.lower()
    if suffix in SOURCE_EXTENSIONS or "adapter" in name:
        return "generated_test_code"
    if "schema" in name:
        return "generated_test_case_schema"
    if "negative" in name or "invalid" in name:
        return "generated_negative_test_case"
    if "recipe" in name or "dsl" in name or "normalized" in name or "smoke" in name:
        return "generated_test_case"
    if suffix in {".md", ".png"} or "report" in name or "summary" in name:
        return "machine_verification_report"
    if "candidate" in normalized_hint:
        return "selected_model_candidate"
    if suffix in CASE_EXTENSIONS:
        return "generated_test_definition"
    return hint or "supporting_artifact"


def _file_record(
    repo_root: Path,
    path: Path,
    *,
    role: str = "",
    logical_path: str = "",
    source_path: str = "",
    sha256_override: str = "",
    size_override: int | None = None,
) -> dict[str, Any]:
    exists = path.is_file()
    return {
        "role": role or _artifact_role(path),
        "path": logical_path or _display_path(repo_root, path),
        "source_path": source_path,
        "exists": exists or bool(sha256_override),
        "size_bytes": path.stat().st_size if exists else int(size_override or 0),
        "sha256": _sha256(path) if exists else sha256_override,
    }


def _iter_directory_files(path: Path) -> Iterable[Path]:
    count = 0
    for item in sorted(path.rglob("*"), key=lambda entry: str(entry).casefold()):
        if not item.is_file() or "build" in {part.lower() for part in item.parts}:
            continue
        if item.suffix.lower() not in SOURCE_EXTENSIONS | CASE_EXTENSIONS | {".md", ".png"}:
            continue
        yield item
        count += 1
        if count >= MAX_DIRECTORY_FILES:
            break


def _selected_branch(result: Mapping[str, Any]) -> dict[str, Any]:
    selected_id = _text(result.get("selected_candidate_id"))
    for branch in _as_list(result.get("candidates")):
        if isinstance(branch, Mapping) and _text(branch.get("candidate_id")) == selected_id:
            return dict(branch)
    return {}


def _load_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return _as_dict(value)


def _candidate_payload(candidate: Mapping[str, Any], kind: str) -> dict[str, Any]:
    if kind == "attack_dsl":
        return _as_dict(candidate.get("dsl")) or dict(candidate)
    if kind == "flat_recipe":
        return _as_dict(candidate.get("recipe")) or dict(candidate)
    return dict(candidate)


def _expectation_keys(value: Mapping[str, Any]) -> list[str]:
    expectations = _as_dict(value.get("expectations"))
    return sorted(str(key) for key in expectations)


def _input_summary(case: Mapping[str, Any]) -> str:
    api = _text(case.get("api")) or "未声明 API"
    target = _text(case.get("target_kind")) or _text(_as_dict(case.get("target")).get("kind"))
    tool = _text(case.get("tool_kind")) or _text(_as_dict(case.get("tool")).get("kind"))
    source = _text(case.get("source_file")) or _text(case.get("target_source_file"))
    parts = [f"API={api}"]
    if target:
        parts.append(f"target={target}")
    if tool:
        parts.append(f"tool={tool}")
    if source:
        parts.append(f"输入资产={source}")
    if not target and not tool and not source:
        family = _text(case.get("geometry_family")) or _text(case.get("family"))
        parts.append(f"几何族={family or '由候选定义'}")
    return "；".join(parts)


def _case_record(case: Mapping[str, Any], index: int, *, default_api: str = "") -> dict[str, Any]:
    oracle_keys = _expectation_keys(case)
    source_review = _as_dict(case.get("source_review"))
    return {
        "case_id": _text(case.get("case_id")) or _text(case.get("id")) or f"case_{index:04d}",
        "api": _text(case.get("api")) or default_api,
        "input_summary_zh_cn": _input_summary(case),
        "oracle_summary_zh_cn": (
            "验证以下确定性 Oracle：" + "、".join(oracle_keys)
            if oracle_keys
            else "候选未直接声明 Oracle；必须以固定门禁和执行报告为准。"
        ),
        "hypothesis": _text(case.get("hypothesis")) or _text(source_review.get("summary")),
        "source_ref": _text(case.get("source_ref")),
    }


def _case_records(payload: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    if kind == "attack_dsl":
        records = [
            _case_record(case, index)
            for index, case in enumerate(_as_list(payload.get("cases")), start=1)
            if isinstance(case, Mapping)
        ]
        bases = _as_dict(payload.get("cluster_bases"))
        for base_id, base in bases.items():
            if not isinstance(base, Mapping):
                continue
            record = _case_record(base, len(records) + 1)
            record["case_id"] = f"cluster_base:{base_id}"
            records.append(record)
        for index, cluster in enumerate(_as_list(payload.get("parameter_clusters")), start=1):
            if not isinstance(cluster, Mapping):
                continue
            cluster_bases = "、".join(str(item) for item in _as_list(cluster.get("bases")))
            records.append(
                {
                    "case_id": _text(cluster.get("cluster_id")) or f"cluster_{index:04d}",
                    "api": "parameter_cluster",
                    "input_summary_zh_cn": (
                        f"参数簇类型={_text(cluster.get('type'))}；基几何={cluster_bases}；"
                        "固定代码确定性展开，每簇最多 50 例。"
                    ),
                    "oracle_summary_zh_cn": "以展开后 recipe 的 Oracle 与固定门禁抽样校验为准。",
                    "hypothesis": "",
                    "source_ref": "",
                }
            )
        return records
    if kind == "api_plugin_candidate":
        api = _text(payload.get("api"))
        records: list[dict[str, Any]] = []
        smoke = _as_dict(payload.get("smoke_recipe"))
        negative = _as_dict(payload.get("negative_recipe"))
        if smoke:
            records.append(_case_record(smoke, 1, default_api=api))
        if negative:
            item = _case_record(negative, 2, default_api=api)
            item["case_id"] = item["case_id"] or "negative_case"
            item["oracle_summary_zh_cn"] = "负例：必须被严格 schema 因未知字段拒绝。"
            records.append(item)
        return records
    if kind == "campaign_request":
        args = _as_dict(payload.get("args"))
        return [
            {
                "case_id": _text(payload.get("profile_id")) or "campaign_request",
                "api": "campaign",
                "input_summary_zh_cn": (
                    "使用宿主注册的 campaign profile 和有界参数："
                    + json.dumps(args, ensure_ascii=False, sort_keys=True)
                ),
                "oracle_summary_zh_cn": "以 campaign artifact verifier、triage、replay 和审查索引为准。",
                "hypothesis": "",
                "source_ref": "",
            }
        ]
    if kind == "cluster_seed":
        return [
            {
                "case_id": _text(payload.get("cluster_id")) or "cluster_seed",
                "api": _text(payload.get("api")) or "api_boolean",
                "input_summary_zh_cn": "由固定 cluster builder 展开：" + _input_summary(payload),
                "oracle_summary_zh_cn": "以展开后的 DSL/recipe Oracle 和固定门禁为准。",
                "hypothesis": _text(_as_dict(payload.get("source_review")).get("summary")),
                "source_ref": _text(payload.get("source_ref")),
            }
        ]
    return [_case_record(payload, 1)]


def _source_review(payload: Mapping[str, Any], cases: list[dict[str, Any]]) -> dict[str, Any]:
    direct = _as_dict(payload.get("source_review"))
    if direct:
        return direct
    for case in _as_list(payload.get("cases")):
        if isinstance(case, Mapping) and isinstance(case.get("source_review"), Mapping):
            return dict(case["source_review"])
    return {}


def _review_context(
    repo_root: Path,
    task_context: Mapping[str, Any],
    payload: Mapping[str, Any],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    metadata = _as_dict(task_context.get("metadata"))
    form_path = _repo_path(repo_root, metadata.get("form_path"))
    form = _load_object(form_path)
    oracles = _as_strings(form.get("oracles"))
    if not oracles:
        oracles = sorted({oracle for case in cases for oracle in _expectation_keys(case)})
    source_review = _source_review(payload, cases)
    source_refs = _as_strings(form.get("sdk_source_refs"))
    if not source_refs:
        source_refs = [
            _text(item.get("source_ref_id"))
            for item in _as_list(source_review.get("source_refs"))
            if isinstance(item, Mapping) and _text(item.get("source_ref_id"))
        ]
    if not source_refs:
        source_refs = [_text(metadata.get("source_ref"))] if _text(metadata.get("source_ref")) else []
    purpose = _text(form.get("test_goal")) or _text(metadata.get("test_goal"))
    risk = _text(form.get("risk_summary")) or _text(source_review.get("summary"))
    expected = _text(form.get("expected_behavior"))
    return {
        "purpose_zh_cn": purpose or "验证模型生成的 SGGK 测试定义能够通过固定门禁并按预期执行。",
        "input_summary_zh_cn": (
            "；".join(item["input_summary_zh_cn"] for item in cases[:4])
            or "输入由正式候选及其固定绑定定义。"
        ),
        "expected_behavior_zh_cn": expected or "以候选中声明的确定性 Oracle、固定门禁和 SDK 执行结果为准。",
        "risk_summary_zh_cn": risk or "重点审查几何合法性、容差边界、拓扑有效性、来源绑定和 Oracle 完整性。",
        "oracles": oracles,
        "source_refs": source_refs,
    }


def _collect_artifacts(
    repo_root: Path,
    result: Mapping[str, Any],
    candidate_path: Path,
    planned_output_path: str,
) -> list[dict[str, Any]]:
    branch = _selected_branch(result)
    fixed_gate = _as_dict(branch.get("fixed_gate"))
    execution = _as_dict(branch.get("execution")) or _as_dict(result.get("execution"))
    candidate_sha = _sha256(candidate_path)
    artifacts: list[dict[str, Any]] = [
        _file_record(repo_root, candidate_path, role="selected_model_candidate"),
        _file_record(
            repo_root,
            repo_root / planned_output_path,
            role="formal_model_output",
            logical_path=planned_output_path,
            source_path=_display_path(repo_root, candidate_path),
            sha256_override=candidate_sha,
            size_override=candidate_path.stat().st_size,
        ),
    ]
    raw_paths: list[tuple[str, Any]] = [("normalized_test_definition", fixed_gate.get("normalized_path"))]
    for key, value in _as_dict(fixed_gate.get("artifacts")).items():
        raw_paths.append((str(key), value))
    for key, value in _as_dict(execution.get("artifacts")).items():
        if not str(key).endswith("sha256"):
            raw_paths.append((str(key), value))
    seen = {candidate_path.resolve()}
    for hint, raw in raw_paths:
        path = _repo_path(repo_root, raw)
        if path is None or not path.exists():
            continue
        if path.is_dir():
            for item in _iter_directory_files(path):
                resolved = item.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                artifacts.append(_file_record(repo_root, item, role=_artifact_role(item, hint)))
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        artifacts.append(_file_record(repo_root, path, role=_artifact_role(path, hint)))
    return sorted(artifacts, key=lambda item: (item["role"], item["path"]))


def _markdown(packet: Mapping[str, Any], state_path: str) -> str:
    task = _as_dict(packet.get("task"))
    generation = _as_dict(packet.get("generation"))
    summary = _as_dict(packet.get("review_summary"))
    verification = _as_dict(packet.get("machine_verification"))
    source_review = _as_dict(packet.get("source_review"))
    lines = [
        "# SGGK 生成产物中文审查报告",
        "",
        "> 本报告由固定宿主代码生成。机器门禁通过后，仍须通过 Harness 提交自然语言 comment；用户不编辑任何审批 JSON。",
        "",
        "## 1. 任务与生成身份",
        "",
        f"- 任务 ID：`{task.get('task_id', '')}`",
        f"- 任务类型：`{task.get('task_type', '')}`",
        f"- 目标 API：`{task.get('target_api', '')}`",
        f"- 输出类型：`{generation.get('output_kind', '')}`",
        f"- Profile / Model：`{generation.get('profile', '')}` / `{generation.get('model', '')}`",
        (
            f"- Run / Candidate / Role：`{generation.get('run_id', '')}` / "
            f"`{generation.get('candidate_id', '')}` / `{generation.get('role_id', '')}`"
        ),
        f"- 候选 SHA-256：`{generation.get('candidate_sha256', '')}`",
        f"- 表单：`{task.get('form_path', '')}`",
        f"- Prompt：`{task.get('prompt_path', '')}`",
        "",
        "## 2. 审查摘要",
        "",
        f"- 用途：{summary.get('purpose_zh_cn', '')}",
        f"- 输入：{summary.get('input_summary_zh_cn', '')}",
        f"- 预期：{summary.get('expected_behavior_zh_cn', '')}",
        f"- 风险：{summary.get('risk_summary_zh_cn', '')}",
        f"- Oracle：`{summary.get('oracles', [])}`",
        f"- 源码证据：`{summary.get('source_refs', [])}`",
        "",
        "## 3. 源码风险理解与测试增强",
        "",
        f"- 风险摘要：{source_review.get('summary', '无独立源码风险摘要；以表单和固定门禁为准。')}",
        f"- 风险分支：`{source_review.get('risky_branches', [])}`",
        f"- 失败假设：`{source_review.get('failure_hypotheses', [])}`",
        f"- 测试增强：`{source_review.get('test_enhancements', [])}`",
        "",
        "## 4. 生成测试代码与测试用例",
        "",
        "| case_id | API | 输入摘要 | Oracle 摘要 | 假设/来源 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in _as_list(packet.get("cases")):
        if not isinstance(case, Mapping):
            continue
        trace = _text(case.get("hypothesis")) or _text(case.get("source_ref"))
        lines.append(
            f"| `{case.get('case_id', '')}` | `{case.get('api', '')}` | {case.get('input_summary_zh_cn', '')} | "
            f"{case.get('oracle_summary_zh_cn', '')} | {trace} |"
        )
    lines.extend(
        [
            "",
            "## 5. 产物清单与完整性",
            "",
            "| 角色 | 路径 | SHA-256 | 字节数 |",
            "| --- | --- | --- | ---: |",
        ]
    )
    for artifact in _as_list(packet.get("artifacts")):
        if isinstance(artifact, Mapping):
            lines.append(
                f"| `{artifact.get('role', '')}` | `{artifact.get('path', '')}` | "
                f"`{artifact.get('sha256', '')}` | {artifact.get('size_bytes', 0)} |"
            )
    lines.extend(
        [
            "",
            "## 6. 机器验证与用户复核",
            "",
            f"- 固定门禁：`{verification.get('fixed_gate_ok', False)}`",
            (
                f"- SDK 执行：requested=`{verification.get('execution_requested', False)}`，"
                f"ok=`{verification.get('execution_ok', False)}`，"
                f"status=`{verification.get('execution_status', '')}`"
            ),
            "- 用户复核状态：`awaiting_comment`（机器通过不会自动触发 SDK 执行）",
            f"- Harness 内部状态：`{state_path}`（只读，不需要用户编辑）",
            "",
            "### 用户复核清单",
            "",
            "- [ ] 目标 API、函数签名、SDK 头文件和模块绑定正确。",
            "- [ ] 输入几何合法，复杂度和容差变化确实覆盖目标风险。",
            "- [ ] 每个用例都有可观测 Oracle，不能只检查 API 返回成功。",
            "- [ ] 源码引用、风险摘要和失败假设能够互相对应，没有捏造路径或符号。",
            "- [ ] 生成代码没有命令、环境变量、任意路径或越权执行能力。",
            "- [ ] 正例、负例、schema、编译和运行报告的 SHA-256 与本报告一致。",
            "- [ ] 若同意执行，只提交明确的自然语言 comment；Harness 自动绑定当前轮次和全部哈希。",
            "",
        ]
    )
    return "\n".join(lines)


def write_review_packet(
    *,
    repo_root: Path,
    task_root: Path,
    task_context: Mapping[str, Any],
    result: Mapping[str, Any],
    candidate_path: Path,
    planned_output_path: str,
) -> dict[str, Any]:
    """Write a hash-bound Chinese review packet before formal promotion."""

    repo_root = repo_root.resolve()
    candidate = _load_object(candidate_path)
    branch = _selected_branch(result)
    fixed_gate = _as_dict(branch.get("fixed_gate"))
    execution = _as_dict(branch.get("execution")) or _as_dict(result.get("execution"))
    kind = _text(fixed_gate.get("kind")) or _text(candidate.get("kind")) or "unknown"
    payload = _candidate_payload(candidate, kind)
    cases = _case_records(payload, kind)
    metadata = _as_dict(task_context.get("metadata"))
    candidate_provenance = _load_object(_repo_path(repo_root, branch.get("candidate_provenance_path")))
    packet = {
        "schema_version": 1,
        "language": "zh-CN",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "task": {
            "task_id": _text(task_context.get("task_id")),
            "task_type": _text(task_context.get("task_type")) or "manifest_task",
            "target_api": _text(metadata.get("target_api")) or _text(payload.get("api")),
            "form_path": _text(metadata.get("form_path")),
            "prompt_path": _text(task_context.get("prompt_path")),
            "manifest_path": _text(task_context.get("manifest_path")),
        },
        "generation": {
            "run_id": _text(result.get("run_id")),
            "profile": _text(candidate_provenance.get("profile")),
            "model": _text(candidate_provenance.get("model")),
            "output_kind": kind,
            "candidate_id": _text(result.get("selected_candidate_id")),
            "role_id": _text(result.get("selected_role_id")),
            "selection_policy": _text(result.get("selection_policy")),
            "candidate_sha256": _canonical_json_sha256(candidate),
            "prompt_sha256": _text(candidate_provenance.get("prompt_sha256")),
            "message_content_sha256": _text(candidate_provenance.get("message_content_sha256")),
        },
        "review_summary": _review_context(repo_root, task_context, payload, cases),
        "source_review": _source_review(payload, cases),
        "cases": cases,
        "artifacts": _collect_artifacts(repo_root, result, candidate_path, planned_output_path),
        "machine_verification": {
            "authoring_accepted": True,
            "fixed_gate_ok": fixed_gate.get("ok") is True,
            "execution_requested": execution.get("requested") is True,
            "execution_ok": execution.get("ok") is True,
            "execution_status": _text(execution.get("status")),
        },
        "review_workflow": {
            "status": "awaiting_natural_language_comment",
            "managed_by": "harness_session_orchestrator",
            "user_editable": False,
        },
    }
    schema = _read_json(SCHEMA_PATH)
    errors = sorted(Draft202012Validator(schema).iter_errors(packet), key=lambda item: list(item.absolute_path))
    if errors:
        message = "; ".join(f"{'.'.join(map(str, item.absolute_path)) or '$'}: {item.message}" for item in errors[:8])
        raise ReviewPacketError(f"generated review packet failed schema validation: {message}")

    review_dir = task_root / "review"
    packet_path = review_dir / "review_packet.json"
    report_path = review_dir / "review_report.zh-CN.md"
    state_path = review_dir / "review_state.internal.json"
    _write_json(packet_path, packet)
    packet_sha256 = _sha256(packet_path)
    state = {
        "schema_version": 1,
        "review_packet": _display_path(repo_root, packet_path),
        "review_packet_sha256": packet_sha256,
        "status": "awaiting_natural_language_comment",
        "managed_by": "harness_session_orchestrator",
        "user_editable": False,
        "instructions_zh_cn": (
            "用户只需通过 Harness 提交自然语言 comment。轮次、状态、ID 和哈希均由宿主管理；"
            "请勿编辑本内部状态文件。"
        ),
    }
    _write_json(state_path, state)
    _write_text(report_path, _markdown(packet, _display_path(repo_root, state_path)))
    return {
        "schema_version": 1,
        "language": "zh-CN",
        "review_packet_path": _display_path(repo_root, packet_path),
        "review_packet_sha256": packet_sha256,
        "review_report_path": _display_path(repo_root, report_path),
        "review_report_sha256": _sha256(report_path),
        "review_state_path": _display_path(repo_root, state_path),
        "review_state_sha256": _sha256(state_path),
        "review_status": "awaiting_natural_language_comment",
        "artifact_count": len(packet["artifacts"]),
        "case_count": len(packet["cases"]),
    }


def verify_review_attestation(
    repo_root: Path,
    metadata: Mapping[str, Any],
    *,
    expected_candidate_sha256: str,
) -> tuple[bool, str]:
    """Revalidate the review packet, report, state, and every hashed artifact."""

    repo_root = repo_root.resolve()
    required = {
        "review_packet_path": "review_packet_sha256",
        "review_report_path": "review_report_sha256",
        "review_state_path": "review_state_sha256",
    }
    for path_key, hash_key in required.items():
        path = _repo_path(repo_root, metadata.get(path_key))
        expected_hash = _text(metadata.get(hash_key))
        if path is None or not path.is_file():
            return False, f"{path_key} is missing or outside the repository"
        if not expected_hash or _sha256(path) != expected_hash:
            return False, f"{path_key} hash mismatch"
    packet_path = _repo_path(repo_root, metadata.get("review_packet_path"))
    if packet_path is None:
        return False, "review packet path is invalid"
    try:
        packet = _read_json(packet_path)
        schema = _read_json(SCHEMA_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"review packet cannot be read: {exc}"
    errors = list(Draft202012Validator(schema).iter_errors(packet))
    if errors:
        return False, f"review packet schema mismatch: {errors[0].message}"
    generation = _as_dict(_as_dict(packet).get("generation"))
    if _text(generation.get("candidate_sha256")) != expected_candidate_sha256:
        return False, "review packet candidate hash does not match formal provenance"
    for artifact in _as_list(_as_dict(packet).get("artifacts")):
        if not isinstance(artifact, Mapping):
            return False, "review packet artifact entry is invalid"
        path = _repo_path(repo_root, artifact.get("path"))
        expected_hash = _text(artifact.get("sha256"))
        if path is None or not path.is_file():
            return False, f"review artifact is missing: {artifact.get('path')}"
        if not expected_hash or _sha256(path) != expected_hash:
            return False, f"review artifact hash mismatch: {artifact.get('path')}"
    return True, "verified"


def summarize_review_index(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = list(records)
    return {
        "record_count": len(values),
        "by_api": dict(sorted(Counter(_text(item.get("api")) or "unknown" for item in values).items())),
        "by_review_status": dict(
            sorted(
                Counter(
                    _text(_as_dict(item.get("review_workflow")).get("status"))
                    or "awaiting_natural_language_comment"
                    for item in values
                ).items()
            )
        ),
    }
