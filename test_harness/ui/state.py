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
        records.append(
            {
                "path": path.relative_to(session_root).as_posix(),
                "name": path.name,
                "suffix": path.suffix.lower(),
                "bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
            }
        )
    return records


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
            "title": "Qwen 生成",
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
    return {
        "session": public,
        "stages": derive_stages(session, events),
        "events": events,
        "artifacts": list_artifacts(root),
    }


__all__ = ["active_session_root", "read_artifact", "session_snapshot"]
