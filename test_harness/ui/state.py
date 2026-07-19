"""Read-only projection of Harness sessions for the local UI."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from test_harness.tools.oracle_text_zh import (
    FAULT_MODULE_LABEL_ZH,
    FAULT_MODULE_NOTE_ZH,
    phase_label,
    signature_kind_label,
    translate_oracle_failure,
    translate_reasons,
)

TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cmake",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".hxx",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".txt",
    ".yaml",
    ".yml",
}
IMAGE_SUFFIXES = {".png": "image/png"}
MAX_PREVIEW_BYTES = 4 * 1024 * 1024

ARTIFACT_GROUPS = {
    "reports": {
        "label": "重点报告",
        "description": "建议先看这里，了解当前方案或最终结论。",
        "order": 10,
    },
    "proposal": {
        "label": "测试方案与代码",
        "description": "模型生成并通过固定门禁的测试内容。",
        "order": 20,
    },
    "execution": {
        "label": "SDK 运行结果",
        "description": "真实 SDK 执行的结果、诊断与日志。",
        "order": 30,
    },
    "review": {
        "label": "审查与批准记录",
        "description": "用户意见、模型理解与执行批准记录。",
        "order": 40,
    },
    "details": {
        "label": "技术细节",
        "description": "Harness 内部清单、提示词、事件和完整性记录。",
        "order": 50,
    },
}

CODE_SUFFIXES = {".c", ".cc", ".cmake", ".cpp", ".cxx", ".h", ".hpp", ".hxx", ".py"}


def _artifact_group(relative: str) -> str:
    path = Path(relative)
    parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    suffix = path.suffix.lower()

    if (
        name in {
            "final_report.zh-cn.md",
            "final_rejection_report.zh-cn.md",
            "generation_failure_report.zh-cn.md",
        }
        or (name.startswith("第") and "测试方案审查" in name)
    ):
        return "reports"
    if parts and parts[0] == "execution":
        return "execution"
    if "comments" in parts or "approval" in parts:
        return "review"
    if name.endswith(".provenance.json"):
        return "details"
    if "candidate" in parts or suffix in CODE_SUFFIXES:
        return "proposal"
    if "review" in parts:
        return "review"
    if suffix == ".log" or "execution" in name or "validation" in name:
        return "execution"
    return "details"


def _artifact_label(relative: str) -> str:
    path = Path(relative)
    name = path.name
    lower = name.lower()
    labels = {
        "final_report.zh-cn.md": "最终测试报告",
        "final_rejection_report.zh-cn.md": "任务拒绝报告",
        "generation_failure_report.zh-cn.md": "生成失败报告",
        "candidate.json": "模型生成的测试方案",
        "candidate.provenance.json": "方案来源与完整性",
        "generation_result.json": "模型生成与门禁结果",
        "execution_result.json": "SDK 执行结果",
        "authoring_prompt.md": "发送给模型的任务说明",
        "api_test_form.json": "Harness 测试意图",
        "model_task_manifest.json": "模型生成任务清单",
        "review_subject_digest.json": "本轮审查内容摘要",
        "round_manifest.json": "本轮完整性清单",
        "review_packet.json": "机器审查包",
        "review_report.zh-cn.md": "机器审查报告",
        "user_comment.txt": "用户审查意见",
        "interpretation.json": "模型对审查意见的理解",
        "session.json": "会话状态记录",
    }
    if lower in labels:
        return labels[lower]
    if lower.startswith("第") and "测试方案审查" in lower and lower.endswith(".zh-cn.md"):
        return name[: -len(".zh-CN.md")]
    if len(path.parts) == 2 and path.parts[0].lower() == "events" and path.stem.isdigit():
        return f"事件记录 {path.stem}"
    return name


def _artifact_description(relative: str, group: str) -> str:
    name = Path(relative).name.lower()
    descriptions = {
        "final_report.zh-cn.md": "真实 SDK 测试的结论、失败摘要和主要证据入口。",
        "final_rejection_report.zh-cn.md": "本次任务被拒绝的原因和未执行说明。",
        "generation_failure_report.zh-cn.md": "模型生成或固定门禁未完成时的错误摘要。",
        "candidate.json": "模型返回并通过固定机器门禁的完整测试候选。",
        "candidate.provenance.json": "记录候选来源、哈希和固定门禁证据，通常无需人工查看。",
        "generation_result.json": "各候选的生成、修复和固定门禁状态。",
        "execution_result.json": "真实 SDK 构建与执行返回的结构化结果。",
        "authoring_prompt.md": "Harness 发送给模型的完整生成要求。",
        "api_test_form.json": "Harness 根据公开接口自动整理的测试目标与约束。",
        "review_subject_digest.json": "供审查意见解释使用的当前方案摘要。",
        "round_manifest.json": "把本轮方案、报告和哈希绑定在一起的不可变记录。",
        "review_packet.json": "固定机器审查使用的详细输入与判定证据。",
        "review_report.zh-cn.md": "固定机器门禁生成的详细审查说明。",
        "user_comment.txt": "用户针对当前轮次提交的原始自然语言意见。",
        "interpretation.json": "模型对用户意见的结构化理解和下一步决定。",
        "session.json": "Harness 的内部会话状态，通常无需人工查看。",
    }
    if name in descriptions:
        return descriptions[name]
    if name.startswith("第") and "测试方案审查" in name:
        return "本轮测试目标、完整候选、机器门禁状态和建议的下一步。"
    return str(ARTIFACT_GROUPS[group]["description"])


def _artifact_kind(suffix: str) -> str:
    if suffix == ".md":
        return "报告"
    if suffix in {".json", ".jsonl"}:
        return "JSON"
    if suffix in CODE_SUFFIXES:
        return "代码"
    if suffix == ".log":
        return "日志"
    return suffix.lstrip(".").upper() or "文本"


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


def _inside(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError("artifact path must be relative")
    result = (root / relative).resolve()
    try:
        result.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("artifact path escapes the active session") from exc
    return result


def active_session_root(repo_root: Path) -> Path | None:
    sessions = repo_root / "artifacts" / "harness_sessions"
    pointer = sessions / "active.json"
    if not pointer.is_file():
        return None
    session_id = str(_read_object(pointer).get("session_id") or "")
    if not session_id:
        return None
    root = (sessions / session_id).resolve()
    try:
        root.relative_to(sessions.resolve())
    except ValueError:
        return None
    return root if (root / "session.json").is_file() else None


def list_artifacts(session_root: Path | None) -> list[dict[str, Any]]:
    if session_root is None:
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(session_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        stat = path.stat()
        relative = path.relative_to(session_root).as_posix()
        group = _artifact_group(relative)
        records.append(
            {
                "path": relative,
                "name": path.name,
                "suffix": path.suffix.lower(),
                "bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "previewable": stat.st_size <= MAX_PREVIEW_BYTES,
                "kind": _artifact_kind(path.suffix.lower()),
                "label": _artifact_label(relative),
                "description": _artifact_description(relative, group),
                "group": group,
                "group_label": ARTIFACT_GROUPS[group]["label"],
                "group_order": ARTIFACT_GROUPS[group]["order"],
                "featured": False,
            }
        )
    return records


def _relative_artifact_path(repo_root: Path, session_root: Path | None, value: Any) -> str:
    if session_root is None or not isinstance(value, str | Path) or not str(value):
        return ""
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (repo_root / path).resolve()
    try:
        return resolved.relative_to(session_root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def _artifact_status(session: dict[str, Any] | None) -> dict[str, str]:
    state = str((session or {}).get("state") or "idle")
    round_number = int((session or {}).get("current_round") or 0)
    values: dict[str, tuple[str, str, str]] = {
        "idle": (
            "neutral",
            "还没有测试产物",
            "输入公开接口并开始生成后，这里会按用途整理可查看的文本方案、报告和运行结果；"
            "二进制产物仍保留在 session 目录。",
        ),
        "created": (
            "info",
            "测试任务已建立",
            "Harness 正在解析接口并准备模型生成任务。",
        ),
        "generating": (
            "info",
            f"正在生成第 {round_number + 1} 轮测试方案",
            "模型回复和固定门禁完成后，会自动出现一份建议先看的审查报告。",
        ),
        "interpreting_comment": (
            "info",
            f"正在处理第 {round_number} 轮审查意见",
            "模型正在理解你的修改或批准意图，原始方案仍保持不变。",
        ),
        "awaiting_comment": (
            "ready",
            f"第 {round_number} 轮测试方案可以审查了",
            "先看本轮审查报告；需要调整就提交意见，确认无误后再批准执行真实 SDK 测试。",
        ),
        "executing": (
            "info",
            "正在运行真实 SDK 测试",
            "当前方案已经批准，Harness 正在构建、执行并收集验证证据。",
        ),
        "completed": (
            "success",
            "真实 SDK 测试已通过",
            "先查看最终测试报告；需要复核时再展开 SDK 执行结果和技术细节。",
        ),
        "execution_failed": (
            "error",
            "真实 SDK 测试未通过",
            "先查看最终测试报告中的失败摘要，再按需展开 SDK 执行结果和日志。",
        ),
        "generation_failed": (
            "error",
            "测试方案生成未完成",
            "查看生成失败报告了解模型响应或固定门禁问题；本次没有执行真实 SDK 测试。",
        ),
        "rejected": (
            "neutral",
            "测试任务已拒绝",
            "查看任务拒绝报告了解用户意见；本次没有执行真实 SDK 测试。",
        ),
        "unreadable": (
            "error",
            "会话产物无法读取",
            "session.json 已损坏或编码异常，请保留目录并交由维护人员检查。",
        ),
    }
    tone, title, detail = values.get(
        state,
        ("info", "测试产物正在更新", "Harness 会在当前操作完成后刷新这里的报告和文件。"),
    )
    return {"state": state, "tone": tone, "title": title, "detail": detail}


def _artifact_summary(
    repo_root: Path,
    session_root: Path | None,
    session: dict[str, Any] | None,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    status = _artifact_status(session)
    by_path = {str(item["path"]): item for item in artifacts}
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_action(role: str, label: str, path: str, hint: str) -> None:
        item = by_path.get(path)
        if item is None or path in seen:
            return
        seen.add(path)
        item["featured"] = True
        actions.append(
            {
                "role": role,
                "label": label,
                "hint": hint,
                "path": path,
                "previewable": item["previewable"],
            }
        )

    state = str((session or {}).get("state") or "idle")
    terminal_states = {"completed", "execution_failed", "generation_failed", "rejected"}
    final_report = _relative_artifact_path(
        repo_root, session_root, (session or {}).get("final_report_path", "")
    )
    review_report = _relative_artifact_path(
        repo_root, session_root, (session or {}).get("current_review_report_path", "")
    )
    if state in terminal_states and final_report:
        label = "查看最终测试报告"
        if state == "generation_failed":
            label = "查看生成失败报告"
        elif state == "rejected":
            label = "查看任务拒绝报告"
        add_action("report", label, final_report, "先看结论与下一步")
    elif state in {"awaiting_comment", "interpreting_comment"} and review_report:
        add_action("report", "先看本轮审查报告", review_report, "用自然语言说明了方案和下一步")

    round_number = int((session or {}).get("current_round") or 0)
    candidate_states = {"awaiting_comment", "interpreting_comment", "executing", "completed", "execution_failed"}
    if round_number > 0 and state in candidate_states:
        candidate = f"rounds/{round_number:04d}/candidate/candidate.json"
        add_action("proposal", "查看模型测试方案", candidate, "复核完整的结构化候选")

    execution_states = {"executing", "completed", "execution_failed"}
    execution_root = _relative_artifact_path(
        repo_root, session_root, (session or {}).get("current_execution_attempt_path", "")
    )
    if state in execution_states and execution_root:
        execution_result = f"{execution_root.rstrip('/')}/execution_result.json"
        add_action("execution", "查看 SDK 执行明细", execution_result, "用于定位构建、运行或验证问题")

    groups: list[dict[str, Any]] = []
    ordered_groups = sorted(ARTIFACT_GROUPS.items(), key=lambda item: int(item[1]["order"]))
    for group_id, metadata in ordered_groups:
        count = sum(item["group"] == group_id for item in artifacts)
        if count:
            groups.append(
                {
                    "id": group_id,
                    "label": metadata["label"],
                    "description": metadata["description"],
                    "order": metadata["order"],
                    "count": count,
                }
            )

    return {
        **status,
        "error": str((session or {}).get("last_error") or ""),
        "total": len(artifacts),
        "previewable": sum(bool(item["previewable"]) for item in artifacts),
        "actions": actions,
        "groups": groups,
    }


def read_artifact(session_root: Path | None, relative: str) -> dict[str, Any]:
    if session_root is None:
        raise FileNotFoundError("there is no active session")
    path = _inside(session_root, relative)
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        if not path.is_file():
            raise FileNotFoundError("artifact is unavailable or not previewable")
        if path.stat().st_size > MAX_PREVIEW_BYTES:
            raise ValueError("artifact is too large to preview")
        return {
            "path": path.relative_to(session_root).as_posix(),
            "kind": "image",
            "mime": IMAGE_SUFFIXES[suffix],
            "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            "bytes": path.stat().st_size,
        }
    if not path.is_file() or suffix not in TEXT_SUFFIXES:
        raise FileNotFoundError("artifact is unavailable or not previewable")
    if path.stat().st_size > MAX_PREVIEW_BYTES:
        raise ValueError("artifact is too large to preview")
    return {
        "path": path.relative_to(session_root).as_posix(),
        "content": path.read_text(encoding="utf-8-sig", errors="replace"),
        "bytes": path.stat().st_size,
    }


def _events(root: Path | None) -> list[dict[str, Any]]:
    if root is None:
        return []
    result: list[dict[str, Any]] = []
    for path in sorted((root / "events").glob("*.json")):
        try:
            event = _read_object(path)
        except (OSError, json.JSONDecodeError):
            continue
        result.append(
            {
                "sequence": event.get("sequence"),
                "type": event.get("type") or event.get("event_type") or "EVENT",
                "timestamp": event.get("recorded_at") or event.get("timestamp") or event.get("created_at") or "",
                "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
            }
        )
    return result


def _stage(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def derive_stages(session: dict[str, Any] | None, events: list[dict[str, Any]]) -> list[dict[str, str]]:
    names = [str(event.get("type") or "") for event in events]
    state = str((session or {}).get("state") or "")
    stages = [
        {
            "id": "environment",
            "title": "环境配置",
            **_stage("active" if session is None else "done", "检查 API、SDK 与运行器"),
        },
        {
            "id": "resolve",
            "title": "接口解析",
            **_stage("done" if "PUBLIC_FUNCTION_RESOLVED" in names else "pending", "定位公开接口及能力路由"),
        },
        {
            "id": "generate",
            "title": "GLM-5.2 生成",
            **_stage(
                "done" if "ROUND_READY_FOR_REVIEW" in names else ("active" if state == "generating" else "pending"),
                "生成测试代码和测试方案",
            ),
        },
        {
            "id": "review",
            "title": "审查与修改",
            **_stage(
                "done" if "COMMENT_INTERPRETED" in names else ("active" if state == "awaiting_comment" else "pending"),
                "查看文档、代码并提交意见",
            ),
        },
        {
            "id": "approval",
            "title": "执行批准",
            **_stage("done" if "EXECUTION_APPROVED" in names else "pending", "批准当前不可变候选"),
        },
        {
            "id": "execution",
            "title": "SDK 实测",
            **_stage(
                "done"
                if "EXECUTION_COMPLETED" in names
                else ("error" if "EXECUTION_FAILED" in names else ("active" if state == "executing" else "pending")),
                "构建并运行真实 SDK 用例",
            ),
        },
        {
            "id": "report",
            "title": "结果报告",
            **_stage(
                "done"
                if state == "completed"
                else ("error" if state in {"execution_failed", "generation_failed"} else "pending"),
                "汇总证据与复现结论",
            ),
        },
    ]
    return stages


def _case_count(candidate: Mapping[str, Any]) -> int:
    """Count test cases in a formal candidate, tolerant of every candidate kind."""

    if not isinstance(candidate, Mapping):
        return 0
    for key in ("cases", "recipes", "inputs", "checks"):
        value = candidate.get(key)
        if isinstance(value, list) and value:
            return len(value)
    dsl = candidate.get("dsl")
    if isinstance(dsl, Mapping) and isinstance(dsl.get("cases"), list):
        return len(dsl["cases"])
    recipe = candidate.get("recipe")
    if isinstance(recipe, Mapping) and isinstance(recipe.get("cases"), list):
        return len(recipe["cases"])
    return 0


def _candidate_kind(candidate: Mapping[str, Any]) -> str:
    if isinstance(candidate, Mapping):
        kind = str(candidate.get("kind") or "").strip()
        if kind:
            return kind
    return ""


def round_overview(
    repo_root: Path,
    session_root: Path | None,
    session: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """A compact, human-first summary of what the model produced this round.

    Surfaced as a card above the file tree so unfamiliar users can read "what
    was generated / what is the test idea / why it failed" without clicking
    into files. Reuses fields already written by the workflow and the fixed
    review packet; never re-invokes the model.
    """

    state = str((session or {}).get("state") or "idle")
    status = _artifact_status(session)
    overview: dict[str, Any] = {
        "round_number": int((session or {}).get("current_round") or 0),
        "tone": status.get("tone", "neutral"),
        "headline": status.get("title", "还没有测试产物"),
        "purpose": "",
        "risk": "",
        "expected": "",
        "candidate_kind": "",
        "case_count": 0,
        "oracle_count": 0,
        "review_report_path": "",
        "candidate_path": "",
        "fixed_review_report_path": "",
        "failure_reason": "",
        "next_hint": status.get("detail", ""),
        "available": False,
    }

    if session_root is None or not session_root.is_dir():
        return overview

    round_number = overview["round_number"]
    if round_number > 0:
        round_manifest_path = session_root / "rounds" / f"{round_number:04d}" / "round_manifest.json"
        try:
            manifest = _read_object(round_manifest_path)
        except (OSError, json.JSONDecodeError):
            manifest = {}
        review_report = _relative_artifact_path(
            repo_root, session_root, (session or {}).get("current_review_report_path", "")
        )
        fixed_review_report = _relative_artifact_path(
            repo_root, session_root, manifest.get("fixed_review_report_path", "")
        )
        candidate_rel = _relative_artifact_path(
            repo_root, session_root, manifest.get("candidate_path", "")
        )
        overview["review_report_path"] = review_report or fixed_review_report
        overview["fixed_review_report_path"] = fixed_review_report
        overview["candidate_path"] = candidate_rel

        candidate: Mapping[str, Any] = {}
        if candidate_rel:
            try:
                candidate = _read_object(session_root / candidate_rel)
            except (OSError, json.JSONDecodeError):
                candidate = {}
        overview["candidate_kind"] = _candidate_kind(candidate)
        overview["case_count"] = _case_count(candidate)

        review_packet_rel = _relative_artifact_path(
            repo_root, session_root, manifest.get("review_packet_path", "")
        )
        if review_packet_rel:
            try:
                packet = _read_object(session_root / review_packet_rel)
            except (OSError, json.JSONDecodeError):
                packet = {}
            summary = packet.get("review_summary") if isinstance(packet.get("review_summary"), Mapping) else {}
            generation = packet.get("generation") if isinstance(packet.get("generation"), Mapping) else {}
            verification = (
                packet.get("machine_verification")
                if isinstance(packet.get("machine_verification"), Mapping)
                else {}
            )
            overview["purpose"] = str(summary.get("purpose_zh_cn") or "")
            overview["risk"] = str(summary.get("risk_summary_zh_cn") or "")
            overview["expected"] = str(summary.get("expected_behavior_zh_cn") or "")
            oracles = summary.get("oracles")
            overview["oracle_count"] = len(oracles) if isinstance(oracles, list) else 0
            if not overview["candidate_kind"] and generation.get("output_kind"):
                overview["candidate_kind"] = str(generation.get("output_kind") or "")
            overview["gate_ok"] = bool(verification.get("fixed_gate_ok"))
            overview["authoring_accepted"] = bool(verification.get("authoring_accepted"))

        overview["available"] = True

    last_error = str((session or {}).get("last_error") or "")
    terminal = {"generation_failed", "execution_failed", "rejected"}
    if state in terminal or last_error:
        overview["failure_reason"] = last_error
        if state == "generation_failed":
            overview["tone"] = "error"
            overview["headline"] = (
                f"第 {round_number} 轮测试方案生成未完成" if round_number > 0 else "测试方案生成未完成"
            )
            overview["next_hint"] = "查看下方生成失败报告了解模型响应或固定门禁问题。"
        elif state == "execution_failed":
            overview["tone"] = "error"
            overview["headline"] = (
                f"第 {round_number} 轮 SDK 实测未通过" if round_number > 0 else "SDK 实测未通过"
            )
            overview["next_hint"] = "查看下方最终测试报告中的失败摘要与 SDK 执行明细。"
        elif state == "rejected":
            overview["tone"] = "neutral"
            overview["headline"] = "测试任务已拒绝"
            overview["next_hint"] = "查看任务拒绝报告了解用户意见与拒绝原因。"

    return overview


EXECUTION_OVERVIEW_STATES = {"executing", "completed", "execution_failed"}
EXECUTION_OVERVIEW_MAX_CASES = 100
EXECUTION_OVERVIEW_MAX_COMMANDS = 24
EXECUTION_OVERVIEW_MAX_GROUPS = 20
EXECUTION_OVERVIEW_MAX_REASONS = 8
EXECUTION_OVERVIEW_MAX_VALIDATION_FAILURES = 8
EXECUTION_OVERVIEW_TEXT_LIMIT = 200
EXECUTION_OVERVIEW_ERROR_LIMIT = 400


def _read_json_quiet(path: Path) -> dict[str, Any]:
    """Read one JSON object; missing or corrupt content degrades to {}."""

    try:
        return _read_object(path)
    except (OSError, ValueError):
        return {}


def _bounded_strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            result.append(item)
        if len(result) >= limit:
            break
    return result


def _truncate_text(value: Any, limit: int = EXECUTION_OVERVIEW_TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _nonneg_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _elapsed_seconds(value: Any) -> float:
    try:
        return round(float(value or 0.0), 3)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _returncode(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _command_steps(execution: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project command records without argv/stdout/stderr so no command text reaches the UI."""

    commands = execution.get("commands")
    if not isinstance(commands, list):
        return []
    steps: list[dict[str, Any]] = []
    for command in commands[:EXECUTION_OVERVIEW_MAX_COMMANDS]:
        if not isinstance(command, Mapping):
            continue
        returncode = _returncode(command.get("returncode"))
        ok_value = command.get("ok")
        steps.append(
            {
                "name": str(command.get("name") or ""),
                "returncode": returncode,
                "ok": bool(ok_value) if isinstance(ok_value, bool) else returncode == 0,
                "elapsed_seconds": _elapsed_seconds(command.get("elapsed_seconds")),
            }
        )
    return steps


def _case_outcome(result: Mapping[str, Any]) -> str:
    if bool(result.get("skipped")):
        return "skip"
    if bool(result.get("timed_out")):
        return "timeout"
    if _returncode(result.get("returncode")) == 0:
        return "pass"
    return "fail"


def _triage_reason_map(triage: Mapping[str, Any]) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = {}
    for key in ("failures", "cases"):
        entries = triage.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            case_id = str(entry.get("case_id") or "")
            if not case_id or case_id in reasons:
                continue
            reasons[case_id] = _bounded_strings(entry.get("reasons"), EXECUTION_OVERVIEW_MAX_REASONS)
    return reasons


def _execution_case_row(
    repo_root: Path,
    session_root: Path,
    result: Mapping[str, Any],
    triage_reasons: Mapping[str, list[str]],
) -> dict[str, Any]:
    case_id = str(result.get("case_id") or "")
    returncode = _returncode(result.get("returncode"))
    timed_out = bool(result.get("timed_out"))
    skipped = bool(result.get("skipped"))
    artifact_rel = _relative_artifact_path(repo_root, session_root, result.get("artifact_dir") or "")
    phase = ""
    error_message = ""
    validation_failures: list[str] = []
    if artifact_rel:
        case_dir = session_root / artifact_rel
        run_state = _read_json_quiet(case_dir / "run_state.json")
        phase = str(run_state.get("last_phase") or run_state.get("phase") or "")
        status = _read_json_quiet(case_dir / "report" / "status.json")
        if status.get("succeeded") is not True:
            error_message = _truncate_text(status.get("error_message"))
        validation = _read_json_quiet(case_dir / "report" / "validation.json")
        validation_failures = _bounded_strings(
            validation.get("failures"), EXECUTION_OVERVIEW_MAX_VALIDATION_FAILURES
        )
    case_reasons = list(triage_reasons.get(case_id, []))
    return {
        "case_id": case_id,
        "outcome": _case_outcome(result),
        "returncode": returncode,
        "timed_out": timed_out,
        "skipped": skipped,
        "elapsed_seconds": _elapsed_seconds(result.get("elapsed_seconds")),
        "phase": phase,
        "phase_label": phase_label(phase) if phase else "",
        "error_message": error_message,
        "validation_failures": validation_failures,
        "validation_failures_zh": [translate_oracle_failure(failure) for failure in validation_failures],
        "triage_reasons": case_reasons,
        "triage_reasons_zh": translate_reasons(case_reasons),
        "artifact_path": artifact_rel,
    }


def _failure_group_rows(triage: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups = triage.get("failure_groups")
    if not isinstance(groups, list):
        return []
    rows: list[dict[str, Any]] = []
    for group in groups[:EXECUTION_OVERVIEW_MAX_GROUPS]:
        if not isinstance(group, Mapping):
            continue
        signature = group.get("representative_failure_signature")
        signature = signature if isinstance(signature, Mapping) else {}
        reasons = _bounded_strings(group.get("reasons"), EXECUTION_OVERVIEW_MAX_REASONS)
        rows.append(
            {
                "count": _nonneg_int(group.get("count")),
                "apis": _bounded_strings(group.get("apis"), EXECUTION_OVERVIEW_MAX_REASONS),
                "reasons": reasons,
                "reasons_zh": translate_reasons(reasons),
                "representative_case_id": str(group.get("representative_case_id") or ""),
                "signature": {
                    "kind": str(signature.get("kind") or ""),
                    "phase": str(signature.get("phase") or ""),
                    "sdk_error_code": _returncode(signature.get("sdk_error_code")),
                },
            }
        )
    return rows


def execution_overview(
    repo_root: Path,
    session_root: Path | None,
    session: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """A compact, human-first summary of one SDK execution attempt.

    Mirrors round_overview(): reads only artifacts already written by the
    pipeline (execution_result.json, recipe_summary.json, per-case reports,
    triage_summary.json) and the session record, never re-runs anything.  It
    never raises: missing files yield empty sections and corrupt JSON is
    treated as missing, so the UI card degrades instead of breaking the
    snapshot.  Every path surfaced to the frontend is session-relative.
    """

    overview: dict[str, Any] = {
        "available": False,
        "attempt_path": "",
        "execution_result_path": "",
        "state": str((session or {}).get("state") or ""),
        "ok": None,
        "status": "",
        "candidate_cause": "",
        "error": "",
        "commands": [],
        "total_elapsed_seconds": 0.0,
        "totals": {"total": 0, "passed": 0, "failed": 0, "timed_out": 0, "skipped": 0},
        "cases": [],
        "cases_truncated": False,
        "failure_groups": [],
        "parasolid": {"ran": False},
        "visual_review": {"ran": False},
        "cases_root": "",
        "triage_root": "",
    }
    if session_root is None or not isinstance(session, Mapping):
        return overview
    if overview["state"] not in EXECUTION_OVERVIEW_STATES:
        return overview
    attempt_rel = _relative_artifact_path(repo_root, session_root, session.get("current_execution_attempt_path"))
    if not attempt_rel:
        return overview
    overview["attempt_path"] = attempt_rel
    overview["available"] = True
    try:
        attempt_root = session_root / attempt_rel
        result_path = attempt_root / "execution_result.json"
        if result_path.is_file():
            overview["execution_result_path"] = f"{attempt_rel}/execution_result.json"
        result_doc = _read_json_quiet(result_path)
        task_results = result_doc.get("results")
        task_result = next(
            (item for item in task_results if isinstance(item, Mapping)),
            {},
        ) if isinstance(task_results, list) else {}
        execution = task_result.get("execution") if isinstance(task_result.get("execution"), Mapping) else {}
        ok_value = execution.get("ok")
        if isinstance(ok_value, bool):
            overview["ok"] = ok_value
        elif isinstance(result_doc.get("ok"), bool):
            overview["ok"] = bool(result_doc.get("ok"))
        overview["status"] = str(execution.get("status") or "")
        overview["candidate_cause"] = str(execution.get("candidate_cause") or "")
        overview["error"] = _truncate_text(
            execution.get("error") or task_result.get("error") or result_doc.get("error"),
            EXECUTION_OVERVIEW_ERROR_LIMIT,
        )
        overview["commands"] = _command_steps(execution)
        overview["total_elapsed_seconds"] = round(
            sum(step["elapsed_seconds"] for step in overview["commands"]), 3
        )
        artifacts = execution.get("artifacts") if isinstance(execution.get("artifacts"), Mapping) else {}
        cases_root_rel = _relative_artifact_path(repo_root, session_root, artifacts.get("cases") or "")
        triage_root_rel = _relative_artifact_path(repo_root, session_root, artifacts.get("triage") or "")
        overview["cases_root"] = cases_root_rel
        overview["triage_root"] = triage_root_rel

        recipe_summary = (
            _read_json_quiet(session_root / cases_root_rel / "recipe_summary.json") if cases_root_rel else {}
        )
        for key in ("total", "passed", "failed", "timed_out", "skipped"):
            overview["totals"][key] = _nonneg_int(recipe_summary.get(key))
        triage_doc = (
            _read_json_quiet(session_root / triage_root_rel / "triage_summary.json") if triage_root_rel else {}
        )
        reason_map = _triage_reason_map(triage_doc)
        results = recipe_summary.get("results")
        ordered = sorted(
            (item for item in results if isinstance(item, Mapping)),
            key=lambda item: (0 if _case_outcome(item) in {"fail", "timeout"} else 1, str(item.get("case_id") or "")),
        ) if isinstance(results, list) else []
        overview["cases_truncated"] = len(ordered) > EXECUTION_OVERVIEW_MAX_CASES
        overview["cases"] = [
            _execution_case_row(repo_root, session_root, item, reason_map)
            for item in ordered[:EXECUTION_OVERVIEW_MAX_CASES]
        ]
        overview["failure_groups"] = _failure_group_rows(triage_doc)

        parasolid = session.get("parasolid_comparison")
        if isinstance(parasolid, Mapping) and parasolid.get("ran") is True:
            verdict_counts = parasolid.get("verdict_counts")
            raw_attention = parasolid.get("attention_cases")
            attention_cases = []
            for entry in raw_attention[:24] if isinstance(raw_attention, list) else []:
                if not isinstance(entry, Mapping):
                    continue
                attention_cases.append(
                    {
                        "case_id": str(entry.get("case_id") or ""),
                        "verdict": str(entry.get("verdict") or ""),
                        "cause_class": str(entry.get("cause_class") or ""),
                    }
                )
            overview["parasolid"] = {
                "ran": True,
                "ok": bool(parasolid.get("ok")),
                "total": _nonneg_int(parasolid.get("total")),
                "consistent": _nonneg_int(parasolid.get("consistent")),
                "attention": _nonneg_int(parasolid.get("attention")),
                "verdict_counts": dict(verdict_counts) if isinstance(verdict_counts, Mapping) else {},
                "attention_cases": attention_cases,
                "report_path": _relative_artifact_path(
                    repo_root, session_root, parasolid.get("report_path") or ""
                ),
            }

        visual = session.get("visual_review")
        if isinstance(visual, Mapping) and visual.get("ran") is True:
            visual_summary = visual.get("summary") if isinstance(visual.get("summary"), Mapping) else {}
            visual_cases = []
            raw_visual_cases = visual.get("cases") if isinstance(visual.get("cases"), list) else []
            for entry in raw_visual_cases[:24]:
                if not isinstance(entry, Mapping):
                    continue
                visual_cases.append(
                    {
                        "case_id": str(entry.get("case_id") or ""),
                        "plausibility": str(entry.get("plausibility") or ""),
                        "flags": _bounded_strings(entry.get("flags"), 8),
                    }
                )
            overview["visual_review"] = {
                "ran": True,
                "ok": bool(visual.get("ok")),
                "note": _truncate_text(visual.get("note") or "", 200),
                "summary": {
                    key: _nonneg_int(visual_summary.get(key))
                    for key in ("reviewed", "plausible", "suspect", "implausible", "flags")
                },
                "report_path": _relative_artifact_path(
                    repo_root, session_root, visual.get("report_path") or ""
                ),
                "cases": visual_cases,
            }
    except Exception:  # noqa: BLE001 - the read-only projection must never break the UI snapshot
        pass
    return overview


FAILURE_ANALYSIS_MAX_CASES = 64
FAILURE_ANALYSIS_MAX_REASONS = 8
FAILURE_ANALYSIS_MAX_ORACLE_FAILURES = 4
FAILURE_ANALYSIS_MAX_EVIDENCE = 4


def failure_analysis(
    repo_root: Path,
    session_root: Path | None,
    session: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project the failure-showcase session record for the 失败分析 UI tab.

    Reads ``session["failure_showcase"]`` plus the session-scoped per-case
    ``pre_analysis.json`` mirrors written by the workflow hook.  Never raises:
    missing or corrupt inputs degrade to an unavailable/empty projection.
    """

    result: dict[str, Any] = {
        "available": False,
        "note": "",
        "root": "",
        "db": "",
        "cases": [],
    }
    if session_root is None or not isinstance(session, Mapping):
        return result
    showcase = session.get("failure_showcase")
    if not isinstance(showcase, Mapping) or showcase.get("ran") is not True:
        return result
    result["available"] = True
    result["note"] = _truncate_text(showcase.get("note") or "", 400)
    result["root"] = str(showcase.get("root") or "")
    result["db"] = str(showcase.get("db") or "")
    cases = showcase.get("cases") if isinstance(showcase.get("cases"), list) else []
    for case in cases[:FAILURE_ANALYSIS_MAX_CASES]:
        if not isinstance(case, Mapping):
            continue
        pre: dict[str, Any] = {}
        pre_rel = str(case.get("pre_analysis") or "")
        if pre_rel:
            pre_rel = _relative_artifact_path(repo_root, session_root, pre_rel) or pre_rel
            try:
                pre_path = _inside(session_root, pre_rel)
            except ValueError:
                continue
            if pre_path.is_file():
                pre = _read_json_quiet(pre_path)
        analysis_png = str(case.get("analysis_png") or "")
        if analysis_png:
            analysis_png = _relative_artifact_path(repo_root, session_root, analysis_png) or analysis_png
            try:
                png_path = _inside(session_root, analysis_png)
                if not png_path.is_file() or png_path.stat().st_size > MAX_PREVIEW_BYTES:
                    analysis_png = ""
            except (ValueError, OSError):
                analysis_png = ""
        signature = pre.get("signature") if isinstance(pre.get("signature"), Mapping) else {}
        parasolid = pre.get("parasolid") if isinstance(pre.get("parasolid"), Mapping) else {}
        confidence = pre.get("confidence")
        reproduce = str(case.get("reproduce") or "")
        repro_cpp = str(case.get("repro_cpp") or "")
        triage_reasons = _bounded_strings(pre.get("triage_reasons"), FAILURE_ANALYSIS_MAX_REASONS)
        oracle_failures = _bounded_strings(pre.get("oracle_failures"), FAILURE_ANALYSIS_MAX_ORACLE_FAILURES)
        fault_module = str(pre.get("fault_module") or "")
        signature_kind = str(signature.get("kind") or "")
        signature_phase = str(signature.get("phase") or "")
        result["cases"].append(
            {
                "case_id": str(case.get("case_id") or pre.get("case_id") or ""),
                "outcome": str(pre.get("outcome") or ""),
                "signature": {
                    "kind": signature_kind,
                    "phase": signature_phase,
                    "sdk_error_code": (
                        signature.get("sdk_error_code")
                        if isinstance(signature.get("sdk_error_code"), int)
                        and not isinstance(signature.get("sdk_error_code"), bool)
                        else None
                    ),
                },
                "signature_kind_label": signature_kind_label(signature_kind) if signature_kind else "",
                "signature_phase_label": phase_label(signature_phase) if signature_phase else "",
                "triage_reasons": triage_reasons,
                "triage_reasons_zh": translate_reasons(triage_reasons),
                "oracle_failures": oracle_failures,
                "oracle_failures_zh": [translate_oracle_failure(failure) for failure in oracle_failures],
                "parasolid": {
                    "verdict": str(parasolid.get("verdict") or ""),
                    "cause_class": str(parasolid.get("cause_class") or ""),
                },
                "fault_domain": str(pre.get("fault_domain") or ""),
                "fault_module": fault_module,
                "fault_module_label": FAULT_MODULE_LABEL_ZH.get(fault_module, fault_module),
                "confidence": (
                    confidence
                    if isinstance(confidence, int | float) and not isinstance(confidence, bool)
                    else None
                ),
                "priority": str(pre.get("priority") or "high"),
                "priority_reason_zh": _truncate_text(pre.get("priority_reason_zh") or "", 240),
                "visual_fault_hint": str(pre.get("visual_fault_hint") or ""),
                "visual_disagrees": bool(pre.get("visual_disagrees")),
                "evidence": _bounded_strings(pre.get("evidence"), FAILURE_ANALYSIS_MAX_EVIDENCE),
                "notes": _truncate_text(pre.get("notes") or "", 300),
                "analysis_png": analysis_png,
                "showcase_dir": str(case.get("dir") or ""),
                "reproduce": reproduce,
                "repro_cpp": repro_cpp,
                "analysis_md": str(case.get("analysis") or ""),
                "reproduction_note": (
                    "证据目录里有自动生成的 google-test 复现源文件（*_repro.cpp），"
                    "可直接拷入 SGGK 测试树编译定位问题；也可用 reproduce.ps1 原样重跑该用例。"
                    if repro_cpp
                    else (
                        "在证据目录运行 reproduce.ps1 即可原样重跑该用例（固定内容：会话同一 runner 与复制的 recipe）。"
                        if reproduce
                        else ""
                    )
                ),
            }
        )
    module_order = {
        "api_under_test": 0,
        "distance_oracle": 10,
        "point_relation_oracle": 11,
        "clash_oracle": 12,
        "plane_extreme_oracle": 13,
        "step_export": 20,
        "step_import": 21,
        "unclassified": 30,
        "test_authoring": 40,
    }
    grouped: dict[str, dict[str, Any]] = {}
    for item in result["cases"]:
        module = str(item.get("fault_module") or "unclassified")
        group = grouped.setdefault(
            module,
            {
                "module": module,
                "label": FAULT_MODULE_LABEL_ZH.get(module, module),
                "note": FAULT_MODULE_NOTE_ZH.get(module, ""),
                "count": 0,
                "high_count": 0,
                "low_count": 0,
            },
        )
        group["count"] += 1
        if item.get("priority") == "low":
            group["low_count"] += 1
        else:
            group["high_count"] += 1
    result["groups"] = sorted(
        grouped.values(),
        key=lambda group: (module_order.get(str(group["module"]), 25), str(group["module"])),
    )
    result["summary"] = {
        "total": len(result["cases"]),
        "high": sum(1 for item in result["cases"] if item.get("priority") != "low"),
        "low": sum(1 for item in result["cases"] if item.get("priority") == "low"),
    }
    return result


def session_snapshot(repo_root: Path) -> dict[str, Any]:
    root = active_session_root(repo_root)
    session: dict[str, Any] | None = None
    if root is not None:
        try:
            session = _read_object(root / "session.json")
        except (OSError, json.JSONDecodeError):
            session = {"state": "unreadable", "last_error": "session.json cannot be read"}
    events = _events(root)
    public = {
        "session_id": (session or {}).get("session_id", ""),
        "public_function": (session or {}).get("public_function", ""),
        "state": (session or {}).get("state", "idle"),
        "current_round": (session or {}).get("current_round", 0),
        "review_report_path": (session or {}).get("current_review_report_path", ""),
        "final_report_path": (session or {}).get("final_report_path", ""),
        "last_error": (session or {}).get("last_error", ""),
    }
    artifacts = list_artifacts(root)
    return {
        "session": public,
        "stages": derive_stages(session, events),
        "events": events,
        "artifacts": artifacts,
        "artifact_summary": _artifact_summary(repo_root, root, session, artifacts),
        "round_overview": round_overview(repo_root, root, session),
        "execution_overview": execution_overview(repo_root, root, session),
        "failure_analysis": failure_analysis(repo_root, root, session),
    }


__all__ = [
    "active_session_root",
    "failure_analysis",
    "read_artifact",
    "session_snapshot",
    "round_overview",
    "execution_overview",
]
