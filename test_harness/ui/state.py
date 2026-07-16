"""Read-only projection of Harness sessions for the local UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    if session_root is None or not isinstance(value, (str, Path)) or not str(value):
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
            "输入公开接口并开始生成后，这里会按用途整理可查看的文本方案、报告和运行结果；二进制产物仍保留在 session 目录。",
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
    if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
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
    }


__all__ = ["active_session_root", "read_artifact", "session_snapshot"]
