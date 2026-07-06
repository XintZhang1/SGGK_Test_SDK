#!/usr/bin/env python3
"""Export editable bug-record drafts from SGGK triage and failure bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any


INPUT_ASSET_FILENAMES = {
    "source_sgt": "source.sgt",
    "source_step": "source.step",
    "source_stp": "source.stp",
    "source_iges": "source.iges",
    "source_igs": "source.igs",
    "target_sgt": "target.sgt",
    "tool_sgt": "tool.sgt",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage", action="append", default=[], help="triage_summary.json or triage directory")
    parser.add_argument("--bundle-index", action="append", default=[], help="bundle_index.json or bundle directory")
    parser.add_argument("--replay", action="append", default=[], help="replay_summary.json or replay directory")
    parser.add_argument("--debug-handoff", action="append", default=[], help="debug_handoff_index.json or debug_handoff directory")
    parser.add_argument("--out", required=True, help="Output bug-record draft JSON")
    parser.add_argument("--bug-prefix", default="draft", help="Prefix used for generated bug_id values")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of records to emit")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def sanitize_name(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return text or "record"


def normalize_triage_path(raw: str) -> Path:
    path = Path(raw)
    return path / "triage_summary.json" if path.is_dir() else path


def normalize_bundle_index_path(raw: str) -> Path:
    path = Path(raw)
    return path / "bundle_index.json" if path.is_dir() else path


def normalize_replay_path(raw: str) -> Path:
    path = Path(raw)
    return path / "replay_summary.json" if path.is_dir() else path


def normalize_debug_handoff_path(raw: str) -> Path:
    path = Path(raw)
    return path / "debug_handoff_index.json" if path.is_dir() else path


def resolve_existing_path(raw: Any, base: Path | None = None) -> str:
    text = as_str(raw)
    if not text:
        return ""
    path = Path(text)
    candidates: list[Path] = []
    if path.is_absolute():
        candidates.append(path)
    else:
        if base is not None:
            candidates.append((base / path).resolve())
        candidates.append((Path.cwd() / path).resolve())
        candidates.append(path.resolve())
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(candidates[0] if candidates else path)


def resolve_existing_file(raw: Any, base: Path | None = None) -> str:
    resolved = resolve_existing_path(raw, base)
    return resolved if resolved and Path(resolved).exists() else ""


def first_existing_path(values: Any, base: Path | None = None) -> str:
    for value in as_list(values):
        resolved = resolve_existing_path(value, base)
        if resolved and Path(resolved).exists():
            return resolved
    return ""


def case_input_asset_paths(case_dir: Path | None) -> dict[str, str]:
    if case_dir is None:
        return {}
    input_dir = case_dir / "input"
    paths: dict[str, str] = {}
    for key, filename in INPUT_ASSET_FILENAMES.items():
        candidate = input_dir / filename
        if candidate.is_file():
            paths[key] = str(candidate)
    return paths


def bundle_input_asset_paths(inputs: dict[str, Any], artifact_inputs: dict[str, Any], base: Path) -> dict[str, str]:
    paths: dict[str, str] = {}
    for key in INPUT_ASSET_FILENAMES:
        resolved = resolve_existing_path(inputs.get(key) or artifact_inputs.get(key), base)
        if resolved:
            paths[key] = resolved
    return paths


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = as_str(value)
        if text:
            return text
    return ""


def first_replay_attempt_path(replay: Any, key: str) -> str:
    if not isinstance(replay, dict):
        return ""
    for attempt in as_list(replay.get("attempts")):
        if isinstance(attempt, dict) and as_str(attempt.get(key)):
            return as_str(attempt.get(key))
    return ""


def replay_lookup_from_summary(summary: Any) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    if not isinstance(summary, dict):
        return lookup
    for item in as_list(summary.get("results")):
        if isinstance(item, dict) and as_str(item.get("fingerprint")):
            lookup[as_str(item["fingerprint"])] = item
    return lookup


def collect_replay_lookup(paths: list[str]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in paths:
        path = normalize_replay_path(raw)
        for fingerprint, item in replay_lookup_from_summary(load_json(path)).items():
            merged[fingerprint] = item
    return merged


def sibling_path(path_text: str, filename: str) -> str:
    if not path_text:
        return ""
    return str(Path(path_text).with_name(filename))


def debug_handoff_lookup_from_index(index_path: Path) -> dict[str, dict[str, Any]]:
    index = load_json(index_path)
    if not isinstance(index, dict):
        return {}
    base = index_path.parent
    lookup: dict[str, dict[str, Any]] = {}
    for pack in as_list(index.get("packs")):
        if not isinstance(pack, dict):
            continue
        fingerprint = as_str(pack.get("fingerprint"))
        if not fingerprint:
            continue
        pack_dir = resolve_existing_path(pack.get("pack_dir"), base)
        readme = resolve_existing_path(pack.get("readme"), base)
        manifest = resolve_existing_path(pack.get("manifest"), base)
        visual_index = resolve_existing_path(pack.get("visual_index"), base)
        focus_index = resolve_existing_path(pack.get("focus_index"), base)
        evidence = {
            "debug_handoff_index": str(index_path.resolve()),
            "debug_handoff_report": resolve_existing_file(index.get("report_path"), base)
            or resolve_existing_file(str(base / "debug_handoff_report.md"), base),
            "pack_dir": pack_dir,
            "readme": readme,
            "manifest": manifest,
            "visual_index": visual_index,
            "visual_index_json": resolve_existing_file(sibling_path(visual_index, "visual_index.json"), base),
            "focus_index": focus_index,
            "focus_index_json": resolve_existing_file(sibling_path(focus_index, "focus_index.json"), base),
            "sgt_paths": resolve_existing_file(str(Path(pack_dir) / "sgt_paths.txt") if pack_dir else "", base),
            "open_folder": resolve_existing_file(str(Path(pack_dir) / "open_folder.ps1") if pack_dir else "", base),
            "open_in_gui": resolve_existing_file(str(Path(pack_dir) / "open_in_gui.ps1") if pack_dir else "", base),
            "debug_sgt_count": pack.get("debug_sgt_count"),
            "focus_sgt_count": pack.get("focus_sgt_count"),
            "input_sgt_count": pack.get("input_sgt_count"),
            "has_preview": pack.get("has_preview"),
            "gui": index.get("gui"),
            "topology_extractor": index.get("topology_extractor"),
        }
        lookup[fingerprint] = {key: value for key, value in evidence.items() if value not in ("", None, [], {})}
    return lookup


def collect_debug_handoff_lookup(paths: list[str]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in paths:
        path = normalize_debug_handoff_path(raw)
        for fingerprint, item in debug_handoff_lookup_from_index(path).items():
            merged[fingerprint] = item
    return merged


def normalize_replay_status(raw: Any) -> str:
    status = as_str(raw)
    if not status:
        return "unreplayed"
    aliases = {
        "still_failing": "stable_failure",
        "reproduced": "stable_failure",
        "fixed": "not_reproduced",
        "recipe_unavailable": "unavailable",
    }
    return aliases.get(status, status)


def topo_track_diagnostic(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    diagnostic = {
        "status": "ok",
        "reason": "",
        "skipped": bool(summary.get("skipped", False)),
        "skip_reason": as_str(summary.get("reason")),
        "item_count": int(summary.get("item_count", 0) or 0),
        "ancestor_count": int(summary.get("ancestor_count", 0) or 0),
        "resolved_ancestor_count": int(summary.get("resolved_ancestor_count", 0) or 0),
        "unresolved_ancestor_count": int(summary.get("unresolved_ancestor_count", 0) or 0),
        "ambiguous_ancestor_count": int(summary.get("ambiguous_ancestor_count", 0) or 0),
        "ancestor_input_role_counts": summary.get("ancestor_input_role_counts", {}),
    }
    if diagnostic["skipped"]:
        diagnostic["status"] = "skipped"
        diagnostic["reason"] = diagnostic["skip_reason"]
    elif diagnostic["unresolved_ancestor_count"] or diagnostic["ambiguous_ancestor_count"]:
        diagnostic["status"] = "incomplete"
        diagnostic["reason"] = "topo-track has unresolved or ambiguous ancestors"
    elif diagnostic["item_count"] == 0 and diagnostic["ancestor_count"] == 0:
        diagnostic["status"] = "empty"
        diagnostic["reason"] = "topo-track produced no items"
    return diagnostic


def topo_track_diagnostic_from_report(path_text: str) -> dict[str, Any]:
    if not path_text:
        return {}
    return topo_track_diagnostic(load_json(Path(path_text)))


def first_contact_from_list(values: Any) -> dict[str, Any]:
    for item in as_list(values):
        if isinstance(item, dict):
            return item
    return {}


def first_contact(localization: Any) -> dict[str, Any]:
    if not isinstance(localization, dict):
        return {}
    primary = localization.get("primary_contact")
    if isinstance(primary, dict):
        return primary
    return first_contact_from_list(localization.get("contact_candidates"))


def infer_expected(record: dict[str, Any]) -> dict[str, Any]:
    expected: dict[str, Any] = {"returncode": 2}
    reasons = set(as_str(item) for item in as_list(record.get("reasons")) if as_str(item))
    runner = as_dict(record.get("runner"))
    runner_returncode = as_int_or_none(runner.get("returncode"))
    if runner_returncode is not None and "runner_nonzero_exit" in reasons:
        expected["returncode"] = runner_returncode
    if bool(runner.get("timed_out")) or "runner_timeout" in reasons:
        expected["runner_timeout"] = True
    if as_list(record.get("validation_failures")):
        expected["result_must_fail_validation"] = True
    if as_list(record.get("roundtrip_failures")):
        expected["result_must_fail_roundtrip_comparison"] = True
    if not expected.keys() - {"returncode"}:
        expected["result_must_fail"] = True
    return expected


def title_for(record: dict[str, Any]) -> str:
    case_id = first_nonempty(record.get("representative_case_id"), record.get("fingerprint"), "case")
    api = first_nonempty(record.get("api"), "api")
    failures = as_list(record.get("validation_failures")) or as_list(record.get("roundtrip_failures")) or as_list(record.get("reasons"))
    head = as_str(failures[0]) if failures else "failure"
    if len(head) > 120:
        head = head[:117] + "..."
    return f"{case_id} {api} failure: {head}"


def append_unique(target: list[Any], values: list[Any]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def merge_record(records: dict[str, dict[str, Any]], record: dict[str, Any]) -> None:
    fingerprint = as_str(record.get("fingerprint"))
    if not fingerprint:
        return
    current = records.setdefault(fingerprint, {})
    for key, value in record.items():
        if value in ("", None, [], {}):
            continue
        if key in {
            "reasons",
            "validation_failures",
            "validation_oracle_details",
            "roundtrip_failures",
            "roundtrip_oracle_details",
            "localized_inputs",
            "observations",
            "sources",
        }:
            current.setdefault(key, [])
            append_unique(current[key], as_list(value))
            continue
        if key == "replay" and isinstance(value, dict):
            current.setdefault("replay", {})
            current["replay"].update({child_key: child_value for child_key, child_value in value.items() if child_value})
            continue
        if key == "debug_handoff" and isinstance(value, dict):
            current.setdefault("debug_handoff", {})
            current["debug_handoff"].update({child_key: child_value for child_key, child_value in value.items() if child_value})
            continue
        current[key] = value


def apply_debug_handoff_lookup(records: dict[str, dict[str, Any]], lookup: dict[str, dict[str, Any]]) -> None:
    for fingerprint, evidence in lookup.items():
        if fingerprint in records:
            merge_record(records, {"fingerprint": fingerprint, "debug_handoff": evidence})


def complete_record(record: dict[str, Any], bug_prefix: str) -> dict[str, Any]:
    fingerprint = as_str(record.get("fingerprint"))
    prefix = sanitize_name(bug_prefix)
    bug_id = first_nonempty(record.get("bug_id"), f"{prefix}_{fingerprint}" if prefix else fingerprint)
    completed = dict(record)
    completed["bug_id"] = bug_id
    completed["title"] = first_nonempty(record.get("title"), title_for(record))
    completed["expected"] = as_dict(record.get("expected")) or infer_expected(record)
    completed["topo_track_policy"] = first_nonempty(record.get("topo_track_policy"), "diagnostic_when_modeling_fails")
    completed["modeling_failure_required"] = bool(record.get("modeling_failure_required", True))
    completed["replay_status"] = normalize_replay_status(record.get("replay_status"))
    completed.setdefault("observations", [])
    return completed


def record_from_triage_group(group: dict[str, Any], triage_path: Path, replay_lookup: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fingerprint = as_str(group.get("fingerprint"))
    replay = replay_lookup.get(fingerprint, {})
    replay_status = normalize_replay_status(replay.get("status") if isinstance(replay, dict) else "")
    original_recipe = first_existing_path(group.get("recipe_paths"), triage_path.parent)
    replay_recipe = resolve_existing_path(first_replay_attempt_path(replay, "recipe"), triage_path.parent)
    replay_artifact = resolve_existing_path(first_replay_attempt_path(replay, "artifact_dir"), triage_path.parent)
    case_dir_text = resolve_existing_path(group.get("representative_case_dir"), triage_path.parent)
    case_dir = Path(case_dir_text) if case_dir_text else None
    paths: dict[str, str] = {}
    if case_dir is not None:
        paths = {
            "representative_case_dir": str(case_dir) if case_dir.exists() else "",
            "debug_geometry": str(case_dir / "debug_geometry") if (case_dir / "debug_geometry").is_dir() else "",
            "debug_geometry_index": str(case_dir / "report" / "debug_geometry_index.json") if (case_dir / "report" / "debug_geometry_index.json").is_file() else "",
        }
        paths.update(case_input_asset_paths(case_dir))
    return {
        "fingerprint": fingerprint,
        "representative_case_id": group.get("representative_case_id"),
        "api": (group.get("apis") or [""])[0] if isinstance(group.get("apis"), list) else "",
        "reasons": group.get("reasons"),
        "validation_failures": group.get("representative_validation_failures"),
        "validation_oracle_details": group.get("representative_validation_oracle_details"),
        "roundtrip_failures": group.get("representative_roundtrip_failures"),
        "roundtrip_oracle_details": group.get("representative_roundtrip_oracle_details"),
        "warnings": group.get("representative_warnings"),
        "runner": group.get("representative_runner"),
        "dsl": group.get("representative_dsl"),
        "localized_inputs": group.get("representative_localized_inputs"),
        "primary_contact": first_contact_from_list(group.get("representative_input_contact_candidates")),
        "topo_track_diagnostic": topo_track_diagnostic(group.get("representative_topo_track_summary")),
        "topo_track_policy": "diagnostic_when_modeling_fails",
        "replay_status": replay_status,
        "sources": [str(triage_path)],
        "original_recipe": original_recipe,
        "replay_artifact": replay_artifact,
        "debug_geometry": paths.get("debug_geometry", ""),
        "debug_geometry_index": paths.get("debug_geometry_index", ""),
        "representative_case_dir": paths.get("representative_case_dir", ""),
        **{key: paths.get(key, "") for key in INPUT_ASSET_FILENAMES},
        "replay": {"recipe_path": replay_recipe or original_recipe},
    }


def collect_from_triage(records: dict[str, dict[str, Any]], paths: list[str], replay_lookup: dict[str, dict[str, Any]]) -> None:
    for raw in paths:
        path = normalize_triage_path(raw)
        summary = load_json(path)
        if not isinstance(summary, dict):
            continue
        for group in as_list(summary.get("failure_groups")):
            if isinstance(group, dict):
                merge_record(records, record_from_triage_group(group, path, replay_lookup))


def record_from_bundle(bundle: dict[str, Any], index_path: Path, replay_lookup: dict[str, dict[str, Any]]) -> dict[str, Any]:
    index_base = index_path.parent
    fingerprint = as_str(bundle.get("fingerprint"))
    manifest_path = Path(resolve_existing_path(bundle.get("bundle_manifest"), index_base))
    localization_path = Path(resolve_existing_path(bundle.get("localization_summary"), index_base))
    manifest = as_dict(load_json(manifest_path))
    localization = as_dict(load_json(localization_path))
    copied = as_dict(manifest.get("copied"))
    reports = as_dict(copied.get("reports"))
    recipes = as_dict(copied.get("recipes"))
    inputs = as_dict(copied.get("inputs"))
    artifact_inputs = as_dict(manifest.get("artifact_inputs"))
    input_paths = bundle_input_asset_paths(inputs, artifact_inputs, index_base)
    replay = dict(replay_lookup.get(fingerprint, {}))
    for key, value in as_dict(manifest.get("replay")).items():
        if value not in ("", None, [], {}):
            replay[key] = value
    replay_status = normalize_replay_status(bundle.get("replay_status") or replay.get("status"))
    topo_summary_path = resolve_existing_path(reports.get("topo_track_summary.json"), index_base)
    replay_recipe = first_nonempty(
        resolve_existing_path(recipes.get("replay"), index_base),
        resolve_existing_path(first_replay_attempt_path(replay, "recipe"), index_base),
        resolve_existing_path(recipes.get("original"), index_base),
        first_existing_path(manifest.get("recipe_paths"), index_base),
    )
    return {
        "fingerprint": fingerprint,
        "representative_case_id": first_nonempty(bundle.get("representative_case_id"), manifest.get("representative_case_id")),
        "api": manifest.get("api"),
        "reasons": manifest.get("reasons"),
        "validation_failures": manifest.get("validation_failures"),
        "validation_oracle_details": manifest.get("validation_oracle_details"),
        "roundtrip_failures": manifest.get("roundtrip_failures"),
        "roundtrip_oracle_details": manifest.get("roundtrip_oracle_details"),
        "runner": manifest.get("runner"),
        "localized_inputs": localization.get("localized_inputs") or manifest.get("localized_inputs"),
        "primary_contact": first_contact(localization),
        "dsl": manifest.get("dsl"),
        "topo_track_diagnostic": topo_track_diagnostic_from_report(topo_summary_path),
        "topo_track_policy": "diagnostic_when_modeling_fails",
        "replay_status": replay_status,
        "sources": [str(index_path), str(manifest_path), str(localization_path)],
        "bundle_dir": resolve_existing_path(bundle.get("bundle_dir"), index_base),
        "bundle_manifest": str(manifest_path),
        "bug_report": resolve_existing_path(bundle.get("bug_report"), index_base),
        "localization_summary": str(localization_path),
        "zip": resolve_existing_path(bundle.get("zip"), index_base),
        "preview": resolve_existing_path(copied.get("preview"), index_base),
        "debug_geometry": resolve_existing_path(copied.get("debug_geometry"), index_base),
        "debug_geometry_index": resolve_existing_path(reports.get("debug_geometry_index.json"), index_base),
        "reproduce_script": resolve_existing_path(copied.get("reproduce_script"), index_base),
        "original_recipe": resolve_existing_path(recipes.get("original"), index_base),
        "replay_artifact": resolve_existing_path(replay.get("first_artifact_dir") or first_replay_attempt_path(replay, "artifact_dir"), index_base),
        **input_paths,
        "replay": {"recipe_path": replay_recipe},
    }


def collect_from_bundle_indices(records: dict[str, dict[str, Any]], paths: list[str], replay_lookup: dict[str, dict[str, Any]]) -> None:
    for raw in paths:
        index_path = normalize_bundle_index_path(raw)
        index = load_json(index_path)
        if not isinstance(index, dict):
            continue
        for bundle in as_list(index.get("bundles")):
            if isinstance(bundle, dict):
                merge_record(records, record_from_bundle(bundle, index_path, replay_lookup))


def main() -> int:
    args = parse_args()
    replay_lookup = collect_replay_lookup(args.replay)
    debug_handoff_lookup = collect_debug_handoff_lookup(args.debug_handoff)
    records_by_fingerprint: dict[str, dict[str, Any]] = {}
    collect_from_triage(records_by_fingerprint, args.triage, replay_lookup)
    collect_from_bundle_indices(records_by_fingerprint, args.bundle_index, replay_lookup)
    apply_debug_handoff_lookup(records_by_fingerprint, debug_handoff_lookup)
    records = [complete_record(record, args.bug_prefix) for record in records_by_fingerprint.values()]
    records.sort(key=lambda item: (as_str(item.get("replay_status")), as_str(item.get("fingerprint"))))
    if args.limit > 0:
        records = records[: args.limit]
    root = {
        "schema_version": 1,
        "registry_id": "drafted_bug_records",
        "description": "Draft bug records generated from triage and failure-bundle artifacts. Review bug_id, title, and notes before checking in as a maintained regression record.",
        "defaults": {
            "modeling_failure_required": True,
            "topo_track_policy": "diagnostic_when_modeling_fails",
        },
        "records": records,
    }
    out_path = Path(args.out)
    write_json(out_path, root)
    print(f"draft_records={out_path.resolve()}")
    print(f"records={len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
