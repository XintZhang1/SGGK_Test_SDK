#!/usr/bin/env python3
"""Validate interface form input assets without running models or SDK code.

This gate is intentionally read-only. It scans interface forms for
``geometry.input_asset`` and ``input_assets`` declarations, checks whether the
declared local files/directories/pattern roots exist, and emits an operator
queue for missing corpus inputs. It never downloads assets, generates recipes,
runs the SDK, applies patches, commits files, or calls a model API.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FORMS_ROOT = REPO_ROOT / "test_harness" / "forms" / "interface_distillation"
DEFAULT_MANIFEST = DEFAULT_FORMS_ROOT / "00_manifest.json"
SOURCE_FILE_SUFFIXES = {".step", ".stp", ".iges", ".igs", ".sgt"}
API_SOURCE_SUFFIXES = {
    "step_import": {".step", ".stp"},
    "iges_import": {".iges", ".igs"},
    "step_roundtrip": {".sgt"},
    "iges_roundtrip": {".sgt"},
    "check_sgt": {".sgt"},
}
PLACEHOLDER_RE = re.compile(r"<[^<>]+>")
SEVERITY_RANK = {"info": 0, "test_gap": 1, "risk": 2, "blocker": 3}
WINDOWS_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Interface form manifest path")
    parser.add_argument("--forms-root", default=str(DEFAULT_FORMS_ROOT), help="Directory containing interface form JSON files")
    parser.add_argument("--form", action="append", default=[], help="Specific form path to validate; repeatable")
    parser.add_argument("--report", default="", help="Optional JSON report path")
    parser.add_argument("--markdown", default="", help="Optional Markdown report path")
    parser.add_argument("--max-candidates", type=int, default=40, help="Maximum source file candidates to list per asset")
    parser.add_argument(
        "--fail-on",
        choices=sorted(SEVERITY_RANK),
        default="blocker",
        help="Return non-zero when findings include this severity or worse.",
    )
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 after writing reports")
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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def finding(
    severity: str,
    code: str,
    message: str,
    *,
    form_id: str = "",
    path: Path | str = "",
    field: str = "",
) -> dict[str, Any]:
    path_text = rel_display(path) if isinstance(path, Path) else path
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "form_id": form_id,
        "path": path_text,
        "field": field,
    }


def fixed_boundary() -> dict[str, Any]:
    return {
        "model_calls": False,
        "direct_api_calls": False,
        "runs_sdk": False,
        "generates_outputs": False,
        "generates_recipes": False,
        "downloads_assets": False,
        "applies_patches": False,
        "commits_changes": False,
        "production_flow": "read_saved_forms_and_local_asset_paths_only",
    }


def manifest_form_paths(manifest_path: Path, forms_root: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    try:
        manifest = read_json(manifest_path)
    except FileNotFoundError:
        return [], [finding("blocker", "ASSET_INPUT_MANIFEST_MISSING", "Interface form manifest is missing.", path=manifest_path)]
    except json.JSONDecodeError as exc:
        return [], [
            finding(
                "blocker",
                "ASSET_INPUT_MANIFEST_INVALID_JSON",
                f"Interface form manifest is invalid JSON: {exc}",
                path=manifest_path,
            )
        ]
    forms = as_list(as_dict(manifest).get("forms"))
    paths: list[Path] = []
    findings: list[dict[str, Any]] = []
    for index, item in enumerate(forms):
        raw = as_dict(item).get("form")
        if not isinstance(raw, str) or not raw:
            findings.append(
                finding(
                    "blocker",
                    "ASSET_INPUT_MANIFEST_FORM_MISSING",
                    "Manifest form entry must include a form filename.",
                    path=manifest_path,
                    field=f"forms[{index}].form",
                )
            )
            continue
        paths.append(forms_root / raw)
    return paths, findings


def load_form(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    try:
        loaded = read_json(path)
    except FileNotFoundError:
        return None, [finding("blocker", "ASSET_INPUT_FORM_MISSING", "Interface form file is missing.", path=path)]
    except json.JSONDecodeError as exc:
        return None, [finding("blocker", "ASSET_INPUT_FORM_INVALID_JSON", f"Interface form is invalid JSON: {exc}", path=path)]
    if not isinstance(loaded, dict):
        return None, [finding("blocker", "ASSET_INPUT_FORM_NOT_OBJECT", "Interface form root must be a JSON object.", path=path)]
    return loaded, []


def declared_assets(form: dict[str, Any]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    geometry = as_dict(form.get("geometry"))
    if isinstance(geometry.get("input_asset"), str) and geometry["input_asset"].strip():
        candidates.append(("geometry.input_asset", geometry["input_asset"].strip()))
    input_assets = as_dict(form.get("input_assets"))
    for key, value in input_assets.items():
        if isinstance(key, str) and isinstance(value, str) and value.strip():
            candidates.append((f"input_assets.{key}", value.strip()))
    merged: dict[str, list[str]] = {}
    order: list[str] = []
    for label, raw in candidates:
        if raw not in merged:
            merged[raw] = []
            order.append(raw)
        merged[raw].append(label)
    return [(", ".join(merged[raw]), raw) for raw in order]


def source_suffixes_for_form(form: dict[str, Any]) -> set[str]:
    target_api = str(form.get("target_api") or "")
    if target_api in API_SOURCE_SUFFIXES:
        return set(API_SOURCE_SUFFIXES[target_api])
    geometry = form.get("geometry")
    input_assets = form.get("input_assets")
    text = json.dumps({"geometry": geometry, "input_assets": input_assets}, ensure_ascii=False).lower()
    if target_api == "api_boolean" and ("loaded_sgt" in text or ".sgt" in text or "result_1.sgt" in text):
        return {".sgt"}
    return set(SOURCE_FILE_SUFFIXES)


def is_corpus_metadata_index(form: dict[str, Any], label: str, raw_path: str) -> bool:
    """Corpus summaries describe a campaign corpus; they are not per-case source pick lists."""
    if str(form.get("target_api") or "") != "api_boolean":
        return False
    if str(form.get("run_profile") or "").lower() != "corpus":
        return False
    if "dataset_index" not in label:
        return False
    return Path(raw_path).name.lower() == "corpus_summary.json"


def source_file_matches(path_text: str, expected_suffixes: set[str]) -> bool:
    suffix = Path(path_text).suffix.lower()
    return suffix in SOURCE_FILE_SUFFIXES and (not expected_suffixes or suffix in expected_suffixes)


def is_absolute_path_text(path_text: str) -> bool:
    return bool(WINDOWS_ABSOLUTE_RE.match(path_text)) or Path(path_text).is_absolute()


def path_under_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT.resolve())
    except (OSError, ValueError):
        return False
    return True


def portable_source_path_summary(source_files: list[str], max_examples: int = 5) -> dict[str, Any]:
    external: list[str] = []
    nonportable: list[str] = []
    missing: list[str] = []
    portable_count = 0
    external_count = 0
    nonportable_count = 0
    missing_count = 0
    checked_count = 0
    for raw in source_files:
        text = str(raw or "").strip()
        if not text:
            continue
        checked_count += 1
        path = Path(text)
        absolute = is_absolute_path_text(text)
        candidate = path if absolute else REPO_ROOT / path
        under_repo = path_under_repo(candidate)
        if not under_repo:
            external_count += 1
            if len(external) < max_examples:
                external.append(text)
            continue
        if not candidate.is_file():
            missing_count += 1
            if len(missing) < max_examples:
                missing.append(text)
            continue
        if absolute or "\\" in text:
            nonportable_count += 1
            if len(nonportable) < max_examples:
                nonportable.append(text)
            continue
        portable_count += 1
    return {
        "checked_count": checked_count,
        "portable_count": portable_count,
        "external_count": external_count,
        "external_examples": external,
        "nonportable_count": nonportable_count,
        "nonportable_examples": nonportable,
        "missing_count": missing_count,
        "missing_examples": missing,
    }


def source_file_candidates(root: Path, max_candidates: int, expected_suffixes: set[str]) -> tuple[list[str], bool]:
    candidates: list[str] = []
    limit_reached = False
    if not root.exists():
        return candidates, limit_reached
    if root.is_file():
        if source_file_matches(str(root), expected_suffixes):
            return [rel_display(root)], False
        return candidates, False
    for child in sorted(root.rglob("*")):
        if not child.is_file() or not source_file_matches(str(child), expected_suffixes):
            continue
        candidates.append(rel_display(child))
        if len(candidates) >= max_candidates:
            limit_reached = True
            break
    return candidates, limit_reached


def walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(walk_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(walk_strings(item))
        return result
    return []


def dataset_source_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if Path(value).suffix.lower() in SOURCE_FILE_SUFFIXES else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(dataset_source_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            if isinstance(item, str) and key in {"path", "source_file", "file"}:
                result.append(item)
            elif isinstance(item, (dict, list)):
                result.extend(dataset_source_strings(item))
        return result
    return []


def dataset_entry_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("items", "files", "sources", "records", "cases", "entries", "results"):
            item = value.get(key)
            if isinstance(item, list):
                return len(item)
    return 0


def summarize_dataset_index(path: Path, max_candidates: int, expected_suffixes: set[str]) -> dict[str, Any]:
    try:
        loaded = read_json(path)
    except json.JSONDecodeError as exc:
        return {"parse_error": str(exc), "entry_count": 0, "source_files": [], "matching_source_files": []}
    strings = dataset_source_strings(loaded)
    source_files: list[str] = []
    matching_source_files: list[str] = []
    seen_source_files: set[str] = set()
    by_suffix: Counter[str] = Counter()
    for value in strings:
        suffix = Path(value).suffix.lower()
        if suffix not in SOURCE_FILE_SUFFIXES:
            continue
        if value in seen_source_files:
            continue
        seen_source_files.add(value)
        by_suffix[suffix] += 1
        if value not in source_files and len(source_files) < max_candidates:
            source_files.append(value)
        if source_file_matches(value, expected_suffixes) and value not in matching_source_files:
            matching_source_files.append(value)
            if len(matching_source_files) >= max_candidates:
                continue
    return {
        "entry_count": dataset_entry_count(loaded),
        "source_file_count": sum(by_suffix.values()),
        "source_file_suffix_counts": dict(sorted(by_suffix.items())),
        "source_files": source_files,
        "matching_source_file_count": sum(count for suffix, count in by_suffix.items() if suffix in expected_suffixes),
        "matching_source_files": matching_source_files,
    }


def placeholder_root(raw_path: str) -> Path:
    first = PLACEHOLDER_RE.split(raw_path, maxsplit=1)[0].rstrip("/\\")
    if not first:
        return REPO_ROOT
    return repo_path(first)


def status_for_asset(
    label: str,
    raw_path: str,
    *,
    max_candidates: int,
    expected_suffixes: set[str],
    metadata_only_index: bool = False,
) -> dict[str, Any]:
    path = repo_path(raw_path)
    label_tail = label.split(".")[-1]
    record: dict[str, Any] = {
        "label": label,
        "path": raw_path,
        "resolved_path": rel_display(path),
        "status": "unknown",
        "exists": False,
        "kind": "",
        "expected_source_suffixes": sorted(expected_suffixes),
        "metadata_only_index": metadata_only_index,
        "available_source_files": [],
        "candidate_limit_reached": False,
    }
    if PLACEHOLDER_RE.search(raw_path):
        root = placeholder_root(raw_path)
        candidates, limit_reached = source_file_candidates(root, max_candidates, expected_suffixes)
        record.update(
            {
                "kind": "placeholder_pattern",
                "pattern_root": rel_display(root),
                "pattern_root_exists": root.exists(),
                "available_source_files": candidates,
                "candidate_limit_reached": limit_reached,
                "status": "placeholder_candidates_found" if candidates else "placeholder_unresolved",
                "exists": bool(candidates),
            }
        )
        if not root.exists():
            record["status"] = "placeholder_root_missing"
        return record

    if not path.exists():
        missing_status = "path_missing"
        if label_tail == "dataset_index":
            missing_status = "dataset_index_missing"
        elif label_tail == "dataset_root":
            missing_status = "dataset_root_missing"
        elif label_tail == "source_file" or path.suffix.lower() in SOURCE_FILE_SUFFIXES:
            missing_status = "source_file_missing"
        record["status"] = missing_status
        return record

    record["exists"] = True
    if path.is_file():
        record["kind"] = "file"
        if label_tail == "dataset_index" or path.suffix.lower() == ".json":
            summary = summarize_dataset_index(path, max_candidates, expected_suffixes)
            record["dataset_index"] = summary
            if summary.get("parse_error"):
                record["status"] = "dataset_index_invalid_json"
            elif int(summary.get("entry_count") or 0) == 0 and int(summary.get("source_file_count") or 0) == 0:
                record["status"] = "dataset_index_empty"
            elif expected_suffixes and int(summary.get("matching_source_file_count") or 0) == 0 and not metadata_only_index:
                record["status"] = "dataset_index_no_matching_source"
            else:
                record["status"] = "dataset_index_ready"
                if not metadata_only_index:
                    record["available_source_files"] = summary.get("matching_source_files", [])
            return record
        if source_file_matches(str(path), expected_suffixes):
            record["status"] = "source_file_ready"
            record["available_source_files"] = [rel_display(path)]
            return record
        record["status"] = "file_exists"
        return record

    if path.is_dir():
        candidates, limit_reached = source_file_candidates(path, max_candidates, expected_suffixes)
        record.update(
            {
                "kind": "directory",
                "available_source_files": candidates,
                "candidate_limit_reached": limit_reached,
                "status": "dataset_root_ready" if candidates else "dataset_root_empty",
            }
        )
        return record

    record["status"] = "path_exists_unsupported_type"
    return record


def asset_finding(form_id: str, form_path: Path, asset: dict[str, Any]) -> dict[str, Any] | None:
    status = str(asset.get("status") or "")
    field = str(asset.get("label") or "")
    raw_path = str(asset.get("path") or "")
    messages = {
        "source_file_missing": ("test_gap", "ASSET_INPUT_SOURCE_FILE_MISSING", "Declared source_file is missing."),
        "dataset_index_missing": ("test_gap", "ASSET_INPUT_DATASET_INDEX_MISSING", "Declared dataset_index is missing."),
        "dataset_root_missing": ("test_gap", "ASSET_INPUT_DATASET_ROOT_MISSING", "Declared dataset_root is missing."),
        "dataset_root_empty": ("test_gap", "ASSET_INPUT_DATASET_ROOT_EMPTY", "Declared dataset_root contains no supported source files."),
        "dataset_index_empty": ("test_gap", "ASSET_INPUT_DATASET_INDEX_EMPTY", "Declared dataset_index has no visible entries or source files."),
        "dataset_index_no_matching_source": ("test_gap", "ASSET_INPUT_DATASET_INDEX_NO_MATCHING_SOURCE", "Declared dataset_index has no source files matching the target API."),
        "dataset_index_invalid_json": ("risk", "ASSET_INPUT_DATASET_INDEX_INVALID_JSON", "Declared dataset_index is invalid JSON."),
        "placeholder_root_missing": ("test_gap", "ASSET_INPUT_PATTERN_ROOT_MISSING", "Declared placeholder source_file root is missing."),
        "placeholder_unresolved": ("test_gap", "ASSET_INPUT_PATTERN_UNRESOLVED", "Declared placeholder source_file pattern has no matching local source files."),
        "path_missing": ("test_gap", "ASSET_INPUT_PATH_MISSING", "Declared input asset path is missing."),
    }
    if status not in messages:
        return None
    severity, code, message = messages[status]
    return finding(severity, code, f"{message} path={raw_path}", form_id=form_id, path=form_path, field=field)


def asset_source_path_findings(form_id: str, form_path: Path, asset: dict[str, Any]) -> list[dict[str, Any]]:
    field = str(asset.get("label") or "")
    raw_path = str(asset.get("path") or "")
    summary = as_dict(asset.get("source_path_portability"))
    findings: list[dict[str, Any]] = []
    external = as_list(summary.get("external_examples"))
    if external:
        findings.append(
            finding(
                "blocker",
                "ASSET_INPUT_SOURCE_PATH_OUTSIDE_REPO",
                f"Exposed source_file candidates must be repo-local before they are shown to the model. path={raw_path}; examples={external}",
                form_id=form_id,
                path=form_path,
                field=field,
            )
        )
    missing = as_list(summary.get("missing_examples"))
    if missing:
        findings.append(
            finding(
                "test_gap",
                "ASSET_INPUT_SOURCE_PATH_MISSING_LOCAL",
                f"Exposed source_file candidates are repo-relative but do not exist locally. path={raw_path}; examples={missing}",
                form_id=form_id,
                path=form_path,
                field=field,
            )
        )
    nonportable = as_list(summary.get("nonportable_examples"))
    if nonportable:
        findings.append(
            finding(
                "risk",
                "ASSET_INPUT_SOURCE_PATH_NOT_PORTABLE",
                f"Exposed source_file candidates should use repo-relative POSIX-style paths. path={raw_path}; examples={nonportable}",
                form_id=form_id,
                path=form_path,
                field=field,
            )
        )
    return findings


def action_for_asset(record: dict[str, Any], asset: dict[str, Any], form_path: Path) -> dict[str, Any] | None:
    status = str(asset.get("status") or "")
    action_map = {
        "source_file_missing": ("materialize_input_asset", 10),
        "dataset_index_missing": ("materialize_dataset_index", 10),
        "dataset_root_missing": ("materialize_input_asset", 10),
        "dataset_root_empty": ("populate_input_asset_root", 20),
        "dataset_index_empty": ("populate_dataset_index", 20),
        "dataset_index_no_matching_source": ("materialize_dataset_index", 10),
        "dataset_index_invalid_json": ("repair_dataset_index", 15),
        "placeholder_root_missing": ("materialize_input_asset", 10),
        "placeholder_unresolved": ("materialize_input_asset", 10),
        "path_missing": ("materialize_input_asset", 20),
    }
    if status not in action_map:
        return None
    action_state, priority = action_map[status]
    candidates = [str(item) for item in as_list(asset.get("available_source_files")) if str(item)]
    next_actions = [
        "Materialize the declared local input asset, then rerun validate_input_assets.py.",
        "Update the form input_assets path only after the asset exists in the workspace.",
        "Do not ask the model for a synthetic source or harness extension to hide missing input data.",
    ]
    if status == "dataset_index_missing":
        next_actions[0] = "Create or copy the dataset_index JSON and make sure it references existing STEP/IGES/SGT files."
    elif status in {"placeholder_root_missing", "placeholder_unresolved"}:
        next_actions[0] = "Materialize imported SGT outputs or replace the placeholder pattern with a concrete reviewed source_file."
    elif status == "dataset_index_no_matching_source":
        next_actions[0] = "Create or copy a dataset_index JSON whose files match this form's target API suffixes."
    request_id = str(record.get("request_id") or "")
    strict_abc_iges = (
        request_id == "iface_14_iges_import_abc_complex"
        and status == "dataset_index_no_matching_source"
        and str(record.get("target_api") or "") == "iges_import"
    )
    recommended_index = ""
    materialization_command = ""
    if strict_abc_iges:
        recommended_index = "artifacts/abc_fetch_smoke/iges_complex_dataset_index.json"
        materialization_command = (
            "python .\\test_harness\\tools\\materialize_input_assets.py "
            "--source-abc-root <real_abc_iges_cache_root> "
            "--max-strict-iges 48 "
            "--target-strict-iges-index .\\artifacts\\abc_fetch_smoke\\iges_complex_dataset_index.json "
            "--manifest-out .\\artifacts\\input_asset_materialization_strict_iges_audit\\manifest.json "
            "--markdown .\\artifacts\\input_asset_materialization_strict_iges_audit\\report.md"
        )
        next_actions = [
            (
                "Materialize a real ABC IGES/IGS complex dataset index with materialize_input_assets.py; "
                "the expected repo-local index is artifacts/abc_fetch_smoke/iges_complex_dataset_index.json."
            ),
            "Do not use input_assets.local_smoke_dataset_index as the strict ABC corpus; it is only a labeled local IGES fallback.",
            "After the strict index exists, update iface_14 input_assets.dataset_index to that index and rerun validate_input_assets.py.",
        ]
    action = {
        "request_id": str(record.get("request_id") or ""),
        "action_state": action_state,
        "priority": priority,
        "target_api": str(record.get("target_api") or ""),
        "form_path": rel_display(form_path),
        "field": str(asset.get("label") or ""),
        "declared_path": str(asset.get("path") or ""),
        "status": status,
        "candidate_count": len(candidates),
        "available_source_files": candidates,
        "next_actions": next_actions,
        "boundary": fixed_boundary(),
    }
    if strict_abc_iges:
        action["recommended_index_path"] = recommended_index
        action["materialization_command"] = materialization_command
        action["audit_manifest"] = "artifacts/input_asset_materialization_strict_iges_audit/manifest.json"
        action["fallback_dataset_index"] = "artifacts/iges_import_smoke/dataset_index.json"
    return action


def action_for_source_path_issue(record: dict[str, Any], asset: dict[str, Any], form_path: Path) -> dict[str, Any] | None:
    summary = as_dict(asset.get("source_path_portability"))
    status = ""
    priority = 15
    examples: list[Any] = []
    if int(summary.get("external_count") or 0) > 0:
        status = "source_path_outside_repo"
        priority = 5
        examples = as_list(summary.get("external_examples"))
    elif int(summary.get("missing_count") or 0) > 0:
        status = "source_path_missing_local"
        priority = 10
        examples = as_list(summary.get("missing_examples"))
    elif int(summary.get("nonportable_count") or 0) > 0:
        status = "source_path_not_portable"
        priority = 30
        examples = as_list(summary.get("nonportable_examples"))
    if not status:
        return None
    return {
        "request_id": str(record.get("request_id") or ""),
        "action_state": "materialize_repo_local_source_paths",
        "priority": priority,
        "target_api": str(record.get("target_api") or ""),
        "form_path": rel_display(form_path),
        "field": str(asset.get("label") or ""),
        "declared_path": str(asset.get("path") or ""),
        "status": status,
        "candidate_count": int(summary.get("checked_count") or 0),
        "available_source_files": [str(item) for item in examples if str(item)],
        "next_actions": [
            "Materialize or rewrite exposed source_file candidates to repo-relative paths under artifacts/ before building prompts.",
            "Keep external absolute corpus paths only in provenance fields such as materialized_from.",
            "Rerun validate_input_assets.py and rebuild the prompt pack before handing tasks to the model.",
        ],
        "boundary": fixed_boundary(),
    }


def record_for_form(path: Path, max_candidates: int) -> dict[str, Any]:
    loaded, findings = load_form(path)
    if loaded is None:
        return {
            "form_path": rel_display(path),
            "request_id": "",
            "target_api": "",
            "asset_state": "form_unreadable",
            "assets": [],
            "available_source_files": [],
            "findings": findings,
            "actions": [],
        }
    form_id = str(loaded.get("request_id") or path.stem)
    target_api = str(loaded.get("target_api") or "")
    expected_suffixes = source_suffixes_for_form(loaded)
    assets = [
        status_for_asset(
            label,
            raw,
            max_candidates=max_candidates,
            expected_suffixes=expected_suffixes,
            metadata_only_index=is_corpus_metadata_index(loaded, label, raw),
        )
        for label, raw in declared_assets(loaded)
    ]
    for asset in assets:
        asset["source_path_portability"] = portable_source_path_summary(
            [str(item) for item in as_list(asset.get("available_source_files")) if str(item)]
        )
        maybe = asset_finding(form_id, path, asset)
        if maybe:
            findings.append(maybe)
        findings.extend(asset_source_path_findings(form_id, path, asset))
    available_source_files: list[str] = []
    for asset in assets:
        for source_file in as_list(asset.get("available_source_files")):
            text = str(source_file or "")
            if text and text not in available_source_files:
                available_source_files.append(text)
    action_context = {"request_id": form_id, "target_api": target_api}
    actions = [action_for_asset(action_context, asset, path) for asset in assets]
    actions.extend(action_for_source_path_issue(action_context, asset, path) for asset in assets)
    actions = [action for action in actions if action]
    if not assets:
        asset_state = "no_input_assets_declared"
    elif actions:
        asset_state = "needs_input_asset"
    else:
        asset_state = "ready"
    return {
        "form_path": rel_display(path),
        "request_id": form_id,
        "target_api": target_api,
        "expected_source_suffixes": sorted(expected_suffixes),
        "asset_state": asset_state,
        "assets": assets,
        "available_source_files": available_source_files,
        "has_available_source_files": bool(available_source_files),
        "findings": findings,
        "actions": actions,
    }


def severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in findings:
        counter[str(item.get("severity") or "risk")] += 1
    return {severity: int(counter.get(severity, 0)) for severity in SEVERITY_RANK}


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    forms_root = repo_path(args.forms_root)
    manifest_path = repo_path(args.manifest)
    if args.form:
        form_paths = [repo_path(item) for item in args.form]
        manifest_findings: list[dict[str, Any]] = []
    else:
        form_paths, manifest_findings = manifest_form_paths(manifest_path, forms_root)
    records = [record_for_form(path, args.max_candidates) for path in form_paths]
    findings = list(manifest_findings)
    findings.extend(item for record in records for item in as_list(record.get("findings")) if isinstance(item, dict))
    actions = [item for record in records for item in as_list(record.get("actions")) if isinstance(item, dict)]
    actions.sort(key=lambda item: (int(item.get("priority") or 99), str(item.get("request_id") or ""), str(item.get("field") or "")))
    counts = severity_counts(findings)
    by_state: dict[str, int] = {}
    by_asset_state: dict[str, int] = {}
    for action in actions:
        state = str(action.get("action_state") or "unknown")
        by_state[state] = by_state.get(state, 0) + 1
    for record in records:
        state = str(record.get("asset_state") or "unknown")
        by_asset_state[state] = by_asset_state.get(state, 0) + 1
    return {
        "schema_version": 1,
        "generated_at": now_iso_like(),
        "ok": counts["blocker"] == 0,
        "boundary": fixed_boundary(),
        "inputs": {
            "manifest": rel_display(manifest_path),
            "forms_root": rel_display(forms_root),
            "forms": [rel_display(path) for path in form_paths],
            "max_candidates": args.max_candidates,
        },
        "record_count": len(records),
        "asset_count": sum(len(as_list(record.get("assets"))) for record in records),
        "action_count": len(actions),
        "counts": counts,
        "asset_state_counts": dict(sorted(by_asset_state.items())),
        "by_action_state": dict(sorted(by_state.items())),
        "operator_action_queue": actions,
        "records": records,
        "findings": findings,
    }


def report_ok(report: dict[str, Any], fail_on: str) -> bool:
    threshold = SEVERITY_RANK[fail_on]
    counts = as_dict(report.get("counts"))
    return all(int(counts.get(severity, 0)) == 0 for severity, rank in SEVERITY_RANK.items() if rank >= threshold)


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Input Asset Readiness",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- OK: `{report.get('ok')}`",
        f"- Forms: `{report.get('record_count')}`",
        f"- Assets: `{report.get('asset_count')}`",
        f"- Actions: `{report.get('action_count')}`",
        f"- Counts: `{report.get('counts')}`",
        f"- Asset states: `{report.get('asset_state_counts')}`",
        "",
        "## Boundary",
        "",
        "- This gate is read-only.",
        "- It checks only saved form declarations and local filesystem availability.",
        "- Missing inputs produce operator actions; they are not harness extension requests by themselves.",
        "",
        "## Forms",
        "",
        "| request | api | state | available source files | actions |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for record in as_list(report.get("records")):
        if not isinstance(record, dict):
            continue
        lines.append(
            f"| `{record.get('request_id')}` | `{record.get('target_api')}` | `{record.get('asset_state')}` | "
            f"{len(as_list(record.get('available_source_files')))} | {len(as_list(record.get('actions')))} |"
        )
    actions = as_list(report.get("operator_action_queue"))
    lines.extend(["", "## Operator Action Queue", "", "| action | request | field | status | path |", "| --- | --- | --- | --- | --- |"])
    if actions:
        for action in actions:
            if not isinstance(action, dict):
                continue
            lines.append(
                f"| `{action.get('action_state')}` | `{action.get('request_id')}` | `{action.get('field')}` | "
                f"`{action.get('status')}` | `{action.get('declared_path')}` |"
            )
    else:
        lines.append("| none |  |  |  |  |")
    findings = as_list(report.get("findings"))
    if findings:
        lines.extend(["", "## Findings", "", "| severity | code | form | field | message |", "| --- | --- | --- | --- | --- |"])
        for item in findings:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"| `{item.get('severity')}` | `{item.get('code')}` | `{item.get('form_id') or ''}` | "
                f"`{item.get('field') or ''}` | {item.get('message')} |"
            )
    else:
        lines.extend(["", "## Findings", "", "- None."])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    report = build_report(args)
    report["ok"] = report_ok(report, args.fail_on)
    if args.report:
        write_json(repo_path(args.report), report)
    if args.markdown:
        write_text(repo_path(args.markdown), markdown_report(report))
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "record_count": report.get("record_count", 0),
                "asset_count": report.get("asset_count", 0),
                "action_count": report.get("action_count", 0),
                "counts": report.get("counts", {}),
                "by_action_state": report.get("by_action_state", {}),
            },
            indent=2,
        )
    )
    return 0 if report["ok"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
