"""Gather bounded prior review feedback for one public function from past sessions.

The Harness persists every review comment and its typed interpretation under
``artifacts/harness_sessions/<session_id>/rounds/<NNNN>/comments/<key>/``.
When a new session starts for the same public function, this module collects
the most recent accepted/rejected feedback so the model can build on earlier
review suggestions instead of starting from a blank slate.  Collection is
read-only, bounded, and never includes credentials, paths, or hashes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_SESSIONS = 3
MAX_COMMENTS = 12
MAX_COMMENT_CHARS = 500
MAX_INSTRUCTION_CHARS = 300
MAX_TOTAL_CHARS = 6000
_FORBIDDEN_KEY_PARTS = (
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "sha256",
    "hash",
    "path",
    "url",
)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bounded(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _safe_text(text: Any) -> str:
    """Drop text that looks like it carries paths, hashes, or credentials."""

    value = str(text or "")
    lowered = value.lower()
    if any(part in lowered for part in ("sk-", "bearer ", "api_key", "apikey", "password", "token")):
        return ""
    if "\\" in value or ("/" in value and ":" in value):
        return ""
    if len(value) >= 64 and all(char in "0123456789abcdef" for char in lowered[:64]):
        return ""
    return value


def _session_memory_entry(session_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    rounds_root = session_root / "rounds"
    if not rounds_root.is_dir():
        return entries
    for round_dir in sorted(rounds_root.iterdir()):
        comments_root = round_dir / "comments"
        if not comments_root.is_dir():
            continue
        for comment_dir in sorted(comments_root.iterdir()):
            comment_path = comment_dir / "user_comment.txt"
            decision_path = comment_dir / "comment_decision.json"
            if not comment_path.is_file() or not decision_path.is_file():
                continue
            try:
                comment = comment_path.read_text(encoding="utf-8-sig").strip()
            except OSError:
                continue
            decision_record = _read_object(decision_path)
            decision = decision_record.get("decision")
            if not isinstance(decision, dict):
                continue
            requested = []
            for change in decision.get("requested_changes") or []:
                if not isinstance(change, dict):
                    continue
                instruction = _safe_text(change.get("instruction"))
                if not instruction:
                    continue
                requested.append(
                    {
                        "scope": str(change.get("scope") or "other"),
                        "instruction": _bounded(instruction, MAX_INSTRUCTION_CHARS),
                        "priority": str(change.get("priority") or "medium"),
                    }
                )
            constraints = [
                _bounded(_safe_text(item), MAX_INSTRUCTION_CHARS)
                for item in decision.get("constraints") or []
                if _safe_text(item)
            ]
            entries.append(
                {
                    "round": round_dir.name,
                    "decision": str(decision.get("decision") or ""),
                    "user_comment": _bounded(_safe_text(comment), MAX_COMMENT_CHARS),
                    "summary_zh_cn": _bounded(_safe_text(decision.get("summary_zh_cn")), MAX_COMMENT_CHARS),
                    "requested_changes": requested,
                    "constraints": constraints,
                }
            )
    return entries


def gather_prior_review_memory(
    sessions_root: str | Path,
    public_function: str,
    *,
    exclude_session_id: str = "",
    max_sessions: int = MAX_SESSIONS,
    max_comments: int = MAX_COMMENTS,
) -> dict[str, Any]:
    """Collect prior review feedback for ``public_function`` from past sessions.

    Returns a bounded, JSON-serializable memory block.  ``enabled`` is False
    when no usable prior feedback exists, so the prompt stays clean.
    """

    root = Path(sessions_root).expanduser().resolve()
    target = str(public_function or "").strip()
    memory: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "enabled": False,
        "public_function": target,
        "session_count": 0,
        "comment_count": 0,
        "sessions": [],
    }
    if not target or not root.is_dir():
        return memory

    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for session_root in sorted(root.iterdir()):
        if not session_root.is_dir():
            continue
        session_file = session_root / "session.json"
        if not session_file.is_file():
            continue
        session = _read_object(session_file)
        if str(session.get("public_function") or "") != target:
            continue
        session_id = str(session.get("session_id") or session_root.name)
        if exclude_session_id and session_id == exclude_session_id:
            continue
        updated = str(session.get("updated_at") or session.get("created_at") or "")
        candidates.append((updated, session_root, session))
    # Most recently updated sessions first.
    candidates.sort(key=lambda item: item[0], reverse=True)

    total_chars = 0
    for _updated, session_root, session in candidates[: max(0, max_sessions)]:
        entries = _session_memory_entry(session_root)
        if not entries:
            continue
        session_block: dict[str, Any] = {
            "session_id": str(session.get("session_id") or session_root.name),
            "state": str(session.get("state") or ""),
            "updated_at": str(session.get("updated_at") or ""),
            "feedback": [],
        }
        for entry in entries:
            if len(memory["sessions"]) >= 0 and memory["comment_count"] >= max_comments:
                break
            entry_chars = len(entry["user_comment"]) + len(entry["summary_zh_cn"])
            entry_chars += sum(len(change["instruction"]) for change in entry["requested_changes"])
            entry_chars += sum(len(item) for item in entry["constraints"])
            if total_chars + entry_chars > MAX_TOTAL_CHARS:
                break
            total_chars += entry_chars
            session_block["feedback"].append(entry)
            memory["comment_count"] += 1
        if session_block["feedback"]:
            memory["sessions"].append(session_block)
            memory["session_count"] += 1
        if memory["comment_count"] >= max_comments or total_chars >= MAX_TOTAL_CHARS:
            break

    memory["enabled"] = memory["comment_count"] > 0
    return memory


__all__ = ["gather_prior_review_memory"]
