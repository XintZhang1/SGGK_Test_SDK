#!/usr/bin/env python3
"""Build GUI-ready debug geometry handoff packs from SGGK bug registries or triage."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any


INPUT_SGT_KEYS = ("source_sgt", "target_sgt", "tool_sgt")
REPORT_NAMES = (
    "validation.json",
    "debug_geometry_index.json",
    "input_topology_index.json",
    "topo_track_summary.json",
    "topo_track.json",
    "properties.json",
    "roundtrip_comparison.json",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", action="append", default=[], help="bug_registry.json or registry directory")
    parser.add_argument("--triage", action="append", default=[], help="triage_summary.json or triage directory")
    parser.add_argument("--preview-dir", action="append", default=[], help="Optional preview directory for <case_id>.png")
    parser.add_argument("--gui", default="", help="Optional SggkGui.exe path for generated open_in_gui.ps1 scripts")
    parser.add_argument("--topology-extractor", default="", help="Optional sggk_topology_extract executable for primary-contact Face/Edge/Vertex SGT export")
    parser.add_argument("--out", required=True, help="Output directory for handoff packs")
    parser.add_argument("--limit", type=int, default=0, help="Maximum packs to write; 0 means all")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"_json_error": f"{exc.msg} at line {exc.lineno}, column {exc.colno}"}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def as_str(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return value if isinstance(value, str) else ""


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_json_path(raw: str, filename: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        return path / filename
    return path


def sanitize_name(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "debug")).strip("._")
    return text or "debug"


def resolve_existing_path(raw: Any, bases: list[Path] | None = None) -> str:
    text = as_str(raw)
    if not text:
        return ""
    path = Path(text)
    candidates: list[Path] = []
    if path.is_absolute():
        candidates.append(path)
    else:
        for base in bases or []:
            candidates.append((base / path).resolve())
        candidates.append((Path.cwd() / path).resolve())
        candidates.append(path.resolve())
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(candidates[0] if candidates else path)


def copy_file(src: Any, dst: Path) -> str:
    source_text = as_str(src)
    if not source_text:
        return ""
    source = Path(source_text)
    if not source.is_file():
        return ""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dst)
    return str(dst)


def copy_tree(src: Any, dst: Path) -> str:
    source_text = as_str(src)
    if not source_text:
        return ""
    source = Path(source_text)
    if not source.is_dir():
        return ""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(source, dst)
    return str(dst)


def unique_by_source(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        source = as_str(item.get("source_path"))
        if not source or source in seen:
            continue
        seen.add(source)
        result.append(item)
    return result


def unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = as_str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def unique_report_assets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        name = as_str(item.get("name"))
        source = as_str(item.get("source_path"))
        key = (name, source)
        if not source or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def debug_assets_from_index(index_path: str, case_dir: str) -> list[dict[str, Any]]:
    data = load_json(Path(index_path))
    if not isinstance(data, dict):
        return []
    case_base = Path(case_dir) if case_dir else Path(index_path).parent.parent
    assets: list[dict[str, Any]] = []
    for asset in as_list(data.get("assets")):
        if not isinstance(asset, dict):
            continue
        raw_path = as_str(asset.get("path"))
        source_path = resolve_existing_path(raw_path, [case_base])
        if not source_path or not Path(source_path).is_file():
            continue
        assets.append(
            {
                "kind": "debug",
                "check_id": as_str(asset.get("check_id")),
                "label": as_str(asset.get("label")),
                "source_path": source_path,
                "relative_path": raw_path,
                "topology": asset.get("topology") if isinstance(asset.get("topology"), dict) else {},
                "locator": asset.get("locator") if isinstance(asset.get("locator"), dict) else {},
            }
        )
    return assets


def debug_assets_from_details(details: list[Any], case_dir: str) -> list[dict[str, Any]]:
    case_base = Path(case_dir) if case_dir else Path.cwd()
    assets: list[dict[str, Any]] = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        for asset in as_list(detail.get("debug_geometry")):
            if not isinstance(asset, dict):
                continue
            raw_path = as_str(asset.get("path"))
            source_path = resolve_existing_path(raw_path, [case_base])
            if not source_path or not Path(source_path).is_file():
                continue
            assets.append(
                {
                    "kind": "debug",
                    "check_id": as_str(asset.get("check_id") or detail.get("id")),
                    "label": as_str(asset.get("label")),
                    "source_path": source_path,
                    "relative_path": raw_path,
                    "topology": asset.get("topology") if isinstance(asset.get("topology"), dict) else {},
                    "locator": asset.get("locator") if isinstance(asset.get("locator"), dict) else {},
                }
            )
    return assets


def input_assets(paths: dict[str, Any]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for key in INPUT_SGT_KEYS:
        source_path = resolve_existing_path(paths.get(key))
        if source_path and Path(source_path).is_file():
            assets.append({"kind": "input", "label": key, "source_path": source_path})
    return assets


def report_assets(paths: dict[str, Any], case_dir: str) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    case_report = Path(case_dir) / "report" if case_dir else None
    for name in REPORT_NAMES:
        source_path = ""
        if name == "debug_geometry_index.json":
            source_path = resolve_existing_path(paths.get("debug_geometry_index"))
        if not source_path and case_report is not None:
            source_path = resolve_existing_path(case_report / name)
        if source_path and Path(source_path).is_file():
            assets.append({"name": name, "source_path": source_path})
    return assets


def find_preview(case_id: str, paths: dict[str, Any], preview_dirs: list[str]) -> str:
    preview = resolve_existing_path(paths.get("preview"))
    if preview and Path(preview).is_file():
        return preview
    case_ids = unique_strings([case_id] + as_list(paths.get("preview_case_ids")))
    for raw_dir in preview_dirs:
        preview_dir = Path(raw_dir)
        for candidate_case in case_ids:
            safe_case = sanitize_name(candidate_case)
            candidates = [
                preview_dir / f"{candidate_case}.png",
                preview_dir / f"{safe_case}.png",
                preview_dir / safe_case / "preview.png",
            ]
            for candidate in candidates:
                if candidate.is_file():
                    return str(candidate)
    return ""


def record_from_registry_bug(bug: dict[str, Any], source_path: Path) -> dict[str, Any]:
    paths = as_dict(bug.get("paths"))
    case_dir = resolve_existing_path(paths.get("representative_case_dir"))
    debug_index = resolve_existing_path(paths.get("debug_geometry_index"))
    details = as_list(bug.get("validation_oracle_details")) + as_list(bug.get("roundtrip_oracle_details"))
    debug_assets = debug_assets_from_index(debug_index, case_dir) if debug_index else []
    debug_assets.extend(debug_assets_from_details(details, case_dir))
    return {
        "source_kind": "registry",
        "source_path": str(source_path),
        "fingerprint": as_str(bug.get("fingerprint")),
        "bug_id": as_str(bug.get("bug_id")),
        "case_id": as_str(bug.get("representative_case_id")),
        "api": as_str(bug.get("api")),
        "reasons": as_list(bug.get("reasons")),
        "validation_failures": as_list(bug.get("validation_failures")),
        "roundtrip_failures": as_list(bug.get("roundtrip_failures")),
        "dsl": as_dict(bug.get("dsl")),
        "primary_contact": as_dict(bug.get("primary_contact")),
        "primary_contact_label": as_str(bug.get("primary_contact_label")),
        "case_dir": case_dir,
        "paths": paths,
        "debug_assets": unique_by_source(debug_assets),
        "input_assets": input_assets(paths),
        "report_assets": report_assets(paths, case_dir),
    }


def collect_registry_records(raw_paths: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in raw_paths:
        path = normalize_json_path(raw, "bug_registry.json")
        registry = load_json(path)
        if not isinstance(registry, dict):
            continue
        for bug in as_list(registry.get("bugs")):
            if isinstance(bug, dict):
                records.append(record_from_registry_bug(bug, path))
    return records


def first_failure_by_case(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for failure in as_list(summary.get("failures")):
        if isinstance(failure, dict) and as_str(failure.get("case_id")):
            result.setdefault(as_str(failure["case_id"]), failure)
    return result


def record_from_triage_group(group: dict[str, Any], failure: dict[str, Any], source_path: Path) -> dict[str, Any]:
    case_dir = resolve_existing_path(group.get("representative_case_dir") or failure.get("case_dir"))
    recipe_paths = as_list(group.get("recipe_paths"))
    paths = {
        "triage_summary": str(source_path),
        "representative_case_dir": case_dir,
        "original_recipe": resolve_existing_path(recipe_paths[0]) if recipe_paths else resolve_existing_path(failure.get("recipe_path")),
        "debug_geometry": str(Path(case_dir) / "debug_geometry") if case_dir else "",
        "debug_geometry_index": str(Path(case_dir) / "report" / "debug_geometry_index.json") if case_dir else "",
        "target_sgt": str(Path(case_dir) / "input" / "target.sgt") if case_dir else "",
        "tool_sgt": str(Path(case_dir) / "input" / "tool.sgt") if case_dir else "",
        "source_sgt": str(Path(case_dir) / "input" / "source.sgt") if case_dir else "",
    }
    details = as_list(group.get("representative_validation_oracle_details")) + as_list(group.get("representative_roundtrip_oracle_details"))
    debug_index = resolve_existing_path(paths.get("debug_geometry_index"))
    debug_assets = debug_assets_from_index(debug_index, case_dir) if debug_index else []
    debug_assets.extend(debug_assets_from_details(details, case_dir))
    return {
        "source_kind": "triage",
        "source_path": str(source_path),
        "fingerprint": as_str(group.get("fingerprint")),
        "bug_id": "",
        "case_id": as_str(group.get("representative_case_id") or failure.get("case_id")),
        "api": (as_list(group.get("apis")) or [as_str(failure.get("api"))])[0],
        "reasons": as_list(group.get("reasons")),
        "validation_failures": as_list(group.get("representative_validation_failures")),
        "roundtrip_failures": as_list(group.get("representative_roundtrip_failures")),
        "dsl": as_dict(group.get("representative_dsl") or failure.get("dsl")),
        "primary_contact": (as_list(group.get("representative_input_contact_candidates")) or [{}])[0],
        "primary_contact_label": "",
        "case_dir": case_dir,
        "paths": paths,
        "debug_assets": unique_by_source(debug_assets),
        "input_assets": input_assets(paths),
        "report_assets": report_assets(paths, case_dir),
    }


def collect_triage_records(raw_paths: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in raw_paths:
        path = normalize_json_path(raw, "triage_summary.json")
        summary = load_json(path)
        if not isinstance(summary, dict):
            continue
        failures = first_failure_by_case(summary)
        for group in as_list(summary.get("failure_groups")):
            if not isinstance(group, dict):
                continue
            case_id = as_str(group.get("representative_case_id"))
            records.append(record_from_triage_group(group, failures.get(case_id, {}), path))
    return records


LOCALIZATION_PATH_KEYS = {
    "triage_summary",
    "representative_case_dir",
    "debug_geometry",
    "debug_geometry_index",
    "target_sgt",
    "tool_sgt",
    "source_sgt",
    "original_recipe",
}


def canonical_path_key(raw: Any) -> str:
    text = as_str(raw)
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except OSError:
        return text


def record_recipe_keys(record: dict[str, Any]) -> list[str]:
    paths = as_dict(record.get("paths"))
    return unique_strings([canonical_path_key(paths.get("replay_recipe")), canonical_path_key(paths.get("original_recipe"))])


def merge_paths(identity_record: dict[str, Any], localization_record: dict[str, Any]) -> dict[str, Any]:
    paths = dict(as_dict(identity_record.get("paths")))
    localization_paths = as_dict(localization_record.get("paths"))
    for key, value in localization_paths.items():
        if key in LOCALIZATION_PATH_KEYS and as_str(value):
            paths[key] = value
        elif key not in paths or not as_str(paths.get(key)):
            paths[key] = value
    preview_ids = unique_strings(
        as_list(paths.get("preview_case_ids"))
        + [identity_record.get("case_id"), localization_record.get("case_id")]
    )
    if preview_ids:
        paths["preview_case_ids"] = preview_ids
    return paths


def merge_handoff_records(identity_record: dict[str, Any], localization_record: dict[str, Any]) -> dict[str, Any]:
    paths = merge_paths(identity_record, localization_record)
    case_dir = as_str(localization_record.get("case_dir")) or as_str(identity_record.get("case_dir"))
    primary_contact = as_dict(localization_record.get("primary_contact")) or as_dict(identity_record.get("primary_contact"))
    merged = {
        **identity_record,
        "source_kind": "registry+triage",
        "source_paths": unique_strings([identity_record.get("source_path"), localization_record.get("source_path")]),
        "case_dir": case_dir,
        "paths": paths,
        "reasons": unique_strings(as_list(identity_record.get("reasons")) + as_list(localization_record.get("reasons"))),
        "api": as_str(identity_record.get("api")) or as_str(localization_record.get("api")),
        "primary_contact": primary_contact,
        "primary_contact_label": as_str(identity_record.get("primary_contact_label")),
        "case_id_aliases": unique_strings([identity_record.get("case_id"), localization_record.get("case_id")]),
        "debug_assets": unique_by_source(
            as_list(identity_record.get("debug_assets")) + as_list(localization_record.get("debug_assets"))
        ),
        "input_assets": input_assets(paths),
        "report_assets": unique_report_assets(
            report_assets(paths, case_dir)
            + as_list(identity_record.get("report_assets"))
            + as_list(localization_record.get("report_assets"))
        ),
    }
    if not as_list(merged.get("validation_failures")):
        merged["validation_failures"] = as_list(localization_record.get("validation_failures"))
    if not as_list(merged.get("roundtrip_failures")):
        merged["roundtrip_failures"] = as_list(localization_record.get("roundtrip_failures"))
    if not as_dict(merged.get("dsl")):
        merged["dsl"] = as_dict(localization_record.get("dsl"))
    return merged


def merge_registry_and_triage_records(
    registry_records: list[dict[str, Any]], triage_records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not registry_records or not triage_records:
        return registry_records + triage_records

    triage_by_recipe: dict[str, dict[str, Any]] = {}
    for record in triage_records:
        for key in record_recipe_keys(record):
            triage_by_recipe.setdefault(key, record)

    merged: list[dict[str, Any]] = []
    used: set[int] = set()
    for registry_record in registry_records:
        match = None
        for key in record_recipe_keys(registry_record):
            match = triage_by_recipe.get(key)
            if match is not None:
                break
        if match is None:
            merged.append(registry_record)
            continue
        merged.append(merge_handoff_records(registry_record, match))
        used.add(id(match))

    for triage_record in triage_records:
        if id(triage_record) not in used:
            merged.append(triage_record)
    return merged


def powershell_quote(path: str) -> str:
    return path.replace("'", "''")


def detect_gui(explicit: str) -> str:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / "SGK1.4.10" / "SGGK" / "x64-win" / "bin" / "SggkGui.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return explicit


def detect_topology_extractor(explicit: str) -> str:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    names = ["sggk_topology_extract.exe", "sggk_topology_extract"]
    for config in ("Release", "RelWithDebInfo", "Debug"):
        for name in names:
            candidates.append(Path.cwd() / "build" / "test_harness" / config / name)
    for name in names:
        candidates.append(Path.cwd() / name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return explicit


def locator_summary(locator: Any) -> str:
    locator_dict = as_dict(locator)
    if not locator_dict:
        return ""
    if "point" in locator_dict:
        return f"point={locator_dict.get('point')}"
    if "start_point" in locator_dict or "end_point" in locator_dict:
        return f"start={locator_dict.get('start_point')} end={locator_dict.get('end_point')} length={locator_dict.get('length')}"
    if "area" in locator_dict:
        return f"area={locator_dict.get('area')} sense={locator_dict.get('sense')}"
    bbox = as_dict(locator_dict.get("bbox"))
    if bbox:
        return f"bbox={bbox.get('min')}..{bbox.get('max')}"
    return json.dumps(locator_dict, ensure_ascii=False)


def contact_summary(contact: Any) -> str:
    contact_dict = as_dict(contact)
    if not contact_dict:
        return ""
    target = as_dict(contact_dict.get("target"))
    tool = as_dict(contact_dict.get("tool"))
    left = f"{target.get('role', '')} {target.get('type', '')}#{target.get('id')}[{target.get('local_index')}]"
    right = f"{tool.get('role', '')} {tool.get('type', '')}#{tool.get('id')}[{tool.get('local_index')}]"
    return f"{left} <-> {right} gap={contact_dict.get('bbox_distance')} overlaps={contact_dict.get('axis_overlaps')}"


def copy_pack_files(record: dict[str, Any], pack_dir: Path, preview_dirs: list[str]) -> dict[str, Any]:
    copied_debug_dir = copy_tree(as_dict(record.get("paths")).get("debug_geometry"), pack_dir / "debug_geometry")
    copied: dict[str, Any] = {
        "debug_geometry_dir": copied_debug_dir,
        "debug_sgts": [],
        "input_sgts": [],
        "reports": {},
        "recipes": {},
        "preview": "",
    }

    copied_sources: set[str] = set()
    for asset in as_list(record.get("debug_assets")):
        if not isinstance(asset, dict):
            continue
        source_path = as_str(asset.get("source_path"))
        if not source_path or source_path in copied_sources:
            continue
        copied_sources.add(source_path)
        source = Path(source_path)
        if copied_debug_dir and source.parent == Path(copied_debug_dir):
            copied_path = str(source)
        else:
            rel = as_str(asset.get("relative_path"))
            name = Path(rel).name if rel else source.name
            copied_path = copy_file(source_path, pack_dir / "debug_geometry" / name)
        if not copied_path:
            continue
        copied["debug_sgts"].append(
            {
                **asset,
                "copied_path": copied_path,
            }
        )

    for asset in as_list(record.get("input_assets")):
        if not isinstance(asset, dict):
            continue
        label = sanitize_name(asset.get("label"))
        source = as_str(asset.get("source_path"))
        copied_path = copy_file(source, pack_dir / "input" / f"{label}_{Path(source).name}")
        if copied_path:
            copied["input_sgts"].append({**asset, "copied_path": copied_path})

    for asset in as_list(record.get("report_assets")):
        if not isinstance(asset, dict):
            continue
        name = as_str(asset.get("name"))
        copied_path = copy_file(asset.get("source_path"), pack_dir / "report" / name)
        if copied_path:
            copied["reports"][name] = copied_path

    paths = as_dict(record.get("paths"))
    for key, filename in (("replay_recipe", "replay_recipe.json"), ("original_recipe", "original_recipe.json")):
        copied_path = copy_file(resolve_existing_path(paths.get(key)), pack_dir / "recipes" / filename)
        if copied_path:
            copied["recipes"][key] = copied_path

    preview = find_preview(as_str(record.get("case_id")), paths, preview_dirs)
    if preview:
        copied["preview"] = copy_file(preview, pack_dir / "preview.png")

    return copied


def topology_source_for_role(record: dict[str, Any], role: str) -> str:
    paths = as_dict(record.get("paths"))
    candidates = []
    if role == "target":
        candidates.append(paths.get("target_sgt"))
    elif role == "tool":
        candidates.append(paths.get("tool_sgt"))
    elif role == "source":
        candidates.append(paths.get("source_sgt"))
    for candidate in candidates:
        resolved = resolve_existing_path(candidate)
        if resolved and Path(resolved).is_file():
            return resolved
    return ""


def extract_focus_topologies(record: dict[str, Any], pack_dir: Path, extractor: str) -> list[dict[str, Any]]:
    contact = as_dict(record.get("primary_contact"))
    if not contact:
        return []
    focus_dir = pack_dir / "focus"
    results: list[dict[str, Any]] = []
    extractor_available = bool(extractor and Path(extractor).is_file())
    for side in ("target", "tool"):
        topo = as_dict(contact.get(side))
        role = as_str(topo.get("role")) or side
        topo_type = as_str(topo.get("type"))
        topo_id = topo.get("id")
        local_index = topo.get("local_index")
        side_short = {"target": "tgt", "tool": "tool"}.get(side, side[:3] or "x")
        label = sanitize_name(f"{side_short}_{topo_type}_{topo_id}_{local_index}")
        expected_path = focus_dir / f"{label}.sgt"
        item = {
            "kind": "focus",
            "side": side,
            "role": role,
            "label": label,
            "source_path": "",
            "type": topo_type,
            "id": topo_id,
            "local_index": local_index,
            "command": [],
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "status": "",
            "copied_path": "",
        }
        if not topo_type:
            item["status"] = "missing_topology_type"
            results.append(item)
            continue
        source_sgt = topology_source_for_role(record, role)
        item["source_path"] = source_sgt
        if not source_sgt:
            item["status"] = "missing_source_sgt"
            results.append(item)
            continue
        if not extractor_available:
            item["status"] = "topology_extractor_unavailable"
            results.append(item)
            continue
        cmd = [
            extractor,
            "--source",
            source_sgt,
            "--out",
            str(focus_dir),
            "--type",
            topo_type,
            "--label",
            label,
        ]
        if isinstance(topo_id, int):
            cmd.extend(["--id", str(topo_id)])
        if isinstance(local_index, int):
            cmd.extend(["--local-index", str(local_index)])
        completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        item["command"] = cmd
        item["returncode"] = completed.returncode
        item["stdout"] = completed.stdout
        item["stderr"] = completed.stderr
        item["copied_path"] = str(expected_path) if expected_path.is_file() else ""
        item["status"] = "ok" if item["copied_path"] else "extract_failed"
        results.append(item)
    return results


def focus_status(item: dict[str, Any]) -> str:
    if as_str(item.get("copied_path")):
        return "ok"
    status = as_str(item.get("status"))
    if status:
        return status
    return f"extract_returncode={item.get('returncode')}"


def focus_topology_text(item: dict[str, Any]) -> str:
    topo_type = as_str(item.get("type")) or "unknown"
    return f"{topo_type}#{item.get('id')}[{item.get('local_index')}]"


def topology_text(topology: Any) -> str:
    topology_dict = as_dict(topology)
    if not topology_dict:
        return ""
    topo_type = as_str(topology_dict.get("type")) or "unknown"
    topo_id = topology_dict.get("id")
    return f"{topo_type}#{topo_id}"


def write_focus_index(record: dict[str, Any], copied: dict[str, Any], pack_dir: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for item in as_list(copied.get("focus_sgts")):
        if not isinstance(item, dict):
            continue
        copied_path = as_str(item.get("copied_path"))
        entries.append(
            {
                "side": item.get("side"),
                "role": item.get("role"),
                "label": item.get("label"),
                "topology": {
                    "type": item.get("type"),
                    "id": item.get("id"),
                    "local_index": item.get("local_index"),
                },
                "source_sgt": item.get("source_path"),
                "focus_sgt": copied_path,
                "gui_open_path": copied_path,
                "status": focus_status(item),
                "returncode": item.get("returncode"),
                "stderr": item.get("stderr"),
                "command": item.get("command"),
            }
        )
    index = {
        "generated_at": now_iso_like(),
        "fingerprint": record.get("fingerprint"),
        "bug_id": record.get("bug_id"),
        "case_id": record.get("case_id"),
        "api": record.get("api"),
        "primary_contact_label": as_str(record.get("primary_contact_label")) or contact_summary(record.get("primary_contact")),
        "primary_contact": record.get("primary_contact"),
        "topology_extractor": copied.get("topology_extractor"),
        "sgt_paths": str(pack_dir / "sgt_paths.txt"),
        "entries": entries,
    }
    json_path = pack_dir / "focus_index.json"
    md_path = pack_dir / "focus_index.md"
    write_json(json_path, index)

    lines = [
        "# Focus Topology Index",
        "",
        f"- Case: `{record.get('case_id')}`",
        f"- Fingerprint: `{record.get('fingerprint')}`",
        f"- API: `{record.get('api', '')}`",
        f"- Primary/contact candidate: {index['primary_contact_label'] or '`unavailable`'}",
        f"- Topology extractor: `{copied.get('topology_extractor') or ''}`",
        f"- SGT path list: `{index['sgt_paths']}`",
        "",
    ]
    if entries:
        lines.extend(
            [
                "| side | role | topology | status | focus SGT | source SGT |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for entry in entries:
            topology = as_dict(entry.get("topology"))
            topology_text = f"{topology.get('type')}#{topology.get('id')}[{topology.get('local_index')}]"
            lines.append(
                "| `{side}` | `{role}` | `{topology}` | `{status}` | `{focus}` | `{source}` |".format(
                    side=entry.get("side", ""),
                    role=entry.get("role", ""),
                    topology=topology_text,
                    status=entry.get("status", ""),
                    focus=entry.get("focus_sgt", ""),
                    source=entry.get("source_sgt", ""),
                )
            )
        lines.append("")
    else:
        lines.extend(["No primary-contact topology entries were available.", ""])
    write_text(md_path, "\n".join(lines))
    return {
        "json": str(json_path),
        "markdown": str(md_path),
        "entry_count": len(entries),
        "extracted_count": sum(1 for entry in entries if as_str(entry.get("focus_sgt"))),
        "diagnostic_count": sum(1 for entry in entries if not as_str(entry.get("focus_sgt"))),
    }


def visual_entry_debug(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "debug_geometry",
        "label": item.get("label"),
        "check_id": item.get("check_id"),
        "topology": item.get("topology") if isinstance(item.get("topology"), dict) else {},
        "locator": item.get("locator") if isinstance(item.get("locator"), dict) else {},
        "source_path": item.get("source_path"),
        "copied_path": item.get("copied_path"),
        "gui_open_path": item.get("copied_path"),
        "status": "ok" if as_str(item.get("copied_path")) else "missing",
    }


def visual_entry_focus(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "focus_topology",
        "side": item.get("side"),
        "role": item.get("role"),
        "label": item.get("label"),
        "topology": {
            "type": item.get("type"),
            "id": item.get("id"),
            "local_index": item.get("local_index"),
        },
        "source_path": item.get("source_path"),
        "copied_path": item.get("copied_path"),
        "gui_open_path": item.get("copied_path"),
        "status": focus_status(item),
    }


def visual_entry_input(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "input_sgt",
        "label": item.get("label"),
        "source_path": item.get("source_path"),
        "copied_path": item.get("copied_path"),
        "gui_open_path": item.get("copied_path"),
        "status": "ok" if as_str(item.get("copied_path")) else "missing",
    }


def visual_entry_label(entry: dict[str, Any]) -> str:
    label = as_str(entry.get("label"))
    if label:
        return label
    if as_str(entry.get("kind")) == "focus_topology":
        topology = as_dict(entry.get("topology"))
        return f"{entry.get('side', '')}_{topology.get('type', '')}_{topology.get('id', '')}"
    return as_str(entry.get("kind")) or "sgt"


def write_visual_index(record: dict[str, Any], copied: dict[str, Any], pack_dir: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for item in as_list(copied.get("debug_sgts")):
        if isinstance(item, dict):
            entries.append(visual_entry_debug(item))
    for item in as_list(copied.get("focus_sgts")):
        if isinstance(item, dict):
            entries.append(visual_entry_focus(item))
    for item in as_list(copied.get("input_sgts")):
        if isinstance(item, dict):
            entries.append(visual_entry_input(item))

    index = {
        "generated_at": now_iso_like(),
        "fingerprint": record.get("fingerprint"),
        "bug_id": record.get("bug_id"),
        "case_id": record.get("case_id"),
        "api": record.get("api"),
        "sgt_paths": str(pack_dir / "sgt_paths.txt"),
        "entry_count": len(entries),
        "debug_geometry_count": sum(1 for entry in entries if entry.get("kind") == "debug_geometry"),
        "focus_topology_count": sum(1 for entry in entries if entry.get("kind") == "focus_topology" and as_str(entry.get("copied_path"))),
        "input_sgt_count": sum(1 for entry in entries if entry.get("kind") == "input_sgt"),
        "entries": entries,
    }
    json_path = pack_dir / "visual_index.json"
    md_path = pack_dir / "visual_index.md"
    write_json(json_path, index)

    lines = [
        "# Visual SGT Index",
        "",
        f"- Case: `{record.get('case_id')}`",
        f"- Fingerprint: `{record.get('fingerprint')}`",
        f"- API: `{record.get('api', '')}`",
        f"- SGT path list: `{index['sgt_paths']}`",
        "",
        "| kind | label | check/side | topology | status | copied path | source path |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        topology = as_dict(entry.get("topology"))
        if entry.get("kind") == "focus_topology":
            topology_value = f"{topology.get('type')}#{topology.get('id')}[{topology.get('local_index')}]"
            check_value = as_str(entry.get("side"))
        else:
            topology_value = topology_text(topology)
            check_value = as_str(entry.get("check_id"))
        lines.append(
            "| `{kind}` | `{label}` | `{check}` | `{topology}` | `{status}` | `{copied}` | `{source}` |".format(
                kind=entry.get("kind", ""),
                label=visual_entry_label(entry),
                check=check_value,
                topology=topology_value,
                status=entry.get("status", ""),
                copied=entry.get("copied_path", ""),
                source=entry.get("source_path", ""),
            )
        )
    lines.append("")
    write_text(md_path, "\n".join(lines))
    return {
        "json": str(json_path),
        "markdown": str(md_path),
        "entry_count": len(entries),
        "debug_geometry_count": index["debug_geometry_count"],
        "focus_topology_count": index["focus_topology_count"],
        "input_sgt_count": index["input_sgt_count"],
    }


def write_pack_scripts(pack_dir: Path, copied: dict[str, Any], gui_path: str) -> None:
    sgt_paths = [as_str(item.get("copied_path")) for item in as_list(copied.get("debug_sgts"))]
    sgt_paths.extend(as_str(item.get("copied_path")) for item in as_list(copied.get("focus_sgts")))
    sgt_paths.extend(as_str(item.get("copied_path")) for item in as_list(copied.get("input_sgts")))
    sgt_paths = [path for path in sgt_paths if path]
    write_text(pack_dir / "sgt_paths.txt", "\n".join(sgt_paths) + ("\n" if sgt_paths else ""))
    write_text(
        pack_dir / "open_folder.ps1",
        "\n".join(
            [
                "$Here = Split-Path -Parent $MyInvocation.MyCommand.Path",
                "Invoke-Item -LiteralPath $Here",
                "",
            ]
        ),
    )
    if gui_path:
        lines = [
            "param(",
            f"  [string]$Gui = '{powershell_quote(gui_path)}'",
            ")",
            "$Here = Split-Path -Parent $MyInvocation.MyCommand.Path",
            "$List = Join-Path $Here 'sgt_paths.txt'",
            "if (!(Test-Path -LiteralPath $Gui)) {",
            "  Write-Host \"GUI not found, opening handoff folder instead: $Gui\"",
            "  Invoke-Item -LiteralPath $Here",
            "  exit 0",
            "}",
            "Get-Content -LiteralPath $List | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | ForEach-Object {",
            "  Start-Process -FilePath $Gui -ArgumentList @($_)",
            "}",
            "",
        ]
        write_text(pack_dir / "open_in_gui.ps1", "\n".join(lines))


def write_pack_readme(record: dict[str, Any], copied: dict[str, Any], path: Path) -> None:
    dsl = as_dict(record.get("dsl"))
    contact = as_str(record.get("primary_contact_label")) or contact_summary(record.get("primary_contact"))
    lines = [
        f"# {record.get('case_id') or record.get('fingerprint')}",
        "",
        f"- Fingerprint: `{record.get('fingerprint')}`",
        f"- Bug ID: `{record.get('bug_id', '')}`",
        f"- API: `{record.get('api', '')}`",
        f"- Reasons: `{', '.join(str(item) for item in as_list(record.get('reasons')))}`",
        f"- Source: `{record.get('source_kind')}` `{record.get('source_path')}`",
    ]
    if dsl:
        lines.append(f"- Source task: `{dsl.get('source_task_id', '')}` source_ref=`{dsl.get('source_ref', '')}` risk=`{dsl.get('source_risk_id', '')}`")
    if contact:
        lines.append(f"- Primary/contact candidate: {contact}")
    lines.append("")

    failures = as_list(record.get("validation_failures")) + as_list(record.get("roundtrip_failures"))
    if failures:
        lines.extend(["## Failures", ""])
        for failure in failures[:12]:
            lines.append(f"- {failure}")
        lines.append("")

    lines.extend(["## Debug SGTs", ""])
    debug_sgts = as_list(copied.get("debug_sgts"))
    if debug_sgts:
        lines.append("| label | check | topology | copied path | locator |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in debug_sgts:
            topology = as_dict(item.get("topology"))
            topology_text = ""
            if topology:
                topology_text = f"{topology.get('type')}#{topology.get('id')}"
            lines.append(
                "| `{label}` | `{check}` | `{topology}` | `{path}` | {locator} |".format(
                    label=item.get("label", ""),
                    check=item.get("check_id", ""),
                    topology=topology_text,
                    path=item.get("copied_path", ""),
                    locator=locator_summary(item.get("locator")),
                )
            )
    else:
        lines.append("No debug SGTs were found for this record.")
    lines.append("")

    visual_index = as_dict(copied.get("visual_index"))
    if visual_index:
        lines.extend(
            [
                "## Visual Index",
                "",
                f"- `visual_index.json`: `{visual_index.get('json', '')}`",
                f"- `visual_index.md`: `{visual_index.get('markdown', '')}`",
                "",
            ]
        )

    focus_sgts = as_list(copied.get("focus_sgts"))
    if focus_sgts:
        lines.extend(["## Focus Topology SGTs", "", "| side | role | topology | copied path | status |", "| --- | --- | --- | --- | --- |"])
        for item in focus_sgts:
            lines.append(
                "| `{side}` | `{role}` | `{topology}` | `{path}` | `{status}` |".format(
                    side=item.get("side", ""),
                    role=item.get("role", ""),
                    topology=focus_topology_text(item),
                    path=item.get("copied_path", ""),
                    status=focus_status(item),
                )
            )
        lines.append("")
        focus_index = as_dict(copied.get("focus_index"))
        if focus_index:
            lines.extend(
                [
                    f"- `focus_index.json`: `{focus_index.get('json', '')}`",
                    f"- `focus_index.md`: `{focus_index.get('markdown', '')}`",
                    "",
                ]
            )
    else:
        lines.extend(["## Focus Topology SGTs", "", "No primary-contact topology SGTs were extracted.", ""])

    input_sgts = as_list(copied.get("input_sgts"))
    if input_sgts:
        lines.extend(["## Input SGTs", "", "| label | copied path | source path |", "| --- | --- | --- |"])
        for item in input_sgts:
            lines.append(f"| `{item.get('label', '')}` | `{item.get('copied_path', '')}` | `{item.get('source_path', '')}` |")
        lines.append("")

    if copied.get("preview"):
        lines.extend(["## Preview", "", f"- `preview.png`: `{copied.get('preview')}`", ""])

    reports = as_dict(copied.get("reports"))
    if reports:
        lines.extend(["## Reports", ""])
        for name, report_path in sorted(reports.items()):
            lines.append(f"- `{name}`: `{report_path}`")
        lines.append("")

    recipes = as_dict(copied.get("recipes"))
    if recipes:
        lines.extend(["## Recipes", ""])
        for name, recipe_path in sorted(recipes.items()):
            lines.append(f"- `{name}`: `{recipe_path}`")
        lines.append("")

    lines.extend(
        [
            "## Open",
            "",
            "- `sgt_paths.txt` lists copied SGT files for manual GUI opening.",
            "- Run `open_folder.ps1` to open this handoff folder.",
            "- If `open_in_gui.ps1` exists, it attempts to launch the configured GUI once per SGT path.",
            "",
        ]
    )
    write_text(path, "\n".join(lines))


def build_pack(record: dict[str, Any], out_root: Path, preview_dirs: list[str], gui_path: str, topology_extractor: str) -> dict[str, Any]:
    fingerprint = sanitize_name(record.get("fingerprint"))
    case_id = sanitize_name(record.get("case_id"))
    pack_dir = out_root / f"{fingerprint}_{case_id}"
    pack_dir.mkdir(parents=True, exist_ok=True)
    copied = copy_pack_files(record, pack_dir, preview_dirs)
    copied["focus_sgts"] = extract_focus_topologies(record, pack_dir, topology_extractor)
    copied["topology_extractor"] = topology_extractor
    write_pack_scripts(pack_dir, copied, gui_path)
    copied["focus_index"] = write_focus_index(record, copied, pack_dir)
    copied["visual_index"] = write_visual_index(record, copied, pack_dir)
    manifest = {
        "fingerprint": record.get("fingerprint"),
        "bug_id": record.get("bug_id"),
        "case_id": record.get("case_id"),
        "api": record.get("api"),
        "source_kind": record.get("source_kind"),
        "source_path": record.get("source_path"),
        "source_paths": record.get("source_paths"),
        "case_dir": record.get("case_dir"),
        "case_id_aliases": record.get("case_id_aliases"),
        "reasons": record.get("reasons"),
        "validation_failures": record.get("validation_failures"),
        "roundtrip_failures": record.get("roundtrip_failures"),
        "dsl": record.get("dsl"),
        "primary_contact": record.get("primary_contact"),
        "copied": copied,
    }
    write_json(pack_dir / "manifest.json", manifest)
    write_pack_readme(record, copied, pack_dir / "README.md")
    return {
        "fingerprint": record.get("fingerprint"),
        "bug_id": record.get("bug_id"),
        "case_id": record.get("case_id"),
        "api": record.get("api"),
        "pack_dir": str(pack_dir),
        "debug_sgt_count": len(as_list(copied.get("debug_sgts"))),
        "focus_sgt_count": sum(1 for item in as_list(copied.get("focus_sgts")) if as_str(item.get("copied_path"))),
        "input_sgt_count": len(as_list(copied.get("input_sgts"))),
        "has_preview": bool(copied.get("preview")),
        "readme": str(pack_dir / "README.md"),
        "manifest": str(pack_dir / "manifest.json"),
        "focus_index": as_dict(copied.get("focus_index")).get("markdown", ""),
        "visual_index": as_dict(copied.get("visual_index")).get("markdown", ""),
    }


def write_index_report(index: dict[str, Any], path: Path) -> None:
    lines = [
        "# SGGK Debug Handoff",
        "",
        f"- Generated: `{index.get('generated_at')}`",
        f"- Packs: `{index.get('pack_count')}`",
        f"- Debug SGTs: `{index.get('debug_sgt_count')}`",
        f"- Focus topology SGTs: `{index.get('focus_sgt_count')}`",
        f"- Input SGTs: `{index.get('input_sgt_count')}`",
        f"- APIs: `{index.get('by_api')}`",
        "",
        "| fingerprint | case | API | debug SGTs | focus SGTs | input SGTs | visual index | focus index | pack |",
        "| --- | --- | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for pack in as_list(index.get("packs")):
        if not isinstance(pack, dict):
            continue
        lines.append(
            f"| `{pack.get('fingerprint')}` | `{pack.get('case_id')}` | `{pack.get('api')}` | "
            f"{pack.get('debug_sgt_count')} | {pack.get('focus_sgt_count')} | {pack.get('input_sgt_count')} | "
            f"`{pack.get('visual_index', '')}` | `{pack.get('focus_index', '')}` | `{pack.get('pack_dir')}` |"
        )
    lines.append("")
    write_text(path, "\n".join(lines))


def main() -> int:
    args = parse_args()
    if not args.registry and not args.triage:
        print("Pass at least one --registry or --triage input.")
        return 2
    if args.limit < 0:
        print("--limit must be >= 0")
        return 2

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    gui_path = detect_gui(args.gui)
    topology_extractor = detect_topology_extractor(args.topology_extractor)

    registry_records = collect_registry_records(args.registry)
    triage_records = collect_triage_records(args.triage)
    records = merge_registry_and_triage_records(registry_records, triage_records)
    records = [record for record in records if as_str(record.get("fingerprint"))]
    if args.limit:
        records = records[: args.limit]

    packs = [build_pack(record, out_root, args.preview_dir, gui_path, topology_extractor) for record in records]
    by_api = Counter(as_str(pack.get("api")) or "unknown" for pack in packs)
    index = {
        "generated_at": now_iso_like(),
        "pack_count": len(packs),
        "debug_sgt_count": sum(int(pack.get("debug_sgt_count") or 0) for pack in packs),
        "focus_sgt_count": sum(int(pack.get("focus_sgt_count") or 0) for pack in packs),
        "input_sgt_count": sum(int(pack.get("input_sgt_count") or 0) for pack in packs),
        "by_api": dict(sorted(by_api.items())),
        "gui": gui_path,
        "topology_extractor": topology_extractor,
        "packs": packs,
    }
    write_json(out_root / "debug_handoff_index.json", index)
    write_index_report(index, out_root / "debug_handoff_report.md")
    print(f"index={out_root / 'debug_handoff_index.json'}")
    print(f"report={out_root / 'debug_handoff_report.md'}")
    print(
        f"packs={len(packs)} debug_sgts={index['debug_sgt_count']} "
        f"focus_sgts={index['focus_sgt_count']} input_sgts={index['input_sgt_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
