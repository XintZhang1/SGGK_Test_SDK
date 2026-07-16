#!/usr/bin/env python3
"""Discover local CAD/SGT corpus files and write a runnable dataset index."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

EXTENSION_TO_API = {
    ".sgt": "check_sgt",
    ".step": "step_import",
    ".stp": "step_import",
    ".iges": "iges_import",
    ".igs": "iges_import",
}
DEFAULT_EXCLUDE_DIRS = {".git", ".vs", "__pycache__", "build", "artifacts"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", help="Files or directories to scan")
    parser.add_argument(
        "--out",
        default="artifacts/corpus_discovery/dataset_index.json",
        help="Dataset index JSON path",
    )
    parser.add_argument("--paths-out", default="", help="Optional text file with one discovered path per line")
    parser.add_argument("--report", default="", help="Optional Markdown report path")
    parser.add_argument("--hash-inputs", action="store_true", help="Store SHA1 digests and duplicate content groups")
    parser.add_argument("--include-artifacts", action="store_true", help="Do not exclude directories named artifacts")
    parser.add_argument("--include-build", action="store_true", help="Do not exclude directories named build")
    parser.add_argument("--exclude-dir", action="append", default=[], help="Additional directory name to skip")
    parser.add_argument("--limit", type=int, default=0, help="Maximum discovered files to write; 0 means all")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def file_hashes(path: Path) -> tuple[str, str]:
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest()


def excluded_names(args: argparse.Namespace) -> set[str]:
    names = set(DEFAULT_EXCLUDE_DIRS)
    if args.include_artifacts:
        names.discard("artifacts")
    if args.include_build:
        names.discard("build")
    names.update(name for name in args.exclude_dir if name)
    return names


def is_excluded(path: Path, names: set[str]) -> bool:
    return any(part in names for part in path.parts)


def iter_inputs(roots: list[str], names: set[str]) -> list[Path]:
    found: set[Path] = set()
    for raw in roots:
        root = Path(raw)
        if root.is_file():
            if root.suffix.lower() in EXTENSION_TO_API and not is_excluded(root.resolve(), names):
                found.add(root.resolve())
            continue
        if not root.is_dir():
            continue
        for child in root.rglob("*"):
            if not child.is_file():
                continue
            resolved = child.resolve()
            if is_excluded(resolved, names):
                continue
            if child.suffix.lower() in EXTENSION_TO_API:
                found.add(resolved)
    return sorted(found, key=lambda item: str(item).lower())


def common_root_label(path: Path, roots: list[Path]) -> str:
    resolved = path.resolve()
    best = ""
    best_len = -1
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        label = str(root)
        if len(label) > best_len:
            best = label
            best_len = len(label)
    return best or str(path.parent)


def build_index(args: argparse.Namespace, files: list[Path], excluded: set[str]) -> dict[str, Any]:
    root_paths = [Path(root).resolve() for root in args.roots if Path(root).exists()]
    entries: list[dict[str, Any]] = []
    by_extension: Counter[str] = Counter()
    by_api: Counter[str] = Counter()
    by_root: Counter[str] = Counter()
    total_bytes = 0
    duplicates: dict[str, list[str]] = defaultdict(list)

    for index, path in enumerate(files):
        suffix = path.suffix.lower()
        size = path.stat().st_size
        total_bytes += size
        api = EXTENSION_TO_API[suffix]
        root_label = common_root_label(path, root_paths)
        by_extension[suffix] += 1
        by_api[api] += 1
        by_root[root_label] += 1
        item: dict[str, Any] = {
            "index": index,
            "path": str(path),
            "extension": suffix,
            "api": api,
            "size_bytes": size,
            "root": root_label,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime)),
        }
        if args.hash_inputs:
            sha1, sha256 = file_hashes(path)
            item["sha1"] = sha1
            item["sha256"] = sha256
            duplicates[sha1].append(str(path))
        entries.append(item)

    duplicate_groups = [
        {"sha1": digest, "count": len(paths), "paths": paths}
        for digest, paths in sorted(duplicates.items())
        if len(paths) > 1
    ]
    return {
        "generated_at": now_iso_like(),
        "roots": args.roots,
        "excluded_dir_names": sorted(excluded),
        "supported_extensions": sorted(EXTENSION_TO_API),
        "hash_inputs": args.hash_inputs,
        "total_files": len(entries),
        "total_bytes": total_bytes,
        "by_extension": dict(sorted(by_extension.items())),
        "by_api": dict(sorted(by_api.items())),
        "by_root": dict(sorted(by_root.items())),
        "duplicate_content_groups": duplicate_groups,
        "files": entries,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def write_paths(path: Path, files: list[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(str(file) for file in files) + ("\n" if files else ""), encoding="utf-8")


def write_report(path: Path, index: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# SGGK Corpus Discovery")
    lines.append("")
    lines.append(f"- Generated: {index['generated_at']}")
    lines.append(f"- Files: {index['total_files']}")
    lines.append(f"- Bytes: {index['total_bytes']}")
    lines.append(f"- Excluded dirs: {', '.join(index['excluded_dir_names'])}")
    lines.append("")
    lines.append("## By Extension")
    lines.append("")
    for key, value in index["by_extension"].items():
        lines.append(f"- `{key}`: {value}")
    lines.append("")
    lines.append("## By Root")
    lines.append("")
    for key, value in index["by_root"].items():
        lines.append(f"- `{key}`: {value}")
    if index.get("duplicate_content_groups"):
        lines.append("")
        lines.append("## Duplicate Content")
        lines.append("")
        for group in index["duplicate_content_groups"][:20]:
            lines.append(f"- `{group['sha1']}`: {group['count']} files")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.limit < 0:
        print("--limit must be >= 0")
        return 1
    excluded = excluded_names(args)
    files = iter_inputs(args.roots, excluded)
    if args.limit > 0:
        files = files[: args.limit]
    index = build_index(args, files, excluded)
    out_path = Path(args.out)
    write_json(out_path, index)
    paths_out = Path(args.paths_out) if args.paths_out else out_path.with_suffix(".paths.txt")
    report_path = Path(args.report) if args.report else out_path.with_suffix(".md")
    write_paths(paths_out, files)
    write_report(report_path, index)
    print(f"index={out_path}")
    print(f"paths={paths_out}")
    print(f"report={report_path}")
    print(f"files={index['total_files']} bytes={index['total_bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
