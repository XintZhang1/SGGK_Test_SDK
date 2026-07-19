#!/usr/bin/env python3
"""Audit SGGK corpus dataset indexes before long campaign runs."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]


EXTENSION_TO_API = {
    ".sgt": "check_sgt",
    ".step": "step_import",
    ".stp": "step_import",
    ".iges": "iges_import",
    ".igs": "iges_import",
}
SHA1_RE = re.compile(r"[0-9a-fA-F]{40}")
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-list", action="append", required=True, help="discover_corpus.py JSON index or plain path list")
    parser.add_argument("--out", default="artifacts/dataset_audit", help="Output directory for dataset_audit.json/.md")
    parser.add_argument("--min-files", type=int, default=1, help="Minimum referenced files required")
    parser.add_argument("--warn-tiny-bytes", type=int, default=0, help="Warn for nonempty files smaller than this size; 0 disables")
    parser.add_argument(
        "--require-hashes",
        action="store_true",
        help=(
            "Require a valid per-file SHA-256 content binding; "
            "SHA-1-only legacy indexes do not satisfy this gate"
        ),
    )
    parser.add_argument(
        "--fail-duplicate-ratio",
        type=float,
        default=-1.0,
        help="Fail when duplicate-file ratio is above this value; negative disables",
    )
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def resolve_entry_path(raw: str, dataset_path: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    candidates = [
        (REPO_ROOT / path).resolve(),
        (Path.cwd() / path).resolve(),
        (dataset_path.parent / path).resolve(),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Repository-relative paths are the canonical portable format for indexes
    # generated inside this project. Plain sibling names remain relative to the
    # dataset list so hand-authored text lists continue to work.
    if len(path.parts) > 1 and path.parts[0].lower() in {
        "artifacts",
        "test_harness",
        "sgk1.4.10",
    }:
        return candidates[0]
    return candidates[2]


def infer_api(extension: str) -> str:
    return EXTENSION_TO_API.get(extension.lower(), "unknown")


def normalize_entry(raw: Any, dataset_path: Path, index: int) -> dict[str, Any]:
    if isinstance(raw, str):
        path_text = raw
        extension = Path(path_text).suffix.lower()
        return {
            "index": index,
            "path": path_text,
            "extension": extension,
            "api": infer_api(extension),
            "root": "",
            "sha1": "",
            "sha256": "",
            "declared_size_bytes": None,
        }
    if isinstance(raw, dict):
        path_text = as_str(raw.get("path") or raw.get("source_file") or raw.get("file"))
        extension = as_str(raw.get("extension")).lower() or Path(path_text).suffix.lower()
        return {
            "index": as_int(raw.get("index")) if "index" in raw else index,
            "path": path_text,
            "extension": extension,
            "api": as_str(raw.get("api")) or infer_api(extension),
            "root": as_str(raw.get("root")),
            "sha1": as_str(raw.get("sha1")),
            "sha256": as_str(raw.get("sha256")),
            "declared_size_bytes": raw.get("size_bytes"),
        }
    return {
        "index": index,
        "path": "",
        "extension": "",
        "api": "unknown",
        "root": "",
        "sha1": "",
        "sha256": "",
        "declared_size_bytes": None,
    }


def load_dataset(path: Path) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": str(path),
        "kind": "missing",
        "ok": False,
        "entries": [],
        "declared_total_files": None,
        "declared_hash_inputs": None,
        "load_error": "",
    }
    if not path.is_file():
        item["load_error"] = "dataset list path does not exist"
        return item
    try:
        if path.suffix.lower() == ".json":
            data = read_json(path)
            item["kind"] = "json"
            if isinstance(data, dict):
                raw_entries = data.get("files", [])
                item["declared_total_files"] = data.get("total_files")
                item["declared_hash_inputs"] = data.get("hash_inputs")
            elif isinstance(data, list):
                raw_entries = data
                item["declared_total_files"] = len(data)
            else:
                raw_entries = []
                item["load_error"] = "JSON root is not an object or list"
            if isinstance(raw_entries, list):
                item["entries"] = [normalize_entry(entry, path, index) for index, entry in enumerate(raw_entries)]
        else:
            item["kind"] = "paths"
            lines = [line.strip() for line in read_text(path).splitlines() if line.strip() and not line.strip().startswith("#")]
            item["declared_total_files"] = len(lines)
            item["entries"] = [normalize_entry(line, path, index) for index, line in enumerate(lines)]
    except Exception as exc:  # noqa: BLE001
        item["load_error"] = str(exc)
    item["ok"] = not item["load_error"]
    return item


def add_issue(issues: list[dict[str, Any]], severity: str, kind: str, message: str, path: Any = "", dataset: str = "") -> None:
    issues.append(
        {
            "severity": severity,
            "kind": kind,
            "message": message,
            "path": str(path) if path else "",
            "dataset": dataset,
        }
    )


def audit_datasets(args: argparse.Namespace) -> dict[str, Any]:
    datasets = [load_dataset(Path(raw).resolve()) for raw in args.dataset_list]
    issues: list[dict[str, Any]] = []
    by_extension: Counter[str] = Counter()
    by_api: Counter[str] = Counter()
    by_root: Counter[str] = Counter()
    path_counts: Counter[str] = Counter()
    hash_to_paths: dict[tuple[str, str], list[str]] = defaultdict(list)
    entries_out: list[dict[str, Any]] = []
    existing_count = 0
    missing_count = 0
    empty_count = 0
    tiny_count = 0
    total_bytes = 0
    hash_present_count = 0
    hash_missing_count = 0
    sha1_present_count = 0
    sha256_present_count = 0
    sha256_invalid_count = 0

    for dataset in datasets:
        dataset_path = as_str(dataset.get("path"))
        if not dataset.get("ok"):
            add_issue(issues, "error", "dataset_load_failed", as_str(dataset.get("load_error")) or "failed to load dataset", dataset_path, dataset_path)
            continue
        declared = dataset.get("declared_total_files")
        entries = dataset.get("entries") if isinstance(dataset.get("entries"), list) else []
        if declared is not None and as_int(declared) != len(entries):
            add_issue(
                issues,
                "warning",
                "declared_total_mismatch",
                f"declared total_files={declared} actual entries={len(entries)}",
                dataset_path,
                dataset_path,
            )
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw_path = as_str(entry.get("path"))
            resolved = resolve_entry_path(raw_path, Path(dataset_path))
            extension = as_str(entry.get("extension")).lower() or resolved.suffix.lower()
            api = as_str(entry.get("api")) or infer_api(extension)
            root = as_str(entry.get("root")) or str(resolved.parent)
            sha1 = as_str(entry.get("sha1")).strip().lower()
            sha256 = as_str(entry.get("sha256")).strip().lower()
            valid_sha1 = SHA1_RE.fullmatch(sha1) is not None
            valid_sha256 = SHA256_RE.fullmatch(sha256) is not None
            size = -1
            exists = resolved.is_file()
            if exists:
                size = resolved.stat().st_size
                total_bytes += size
                existing_count += 1
                if size <= 0:
                    empty_count += 1
                    add_issue(issues, "error", "empty_file", "referenced corpus file is empty", resolved, dataset_path)
                elif args.warn_tiny_bytes > 0 and size < args.warn_tiny_bytes:
                    tiny_count += 1
                    add_issue(issues, "warning", "tiny_file", f"file is smaller than {args.warn_tiny_bytes} bytes", resolved, dataset_path)
            else:
                missing_count += 1
                add_issue(issues, "error", "missing_file", "referenced corpus file is missing", resolved, dataset_path)
            if extension not in EXTENSION_TO_API:
                add_issue(issues, "warning", "unsupported_extension", f"unsupported extension `{extension}`", resolved, dataset_path)
            if valid_sha1:
                sha1_present_count += 1
            if valid_sha256:
                sha256_present_count += 1
            elif sha256:
                sha256_invalid_count += 1
            if valid_sha256 or valid_sha1:
                hash_present_count += 1
            else:
                hash_missing_count += 1
            # Prefer SHA-1 for duplicate grouping when both are present so
            # modern indexes remain comparable with legacy SHA-1 indexes.
            if valid_sha1:
                hash_to_paths[("sha1", sha1)].append(str(resolved))
            elif valid_sha256:
                hash_to_paths[("sha256", sha256)].append(str(resolved))
            by_extension[extension or "<none>"] += 1
            by_api[api or "unknown"] += 1
            by_root[root] += 1
            path_counts[str(resolved).lower()] += 1
            entries_out.append(
                {
                    **entry,
                    "resolved_path": str(resolved),
                    "exists": exists,
                    "actual_size_bytes": size if exists else None,
                    "extension": extension,
                    "api": api,
                    "root": root,
                }
            )

    total_files = len(entries_out)
    duplicate_path_count = sum(count - 1 for count in path_counts.values() if count > 1)
    if duplicate_path_count:
        add_issue(issues, "warning", "duplicate_paths", f"duplicate referenced paths={duplicate_path_count}")

    duplicate_groups = []
    for (algorithm, digest), paths in sorted(hash_to_paths.items()):
        if len(paths) <= 1:
            continue
        group = {
            "algorithm": algorithm,
            "digest": digest,
            "count": len(paths),
            "paths": sorted(paths),
        }
        # Preserve the legacy field for consumers that inspect SHA-1 groups.
        group[algorithm] = digest
        duplicate_groups.append(group)
    duplicate_file_count = sum(group["count"] for group in duplicate_groups)
    duplicate_ratio = (duplicate_file_count / total_files) if total_files else 0.0
    if duplicate_groups:
        severity = "warning"
        if args.fail_duplicate_ratio >= 0.0 and duplicate_ratio > args.fail_duplicate_ratio:
            severity = "error"
        add_issue(
            issues,
            severity,
            "duplicate_content",
            f"duplicate content groups={len(duplicate_groups)} duplicate_files={duplicate_file_count} ratio={duplicate_ratio:.3f}",
        )
    if total_files < args.min_files:
        add_issue(issues, "error", "too_few_files", f"dataset has files={total_files}, min_files={args.min_files}")
    sha256_missing_count = total_files - sha256_present_count
    if sha256_missing_count:
        severity = "error" if args.require_hashes else "warning"
        invalid_detail = f" (invalid sha256={sha256_invalid_count})" if sha256_invalid_count else ""
        add_issue(
            issues,
            severity,
            "missing_sha256",
            (
                f"files without valid sha256={sha256_missing_count}{invalid_detail}; "
                "SHA-256 content binding is required for UI/campaign use"
            ),
        )
    if hash_missing_count:
        add_issue(
            issues,
            "warning",
            "missing_content_hashes",
            f"files without a valid sha1 or sha256={hash_missing_count}; duplicate-content audit is incomplete",
        )
    if total_files > 0 and len(by_extension) <= 1:
        add_issue(issues, "warning", "single_extension", f"dataset has one extension family: {', '.join(by_extension)}")
    if total_files > 0 and len(by_api) <= 1:
        add_issue(issues, "warning", "single_api", f"dataset has one API family: {', '.join(by_api)}")

    error_count = sum(1 for issue in issues if issue.get("severity") == "error")
    warning_count = sum(1 for issue in issues if issue.get("severity") == "warning")
    return {
        "generated_at": now_iso_like(),
        "ok": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "dataset_count": len(datasets),
        "total_files": total_files,
        "existing_files": existing_count,
        "missing_files": missing_count,
        "empty_files": empty_count,
        "tiny_files": tiny_count,
        "total_bytes": total_bytes,
        "hash_present_count": hash_present_count,
        "hash_missing_count": hash_missing_count,
        "hash_coverage_ratio": (hash_present_count / total_files) if total_files else 0.0,
        "sha1_present_count": sha1_present_count,
        "sha256_present_count": sha256_present_count,
        "sha256_missing_count": sha256_missing_count,
        "sha256_invalid_count": sha256_invalid_count,
        "sha256_coverage_ratio": (sha256_present_count / total_files) if total_files else 0.0,
        "duplicate_path_count": duplicate_path_count,
        "duplicate_content_group_count": len(duplicate_groups),
        "duplicate_file_count": duplicate_file_count,
        "duplicate_file_ratio": duplicate_ratio,
        "by_extension": dict(sorted(by_extension.items())),
        "by_api": dict(sorted(by_api.items())),
        "by_root": dict(sorted(by_root.items())),
        "duplicate_content_groups": duplicate_groups,
        "datasets": datasets,
        "entries": entries_out,
        "issues": issues,
    }


def markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# SGGK Corpus Dataset Audit",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- OK: `{summary.get('ok')}`",
        f"- Files: `{summary.get('total_files')}`",
        f"- Existing: `{summary.get('existing_files')}`",
        f"- Missing: `{summary.get('missing_files')}`",
        f"- Empty: `{summary.get('empty_files')}`",
        f"- Errors: `{summary.get('error_count')}`",
        f"- Warnings: `{summary.get('warning_count')}`",
        f"- Content-hash coverage (SHA-1 or SHA-256): `{summary.get('hash_present_count')}/{summary.get('total_files')}`",
        f"- SHA-256 campaign-binding coverage: `{summary.get('sha256_present_count')}/{summary.get('total_files')}`",
        f"- Duplicate content groups: `{summary.get('duplicate_content_group_count')}`",
        "",
        "## By Extension",
        "",
    ]
    for key, value in summary.get("by_extension", {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## By API", ""])
    for key, value in summary.get("by_api", {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Issues", "", "| severity | kind | message | path | dataset |", "| --- | --- | --- | --- | --- |"])
    for issue in summary.get("issues", []):
        if not isinstance(issue, dict):
            continue
        lines.append(
            "| `{severity}` | `{kind}` | {message} | `{path}` | `{dataset}` |".format(
                severity=issue.get("severity", ""),
                kind=issue.get("kind", ""),
                message=str(issue.get("message", "")).replace("|", "\\|"),
                path=str(issue.get("path", "")).replace("|", "\\|"),
                dataset=str(issue.get("dataset", "")).replace("|", "\\|"),
            )
        )
    if summary.get("duplicate_content_groups"):
        lines.extend(
            [
                "",
                "## Duplicate Content",
                "",
                "| algorithm | digest | count | first paths |",
                "| --- | --- | ---: | --- |",
            ]
        )
        for group in summary.get("duplicate_content_groups", [])[:20]:
            if not isinstance(group, dict):
                continue
            paths = ", ".join(f"`{path}`" for path in group.get("paths", [])[:3])
            lines.append(
                f"| `{group.get('algorithm')}` | `{group.get('digest')}` | {group.get('count')} | {paths} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.min_files < 0 or args.warn_tiny_bytes < 0:
        print("--min-files and --warn-tiny-bytes must be >= 0")
        return 2
    summary = audit_datasets(args)
    out_dir = Path(args.out).resolve()
    json_path = out_dir / "dataset_audit.json"
    md_path = out_dir / "dataset_audit.md"
    write_json(json_path, summary)
    md_path.write_text(markdown_report(summary), encoding="utf-8")
    print(f"summary={json_path}")
    print(f"report={md_path}")
    print(f"ok={summary['ok']} errors={summary['error_count']} warnings={summary['warning_count']} files={summary['total_files']}")
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
