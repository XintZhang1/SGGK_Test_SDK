#!/usr/bin/env python3
"""Materialize local interface input assets from an existing corpus cache.

This utility is a fixed asset-preparation action for the intranet workflow. It reads an
already-present ABC fetch/import cache and writes SDK-local asset indexes plus
bounded STEP, imported SGT, and local IGES smoke links/copies. It does not download
data, call models, run SDK recipes, apply source patches, or commit files.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_ABC_ROOTS = [
    *([Path(os.environ["SGGK_ABC_CACHE_ROOT"])] if os.environ.get("SGGK_ABC_CACHE_ROOT") else []),
    REPO_ROOT / "artifacts" / "abc_fetch_40chunk_sample50",
    REPO_ROOT / "artifacts" / "abc_fetch_smoke",
]
DEFAULT_SOURCE_IGES_ROOTS = [
    *([Path(os.environ["SGGK_DATA_ROOT"])] if os.environ.get("SGGK_DATA_ROOT") else []),
    REPO_ROOT / "artifacts",
]
SOURCE_FILE_SUFFIXES = {".step", ".stp", ".iges", ".igs", ".sgt"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-abc-root",
        default="",
        help="Existing ABC fetch root containing dataset_index.json, complex_dataset_index.json, and optional full_complex_import.",
    )
    parser.add_argument("--source-import-root", default="", help="Existing import artifact root with */output/result_1.sgt")
    parser.add_argument(
        "--source-iges-root",
        action="append",
        default=[],
        help="Existing local root containing .iges/.igs files for a clearly marked IGES smoke fallback; repeatable.",
    )
    parser.add_argument("--target-abc-root", default="artifacts/abc_fetch_smoke", help="SDK-local target root for dataset indexes")
    parser.add_argument(
        "--target-step-root",
        default="artifacts/abc_fetch_smoke/files",
        help="SDK-local target root for STEP/STP files referenced by materialized dataset indexes",
    )
    parser.add_argument(
        "--target-sgt-root",
        default="artifacts/interface_distillation/02_step_import_abc/top_complex_import",
        help="SDK-local target root for imported SGT samples",
    )
    parser.add_argument("--target-iges-root", default="artifacts/iges_import_smoke", help="SDK-local target root for local IGES smoke samples")
    parser.add_argument(
        "--target-strict-iges-root",
        default="artifacts/abc_fetch_smoke/files/iges_complex",
        help="SDK-local target root for strict ABC IGES/IGS files when a real ABC IGES cache exists",
    )
    parser.add_argument(
        "--target-strict-iges-index",
        default="artifacts/abc_fetch_smoke/iges_complex_dataset_index.json",
        help="SDK-local dataset index to write for strict ABC IGES/IGS files when available",
    )
    parser.add_argument("--manifest-out", default="artifacts/input_asset_materialization/manifest.json")
    parser.add_argument("--markdown", default="artifacts/input_asset_materialization/report.md")
    parser.add_argument("--max-index-files", type=int, default=48, help="Maximum STEP/IGES files to include in materialized indexes; 0 means all")
    parser.add_argument("--max-strict-iges", type=int, default=48, help="Maximum strict ABC IGES/IGS files to materialize; 0 means all")
    parser.add_argument("--max-sgt", type=int, default=8, help="Maximum imported result_1.sgt files to materialize; 0 means all")
    parser.add_argument("--max-iges", type=int, default=8, help="Maximum local IGES/IGS smoke files to materialize; 0 means all")
    parser.add_argument("--mode", choices=["hardlink", "copy"], default="hardlink", help="How to materialize asset files")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing generated index files and SGT targets")
    parser.add_argument("--dry-run", action="store_true", help="Only report planned writes")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def rel_display(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any, *, dry_run: bool = False) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str, *, dry_run: bool = False) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def pick_source_abc_root(raw: str) -> Path:
    if raw:
        return repo_path(raw)
    for candidate in DEFAULT_SOURCE_ABC_ROOTS:
        if candidate.is_dir():
            return candidate
    return DEFAULT_SOURCE_ABC_ROOTS[0]


def source_iges_roots(raw_roots: list[str]) -> list[Path]:
    roots = [repo_path(raw) for raw in raw_roots] if raw_roots else [path for path in DEFAULT_SOURCE_IGES_ROOTS if path.is_dir()]
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve()).lower() if root.exists() else str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def source_path_from_entry(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        value = item.get("path") or item.get("source_file") or item.get("file")
        return value if isinstance(value, str) else ""
    return ""


def entry_api_for_path(path_text: str) -> str:
    suffix = Path(path_text).suffix.lower()
    if suffix in {".step", ".stp"}:
        return "step_import"
    if suffix in {".iges", ".igs"}:
        return "iges_import"
    if suffix == ".sgt":
        return "check_sgt"
    return "unknown"


def load_index_entries(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = read_json(path)
    raw_entries = data.get("files") if isinstance(data, dict) else data
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(as_list(raw_entries)):
        path_text = source_path_from_entry(item)
        if not path_text:
            continue
        suffix = Path(path_text).suffix.lower()
        if suffix not in SOURCE_FILE_SUFFIXES:
            continue
        entry = dict(item) if isinstance(item, dict) else {"path": path_text}
        entry["source_index"] = entry.get("index", index)
        entry["path"] = path_text
        entry["extension"] = str(entry.get("extension") or suffix).lower()
        entry["api"] = str(entry.get("api") or entry_api_for_path(path_text))
        entries.append(entry)
    return entries


def select_entries(entries: list[dict[str, Any]], max_files: int, suffixes: set[str] | None = None) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for item in entries:
        suffix = Path(str(item.get("path") or "")).suffix.lower()
        if suffixes is not None and suffix not in suffixes:
            continue
        selected.append(item)
        if max_files > 0 and len(selected) >= max_files:
            break
    return selected


def materialized_index(source_path: Path, entries: list[dict[str, Any]], *, kind: str) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    by_extension: Counter[str] = Counter()
    by_api: Counter[str] = Counter()
    total_bytes = 0
    for index, item in enumerate(entries):
        out = dict(item)
        path_text = str(out.get("path") or "")
        path = Path(path_text)
        size = path.stat().st_size if path.is_file() else int(out.get("size_bytes") or 0)
        suffix = path.suffix.lower()
        out["index"] = index
        out["path"] = path_text
        out["extension"] = str(out.get("extension") or suffix).lower()
        out["api"] = str(out.get("api") or entry_api_for_path(path_text))
        out["size_bytes"] = size
        out["materialized_from"] = str(source_path)
        by_extension[out["extension"]] += 1
        by_api[out["api"]] += 1
        total_bytes += size
        files.append(out)
    return {
        "generated_at": now_iso_like(),
        "source": "materialize_input_assets",
        "source_index": str(source_path),
        "kind": kind,
        "hash_inputs": any("sha1" in item for item in files),
        "total_files": len(files),
        "total_bytes": total_bytes,
        "by_extension": dict(sorted(by_extension.items())),
        "by_api": dict(sorted(by_api.items())),
        "files": files,
    }


def source_sgt_files(import_root: Path) -> list[Path]:
    if not import_root.is_dir():
        return []
    files = [path for path in import_root.glob("*/output/result_1.sgt") if path.is_file()]
    return sorted(files, key=lambda item: str(item).lower())


def source_iges_files(roots: list[Path], max_files: int = 0) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if root.is_file() and root.suffix.lower() in {".iges", ".igs"}:
            candidates = [root]
        elif root.is_dir():
            candidates = (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".iges", ".igs"})
        else:
            candidates = []
        for path in candidates:
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
            if max_files > 0 and len(files) >= max_files:
                return sorted(files, key=lambda item: str(item).lower())
    return sorted(files, key=lambda item: str(item).lower())


def recursive_source_files(root: Path, suffixes: set[str], max_files: int = 0) -> list[Path]:
    if root.is_file() and root.suffix.lower() in suffixes:
        return [root]
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        files.append(path)
        if max_files > 0 and len(files) >= max_files:
            break
    return sorted(files, key=lambda item: str(item).lower())


def entries_from_paths(paths: list[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        suffix = path.suffix.lower()
        entries.append(
            {
                "source_index": index,
                "path": str(path),
                "extension": suffix,
                "api": entry_api_for_path(str(path)),
                "size_bytes": path.stat().st_size if path.is_file() else 0,
            }
        )
    return entries


def strict_abc_iges_entry_candidates(source_abc_root: Path, complex_entries: list[dict[str, Any]], max_scan: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    suffixes = {".iges", ".igs"}
    indexed = [entry for entry in complex_entries if Path(str(entry.get("path") or "")).suffix.lower() in suffixes]
    scanned_paths = recursive_source_files(source_abc_root, suffixes, max_scan)
    scanned = entries_from_paths(scanned_paths)
    mode = "complex_dataset_index" if indexed else "source_abc_root_recursive_scan"
    candidates = indexed if indexed else scanned
    return candidates, {
        "candidate_mode": mode,
        "complex_index_candidate_count": len(indexed),
        "source_root_scan_candidate_count": len(scanned),
        "source_root_scan_limited": bool(max_scan > 0 and len(scanned) >= max_scan),
        "source_root_scan_examples": [str(path) for path in scanned_paths[:5]],
    }


def safe_path_id(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in value)
    text = "_".join(part for part in text.split("_") if part)
    return text.strip("._-") or "asset"


def step_case_dir(source: Path, index: int) -> str:
    case_base = source.parent.name or source.stem
    digest = hashlib.sha1(str(source.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{index:04d}_{safe_path_id(case_base)}_{digest}"


def materialize_one_file(
    source: Path,
    target: Path,
    *,
    case_dir: str,
    mode: str,
    overwrite: bool,
    dry_run: bool,
) -> dict[str, Any]:
    action = "would_link" if dry_run and mode == "hardlink" else "would_copy" if dry_run else mode
    status = "planned" if dry_run else "ok"
    if target.exists():
        if overwrite:
            if not dry_run:
                target.unlink()
        else:
            return {
                "source": str(source),
                "target": rel_display(target),
                "case_dir": case_dir,
                "status": "exists",
                "action": "skip_existing",
                "size_bytes": target.stat().st_size,
            }
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "hardlink":
            try:
                os.link(source, target)
                action = "hardlink"
            except OSError:
                shutil.copy2(source, target)
                action = "copy_fallback"
        else:
            shutil.copy2(source, target)
            action = "copy"
    return {
        "source": str(source),
        "target": rel_display(target),
        "case_dir": case_dir,
        "status": status,
        "action": action,
        "size_bytes": source.stat().st_size,
    }


def materialize_one_step(
    source: Path,
    target_root: Path,
    *,
    index: int,
    mode: str,
    overwrite: bool,
    dry_run: bool,
) -> dict[str, Any]:
    case_dir = step_case_dir(source, index)
    target = target_root / case_dir / source.name
    return materialize_one_file(source, target, case_dir=case_dir, mode=mode, overwrite=overwrite, dry_run=dry_run)


def materialize_step_entries(
    entries: list[dict[str, Any]],
    target_root: Path,
    *,
    mode: str,
    overwrite: bool,
    dry_run: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        source = Path(str(entry.get("path") or ""))
        if not source.is_file():
            records.append(
                {
                    "source": str(source),
                    "target": "",
                    "case_dir": "",
                    "status": "source_missing",
                    "action": "skip_missing_source",
                    "size_bytes": int(entry.get("size_bytes") or 0),
                }
            )
            continue
        records.append(
            materialize_one_step(
                source,
                target_root,
                index=index,
                mode=mode,
                overwrite=overwrite,
                dry_run=dry_run,
            )
        )
    return records


def index_from_step_records(
    source_path: Path,
    entries: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    kind: str,
    root: Path,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    by_extension: Counter[str] = Counter()
    by_api: Counter[str] = Counter()
    total_bytes = 0
    for index, (entry, record) in enumerate(zip(entries, records)):
        if record.get("status") not in {"ok", "exists", "planned"}:
            continue
        target = str(record.get("target") or "")
        suffix = Path(target).suffix.lower()
        out = dict(entry)
        out["index"] = index
        out["path"] = target
        out["source_file"] = target
        out["extension"] = str(out.get("extension") or suffix).lower()
        out["api"] = str(out.get("api") or entry_api_for_path(target))
        out["size_bytes"] = int(record.get("size_bytes") or out.get("size_bytes") or 0)
        out["root"] = rel_display(root)
        out["case_dir"] = record.get("case_dir")
        out["materialized_from"] = record.get("source")
        out["source_index_file"] = str(source_path)
        by_extension[out["extension"]] += 1
        by_api[out["api"]] += 1
        total_bytes += out["size_bytes"]
        files.append(out)
    return {
        "generated_at": now_iso_like(),
        "source": "materialize_input_assets",
        "source_index": str(source_path),
        "root": rel_display(root),
        "kind": kind,
        "hash_inputs": any("sha1" in item for item in files),
        "total_files": len(files),
        "total_bytes": total_bytes,
        "by_extension": dict(sorted(by_extension.items())),
        "by_api": dict(sorted(by_api.items())),
        "files": files,
    }


def materialize_one_iges(source: Path, target_root: Path, *, mode: str, overwrite: bool, dry_run: bool) -> dict[str, Any]:
    case_base = source.parents[1].name if len(source.parents) > 1 else source.parent.name
    digest = hashlib.sha1(str(source.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:10]
    case_dir = f"{safe_path_id(case_base)}_{digest}"
    target = target_root / case_dir / source.name
    return materialize_one_file(source, target, case_dir=case_dir, mode=mode, overwrite=overwrite, dry_run=dry_run)


def materialize_one_sgt(source: Path, target_root: Path, *, mode: str, overwrite: bool, dry_run: bool) -> dict[str, Any]:
    case_dir = source.parents[1].name
    target = target_root / case_dir / "output" / "result_1.sgt"
    return materialize_one_file(source, target, case_dir=case_dir, mode=mode, overwrite=overwrite, dry_run=dry_run)


def sgt_dataset_index(target_root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if record.get("status") not in {"ok", "exists", "planned"}:
            continue
        target = str(record.get("target") or "")
        files.append(
            {
                "index": index,
                "path": target,
                "source_file": target,
                "extension": ".sgt",
                "api": "check_sgt",
                "size_bytes": int(record.get("size_bytes") or 0),
                "materialized_from": record.get("source"),
                "case_dir": record.get("case_dir"),
            }
        )
    return {
        "generated_at": now_iso_like(),
        "source": "materialize_input_assets",
        "root": rel_display(target_root),
        "total_files": len(files),
        "by_extension": {".sgt": len(files)} if files else {},
        "by_api": {"check_sgt": len(files)} if files else {},
        "files": files,
    }


def iges_dataset_index(target_root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    by_extension: Counter[str] = Counter()
    total_bytes = 0
    for index, record in enumerate(records):
        if record.get("status") not in {"ok", "exists", "planned"}:
            continue
        target = str(record.get("target") or "")
        extension = Path(target).suffix.lower()
        files.append(
            {
                "index": index,
                "path": target,
                "source_file": target,
                "extension": extension,
                "api": "iges_import",
                "size_bytes": int(record.get("size_bytes") or 0),
                "materialized_from": record.get("source"),
                "case_dir": record.get("case_dir"),
                "source_family": "local_generated_roundtrip_iges_smoke",
            }
        )
        by_extension[extension] += 1
        total_bytes += int(record.get("size_bytes") or 0)
    return {
        "generated_at": now_iso_like(),
        "source": "materialize_input_assets",
        "root": rel_display(target_root),
        "kind": "local_iges_smoke_dataset_index",
        "strict_abc_complex_source": False,
        "total_files": len(files),
        "total_bytes": total_bytes,
        "by_extension": dict(sorted(by_extension.items())),
        "by_api": {"iges_import": len(files)} if files else {},
        "files": files,
    }


def markdown_report(manifest: dict[str, Any]) -> str:
    lines = [
        "# Input Asset Materialization",
        "",
        f"- Generated: `{manifest.get('generated_at')}`",
        f"- Dry run: `{manifest.get('dry_run')}`",
        f"- Source ABC root: `{manifest.get('source_abc_root')}`",
        f"- Target ABC root: `{manifest.get('target_abc_root')}`",
        f"- Target STEP root: `{manifest.get('target_step_root')}`",
        f"- Target SGT root: `{manifest.get('target_sgt_root')}`",
        f"- Target IGES smoke root: `{manifest.get('target_iges_root')}`",
        f"- Target strict ABC IGES root: `{manifest.get('target_strict_iges_root')}`",
        f"- Target strict ABC IGES index: `{manifest.get('target_strict_iges_index')}`",
        "",
        "## Summary",
        "",
        f"- Dataset index files: `{manifest.get('dataset_indexes', {}).get('written_count')}`",
        f"- STEP materialized: `{manifest.get('step_assets', {}).get('materialized_count')}`",
        f"- SGT materialized: `{manifest.get('sgt_assets', {}).get('materialized_count')}`",
        f"- SGT source candidates: `{manifest.get('sgt_assets', {}).get('source_candidate_count')}`",
        f"- Local IGES smoke materialized: `{manifest.get('iges_assets', {}).get('materialized_count')}`",
        f"- Local IGES smoke source candidates: `{manifest.get('iges_assets', {}).get('source_candidate_count')}`",
        f"- Strict ABC IGES ready: `{manifest.get('strict_abc_iges_assets', {}).get('ready')}`",
        f"- Strict ABC IGES materialized: `{manifest.get('strict_abc_iges_assets', {}).get('materialized_count')}`",
        f"- Strict ABC IGES source candidates: `{manifest.get('strict_abc_iges_assets', {}).get('source_candidate_count')}`",
        "",
        "## Dataset Indexes",
        "",
        "| file | total | by extension |",
        "| --- | ---: | --- |",
    ]
    for item in as_list(manifest.get("dataset_indexes", {}).get("indexes")):
        if not isinstance(item, dict):
            continue
        lines.append(f"| `{item.get('path')}` | {item.get('total_files')} | `{item.get('by_extension')}` |")
    lines.extend(["", "## STEP Assets", "", "| action | status | target | source |", "| --- | --- | --- | --- |"])
    for item in as_list(manifest.get("step_assets", {}).get("records")):
        if not isinstance(item, dict):
            continue
        lines.append(f"| `{item.get('action')}` | `{item.get('status')}` | `{item.get('target')}` | `{item.get('source')}` |")
    if not as_list(manifest.get("step_assets", {}).get("records")):
        lines.append("| none |  |  |  |")
    strict_assets = as_dict(manifest.get("strict_abc_iges_assets"))
    strict_scan = as_dict(strict_assets.get("scan"))
    lines.extend(
        [
            "",
            "## Strict ABC IGES Assets",
            "",
            f"- Ready: `{strict_assets.get('ready')}`",
            f"- Candidate mode: `{strict_scan.get('candidate_mode')}`",
            f"- Complex index candidates: `{strict_scan.get('complex_index_candidate_count')}`",
            f"- Source root scan candidates: `{strict_scan.get('source_root_scan_candidate_count')}`",
            f"- Index path: `{strict_assets.get('index_path')}`",
            "",
            "| action | status | target | source |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in as_list(strict_assets.get("records")):
        if not isinstance(item, dict):
            continue
        lines.append(f"| `{item.get('action')}` | `{item.get('status')}` | `{item.get('target')}` | `{item.get('source')}` |")
    if not as_list(strict_assets.get("records")):
        lines.append("| none |  |  |  |")
    if as_list(strict_assets.get("next_actions")):
        lines.extend(["", "Next actions:"])
        for item in as_list(strict_assets.get("next_actions")):
            lines.append(f"- {item}")
    lines.extend(["", "## SGT Assets", "", "| action | status | target | source |", "| --- | --- | --- | --- |"])
    for item in as_list(manifest.get("sgt_assets", {}).get("records")):
        if not isinstance(item, dict):
            continue
        lines.append(f"| `{item.get('action')}` | `{item.get('status')}` | `{item.get('target')}` | `{item.get('source')}` |")
    if not as_list(manifest.get("sgt_assets", {}).get("records")):
        lines.append("| none |  |  |  |")
    lines.extend(["", "## Local IGES Smoke Assets", "", "| action | status | target | source |", "| --- | --- | --- | --- |"])
    for item in as_list(manifest.get("iges_assets", {}).get("records")):
        if not isinstance(item, dict):
            continue
        lines.append(f"| `{item.get('action')}` | `{item.get('status')}` | `{item.get('target')}` | `{item.get('source')}` |")
    if not as_list(manifest.get("iges_assets", {}).get("records")):
        lines.append("| none |  |  |  |")
    lines.extend(
        [
            "",
            "Local IGES smoke assets are explicitly marked as generated/local IGES fallback inputs.",
            "They do not satisfy a strict ABC complex IGES corpus requirement unless the source roots are replaced with a real ABC IGES cache.",
        ]
    )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "- This tool only reads existing local corpus caches and writes local artifact indexes/links.",
            "- It does not download corpus data, call a Message API, run SDK recipes, apply patches, or commit files.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    source_abc_root = pick_source_abc_root(args.source_abc_root)
    source_import_root = repo_path(args.source_import_root) if args.source_import_root else source_abc_root / "full_complex_import"
    source_iges_root_list = source_iges_roots(args.source_iges_root)
    target_abc_root = repo_path(args.target_abc_root)
    target_step_root = repo_path(args.target_step_root)
    target_sgt_root = repo_path(args.target_sgt_root)
    target_iges_root = repo_path(args.target_iges_root)
    target_strict_iges_root = repo_path(args.target_strict_iges_root)
    target_strict_iges_index = repo_path(args.target_strict_iges_index)
    source_dataset = source_abc_root / "dataset_index.json"
    source_complex = source_abc_root / "complex_dataset_index.json"
    all_entries = load_index_entries(source_dataset)
    complex_entries = load_index_entries(source_complex) or all_entries
    selected_all = select_entries(all_entries, args.max_index_files)
    selected_complex = select_entries(complex_entries, args.max_index_files)

    step_root_all = target_step_root / "dataset"
    step_root_complex = target_step_root / "complex"
    dataset_step_records = materialize_step_entries(
        selected_all,
        step_root_all,
        mode=args.mode,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    complex_step_records = materialize_step_entries(
        selected_complex,
        step_root_complex,
        mode=args.mode,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    dataset_index = index_from_step_records(source_dataset, selected_all, dataset_step_records, kind="dataset_index", root=step_root_all)
    complex_index = index_from_step_records(source_complex, selected_complex, complex_step_records, kind="complex_dataset_index", root=step_root_complex)
    dataset_path = target_abc_root / "dataset_index.json"
    complex_path = target_abc_root / "complex_dataset_index.json"
    if not args.dry_run:
        target_abc_root.mkdir(parents=True, exist_ok=True)
    if args.overwrite or not dataset_path.exists():
        write_json(dataset_path, dataset_index, dry_run=args.dry_run)
    if args.overwrite or not complex_path.exists():
        write_json(complex_path, complex_index, dry_run=args.dry_run)

    strict_iges_candidates, strict_iges_scan = strict_abc_iges_entry_candidates(
        source_abc_root,
        complex_entries,
        args.max_strict_iges,
    )
    selected_strict_iges = select_entries(strict_iges_candidates, args.max_strict_iges, {".iges", ".igs"})
    strict_iges_records = materialize_step_entries(
        selected_strict_iges,
        target_strict_iges_root,
        mode=args.mode,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    strict_iges_index = index_from_step_records(
        source_complex if strict_iges_scan.get("candidate_mode") == "complex_dataset_index" else source_abc_root,
        selected_strict_iges,
        strict_iges_records,
        kind="strict_abc_iges_complex_dataset_index",
        root=target_strict_iges_root,
    )
    strict_iges_index["strict_abc_complex_source"] = True
    strict_iges_ready = bool(strict_iges_index.get("total_files"))
    strict_iges_index_written = False
    if strict_iges_ready and (args.overwrite or not target_strict_iges_index.exists()):
        write_json(target_strict_iges_index, strict_iges_index, dry_run=args.dry_run)
        strict_iges_index_written = True

    sgt_sources = source_sgt_files(source_import_root)
    selected_sgt_sources = sgt_sources if args.max_sgt == 0 else sgt_sources[: args.max_sgt]
    sgt_records = [
        materialize_one_sgt(source, target_sgt_root, mode=args.mode, overwrite=args.overwrite, dry_run=args.dry_run)
        for source in selected_sgt_sources
    ]
    sgt_index = sgt_dataset_index(target_sgt_root, sgt_records)
    sgt_index_path = target_sgt_root / "sgt_dataset_index.json"
    write_json(sgt_index_path, sgt_index, dry_run=args.dry_run)

    iges_sources = source_iges_files(source_iges_root_list, args.max_iges)
    selected_iges_sources = iges_sources if args.max_iges == 0 else iges_sources[: args.max_iges]
    iges_records = [
        materialize_one_iges(source, target_iges_root, mode=args.mode, overwrite=args.overwrite, dry_run=args.dry_run)
        for source in selected_iges_sources
    ]
    iges_index = iges_dataset_index(target_iges_root, iges_records)
    iges_index_path = target_iges_root / "dataset_index.json"
    write_json(iges_index_path, iges_index, dry_run=args.dry_run)

    return {
        "generated_at": now_iso_like(),
        "dry_run": bool(args.dry_run),
        "boundary": {
            "model_calls": False,
            "direct_api_calls": False,
            "runs_sdk": False,
            "downloads_assets": False,
            "generates_recipes": False,
            "applies_patches": False,
            "commits_changes": False,
            "production_flow": "deterministic_local_asset_materialization",
        },
        "source_abc_root": str(source_abc_root),
        "source_import_root": str(source_import_root),
        "source_iges_roots": [str(path) for path in source_iges_root_list],
        "target_abc_root": rel_display(target_abc_root),
        "target_step_root": rel_display(target_step_root),
        "target_sgt_root": rel_display(target_sgt_root),
        "target_iges_root": rel_display(target_iges_root),
        "target_strict_iges_root": rel_display(target_strict_iges_root),
        "target_strict_iges_index": rel_display(target_strict_iges_index),
        "dataset_indexes": {
            "written_count": 2,
            "indexes": [
                {"path": rel_display(dataset_path), **{key: dataset_index.get(key) for key in ("total_files", "by_extension", "by_api")}},
                {"path": rel_display(complex_path), **{key: complex_index.get(key) for key in ("total_files", "by_extension", "by_api")}},
            ],
        },
        "step_assets": {
            "source_candidate_count": len(selected_all) + len(selected_complex),
            "selected_count": len(selected_all) + len(selected_complex),
            "materialized_count": sum(
                1 for item in dataset_step_records + complex_step_records if item.get("status") in {"ok", "exists", "planned"}
            ),
            "mode": args.mode,
            "root": rel_display(target_step_root),
            "records": dataset_step_records + complex_step_records,
        },
        "strict_abc_iges_assets": {
            "ready": strict_iges_ready,
            "source_candidate_count": len(strict_iges_candidates),
            "selected_count": len(selected_strict_iges),
            "materialized_count": sum(1 for item in strict_iges_records if item.get("status") in {"ok", "exists", "planned"}),
            "mode": args.mode,
            "root": rel_display(target_strict_iges_root),
            "index_path": rel_display(target_strict_iges_index),
            "index_written": strict_iges_index_written,
            "strict_abc_complex_source": True,
            "scan": strict_iges_scan,
            "records": strict_iges_records,
            "next_actions": []
            if strict_iges_ready
            else [
                "Provide a real ABC IGES/IGS cache or complex_dataset_index.json entries with .iges/.igs paths.",
                "Rerun materialize_input_assets.py with --source-abc-root pointing at that cache.",
                "Update iface_14 input_assets.dataset_index only after the strict IGES index exists and validate_input_assets.py is green.",
            ],
        },
        "sgt_assets": {
            "source_candidate_count": len(sgt_sources),
            "selected_count": len(selected_sgt_sources),
            "materialized_count": sum(1 for item in sgt_records if item.get("status") in {"ok", "exists", "planned"}),
            "mode": args.mode,
            "index_path": rel_display(sgt_index_path),
            "records": sgt_records,
        },
        "iges_assets": {
            "source_candidate_count": len(iges_sources),
            "selected_count": len(selected_iges_sources),
            "materialized_count": sum(1 for item in iges_records if item.get("status") in {"ok", "exists", "planned"}),
            "mode": args.mode,
            "index_path": rel_display(iges_index_path),
            "strict_abc_complex_source": False,
            "records": iges_records,
        },
    }


def main() -> int:
    args = parse_args()
    if args.max_index_files < 0 or args.max_strict_iges < 0 or args.max_sgt < 0 or args.max_iges < 0:
        print("--max-index-files, --max-strict-iges, --max-sgt, and --max-iges must be >= 0")
        return 2
    manifest = build_manifest(args)
    manifest_path = repo_path(args.manifest_out)
    report_path = repo_path(args.markdown)
    write_json(manifest_path, manifest, dry_run=args.dry_run)
    write_text(report_path, markdown_report(manifest), dry_run=args.dry_run)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": manifest["dry_run"],
                "manifest": rel_display(manifest_path),
                "dataset_indexes": manifest["dataset_indexes"]["written_count"],
                "step_materialized": manifest["step_assets"]["materialized_count"],
                "strict_abc_iges_ready": manifest["strict_abc_iges_assets"]["ready"],
                "strict_abc_iges_materialized": manifest["strict_abc_iges_assets"]["materialized_count"],
                "strict_abc_iges_candidates": manifest["strict_abc_iges_assets"]["source_candidate_count"],
                "sgt_materialized": manifest["sgt_assets"]["materialized_count"],
                "sgt_candidates": manifest["sgt_assets"]["source_candidate_count"],
                "iges_materialized": manifest["iges_assets"]["materialized_count"],
                "iges_candidates": manifest["iges_assets"]["source_candidate_count"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
