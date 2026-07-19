#!/usr/bin/env python3
"""Verify the four artifact lanes of one External SGGK Harness acceptance run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from test_harness.authoring_gateway.config import (
    PROFILE_SPECS,
    SILICONFLOW_DEFAULT_BASE_URL,
    SILICONFLOW_DEFAULT_MODEL,
)
from test_harness.orchestration.workflow import (
    HarnessWorkflow,
    SessionPaths,
    WorkflowError,
    _sha256_file,
    _sha256_json,
)
from test_harness.tools.compare_nx_sggk_step import (
    COMPARISON_SCHEMA,
    NX_MEASUREMENT_SCHEMA,
)
from test_harness.tools.verify_campaign_artifacts import Verifier as CampaignVerifier

SCHEMA_VERSION = 1
RESULT_KIND = "sggk_external_chain_verification"
HARNESS_ROOT = Path(__file__).resolve().parents[1]
RESULT_SCHEMA = HARNESS_ROOT / "schemas" / "external_chain_verification.schema.json"
SILICONFLOW_PROFILE = "siliconflow"
SILICONFLOW_CATEGORY = "external"
SILICONFLOW_SOURCE_TYPE = PROFILE_SPECS[SILICONFLOW_PROFILE].provenance_source_type
SILICONFLOW_ENDPOINT_SHA256 = hashlib.sha256(
    f"{SILICONFLOW_DEFAULT_BASE_URL}/chat/completions".encode("utf-8")
).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: Any) -> bool:
    text = str(value or "").casefold()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return left.resolve() == right.resolve()


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _display(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _fixed_entry(raw: Path, filename: str) -> Path:
    path = raw.expanduser().resolve()
    return path / filename if path.is_dir() else path


def _resolve_recorded_path(raw: Any, base: Path) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path is not recorded")
    path = Path(raw)
    return (path if path.is_absolute() else base / path).resolve()


def _require_inside(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes {root}") from exc


@dataclass
class Lane:
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)

    def error(self, code: str, message: str, path: Path | str = "") -> None:
        self.errors.append({"code": code, "message": message, "path": str(path) if path else ""})

    def warning(self, code: str, message: str, path: Path | str = "") -> None:
        self.warnings.append({"code": code, "message": message, "path": str(path) if path else ""})

    def add_evidence(
        self,
        code: str,
        message: str,
        value: Any,
        path: Path | str = "",
    ) -> None:
        artifact = Path(path) if path else None
        digest = _sha256_file(artifact) if artifact is not None and artifact.is_file() else ""
        self.evidence.append(
            {
                "code": code,
                "message": message,
                "value": _display(value),
                "path": str(artifact) if artifact is not None else "",
                "sha256": digest,
            }
        )

    def result(self) -> dict[str, Any]:
        ok = not self.errors
        return {
            "ok": ok,
            "status": "passed" if ok else "failed",
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": self.errors,
            "warnings": self.warnings,
            "evidence": self.evidence,
        }


def _load_for_lane(lane: Lane, path: Path, label: str, code: str) -> dict[str, Any] | None:
    if not path.is_file():
        lane.error(code, f"{label}不存在", path)
        return None
    try:
        return _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        lane.error(code, f"{label}不是可读的 JSON 对象：{exc}", path)
        return None


def _validate_schema(lane: Lane, value: dict[str, Any], schema_path: Path, label: str) -> bool:
    try:
        schema = _read_json(schema_path)
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(value),
            key=lambda item: [str(part) for part in item.absolute_path],
        )
    except Exception as exc:  # noqa: BLE001 - turn verifier defects into explicit evidence
        lane.error("SCHEMA_VALIDATION_ERROR", f"{label} schema 校验器失败：{exc}", schema_path)
        return False
    if not errors:
        return True
    first = errors[0]
    location = ".".join(str(part) for part in first.absolute_path) or "<root>"
    lane.error("SCHEMA_VIOLATION", f"{label}不符合 schema，位置 {location}：{first.message}", schema_path)
    return False


def _verify_abc_source_binding(
    lane: Lane,
    corpus_root: Path,
    source_path: Path,
    source_sha256: str,
) -> None:
    manifest_path = corpus_root / "corpus_manifest.json"
    manifest = _load_for_lane(
        lane,
        manifest_path,
        "ABC corpus manifest",
        "ABC_CORPUS_MANIFEST_MISSING",
    )
    if manifest is None:
        return
    if manifest.get("hash_inputs") is not True:
        lane.error("ABC_INPUT_HASHING_DISABLED", "corpus manifest 必须启用 hash_inputs", manifest_path)

    inputs = manifest.get("inputs")
    matching_inputs: list[dict[str, Any]] = []
    if isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, dict):
                continue
            try:
                recorded_source = _resolve_recorded_path(item.get("source_file"), corpus_root)
            except ValueError:
                continue
            if _same_file(recorded_source, source_path):
                matching_inputs.append(item)
    if len(matching_inputs) != 1:
        lane.error(
            "ABC_CORPUS_INPUT_BINDING_INVALID",
            "corpus manifest 必须且只能有 1 条 input 绑定已执行 STEP",
            manifest_path,
        )
    else:
        item = matching_inputs[0]
        if (
            item.get("api") != "step_import"
            or item.get("sha256") != source_sha256
            or item.get("size_bytes") != source_path.stat().st_size
        ):
            lane.error(
                "ABC_CORPUS_INPUT_HASH_MISMATCH",
                "corpus manifest 的 STEP SHA-256/大小/API 与实际输入不一致",
                manifest_path,
            )

    dataset_lists = manifest.get("dataset_lists")
    if not isinstance(dataset_lists, list) or not dataset_lists:
        lane.error("ABC_DATASET_INDEX_MISSING", "corpus manifest 未记录 dataset_index.json", manifest_path)
        return

    bound_indexes: list[tuple[Path, dict[str, Any], int]] = []
    for recorded_index in dataset_lists:
        try:
            index_path = _resolve_recorded_path(recorded_index, corpus_root)
        except ValueError:
            continue
        index = _load_for_lane(lane, index_path, "ABC dataset index", "ABC_DATASET_INDEX_INVALID")
        if index is None:
            continue
        files = index.get("files")
        if not isinstance(files, list):
            lane.error("ABC_DATASET_INDEX_INVALID", "dataset index.files 必须是数组", index_path)
            continue
        for position, item in enumerate(files):
            if not isinstance(item, dict):
                continue
            try:
                indexed_source = _resolve_recorded_path(item.get("path"), index_path.parent)
            except ValueError:
                continue
            if not _same_file(indexed_source, source_path):
                continue
            if (
                item.get("sha256") != source_sha256
                or item.get("size_bytes") != source_path.stat().st_size
            ):
                lane.error(
                    "ABC_DATASET_INDEX_HASH_MISMATCH",
                    "dataset index 中的 STEP SHA-256/大小与实际文件不一致",
                    index_path,
                )
            else:
                bound_indexes.append((index_path, index, position))
    if len(bound_indexes) != 1:
        lane.error(
            "ABC_DATASET_INDEX_BINDING_INVALID",
            "必须且只能有 1 个 dataset index 条目绑定已执行 STEP 及其 SHA-256",
            manifest_path,
        )
        return

    index_path, index, position = bound_indexes[0]
    provenance_path = index_path.parent / "source_archive_provenance.json"
    provenance = _load_for_lane(
        lane,
        provenance_path,
        "ABC download/extract provenance",
        "ABC_SOURCE_ARCHIVE_PROVENANCE_MISSING",
    )
    if provenance is None:
        return
    if provenance.get("kind") != "abc_download_extract_binding" or provenance.get("ok") is not True:
        lane.error(
            "ABC_SOURCE_ARCHIVE_PROVENANCE_INVALID",
            "download/extract provenance 必须为通过的 abc_download_extract_binding",
            provenance_path,
        )

    try:
        provenance_index = provenance["dataset_index"]
        recorded_index_path = _resolve_recorded_path(provenance_index.get("path"), provenance_path.parent)
        extraction = provenance["extraction"]
        extracted_path = _resolve_recorded_path(extraction.get("path"), provenance_path.parent)
        include_list = _resolve_recorded_path(extraction.get("include_list"), provenance_path.parent)
        plan = provenance["plan"]
        plan_path = _resolve_recorded_path(plan.get("path"), provenance_path.parent)
        archive = provenance["archive"]
        archive_path = _resolve_recorded_path(archive.get("path"), provenance_path.parent)
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        lane.error(
            "ABC_SOURCE_ARCHIVE_PROVENANCE_INVALID",
            f"download/extract provenance 路径字段无效：{exc}",
            provenance_path,
        )
        return

    if (
        not _same_file(recorded_index_path, index_path)
        or provenance_index.get("file_sha256") != _sha256_file(index_path)
        or provenance_index.get("selected_index") != position
        or provenance_index.get("recorded_source_sha256") != source_sha256
        or provenance_index.get("source_sha256_verified") is not True
    ):
        lane.error(
            "ABC_SOURCE_ARCHIVE_INDEX_BINDING_INVALID",
            "download/extract provenance 未绑定当前 dataset index 及选中 STEP",
            provenance_path,
        )
    if (
        not _same_file(extracted_path, source_path)
        or extraction.get("sha256") != source_sha256
        or extraction.get("size_bytes") != source_path.stat().st_size
    ):
        lane.error(
            "ABC_EXTRACTED_SOURCE_BINDING_INVALID",
            "download/extract provenance 未绑定实际测试 STEP",
            provenance_path,
        )
    if not include_list.is_file():
        lane.error("ABC_EXTRACT_INCLUDE_LIST_MISSING", "ABC 抽取 include list 不存在", include_list)
    else:
        member = str(extraction.get("archive_member") or "")
        members = {line.strip().replace("\\", "/") for line in include_list.read_text(encoding="utf-8-sig").splitlines()}
        if (
            not member
            or member.replace("\\", "/") not in members
            or extraction.get("include_list_sha256") != _sha256_file(include_list)
        ):
            lane.error(
                "ABC_EXTRACT_MEMBER_BINDING_INVALID",
                "抽取 include list 未绑定 provenance 中的归档成员",
                include_list,
            )

    plan_value = _load_for_lane(lane, plan_path, "ABC full fetch plan", "ABC_FETCH_PLAN_MISSING")
    if plan_value is None:
        return
    if plan.get("sha256") != _sha256_file(plan_path):
        lane.error("ABC_FETCH_PLAN_HASH_MISMATCH", "provenance 中的 fetch plan SHA-256 无效", plan_path)
    archive_records = plan_value.get("archives")
    matching_records = [
        item
        for item in archive_records or []
        if isinstance(item, dict)
        and item.get("archive") == archive_path.name
        and str(item.get("chunk") or "") == str(plan.get("chunk") or "")
        and item.get("format") == plan.get("format")
    ] if isinstance(archive_records, list) else []
    if len(matching_records) != 1:
        lane.error("ABC_FETCH_PLAN_ARCHIVE_MISSING", "fetch plan 未唯一记录当前 STEP 归档", plan_path)
        return
    record = matching_records[0]
    expected_size = record.get("size_bytes")
    expected_md5 = str(record.get("md5") or "").casefold()
    if not archive_path.is_file():
        lane.error("ABC_DOWNLOAD_ARCHIVE_MISSING", "ABC 下载归档不存在", archive_path)
        return
    actual_size = archive_path.stat().st_size
    actual_md5 = _hash_file(archive_path, "md5")
    if (
        expected_size != actual_size
        or archive.get("expected_size_bytes") != expected_size
        or archive.get("actual_size_bytes") != actual_size
        or archive.get("size_verified") is not True
        or not expected_md5
        or actual_md5 != expected_md5
        or str(archive.get("expected_md5") or "").casefold() != expected_md5
        or str(archive.get("actual_md5") or "").casefold() != actual_md5
        or archive.get("md5_verified") is not True
    ):
        lane.error(
            "ABC_DOWNLOAD_ARCHIVE_HASH_MISMATCH",
            "下载归档的实际大小/MD5 与 fetch plan/provenance 不一致",
            archive_path,
        )
    else:
        lane.add_evidence(
            "ABC_DOWNLOAD_ARCHIVE_VERIFIED",
            "下载归档大小与 MD5 已根据 fetch plan 重新计算",
            archive_path.name,
            archive_path,
        )
    lane.add_evidence(
        "ABC_DATASET_INDEX_BOUND",
        "已执行 STEP 绑定可验证 dataset index",
        f"index={position}; sha256={source_sha256}",
        index_path,
    )
    lane.add_evidence(
        "ABC_EXTRACTED_MEMBER_BOUND",
        "下载归档成员已绑定实际测试 STEP",
        extraction.get("archive_member"),
        provenance_path,
    )


def _verify_nx_orchestrator_binding(
    lane: Lane,
    bundle: Path,
    measurement_path: Path,
    comparison_path: Path,
    measurement: dict[str, Any],
    comparison: dict[str, Any],
) -> None:
    summary_path = bundle / "run_summary.json"
    summary = _load_for_lane(
        lane,
        summary_path,
        "NX/SGGK orchestrator summary",
        "NX_ORCHESTRATOR_SUMMARY_MISSING",
    )
    if summary is None:
        return
    if (
        summary.get("kind") != "nx_sggk_step_compare_run"
        or summary.get("ok") is not True
    ):
        lane.error(
            "NX_ORCHESTRATOR_NOT_COMPLETED",
            "固定 NX/SGGK orchestrator 必须完整结束且 ok=true",
            summary_path,
        )
    selection = summary.get("selection")
    paths = summary.get("paths")
    steps = summary.get("steps")
    if not isinstance(selection, dict) or not isinstance(paths, dict) or not isinstance(steps, dict):
        lane.error(
            "NX_ORCHESTRATOR_SUMMARY_INVALID",
            "orchestrator summary 缺少 selection/paths/steps",
            summary_path,
        )
        return
    try:
        source_path = _resolve_recorded_path(selection.get("source"), bundle)
        index_path = _resolve_recorded_path(selection.get("dataset_index"), bundle)
        recorded_measurement = _resolve_recorded_path(paths.get("nx_measurement"), bundle)
        recorded_comparison = _resolve_recorded_path(paths.get("comparison_json"), bundle)
    except ValueError as exc:
        lane.error("NX_ORCHESTRATOR_PATH_INVALID", str(exc), summary_path)
        return
    if not source_path.is_file():
        lane.error("NX_SOURCE_FILE_MISSING", "orchestrator 选中的 STEP 不存在", source_path)
        return
    actual_sha256 = _sha256_file(source_path)
    actual_size = source_path.stat().st_size
    if (
        selection.get("verified") is not True
        or selection.get("sha256") != actual_sha256
        or selection.get("size_bytes") != actual_size
        or measurement.get("input", {}).get("sha256") != actual_sha256
        or measurement.get("input", {}).get("size_bytes") != actual_size
        or comparison.get("input", {}).get("sha256") != actual_sha256
    ):
        lane.error(
            "NX_SOURCE_SHA_MISMATCH",
            "orchestrator/NX/SGGK comparison 未绑定实际 STEP SHA-256 与大小",
            source_path,
        )
    if not _same_file(recorded_measurement, measurement_path) or not _same_file(
        recorded_comparison,
        comparison_path,
    ):
        lane.error(
            "NX_ORCHESTRATOR_ARTIFACT_PATH_MISMATCH",
            "orchestrator summary 未绑定当前 measurement/comparison artifact",
            summary_path,
        )

    index = _load_for_lane(lane, index_path, "NX selected dataset index", "NX_DATASET_INDEX_MISSING")
    position = _int(selection.get("index"))
    files = index.get("files") if index is not None else None
    if position is None or not isinstance(files, list) or position >= len(files):
        lane.error("NX_DATASET_SELECTION_INVALID", "orchestrator 的稳定 files[index] 选择无效", index_path)
    else:
        item = files[position]
        try:
            indexed_source = _resolve_recorded_path(item.get("path"), index_path.parent)
        except (AttributeError, ValueError) as exc:
            lane.error("NX_DATASET_SELECTION_INVALID", str(exc), index_path)
        else:
            if (
                not _same_file(indexed_source, source_path)
                or item.get("sha256") != actual_sha256
                or item.get("size_bytes") != actual_size
            ):
                lane.error(
                    "NX_DATASET_SELECTION_HASH_MISMATCH",
                    "dataset index 的稳定选择未绑定实际 STEP",
                    index_path,
                )

    sggk_step = steps.get("sggk")
    nx_step = steps.get("nx")
    comparison_step = steps.get("comparison")
    expected_comparison_rc = 0 if comparison.get("ok") is True else 2
    expected_outcome = "comparison_passed" if comparison.get("ok") is True else "comparison_mismatch"
    if not (
        isinstance(sggk_step, dict)
        and sggk_step.get("status") == "completed"
        and sggk_step.get("returncode") == 0
        and isinstance(nx_step, dict)
        and nx_step.get("status") == "completed"
        and nx_step.get("returncode") == 0
        and isinstance(comparison_step, dict)
        and comparison_step.get("returncode") == expected_comparison_rc
        and summary.get("outcome") == expected_outcome
        and summary.get("comparison_ok") is comparison.get("ok")
    ):
        lane.error(
            "NX_ORCHESTRATOR_STEP_BINDING_INVALID",
            "SGGK/NX/comparison 返回码或 outcome 与 artifact 不一致",
            summary_path,
        )
    lane.add_evidence(
        "NX_ORCHESTRATOR_COMPLETED",
        "固定 ABC→SGGK→NX→comparison 编排已完成",
        summary.get("outcome"),
        summary_path,
    )
    lane.add_evidence(
        "NX_SOURCE_FILE_VERIFIED",
        "NX/SGGK 实际 STEP 已重新计算 SHA-256",
        actual_sha256,
        source_path,
    )


def _model_repo_root(session_file: Path, lane: Lane) -> tuple[Path, Path] | None:
    session_root = session_file.parent
    if (
        session_root.parent.name.casefold() != "harness_sessions"
        or session_root.parent.parent.name.casefold() != "artifacts"
    ):
        lane.error(
            "MODEL_SESSION_LAYOUT_INVALID",
            "session 必须位于 <repo>/artifacts/harness_sessions/<session-id>/session.json",
            session_file,
        )
        return None
    return session_root.parent.parent.parent.resolve(), session_root


def _round_checker(repo_root: Path) -> HarnessWorkflow:
    checker = HarnessWorkflow.__new__(HarnessWorkflow)
    checker.repo_root = repo_root
    return checker


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_usage(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    prompt_tokens = _int(value.get("prompt_tokens"))
    completion_tokens = _int(value.get("completion_tokens"))
    return (
        prompt_tokens is not None
        and completion_tokens is not None
        and prompt_tokens > 0
        and completion_tokens > 0
    )


def _provider_metadata_mismatches(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["provider metadata is not an object"]
    expected = {
        "profile": SILICONFLOW_PROFILE,
        "profile_category": SILICONFLOW_CATEGORY,
        "endpoint_sha256": SILICONFLOW_ENDPOINT_SHA256,
        "api_key_env": "SILICONFLOW_API_KEY",
        "api_key_present": True,
        "model": SILICONFLOW_DEFAULT_MODEL,
        "model_env": "SILICONFLOW_MODEL",
        "base_url_locked": True,
        "model_locked": True,
        "default_stream": True,
    }
    return [
        f"{key}={value.get(key)!r} (expected {expected_value!r})"
        for key, expected_value in expected.items()
        if value.get(key) != expected_value
    ]


def _verify_provider_metadata(
    lane: Lane,
    value: Any,
    *,
    code: str,
    label: str,
    path: Path,
) -> bool:
    mismatches = _provider_metadata_mismatches(value)
    if mismatches:
        lane.error(code, f"{label} 未锁定真实 SiliconFlow provider：" + "; ".join(mismatches), path)
        return False
    return True


def _response_content(record: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    body = record.get("body")
    if not isinstance(body, Mapping):
        return None, "provider response body is not an object"
    if not isinstance(body.get("id"), str) or not str(body.get("id") or "").strip():
        return None, "provider response has no non-empty id"
    if body.get("model") != SILICONFLOW_DEFAULT_MODEL:
        return None, f"provider response model={body.get('model')!r}"
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return None, "provider response has no choices[0] object"
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        return None, f"provider finish_reason={choice.get('finish_reason')!r}"
    message = choice.get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
        return None, "provider response has no string message.content"
    try:
        candidate = json.loads(message["content"])
    except json.JSONDecodeError as exc:
        return None, f"provider message.content is not JSON: {exc}"
    if not isinstance(candidate, dict):
        return None, "provider message.content JSON root is not an object"
    return candidate, ""


def _verify_provider_response(
    lane: Lane,
    artifact: Mapping[str, Any],
    *,
    expected_candidate: Mapping[str, Any],
    code: str,
    label: str,
    path: Path,
) -> tuple[Mapping[str, Any], str] | None:
    if (
        artifact.get("ok") is not True
        or artifact.get("error") not in (None, "")
        or artifact.get("error_kind") not in (None, "")
        or artifact.get("finish_reason") != "stop"
        or not _positive_usage(artifact.get("usage"))
    ):
        lane.error(code, f"{label} 未记录成功 completion 与正数 token usage", path)
        return None
    response_records = artifact.get("provider_responses")
    if not isinstance(response_records, list) or not response_records:
        lane.error(code, f"{label} 没有 provider_responses", path)
        return None

    selected: Mapping[str, Any] | None = None
    selected_candidate: dict[str, Any] | None = None
    rejection = "no HTTP 2xx provider response"
    for item in reversed(response_records):
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status < 300:
            continue
        candidate, response_error = _response_content(item)
        if candidate is None:
            rejection = response_error
            continue
        if _sha256_json(candidate) != _sha256_json(dict(expected_candidate)):
            rejection = "provider message.content does not match the accepted candidate"
            continue
        selected = item
        selected_candidate = candidate
        break
    if selected is None or selected_candidate is None:
        lane.error(code, f"{label} 没有可绑定的成功 provider response：{rejection}", path)
        return None

    body = selected["body"]
    selected_content = body["choices"][0]["message"]["content"]
    recorded_content_sha256 = artifact.get("message_content_sha256")
    if recorded_content_sha256 not in (None, "") and recorded_content_sha256 != hashlib.sha256(
        selected_content.encode("utf-8")
    ).hexdigest():
        lane.error(code, f"{label} 的 message_content_sha256 与 response body 不一致", path)
        return None
    body_usage = body.get("usage") if isinstance(body, Mapping) else None
    if not _positive_usage(body_usage) or dict(body_usage) != dict(artifact["usage"]):
        lane.error(code, f"{label} 的 response body usage 与 completion usage 不一致或为零", path)
        return None
    if selected.get("stream") is not True:
        lane.error(code, f"{label} 未使用锁定的 SiliconFlow SSE transport", path)
        return None
    headers = selected.get("headers")
    if not isinstance(headers, Mapping) or "text/event-stream" not in str(
        headers.get("content-type") or ""
    ).lower():
        lane.error(code, f"{label} 未记录 text/event-stream Content-Type", path)
        return None
    body_sha256 = selected.get("body_sha256")
    stream = selected.get("stream_metadata")
    if not (
        _is_sha256(body_sha256)
        and isinstance(stream, Mapping)
        and stream.get("raw_stream_sha256") == body_sha256
        and stream.get("raw_stream_complete") is True
        and stream.get("done") is True
        and stream.get("finish_reason") == "stop"
        and stream.get("error") in (None, "")
        and isinstance(stream.get("event_count"), int)
        and not isinstance(stream.get("event_count"), bool)
        and int(stream["event_count"]) > 0
    ):
        lane.error(code, f"{label} 的 SSE 完整性记录无效", path)
        return None

    events = artifact.get("events")
    matching_event = False
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, Mapping):
                continue
            if (
                event.get("status") == selected.get("status")
                and event.get("mode") == selected.get("mode")
                and event.get("transport_try") == selected.get("transport_try")
                and _is_sha256(event.get("request_sha256"))
                and event.get("retry") is False
            ):
                matching_event = True
                break
    if not matching_event:
        lane.error(code, f"{label} 未把成功 response 绑定到请求 SHA-256 event", path)
        return None
    return selected, str(selected_candidate and selected["body"].get("id") or "")


def _verify_attempt_hashes(lane: Lane, attempt_root: Path) -> dict[str, dict[str, Any]] | None:
    names = (
        "request_manifest.json",
        "raw_response.json",
        "contract_report.json",
        "candidate.json",
        "provenance.json",
    )
    hashes_path = attempt_root / "hashes.json"
    hashes = _load_for_lane(
        lane,
        hashes_path,
        "SiliconFlow gateway attempt hashes",
        "MODEL_PROVIDER_ATTEMPT_HASHES_MISSING",
    )
    if hashes is None:
        return None
    if hashes.get("algorithm") != "sha256":
        lane.error("MODEL_PROVIDER_ATTEMPT_HASHES_INVALID", "gateway attempt 未使用 SHA-256", hashes_path)
        return None
    loaded: dict[str, dict[str, Any]] = {}
    for name in names:
        artifact_path = attempt_root / name
        value = _load_for_lane(
            lane,
            artifact_path,
            f"SiliconFlow gateway {name}",
            "MODEL_PROVIDER_ATTEMPT_ARTIFACT_MISSING",
        )
        if value is None:
            return None
        if hashes.get(name) != _sha256_file(artifact_path):
            lane.error(
                "MODEL_PROVIDER_ATTEMPT_HASH_MISMATCH",
                f"{name} 与 hashes.json 不一致",
                artifact_path,
            )
            return None
        loaded[name] = value
    return loaded


def _verify_generation_provider_evidence(
    lane: Lane,
    *,
    repo_root: Path,
    session_root: Path,
    session: Mapping[str, Any],
    round_record: Mapping[str, Any],
) -> dict[str, Any] | None:
    try:
        provenance_path = _resolve_recorded_path(round_record.get("provenance_path"), repo_root)
        candidate_path = _resolve_recorded_path(round_record.get("candidate_path"), repo_root)
        manifest_path = _resolve_recorded_path(round_record.get("manifest_path"), repo_root)
        _require_inside(provenance_path, session_root, "model provenance")
        _require_inside(candidate_path, session_root, "model candidate")
        _require_inside(manifest_path, session_root, "model manifest")
    except ValueError as exc:
        lane.error("MODEL_PROVIDER_PROVENANCE_PATH_INVALID", str(exc), session_root)
        return None
    provenance = _load_for_lane(
        lane,
        provenance_path,
        "accepted model provenance",
        "MODEL_PROVIDER_PROVENANCE_MISSING",
    )
    candidate = _load_for_lane(
        lane,
        candidate_path,
        "accepted model candidate",
        "MODEL_PROVIDER_CANDIDATE_MISSING",
    )
    manifest = _load_for_lane(
        lane,
        manifest_path,
        "reviewed model task manifest",
        "MODEL_PROVIDER_MANIFEST_MISSING",
    )
    if provenance is None or candidate is None or manifest is None:
        return None

    expected_provenance = {
        "schema_version": 3,
        "request_id": round_record.get("task_id"),
        "run_id": round_record.get("run_id"),
        "source_type": SILICONFLOW_SOURCE_TYPE,
        "source_label": SILICONFLOW_PROFILE,
        "profile": SILICONFLOW_PROFILE,
        "model": SILICONFLOW_DEFAULT_MODEL,
        "interface": "openai_compatible_chat_completions_message_content_json",
        "output_path": round_record.get("candidate_path"),
        "candidate_sha256": round_record.get("candidate_sha256"),
    }
    mismatches = [
        f"{key}={provenance.get(key)!r}"
        for key, expected in expected_provenance.items()
        if provenance.get(key) != expected
    ]
    if round_record.get("session_id") != session.get("session_id"):
        mismatches.append("round session_id does not match session.json")
    boundary = provenance.get("boundary")
    acceptance = provenance.get("acceptance")
    fixed_gate = provenance.get("fixed_gate")
    if not (
        isinstance(boundary, Mapping)
        and boundary.get("model_calls") is True
        and boundary.get("direct_api_calls") is True
        and isinstance(acceptance, Mapping)
        and acceptance.get("authoring_accepted") is True
        and acceptance.get("requires_fixed_gate") is False
        and isinstance(fixed_gate, Mapping)
        and fixed_gate.get("ok") is True
        and _is_sha256(provenance.get("message_content_sha256"))
        and _positive_usage(provenance.get("usage"))
    ):
        mismatches.append("boundary/acceptance/fixed_gate/message hash/usage invalid")
    if mismatches:
        lane.error(
            "MODEL_PROVIDER_PROVENANCE_INVALID",
            "formal provenance 未证明 SiliconFlow authoring：" + "; ".join(mismatches),
            provenance_path,
        )
        return None

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], Mapping):
        lane.error("MODEL_PROVIDER_MANIFEST_INVALID", "reviewed manifest 必须只有一个 task", manifest_path)
        return None
    task = tasks[0]
    try:
        prompt_path = _resolve_recorded_path(task.get("prompt_path"), repo_root)
        _require_inside(prompt_path, session_root, "model prompt")
    except ValueError as exc:
        lane.error("MODEL_PROVIDER_PROMPT_PATH_INVALID", str(exc), manifest_path)
        return None
    if not prompt_path.is_file() or not (
        task.get("task_id") == round_record.get("task_id")
        and task.get("provider_profile") == SILICONFLOW_PROFILE
        and task.get("provider_profile_category") == SILICONFLOW_CATEGORY
        and task.get("data_classification") == "public_interface"
        and task.get("allowed_profile_categories") == [SILICONFLOW_CATEGORY]
        and provenance.get("source_path") == task.get("prompt_path")
        and provenance.get("task_prompt_sha256") == _sha256_file(prompt_path)
    ):
        lane.error(
            "MODEL_PROVIDER_PROMPT_BINDING_INVALID",
            "task prompt/profile/classification 未绑定 formal provenance",
            manifest_path,
        )
        return None

    round_number = int(round_record.get("round_number") or 0)
    round_root = session_root / "rounds" / f"{round_number:04d}"
    generation_path = round_root / "pipeline" / "generation_result.json"
    generation = _load_for_lane(
        lane,
        generation_path,
        "model generation_result",
        "MODEL_GENERATION_RESULT_MISSING",
    )
    if generation is None:
        return None
    results = generation.get("results")
    task_result = (
        results[0]
        if isinstance(results, list) and len(results) == 1 and isinstance(results[0], Mapping)
        else None
    )
    if not (
        generation.get("ok") is True
        and generation.get("run_id") == round_record.get("run_id")
        and isinstance(_int(generation.get("message_calls")), int)
        and int(generation["message_calls"]) > 0
        and task_result is not None
        and task_result.get("ok") is True
        and task_result.get("authoring_accepted") is True
        and task_result.get("task_id") == round_record.get("task_id")
        and task_result.get("run_id") == round_record.get("run_id")
        and task_result.get("accepted_path") == round_record.get("candidate_path")
        and task_result.get("provenance_path") == round_record.get("provenance_path")
    ):
        lane.error(
            "MODEL_GENERATION_RESULT_INVALID",
            "generation_result 未绑定本轮 accepted candidate/provenance",
            generation_path,
        )
        return None

    selection = provenance.get("candidate_selection")
    candidate_id = selection.get("candidate_id") if isinstance(selection, Mapping) else None
    candidates = task_result.get("candidates") if isinstance(task_result, Mapping) else None
    selected_candidate = next(
        (
            item
            for item in candidates
            if isinstance(item, Mapping) and item.get("candidate_id") == candidate_id
        ),
        None,
    ) if isinstance(candidates, list) else None
    if not (
        isinstance(candidate_id, str)
        and candidate_id
        and task_result.get("selected_candidate_id") == candidate_id
        and isinstance(selected_candidate, Mapping)
        and selected_candidate.get("candidate_sha256") == round_record.get("candidate_sha256")
    ):
        lane.error(
            "MODEL_PROVIDER_SELECTION_INVALID",
            "formal provenance 未绑定 generation_result 的 selected candidate",
            generation_path,
        )
        return None
    gate_attempt = _int(fixed_gate.get("gate_attempt"))
    candidate_attempts = selected_candidate.get("attempts")
    selected_attempt = (
        next(
            (
                item
                for item in candidate_attempts
                if isinstance(item, Mapping) and item.get("gate_attempt") == gate_attempt
            ),
            None,
        )
        if isinstance(candidate_attempts, list)
        else None
    )
    gateway = selected_attempt.get("gateway") if isinstance(selected_attempt, Mapping) else None
    gateway_attempt = _int(gateway.get("attempts")) if isinstance(gateway, Mapping) else None
    if not (
        gate_attempt is not None
        and gate_attempt > 0
        and isinstance(gateway, Mapping)
        and gateway.get("ok") is True
        and gateway.get("skipped") is False
        and gateway.get("run_id") == round_record.get("run_id")
        and gateway_attempt is not None
        and gateway_attempt > 0
    ):
        lane.error(
            "MODEL_PROVIDER_GATEWAY_BINDING_INVALID",
            "selected candidate 未绑定一次未跳过的 gateway call",
            generation_path,
        )
        return None
    try:
        gateway_root = _resolve_recorded_path(gateway.get("staging_path"), repo_root)
        _require_inside(gateway_root, round_root / "pipeline", "gateway staging")
    except ValueError as exc:
        lane.error("MODEL_PROVIDER_GATEWAY_PATH_INVALID", str(exc), generation_path)
        return None
    attempt_root = gateway_root / f"attempt_{gateway_attempt:02d}"
    attempt_artifacts = _verify_attempt_hashes(lane, attempt_root)
    if attempt_artifacts is None:
        return None
    request = attempt_artifacts["request_manifest.json"]
    raw_response = attempt_artifacts["raw_response.json"]
    attempt_provenance = attempt_artifacts["provenance.json"]
    attempt_candidate = attempt_artifacts["candidate.json"]
    contract = attempt_artifacts["contract_report.json"]
    if not _verify_provider_metadata(
        lane,
        request.get("provider"),
        code="MODEL_PROVIDER_REQUEST_PROFILE_INVALID",
        label="generation request",
        path=attempt_root / "request_manifest.json",
    ):
        return None
    prompt = request.get("prompt")
    response_options = request.get("response_options")
    attempt_expected = {
        "run_id": round_record.get("run_id"),
        "task_id": gateway.get("task_id"),
        "attempt": gateway_attempt,
    }
    if not (
        all(request.get(key) == expected for key, expected in attempt_expected.items())
        and isinstance(prompt, Mapping)
        and prompt.get("user_sha256") == provenance.get("prompt_sha256")
        and isinstance(response_options, Mapping)
        and response_options.get("stream") is True
        and contract.get("ok") is True
        and _sha256_json(attempt_candidate) == round_record.get("candidate_sha256")
    ):
        lane.error(
            "MODEL_PROVIDER_REQUEST_BINDING_INVALID",
            "gateway request/contract/candidate 未绑定本轮 formal provenance",
            attempt_root,
        )
        return None
    expected_attempt_provenance = {
        "run_id": round_record.get("run_id"),
        "task_id": gateway.get("task_id"),
        "attempt": gateway_attempt,
        "profile": SILICONFLOW_PROFILE,
        "source_type": SILICONFLOW_SOURCE_TYPE,
        "model": SILICONFLOW_DEFAULT_MODEL,
        "prompt_sha256": provenance.get("prompt_sha256"),
        "message_content_sha256": provenance.get("message_content_sha256"),
        "candidate_sha256": round_record.get("candidate_sha256"),
    }
    attempt_boundary = attempt_provenance.get("boundary")
    promotion = attempt_provenance.get("promotion")
    if not (
        all(
            attempt_provenance.get(key) == expected
            for key, expected in expected_attempt_provenance.items()
        )
        and attempt_provenance.get("usage") == provenance.get("usage")
        and isinstance(attempt_boundary, Mapping)
        and attempt_boundary.get("model_calls") is True
        and attempt_boundary.get("direct_api_calls") is True
        and isinstance(promotion, Mapping)
        and promotion.get("eligible") is True
        and promotion.get("completed") is True
    ):
        lane.error(
            "MODEL_PROVIDER_ATTEMPT_PROVENANCE_INVALID",
            "gateway attempt provenance 与 formal provenance 不一致",
            attempt_root / "provenance.json",
        )
        return None
    verified_response = _verify_provider_response(
        lane,
        raw_response,
        expected_candidate=attempt_candidate,
        code="MODEL_PROVIDER_GENERATION_RESPONSE_INVALID",
        label="generation response",
        path=attempt_root / "raw_response.json",
    )
    if verified_response is None:
        return None
    if not (
        raw_response.get("message_content_sha256")
        == attempt_provenance.get("message_content_sha256")
        == provenance.get("message_content_sha256")
        and raw_response.get("usage") == provenance.get("usage")
    ):
        lane.error(
            "MODEL_PROVIDER_RESPONSE_PROVENANCE_MISMATCH",
            "raw generation response 未绑定 attempt/formal provenance",
            attempt_root / "raw_response.json",
        )
        return None
    response_record, response_id = verified_response
    lane.add_evidence(
        "MODEL_SILICONFLOW_GENERATION_VERIFIED",
        "锁定 endpoint 的 SiliconFlow GLM-5.2 generation request/response 已与本轮候选交叉复核",
        {
            "response_id": response_id,
            "usage": raw_response.get("usage"),
            "stream": response_record.get("stream"),
        },
        attempt_root / "hashes.json",
    )
    lane.add_evidence(
        "MODEL_PROVIDER_EVIDENCE_SCOPE",
        "provider 证据是本地 SHA-256 交叉绑定的 transport 记录，不是服务商签名的不可抵赖回执",
        "local_hash_bound_transport_evidence",
        provenance_path,
    )
    return {
        "provenance": provenance,
        "provenance_path": provenance_path,
        "prompt_path": prompt_path,
        "response_id": response_id,
    }


def _verify_comment_provider_evidence(
    lane: Lane,
    *,
    repo_root: Path,
    session_root: Path,
    paths: SessionPaths,
    approval: Mapping[str, Any],
    approval_path: Path,
    round_record: Mapping[str, Any],
    generation_evidence: Mapping[str, Any] | None,
) -> None:
    if approval.get("authority") != "fixed_harness_host_after_model_comment_interpretation":
        lane.error(
            "MODEL_APPROVAL_AUTHORITY_INVALID",
            "execution approval 未声明固定 host 在模型解释后授权",
            approval_path,
        )
        return
    try:
        comment_path = _resolve_recorded_path(approval.get("comment_path"), repo_root)
        interpretation_path = _resolve_recorded_path(approval.get("interpretation_path"), repo_root)
        round_number = int(round_record.get("round_number") or 0)
        comments_root = session_root / "rounds" / f"{round_number:04d}" / "comments"
        _require_inside(comment_path, comments_root, "approval comment")
        _require_inside(interpretation_path, comments_root, "approval interpretation")
        if comment_path.parent != interpretation_path.parent:
            raise ValueError("approval comment and interpretation do not share one comment root")
    except ValueError as exc:
        lane.error("MODEL_APPROVAL_INTERPRETATION_PATH_INVALID", str(exc), approval_path)
        return
    interpretation = _load_for_lane(
        lane,
        interpretation_path,
        "model comment interpretation",
        "MODEL_COMMENT_INTERPRETATION_MISSING",
    )
    if interpretation is None or not comment_path.is_file():
        if not comment_path.is_file():
            lane.error("MODEL_APPROVAL_COMMENT_MISSING", "approval user_comment.txt 不存在", comment_path)
        return
    try:
        comment_text = comment_path.read_text(encoding="utf-8")
    except OSError as exc:
        lane.error("MODEL_APPROVAL_COMMENT_INVALID", f"无法读取 approval comment：{exc}", comment_path)
        return
    generation_provenance = (
        generation_evidence.get("provenance")
        if isinstance(generation_evidence, Mapping)
        and isinstance(generation_evidence.get("provenance"), Mapping)
        else {}
    )
    if not (
        approval.get("comment_sha256") == _sha256_file(comment_path)
        and approval.get("interpretation_sha256") == _sha256_json(interpretation)
        and approval.get("task_prompt_sha256") == generation_provenance.get("task_prompt_sha256")
        and approval.get("review_packet_sha256") == round_record.get("review_packet_sha256")
    ):
        lane.error(
            "MODEL_APPROVAL_INTERPRETATION_BINDING_INVALID",
            "approval 未哈希绑定 comment/interpretation/prompt/review packet",
            approval_path,
        )
        return
    decision = interpretation.get("decision")
    expected_interpretation = {
        "schema_version": 2,
        "record_type": "review_comment_decision",
        "status": "model_interpreted",
        "source": "model_message_api",
        "model_called": True,
        "task_id": round_record.get("task_id"),
        "run_id": round_record.get("run_id"),
        "round_number": round_record.get("round_number"),
        "subject_sha256": round_record.get("subject_digest_sha256"),
        "comment_sha256": approval.get("comment_sha256"),
    }
    if not (
        all(
            interpretation.get(key) == expected
            for key, expected in expected_interpretation.items()
        )
        and interpretation.get("user_comment") == comment_text
        and isinstance(decision, Mapping)
        and decision.get("decision") == "approve"
        and interpretation.get("response_sha256") == _sha256_json(dict(decision))
    ):
        lane.error(
            "MODEL_COMMENT_INTERPRETATION_INVALID",
            "comment interpretation 未绑定当前 round/comment 或不是模型 approve 决策",
            interpretation_path,
        )
        return
    if not _verify_provider_metadata(
        lane,
        interpretation.get("provider"),
        code="MODEL_COMMENT_PROVIDER_PROFILE_INVALID",
        label="comment interpretation",
        path=interpretation_path,
    ):
        return

    comment_root = interpretation_path.parent
    message_task_path = comment_root / "message_task.json"
    message_task = _load_for_lane(
        lane,
        message_task_path,
        "comment message_task",
        "MODEL_COMMENT_TASK_MISSING",
    )
    attempt_number = _int(interpretation.get("message_attempts"))
    if message_task is None or attempt_number is None or attempt_number < 1:
        lane.error(
            "MODEL_COMMENT_ATTEMPT_INVALID",
            "comment interpretation 未记录正数 message_attempts",
            interpretation_path,
        )
        return
    message_attempt_path = comment_root / f"message_attempt_{attempt_number:02d}.json"
    message_attempt = _load_for_lane(
        lane,
        message_attempt_path,
        "comment model response",
        "MODEL_COMMENT_RESPONSE_MISSING",
    )
    if message_attempt is None:
        return
    context = message_task.get("context")
    task_expected = {
        "task_type": "review_comment",
        "comment_sha256": interpretation.get("comment_sha256"),
        "context_sha256": interpretation.get("context_sha256"),
        "contract_sha256": interpretation.get("contract_sha256"),
    }
    if not (
        all(message_task.get(key) == expected for key, expected in task_expected.items())
        and message_task.get("comment") == comment_text
        and isinstance(context, Mapping)
        and context.get("task_id") == round_record.get("task_id")
        and context.get("run_id") == round_record.get("run_id")
        and context.get("round_number") == round_record.get("round_number")
        and context.get("subject_sha256") == round_record.get("subject_digest_sha256")
    ):
        lane.error(
            "MODEL_COMMENT_TASK_BINDING_INVALID",
            "comment message_task 未绑定 current round/subject/comment contract",
            message_task_path,
        )
        return
    verified_response = _verify_provider_response(
        lane,
        message_attempt,
        expected_candidate=decision,
        code="MODEL_COMMENT_PROVIDER_RESPONSE_INVALID",
        label="comment interpretation response",
        path=message_attempt_path,
    )
    if verified_response is None:
        return

    interpreted_sequence = 0
    approved_sequence = 0
    event_count = int(paths.session_file.is_file() and _read_json(paths.session_file).get("event_sequence") or 0)
    for sequence in range(1, event_count + 1):
        event = _read_json(paths.events_root / f"{sequence:06d}.json")
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if (
            event.get("event_type") == "COMMENT_INTERPRETED"
            and payload.get("round_number") == round_record.get("round_number")
            and payload.get("decision") == "approve"
            and payload.get("interpretation_sha256") == approval.get("interpretation_sha256")
        ):
            interpreted_sequence = sequence
        if (
            event.get("event_type") == "EXECUTION_APPROVED"
            and payload.get("round_number") == round_record.get("round_number")
            and payload.get("approval_sha256") == approval.get("approval_sha256")
        ):
            approved_sequence = sequence
    if not (0 < interpreted_sequence < approved_sequence):
        lane.error(
            "MODEL_COMMENT_EVENT_BINDING_INVALID",
            "event chain 未按顺序绑定 COMMENT_INTERPRETED 与 EXECUTION_APPROVED",
            paths.events_root,
        )
        return
    response_record, response_id = verified_response
    lane.add_evidence(
        "MODEL_SILICONFLOW_COMMENT_VERIFIED",
        "SiliconFlow GLM-5.2 对自然语言批准的 response 已绑定 interpretation、approval 与事件链",
        {
            "response_id": response_id,
            "usage": message_attempt.get("usage"),
            "stream": response_record.get("stream"),
        },
        message_attempt_path,
    )


def _verify_model_lane(raw: Path) -> dict[str, Any]:
    lane = Lane()
    session_file = _fixed_entry(raw, "session.json")
    session = _load_for_lane(lane, session_file, "Harness session", "MODEL_SESSION_MISSING")
    if session is None:
        return lane.result()
    layout = _model_repo_root(session_file, lane)
    if layout is None:
        return lane.result()
    repo_root, session_root = layout
    paths = SessionPaths(repo_root, session_root.parent, session_root)

    if session.get("state") != "completed":
        lane.error(
            "MODEL_SESSION_NOT_COMPLETED",
            f"session.state={session.get('state')!r}，必须为 completed",
            session_file,
        )
    if not (
        session.get("profile") == SILICONFLOW_PROFILE
        and session.get("provider_profile") == SILICONFLOW_PROFILE
        and session.get("provider_profile_category") == SILICONFLOW_CATEGORY
    ):
        lane.error(
            "MODEL_PROFILE_NOT_SILICONFLOW",
            "session 必须精确绑定 siliconflow/external，不能只声明 external category",
            session_file,
        )
    if session.get("data_classification") != "public_interface":
        lane.error(
            "MODEL_DATA_CLASSIFICATION_INVALID",
            "External session 必须绑定 public_interface 数据分类",
            session_file,
        )
    try:
        HarnessWorkflow._verify_event_head(session, paths)
    except (WorkflowError, OSError, ValueError, json.JSONDecodeError) as exc:
        lane.error("MODEL_EVENT_CHAIN_INVALID", f"事件 hash chain 无效：{exc}", paths.events_root)
        return lane.result()
    lane.add_evidence(
        "MODEL_EVENT_CHAIN_VERIFIED",
        "事件序列及 head SHA-256 已逐条复核",
        session.get("event_sequence"),
        session_file,
    )

    try:
        round_record = _round_checker(repo_root)._load_round(session, paths)  # noqa: SLF001
    except (WorkflowError, OSError, ValueError, json.JSONDecodeError) as exc:
        lane.error("MODEL_ROUND_BINDING_INVALID", f"当前不可变 review round 无效：{exc}", session_root / "rounds")
        return lane.result()
    if not (
        round_record.get("provider_profile") == SILICONFLOW_PROFILE
        and round_record.get("provider_profile_category") == SILICONFLOW_CATEGORY
        and round_record.get("data_classification") == "public_interface"
        and round_record.get("allowed_profile_categories") == [SILICONFLOW_CATEGORY]
    ):
        lane.error(
            "MODEL_ROUND_PROFILE_INVALID",
            "review round 未精确绑定 siliconflow/external/public_interface policy",
            session_root / "rounds",
        )
    generation_evidence = _verify_generation_provider_evidence(
        lane,
        repo_root=repo_root,
        session_root=session_root,
        session=session,
        round_record=round_record,
    )

    current_round = _int(session.get("current_round"))
    if current_round is None or current_round < 1 or session.get("approved_round") != current_round:
        lane.error("MODEL_APPROVED_ROUND_MISMATCH", "approved_round 必须等于 current_round", session_file)

    try:
        approval_path = _resolve_recorded_path(session.get("approval_path"), repo_root)
        _require_inside(approval_path, session_root, "approval")
    except ValueError as exc:
        lane.error("MODEL_APPROVAL_PATH_INVALID", str(exc), session_file)
        return lane.result()
    approval = _load_for_lane(lane, approval_path, "execution approval", "MODEL_APPROVAL_MISSING")
    if approval is None:
        return lane.result()
    unsigned_approval = {key: value for key, value in approval.items() if key != "approval_sha256"}
    if approval.get("approval_sha256") != _sha256_json(unsigned_approval):
        lane.error("MODEL_APPROVAL_HASH_INVALID", "approval_sha256 与 approval 内容不一致", approval_path)
    approval_fields = {
        "record_type": "execution_approval",
        "decision": "approved_for_execution",
        "session_id": session.get("session_id"),
        "task_id": round_record.get("task_id"),
        "round_number": round_record.get("round_number"),
        "round_sha256": round_record.get("round_sha256"),
        "candidate_sha256": round_record.get("candidate_sha256"),
        "reviewed_manifest_sha256": round_record.get("manifest_sha256"),
    }
    for key, expected in approval_fields.items():
        if approval.get(key) != expected:
            lane.error("MODEL_APPROVAL_BINDING_INVALID", f"approval.{key} 未绑定当前 session/round", approval_path)
    _verify_comment_provider_evidence(
        lane,
        repo_root=repo_root,
        session_root=session_root,
        paths=paths,
        approval=approval,
        approval_path=approval_path,
        round_record=round_record,
        generation_evidence=generation_evidence,
    )

    try:
        manifest_path = _resolve_recorded_path(session.get("execution_manifest_path"), repo_root)
        _require_inside(manifest_path, session_root, "execution manifest")
    except ValueError as exc:
        lane.error("MODEL_EXECUTION_MANIFEST_PATH_INVALID", str(exc), session_file)
        return lane.result()
    execution_manifest = _load_for_lane(
        lane,
        manifest_path,
        "approved execution manifest",
        "MODEL_EXECUTION_MANIFEST_MISSING",
    )
    if execution_manifest is None:
        return lane.result()
    manifest_hash = _sha256_file(manifest_path)
    if not (
        session.get("execution_manifest_sha256")
        == approval.get("execution_manifest_sha256")
        == manifest_hash
    ):
        lane.error(
            "MODEL_EXECUTION_MANIFEST_HASH_INVALID",
            "execution manifest 的 session/approval/file SHA 不一致",
            manifest_path,
        )
    if approval.get("execution_manifest_path") != session.get("execution_manifest_path"):
        lane.error(
            "MODEL_EXECUTION_MANIFEST_BINDING_INVALID",
            "approval 与 session 的 execution manifest 路径不一致",
            manifest_path,
        )
    tasks = execution_manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        lane.error("MODEL_EXECUTION_TASK_INVALID", "execution manifest 必须包含且只包含一个 task", manifest_path)
    else:
        task = tasks[0]
        expected_task = {
            "task_id": round_record.get("task_id"),
            "harness_session_id": session.get("session_id"),
            "harness_round_number": round_record.get("round_number"),
            "approved_round_sha256": round_record.get("round_sha256"),
            "approved_candidate_sha256": round_record.get("candidate_sha256"),
            "approval_attestation_path": session.get("approval_path"),
        }
        for key, expected in expected_task.items():
            if task.get(key) != expected:
                lane.error(
                    "MODEL_EXECUTION_TASK_BINDING_INVALID",
                    f"execution task.{key} 未绑定批准轮次",
                    manifest_path,
                )

    event_count = int(session.get("event_sequence") or 0)
    completion_event = _read_json(paths.events_root / f"{event_count:06d}.json")
    if completion_event.get("event_type") != "EXECUTION_COMPLETED":
        lane.error("MODEL_COMPLETION_EVENT_MISSING", "事件链末尾必须是 EXECUTION_COMPLETED", paths.events_root)
        return lane.result()
    payload = completion_event.get("payload")
    if not isinstance(payload, dict):
        lane.error("MODEL_COMPLETION_PAYLOAD_INVALID", "EXECUTION_COMPLETED payload 必须是对象", paths.events_root)
        return lane.result()
    if payload.get("round_number") != round_record.get("round_number"):
        lane.error("MODEL_COMPLETION_ROUND_MISMATCH", "completion event 未绑定批准轮次", paths.events_root)
    try:
        execution_result_path = _resolve_recorded_path(payload.get("execution_result_path"), repo_root)
        final_report_path = _resolve_recorded_path(session.get("final_report_path"), repo_root)
        _require_inside(execution_result_path, session_root, "execution result")
        _require_inside(final_report_path, session_root, "final report")
    except ValueError as exc:
        lane.error("MODEL_COMPLETION_PATH_INVALID", str(exc), paths.events_root)
        return lane.result()
    execution_result = _load_for_lane(
        lane,
        execution_result_path,
        "execution_result.json",
        "MODEL_EXECUTION_RESULT_MISSING",
    )
    if execution_result is None:
        return lane.result()
    if _sha256_file(execution_result_path) != payload.get("execution_result_sha256"):
        lane.error(
            "MODEL_EXECUTION_RESULT_HASH_INVALID",
            "execution_result SHA 与 completion event 不一致",
            execution_result_path,
        )
    if not final_report_path.is_file():
        lane.error("MODEL_FINAL_REPORT_MISSING", "最终中文报告不存在", final_report_path)
    elif _sha256_file(final_report_path) != payload.get("final_report_sha256"):
        lane.error("MODEL_FINAL_REPORT_HASH_INVALID", "最终报告 SHA 与 completion event 不一致", final_report_path)
    results = execution_result.get("results")
    first = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    execution = first.get("execution") if isinstance(first.get("execution"), dict) else {}
    if not (
        execution_result.get("ok") is True
        and execution.get("requested") is True
        and execution.get("ok") is True
    ):
        lane.error(
            "MODEL_SDK_EXECUTION_NOT_PASSED",
            "execution_result 未证明真实 SDK execution requested=true 且 ok=true",
            execution_result_path,
        )

    lane.add_evidence(
        "MODEL_SESSION_COMPLETED",
        "External 模型 Harness session 已完成",
        session.get("session_id"),
        session_file,
    )
    lane.add_evidence(
        "MODEL_APPROVAL_BOUND",
        "执行批准已绑定不可变 round",
        approval.get("approval_sha256"),
        approval_path,
    )
    lane.add_evidence("MODEL_EXECUTION_PASSED", "批准后的 SDK execution 已通过", True, execution_result_path)
    lane.add_evidence(
        "MODEL_FINAL_REPORT_BOUND",
        "最终报告 SHA 已绑定 completion event",
        payload.get("final_report_sha256"),
        final_report_path,
    )
    return lane.result()


def _verify_abc_lane(raw: Path) -> dict[str, Any]:
    lane = Lane()
    summary_path = _fixed_entry(raw, "corpus_summary.json")
    summary = _load_for_lane(lane, summary_path, "ABC corpus summary", "ABC_CORPUS_SUMMARY_MISSING")
    if summary is None:
        return lane.result()
    corpus_root = summary_path.parent
    try:
        recorded_root = _resolve_recorded_path(summary.get("out_root"), corpus_root)
        if not _same_file(recorded_root, corpus_root):
            lane.error("ABC_CORPUS_ROOT_MISMATCH", "corpus_summary.out_root 未绑定当前 artifact 目录", summary_path)
    except ValueError as exc:
        lane.error("ABC_CORPUS_ROOT_MISSING", str(exc), summary_path)

    total = _int(summary.get("total"))
    executed = _int(summary.get("executed"))
    skipped = _int(summary.get("skipped"))
    passed = _int(summary.get("passed"))
    failed = _int(summary.get("failed"))
    results = summary.get("results")
    if None in {total, executed, skipped, passed, failed} or not isinstance(results, list):
        lane.error("ABC_CORPUS_COUNTERS_INVALID", "corpus summary 必须记录非负整数计数和 results 数组", summary_path)
        return lane.result()
    if total != len(results) or executed + skipped != total or passed + failed != total:
        lane.error(
            "ABC_CORPUS_COUNTERS_INCONSISTENT",
            "corpus total/executed/skipped/passed/failed 与 results 不一致",
            summary_path,
        )
    executed_results = [item for item in results if isinstance(item, dict) and item.get("skipped") is not True]
    if executed != len(executed_results) or executed < 1:
        lane.error("ABC_CORPUS_NOT_EXECUTED", "ABC/SGGK corpus 必须实际执行至少 1 例", summary_path)

    step_results = [item for item in executed_results if item.get("api") == "step_import"]
    if not step_results:
        lane.error("ABC_STEP_IMPORT_MISSING", "已执行结果中没有 step_import 接口案例", summary_path)
    else:
        sample = step_results[0]
        try:
            recipe_path = _resolve_recorded_path(sample.get("recipe"), corpus_root)
            _require_inside(recipe_path, corpus_root, "ABC recipe")
            recipe = _read_json(recipe_path)
            source_path = _resolve_recorded_path(sample.get("source_file"), corpus_root)
            case_id = str(recipe.get("case_id") or "")
            case_root = (corpus_root / case_id).resolve()
            _require_inside(case_root, corpus_root, "ABC case")
            manifest_path = case_root / "manifest.json"
            status_path = case_root / "report" / "status.json"
            manifest = _read_json(manifest_path)
            status = _read_json(status_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            lane.error("ABC_CASE_ARTIFACT_INVALID", f"已执行 STEP 案例缺少可复核 artifact：{exc}", corpus_root)
        else:
            source_valid = source_path.is_file() and source_path.suffix.casefold() in {".step", ".stp"}
            source_sha256 = _sha256_file(source_path) if source_valid else ""
            if not source_valid:
                lane.error("ABC_SOURCE_INVALID", "ABC source_file 不是现存 STEP 文件", source_path)
            if recipe.get("api") != "step_import" or manifest.get("api") != "step_import":
                lane.error("ABC_API_BINDING_INVALID", "recipe/manifest 未绑定 step_import", manifest_path)
            if manifest.get("case_id") != case_id:
                lane.error("ABC_CASE_ID_MISMATCH", "manifest.case_id 与 recipe.case_id 不一致", manifest_path)
            if not isinstance(status.get("succeeded"), bool):
                lane.error("ABC_STATUS_INVALID", "SGGK status.succeeded 必须是布尔值", status_path)
            lane.add_evidence(
                "ABC_STEP_CASE_EXECUTED",
                "SGGK step_import 案例已生成状态 artifact",
                case_id,
                status_path,
            )
            lane.add_evidence(
                "ABC_SOURCE_BOUND",
                "执行案例绑定的 ABC STEP SHA-256",
                source_sha256,
                source_path,
            )
            if source_valid and _valid_sha256(source_sha256):
                _verify_abc_source_binding(lane, corpus_root, source_path, source_sha256)

    triage = summary.get("triage")
    if not isinstance(triage, dict):
        lane.error("ABC_TRIAGE_NOT_RECORDED", "corpus summary 未记录 triage", summary_path)
        return lane.result()
    try:
        triage_root = _resolve_recorded_path(triage.get("out"), corpus_root)
        _require_inside(triage_root, corpus_root, "ABC triage")
    except ValueError as exc:
        lane.error("ABC_TRIAGE_PATH_INVALID", str(exc), summary_path)
        return lane.result()
    triage_path = triage_root / "triage_summary.json"
    triage_summary = _load_for_lane(lane, triage_path, "ABC triage summary", "ABC_TRIAGE_SUMMARY_MISSING")
    if triage_summary is None:
        return lane.result()
    total_cases = _int(triage_summary.get("total_cases"))
    failed_cases = _int(triage_summary.get("failed_cases"))
    passed_cases = _int(triage_summary.get("passed_cases"))
    if total_cases is None or failed_cases is None or passed_cases is None:
        lane.error(
            "ABC_TRIAGE_COUNTERS_MISSING",
            "triage 必须明确记录 total_cases/passed_cases/failed_cases",
            triage_path,
        )
    else:
        if total_cases < 1 or total_cases != executed:
            lane.error(
                "ABC_TRIAGE_SCOPE_MISMATCH",
                "triage.total_cases 必须与实际执行案例数一致且至少为 1",
                triage_path,
            )
        if passed_cases + failed_cases != total_cases:
            lane.error(
                "ABC_TRIAGE_COUNTERS_INCONSISTENT",
                "triage passed_cases + failed_cases != total_cases",
                triage_path,
            )
        if failed_cases:
            lane.warning("ABC_TRIAGE_HAS_FAILURES", f"ABC triage 明确记录 {failed_cases} 个失败案例", triage_path)
        lane.add_evidence("ABC_CORPUS_EXECUTED", "ABC/SGGK corpus 实际执行数", executed, summary_path)
        lane.add_evidence("ABC_TRIAGE_FAILURE_COUNT", "ABC triage 明确失败数", failed_cases, triage_path)
    return lane.result()


def _verify_nx_lane(raw: Path) -> dict[str, Any]:
    lane = Lane()
    bundle = raw.expanduser().resolve()
    if bundle.is_file():
        bundle = bundle.parent.parent if bundle.parent.name.casefold() == "comparison" else bundle.parent
    measurement_path = bundle / "nx" / "measurement.json"
    comparison_path = bundle / "comparison" / "comparison.json"
    measurement = _load_for_lane(lane, measurement_path, "NX measurement", "NX_MEASUREMENT_MISSING")
    comparison = _load_for_lane(lane, comparison_path, "NX/SGGK comparison", "NX_COMPARISON_MISSING")
    if measurement is None or comparison is None:
        return lane.result()
    measurement_valid = _validate_schema(lane, measurement, NX_MEASUREMENT_SCHEMA, "NX measurement")
    comparison_valid = _validate_schema(lane, comparison, COMPARISON_SCHEMA, "NX/SGGK comparison")
    if not measurement_valid or not comparison_valid:
        return lane.result()
    if not (
        measurement.get("ok") is True
        and measurement.get("status") == "completed"
        and isinstance(measurement.get("import"), dict)
        and measurement["import"].get("ok") is True
        and isinstance(measurement.get("measurement"), dict)
        and measurement["measurement"].get("ok") is True
    ):
        lane.error("NX_MEASUREMENT_NOT_COMPLETED", "NX STEP import 与 measurement 必须均成功完成", measurement_path)
    nx_sha = measurement["input"]["sha256"]
    comparison_input = comparison["input"]
    if not (
        comparison_input.get("same_input") is True
        and comparison_input.get("sha256") == nx_sha
        and comparison_input.get("nx_sha256") == nx_sha
        and comparison_input.get("sggk_sha256") == nx_sha
        and comparison["checks"]["input_sha256"].get("ok") is True
    ):
        lane.error(
            "NX_INPUT_SHA_MISMATCH",
            "NX measurement 与 SGGK comparison 未绑定同一 input SHA-256",
            comparison_path,
        )
    _verify_nx_orchestrator_binding(
        lane,
        bundle,
        measurement_path,
        comparison_path,
        measurement,
        comparison,
    )

    checks = comparison["checks"]
    recomputed_failures = {f"{name}_failed" for name, check in checks.items() if check.get("ok") is not True}
    recorded_failures = set(comparison.get("failures", []))
    if recomputed_failures != recorded_failures:
        lane.error("NX_FAILURE_LIST_INCONSISTENT", "comparison.failures 与 checks 判定不一致", comparison_path)
    if comparison.get("ok") is True:
        if recorded_failures:
            lane.error("NX_COMPARISON_OK_INCONSISTENT", "comparison.ok=true 但仍记录失败项", comparison_path)
    else:
        diagnostics = comparison.get("diagnostics")
        if not recorded_failures:
            lane.error("NX_DIFFERENCE_MISSING", "comparison.ok=false 但没有失败差异", comparison_path)
        if recorded_failures - {"body_count_failed"}:
            lane.error(
                "NX_DIFFERENCE_NOT_EXPLAINED",
                "当前 schema 的非 bug 诊断只解释跨内核 body 表示差异，其他失败不能作为已验收差异",
                comparison_path,
            )
        if not isinstance(diagnostics, list) or not diagnostics:
            lane.error("NX_DIAGNOSTIC_MISSING", "comparison.ok=false 时必须提供差异诊断与原因", comparison_path)
        else:
            for diagnostic in diagnostics:
                if not isinstance(diagnostic, dict) or diagnostic.get("geometry_bug_confirmed") is not False:
                    lane.error(
                        "NX_GEOMETRY_BUG_CLASSIFICATION_MISSING",
                        "差异必须明确 geometry_bug_confirmed=false",
                        comparison_path,
                    )
                elif not str(diagnostic.get("message") or "").strip():
                    lane.error("NX_DIAGNOSTIC_REASON_MISSING", "差异诊断必须给出非空原因", comparison_path)
            lane.warning(
                "NX_REPRESENTATION_DIFFERENCE",
                "严格 comparison 未通过，但差异已分类为未确认 geometry bug",
                comparison_path,
            )
    lane.add_evidence(
        "NX_MEASUREMENT_COMPLETED",
        "NX STEP measurement 已完成",
        measurement["nx"]["full_version"],
        measurement_path,
    )
    lane.add_evidence("NX_SGGK_INPUT_BOUND", "NX 与 SGGK 使用相同 STEP SHA-256", nx_sha, comparison_path)
    lane.add_evidence(
        "NX_COMPARISON_CLASSIFIED",
        "NX/SGGK comparison 判定及差异分类有效",
        comparison.get("ok"),
        comparison_path,
    )
    return lane.result()


def _campaign_args() -> argparse.Namespace:
    return argparse.Namespace(
        allow_duplicate_inputs=False,
        allow_duplicate_geometry=False,
        allow_tolerance_mismatches=False,
        expect_known_bug_status=[],
    )


def _verify_known_bug_lane(raw: Path) -> dict[str, Any]:
    lane = Lane()
    verification_path = _fixed_entry(raw, "campaign_verification/campaign_verification.json")
    verification = _load_for_lane(
        lane,
        verification_path,
        "known-bug campaign verification",
        "KNOWN_BUG_VERIFICATION_MISSING",
    )
    if verification is None:
        return lane.result()
    if verification.get("ok") is not True or verification.get("error_count") != 0:
        lane.error(
            "KNOWN_BUG_VERIFICATION_FAILED",
            "campaign verification 必须 ok=true 且 error_count=0",
            verification_path,
        )
    checks = verification.get("checks")
    if not isinstance(checks, list) or verification.get("check_count") != len(checks):
        lane.error(
            "KNOWN_BUG_VERIFICATION_COUNTERS_INVALID",
            "campaign verification.check_count 与 checks 不一致",
            verification_path,
        )
    elif any(isinstance(item, dict) and item.get("severity") == "error" for item in checks):
        lane.error("KNOWN_BUG_VERIFICATION_HAS_ERRORS", "campaign verification.checks 仍含 error", verification_path)
    try:
        campaign_root = _resolve_recorded_path(verification.get("campaign_root"), verification_path.parent)
        summary_path = _resolve_recorded_path(verification.get("summary_path"), campaign_root)
        _require_inside(summary_path, campaign_root, "known-bug campaign summary")
    except ValueError as exc:
        lane.error("KNOWN_BUG_CAMPAIGN_PATH_INVALID", str(exc), verification_path)
        return lane.result()
    resolved_raw = raw.expanduser().resolve()
    expected_root = resolved_raw if resolved_raw.is_dir() else verification_path.parent.parent
    if not _same_file(campaign_root, expected_root):
        lane.error(
            "KNOWN_BUG_CAMPAIGN_ROOT_MISMATCH",
            "verification.campaign_root 与显式输入目录不一致",
            verification_path,
        )
    summary = _load_for_lane(lane, summary_path, "known-bug campaign summary", "KNOWN_BUG_SUMMARY_MISSING")
    if summary is None:
        return lane.result()
    independent = CampaignVerifier(campaign_root, _campaign_args()).verify_summary(summary, summary_path)
    if independent.get("ok") is not True:
        first_errors = [item for item in independent.get("checks", []) if item.get("severity") == "error"]
        detail = first_errors[0].get("message") if first_errors else "unknown campaign verification error"
        lane.error("KNOWN_BUG_INDEPENDENT_VERIFY_FAILED", f"重新复核 campaign 失败：{detail}", summary_path)
    for warning in [item for item in independent.get("checks", []) if item.get("severity") == "warning"]:
        lane.warning(
            "KNOWN_BUG_CAMPAIGN_WARNING",
            str(warning.get("message") or "campaign warning"),
            warning.get("path") or "",
        )

    commands = [item for item in summary.get("commands", []) if isinstance(item, dict)]
    for phase in ("known_bug_record_materialize", "known_bug_replay", "known_bug_regression"):
        matching = [item for item in commands if item.get("name") == phase]
        if not matching or any(item.get("ok") is not True for item in matching):
            lane.error("KNOWN_BUG_PHASE_MISSING", f"campaign 未证明 {phase} 阶段成功", summary_path)

    known = summary.get("known_bug_regression")
    if not isinstance(known, dict):
        lane.error("KNOWN_BUG_BLOCK_MISSING", "campaign summary 缺少 known_bug_regression", summary_path)
        return lane.result()
    for key in ("materialize_ok", "replay_ok", "regression_ok"):
        if known.get(key) is not True:
            lane.error("KNOWN_BUG_PHASE_FAILED", f"known_bug_regression.{key} 必须为 true", summary_path)
    try:
        registry_path = _resolve_recorded_path(known.get("registry_path"), campaign_root)
        replay_path = _resolve_recorded_path(known.get("replay_summary"), campaign_root)
        regression_path = _resolve_recorded_path(known.get("regression_summary"), campaign_root)
        for label, path in (
            ("registry", registry_path),
            ("replay", replay_path),
            ("regression", regression_path),
        ):
            _require_inside(path, campaign_root, f"known-bug {label}")
        registry = _read_json(registry_path)
        replay = _read_json(replay_path)
        regression = _read_json(regression_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        lane.error(
            "KNOWN_BUG_ARTIFACT_INVALID",
            f"materialize/replay/regression artifact 无法复核：{exc}",
            campaign_root,
        )
        return lane.result()

    bugs = registry.get("bugs")
    replay_results = replay.get("results")
    regression_results = regression.get("results")
    if _int(registry.get("total")) is None or registry.get("total", 0) < 1 or not isinstance(bugs, list) or not bugs:
        lane.error("KNOWN_BUG_MATERIALIZE_EMPTY", "materialize registry 至少需要 1 条 bug", registry_path)
    if (
        _int(replay.get("executed")) is None
        or replay.get("executed", 0) < 1
        or not isinstance(replay_results, list)
        or not replay_results
    ):
        lane.error("KNOWN_BUG_REPLAY_EMPTY", "replay 至少需要实际执行 1 条", replay_path)
    classified = [
        item
        for item in regression_results or []
        if isinstance(item, dict) and str(item.get("status") or "").strip() and str(item.get("reason") or "").strip()
    ] if isinstance(regression_results, list) else []
    if _int(regression.get("total")) is None or regression.get("total", 0) < 1 or not classified:
        lane.error(
            "KNOWN_BUG_REGRESSION_UNCLASSIFIED",
            "regression 至少需要 1 条含 status/reason 的分类结果",
            regression_path,
        )
    registry_ids = (
        {item.get("bug_id") for item in bugs or [] if isinstance(item, dict)}
        if isinstance(bugs, list)
        else set()
    )
    if classified and not any(item.get("bug_id") in registry_ids for item in classified):
        lane.error("KNOWN_BUG_CHAIN_UNBOUND", "regression 分类未绑定 materialize registry 中的 bug_id", regression_path)

    lane.add_evidence(
        "KNOWN_BUG_VERIFICATION_OK",
        "现有 campaign verification 已通过且已重新复核",
        True,
        verification_path,
    )
    lane.add_evidence("KNOWN_BUG_MATERIALIZED", "materialize bug 记录数", registry.get("total"), registry_path)
    lane.add_evidence("KNOWN_BUG_REPLAYED", "replay 实际执行数", replay.get("executed"), replay_path)
    lane.add_evidence("KNOWN_BUG_REGRESSION_CLASSIFIED", "regression 分类结果数", len(classified), regression_path)
    return lane.result()


def _lane_evidence_value(lane: dict[str, Any], code: str) -> str:
    for item in lane.get("evidence", []):
        if isinstance(item, dict) and item.get("code") == code:
            return str(item.get("value") or "")
    return ""


def _append_lane_error(lane: dict[str, Any], code: str, message: str) -> None:
    lane["errors"].append({"code": code, "message": message, "path": ""})
    lane["error_count"] = len(lane["errors"])
    lane["ok"] = False
    lane["status"] = "failed"


def verify_external_chain(
    *,
    model_session: Path,
    abc_corpus: Path,
    nx_bundle: Path,
    known_bug_campaign: Path,
) -> dict[str, Any]:
    inputs = {
        "model_session": str(model_session.expanduser().resolve()),
        "abc_corpus": str(abc_corpus.expanduser().resolve()),
        "nx_bundle": str(nx_bundle.expanduser().resolve()),
        "known_bug_campaign": str(known_bug_campaign.expanduser().resolve()),
    }
    lanes = {
        "model_harness": _verify_model_lane(model_session),
        "abc_sggk": _verify_abc_lane(abc_corpus),
        "nx_comparison": _verify_nx_lane(nx_bundle),
        "known_bug": _verify_known_bug_lane(known_bug_campaign),
    }
    abc_sha256 = _lane_evidence_value(lanes["abc_sggk"], "ABC_SOURCE_BOUND")
    nx_sha256 = _lane_evidence_value(lanes["nx_comparison"], "NX_SOURCE_FILE_VERIFIED")
    if _valid_sha256(abc_sha256) and _valid_sha256(nx_sha256) and abc_sha256 != nx_sha256:
        _append_lane_error(
            lanes["nx_comparison"],
            "ABC_NX_SOURCE_SHA_MISMATCH",
            "ABC/SGGK corpus 与 NX/SGGK 对比必须使用同一 STEP SHA-256",
        )
    error_count = sum(lane["error_count"] for lane in lanes.values())
    warning_count = sum(lane["warning_count"] for lane in lanes.values())
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "generated_at": _now(),
        "ok": error_count == 0,
        "status": "passed" if error_count == 0 else "failed",
        "error_count": error_count,
        "warning_count": warning_count,
        "inputs": inputs,
        "lanes": lanes,
    }
    schema = _read_json(RESULT_SCHEMA)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(result)
    return result


LANE_TITLES = {
    "model_harness": "模型 Harness session",
    "abc_sggk": "ABC / SGGK corpus",
    "nx_comparison": "NX / SGGK 对比",
    "known_bug": "已知 bug 资料链",
}


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# 外网 Harness 全链路验收报告",
        "",
        f"- 总体结论：**{'通过' if result.get('ok') else '未通过'}**",
        f"- 生成时间：`{result.get('generated_at', '')}`",
        f"- 错误数：`{result.get('error_count', 0)}`",
        f"- 警告数：`{result.get('warning_count', 0)}`",
        "",
        "| 验收链路 | 状态 | 错误 | 警告 |",
        "|---|---|---:|---:|",
    ]
    for key, lane in result["lanes"].items():
        lines.append(
            f"| {LANE_TITLES[key]} | {'通过' if lane['ok'] else '未通过'} | "
            f"{lane['error_count']} | {lane['warning_count']} |"
        )
    for key, lane in result["lanes"].items():
        lines.extend(["", f"## {LANE_TITLES[key]}", ""])
        if lane["evidence"]:
            lines.append("### 已验证证据")
            lines.append("")
            for item in lane["evidence"]:
                suffix = f"；路径：`{item['path']}`" if item["path"] else ""
                lines.append(f"- `{item['code']}` {item['message']}：`{item['value']}`{suffix}")
        if lane["errors"]:
            lines.extend(["", "### 错误", ""])
            for item in lane["errors"]:
                suffix = f"；路径：`{item['path']}`" if item["path"] else ""
                lines.append(f"- `{item['code']}` {item['message']}{suffix}")
        if lane["warnings"]:
            lines.extend(["", "### 警告", ""])
            for item in lane["warnings"]:
                suffix = f"；路径：`{item['path']}`" if item["path"] else ""
                lines.append(f"- `{item['code']}` {item['message']}{suffix}")
    lines.append("")
    return "\n".join(lines)


def write_outputs(out_dir: Path, result: dict[str, Any]) -> tuple[Path, Path]:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "external_chain_verification.json"
    markdown_path = out_dir / "external_chain_verification.zh-CN.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-session", required=True, type=Path, help="session.json or its session directory")
    parser.add_argument("--abc-corpus", required=True, type=Path, help="corpus_summary.json or its artifact directory")
    parser.add_argument("--nx-bundle", required=True, type=Path, help="NX/SGGK comparison bundle root")
    parser.add_argument(
        "--known-bug-campaign",
        required=True,
        type=Path,
        help="campaign root or campaign_verification.json",
    )
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = verify_external_chain(
        model_session=args.model_session,
        abc_corpus=args.abc_corpus,
        nx_bundle=args.nx_bundle,
        known_bug_campaign=args.known_bug_campaign,
    )
    json_path, markdown_path = write_outputs(args.out, result)
    print(f"summary={json_path}")
    print(f"report={markdown_path}")
    print(f"ok={result['ok']} errors={result['error_count']} warnings={result['warning_count']}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
