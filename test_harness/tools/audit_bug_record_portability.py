#!/usr/bin/env python3
"""Audit checked SGGK bug-record JSON files for non-portable replay paths."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time
from typing import Any


PATH_KEYS = {
    "recipe_path",
    "recipe_file",
    "replay_recipe",
    "dsl_path",
    "dsl_file",
    "dsl_source",
    "source",
    "source_file",
    "source_sgt",
    "source_step",
    "source_stp",
    "source_iges",
    "source_igs",
    "target_sgt",
    "tool_sgt",
    "target_source_file",
    "tool_source_file",
    "original_recipe",
    "representative_case_dir",
    "replay_artifact",
    "bundle_dir",
    "bundle_manifest",
    "localization_summary",
    "zip",
    "reproduce_script",
    "bug_report",
    "preview",
    "debug_geometry",
    "debug_geometry_index",
    "debug_handoff_index",
    "debug_handoff_report",
    "debug_handoff_pack",
    "debug_handoff_readme",
    "debug_handoff_manifest",
    "pack_dir",
    "readme",
    "manifest",
    "visual_index",
    "visual_index_json",
    "focus_index",
    "focus_index_json",
    "sgt_paths",
    "open_folder",
    "open_in_gui",
}

ALLOWED_REPO_PREFIXES = (
    "test_harness/fixtures/bug_records/",
    "test_harness/dsl/",
    "test_harness/recipes/",
    "SGK1.4.10/samples/",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", action="append", required=True, help="Bug-record JSON file or directory")
    parser.add_argument("--out", default="artifacts/bug_record_portability", help="Output directory for audit JSON/Markdown")
    parser.add_argument("--repo-root", default=".", help="Repository/workspace root for resolving relative paths")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def expand_record_files(values: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in values:
        path = Path(raw)
        if path.is_dir():
            files.extend(child for child in path.rglob("*.json") if child.is_file())
        elif path.is_file():
            files.append(path)
        else:
            files.append(path)
    return sorted(set(path.resolve() for path in files), key=lambda item: str(item).lower())


def is_path_key(key: str, parent_keys: tuple[str, ...]) -> bool:
    if key in PATH_KEYS:
        return True
    if key == "source" and parent_keys and parent_keys[-1] == "dsl":
        return True
    return False


def normalized_relative(raw: str, record_file: Path, repo_root: Path) -> str:
    path = Path(raw)
    if not path.is_absolute():
        path = (record_file.parent / path).resolve()
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def classify_path(raw: str, record_file: Path, repo_root: Path) -> tuple[str, str]:
    text = raw.replace("\\", "/")
    if not text.strip():
        return "ignored", "empty path"
    if text.startswith(("http://", "https://")):
        return "warning", "URL path is not a local durable fixture"
    if Path(raw).is_absolute():
        return "error", "absolute local path"
    text_no_prefix = text[2:] if text.startswith("./") else text
    if any(text_no_prefix.startswith(prefix) for prefix in ALLOWED_REPO_PREFIXES):
        return "ok", "durable allowed root"
    rel = normalized_relative(raw, record_file, repo_root)
    rel_lower = rel.lower()
    if "/artifacts/" in f"/{rel_lower}" or rel_lower.startswith("artifacts/"):
        return "error", "points under artifacts"
    if any(rel.startswith(prefix) for prefix in ALLOWED_REPO_PREFIXES):
        return "ok", "durable allowed root"
    if raw.startswith("../fixtures/bug_records/"):
        return "ok", "durable bug-record fixture"
    if raw.startswith(("./", "../")):
        return "warning", "relative path outside known durable roots"
    if "/" in text:
        return "warning", "repository-relative path outside known durable roots"
    return "ignored", "not path-like"


def iter_path_fields(value: Any, parent_keys: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    found: list[tuple[tuple[str, ...], str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            next_keys = (*parent_keys, str(key))
            if isinstance(child, str) and is_path_key(str(key), parent_keys):
                found.append((next_keys, child))
            else:
                found.extend(iter_path_fields(child, next_keys))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(iter_path_fields(child, (*parent_keys, str(index))))
    return found


def audit_file(path: Path, repo_root: Path) -> dict[str, Any]:
    item: dict[str, Any] = {
        "file": str(path),
        "ok": False,
        "error_count": 0,
        "warning_count": 0,
        "checks": [],
    }
    try:
        root = read_json(path)
    except Exception as exc:  # noqa: BLE001
        item["checks"].append({"severity": "error", "path": "", "value": "", "reason": f"invalid JSON: {exc}"})
        item["error_count"] = 1
        return item
    for keys, raw in iter_path_fields(root):
        severity, reason = classify_path(raw, path, repo_root)
        if severity == "ignored":
            continue
        check = {
            "severity": severity,
            "path": ".".join(keys),
            "value": raw,
            "reason": reason,
        }
        item["checks"].append(check)
    counts = Counter(as_str(check.get("severity")) for check in item["checks"])
    item["error_count"] = counts.get("error", 0)
    item["warning_count"] = counts.get("warning", 0)
    item["ok"] = item["error_count"] == 0
    return item


def markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# SGGK Bug Record Portability Audit",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- OK: `{summary.get('ok')}`",
        f"- Files: `{summary.get('file_count')}`",
        f"- Errors: `{summary.get('error_count')}`",
        f"- Warnings: `{summary.get('warning_count')}`",
        "",
        "| severity | file | path | reason | value |",
        "| --- | --- | --- | --- | --- |",
    ]
    for file_item in summary.get("files", []):
        if not isinstance(file_item, dict):
            continue
        for check in file_item.get("checks", []):
            if not isinstance(check, dict):
                continue
            lines.append(
                "| `{severity}` | `{file}` | `{path}` | {reason} | `{value}` |".format(
                    severity=check.get("severity", ""),
                    file=file_item.get("file", ""),
                    path=check.get("path", ""),
                    reason=str(check.get("reason", "")).replace("|", "\\|"),
                    value=str(check.get("value", "")).replace("|", "\\|"),
                )
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    out_dir = Path(args.out).resolve()
    files = expand_record_files(args.records)
    file_results = [audit_file(path, repo_root) for path in files]
    error_count = sum(int(item.get("error_count", 0)) for item in file_results)
    warning_count = sum(int(item.get("warning_count", 0)) for item in file_results)
    summary = {
        "generated_at": now_iso_like(),
        "repo_root": str(repo_root),
        "ok": error_count == 0,
        "file_count": len(file_results),
        "error_count": error_count,
        "warning_count": warning_count,
        "files": file_results,
    }
    write_json(out_dir / "bug_record_portability.json", summary)
    (out_dir / "bug_record_portability.md").write_text(markdown_report(summary), encoding="utf-8")
    print(f"summary={out_dir / 'bug_record_portability.json'}")
    print(f"report={out_dir / 'bug_record_portability.md'}")
    print(f"ok={summary['ok']} errors={error_count} warnings={warning_count}")
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
