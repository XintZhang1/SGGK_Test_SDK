#!/usr/bin/env python3
"""Collect SGGK triage/replay/bundle outputs into a persistent bug registry."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time
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
    parser.add_argument("--bundle-index", action="append", default=[], help="bundle_index.json or bundle directory")
    parser.add_argument("--triage", action="append", default=[], help="triage_summary.json or triage directory")
    parser.add_argument("--replay", action="append", default=[], help="replay_summary.json or replay directory")
    parser.add_argument("--out", required=True, help="Output directory for bug_registry.json/md")
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


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


def resolve_existing_path(raw: Any, base: Path | None = None) -> str:
    text = as_str(raw)
    if not text:
        return ""
    path = Path(text)
    candidates = []
    if path.is_absolute():
        candidates.append(path)
    else:
        if base is not None:
            candidates.append((base / path).resolve())
        candidates.append(Path.cwd() / path)
        candidates.append(path.resolve())
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(candidates[0] if candidates else path)


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


def first_replay_attempt_path(replay: Any, key: str) -> str:
    if not isinstance(replay, dict):
        return ""
    for attempt in as_list(replay.get("attempts")):
        if isinstance(attempt, dict) and as_str(attempt.get(key)):
            return as_str(attempt.get(key))
    return ""


def normalize_bundle_index_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        return path / "bundle_index.json"
    return path


def normalize_triage_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        return path / "triage_summary.json"
    return path


def normalize_replay_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        return path / "replay_summary.json"
    return path


def first_contact(localization: Any) -> dict[str, Any]:
    if isinstance(localization, dict):
        primary = localization.get("primary_contact")
        if isinstance(primary, dict):
            return primary
        candidates = localization.get("contact_candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            return candidates[0]
    return {}


def contact_label(contact: Any) -> str:
    if not isinstance(contact, dict):
        return ""
    target = contact.get("target") if isinstance(contact.get("target"), dict) else {}
    tool = contact.get("tool") if isinstance(contact.get("tool"), dict) else {}
    if not target and not tool:
        return ""
    target_label = f"{as_str(target.get('role'))} {as_str(target.get('type'))}#{target.get('id')}[{target.get('local_index')}]"
    tool_label = f"{as_str(tool.get('role'))} {as_str(tool.get('type'))}#{tool.get('id')}[{tool.get('local_index')}]"
    gap = contact.get("bbox_distance")
    overlaps = contact.get("axis_overlaps")
    return f"{target_label} <-> {tool_label} gap={gap} overlaps={overlaps}"


def oracle_detail_label(detail: Any) -> str:
    if not isinstance(detail, dict):
        return ""
    kind = as_str(detail.get("oracle_kind")) or "oracle"
    if kind == "roundtrip_metric":
        return (
            f"{as_str(detail.get('id'))}: source={detail.get('source')} "
            f"result={detail.get('result')} delta={detail.get('delta')} "
            f"tolerance={detail.get('tolerance')}"
        )
    if kind == "roundtrip_bbox":
        return f"{as_str(detail.get('id'))}: source={detail.get('source')} result={detail.get('result')}"
    if kind == "plane_extreme_check":
        return (
            f"{as_str(detail.get('id'))}: axis={detail.get('axis')} side={detail.get('side')} "
            f"expected={detail.get('expected')} actual_extreme={detail.get('actual_extreme')} "
            f"probe={detail.get('probe_coordinate')} failures={detail.get('metric_failures')}"
        )
    check_id = as_str(detail.get("id"))
    pieces = [kind]
    if check_id:
        pieces.append(check_id)
    if detail.get("check_kind"):
        pieces.append(f"kind={detail.get('check_kind')}")
    if detail.get("expected") is not None or detail.get("actual") is not None:
        pieces.append(f"expected={detail.get('expected')} actual={detail.get('actual')}")
    if detail.get("actual_face"):
        pieces.append(f"face={detail.get('actual_face')}")
    if detail.get("topology_a") or detail.get("topology_b"):
        pieces.append(f"topology_a={detail.get('topology_a')} topology_b={detail.get('topology_b')}")
    if detail.get("uv"):
        pieces.append(f"uv={detail.get('uv')}")
    if detail.get("point"):
        pieces.append(f"point={detail.get('point')}")
    if detail.get("point_a") or detail.get("point_b"):
        pieces.append(f"point_a={detail.get('point_a')} point_b={detail.get('point_b')}")
    return "; ".join(pieces)


def locator_label(locator: Any) -> str:
    if not isinstance(locator, dict):
        return ""
    if "point" in locator:
        return f"point={locator.get('point')} tol={locator.get('tolerance')}"
    if "start_point" in locator or "end_point" in locator:
        return f"start={locator.get('start_point')} end={locator.get('end_point')} length={locator.get('length')}"
    if "area" in locator:
        return f"area={locator.get('area')} sense={locator.get('sense')}"
    bbox = locator.get("bbox")
    if isinstance(bbox, dict):
        return f"bbox={bbox.get('min')}..{bbox.get('max')}"
    if locator.get("edge_error"):
        return f"edge_error={locator.get('edge_error')}"
    if locator.get("face_error"):
        return f"face_error={locator.get('face_error')}"
    return ""


def localized_input_label(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    parts = [
        f"{as_str(item.get('role'))} {as_str(item.get('type'))}#{item.get('id')}[{item.get('local_index')}]",
        f"count={item.get('count')}",
    ]
    if as_str(item.get("terminal_operation")):
        parts.append(f"op={as_str(item.get('terminal_operation'))}")
    if item.get("track_type_counts"):
        parts.append(f"track={item.get('track_type_counts')}")
    locator = locator_label(item.get("locator"))
    if locator:
        parts.append(locator)
    return " ".join(parts)


def topo_track_diagnostic(summary: Any) -> dict[str, Any]:
    if summary is None:
        return {}
    if not isinstance(summary, dict):
        return {"status": "invalid", "reason": "topo_track_summary root is not an object"}
    if summary.get("_json_error"):
        return {"status": "invalid", "reason": summary.get("_json_error")}
    diagnostic = {
        "status": "ok",
        "reason": "",
        "skipped": bool(summary.get("skipped", False)),
        "skip_reason": as_str(summary.get("reason")),
        "item_count": as_int(summary.get("item_count")),
        "ancestor_count": as_int(summary.get("ancestor_count")),
        "resolved_ancestor_count": as_int(summary.get("resolved_ancestor_count")),
        "unresolved_ancestor_count": as_int(summary.get("unresolved_ancestor_count")),
        "ambiguous_ancestor_count": as_int(summary.get("ambiguous_ancestor_count")),
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


def validation_failures_from_manifest(manifest: Any) -> list[str]:
    if not isinstance(manifest, dict):
        return []
    failures = manifest.get("validation_failures")
    if isinstance(failures, list):
        return [as_str(item) for item in failures if as_str(item)]
    copied = manifest.get("copied")
    if isinstance(copied, dict):
        reports = copied.get("reports")
        if isinstance(reports, dict):
            validation_path = resolve_existing_path(reports.get("validation.json"))
            validation = load_json(Path(validation_path)) if validation_path else None
            if isinstance(validation, dict) and isinstance(validation.get("failures"), list):
                return [as_str(item) for item in validation["failures"] if as_str(item)]
    return []


def roundtrip_failures_from_report(path_text: str) -> list[str]:
    if not path_text:
        return []
    report = load_json(Path(path_text))
    if not isinstance(report, dict):
        return []
    failures = report.get("failures")
    if isinstance(failures, list):
        return [as_str(item) for item in failures if as_str(item)]
    if report.get("ok") is False:
        return ["roundtrip_comparison_failed"]
    return []


def replay_lookup_from_summary(summary: Any) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    if not isinstance(summary, dict):
        return lookup
    for item in as_list(summary.get("results")):
        if isinstance(item, dict) and as_str(item.get("fingerprint")):
            lookup[as_str(item["fingerprint"])] = item
    return lookup


def update_entry(entries: dict[str, dict[str, Any]], fingerprint: str, patch: dict[str, Any]) -> dict[str, Any]:
    entry = entries.setdefault(
        fingerprint,
        {
            "fingerprint": fingerprint,
            "sources": [],
            "observations": [],
            "paths": {},
        },
    )
    for key, value in patch.items():
        if value in ("", None, [], {}):
            continue
        if key in {"sources", "observations", "localized_inputs"}:
            entry.setdefault(key, [])
            for item in as_list(value):
                if item not in entry[key]:
                    entry[key].append(item)
            continue
        if key == "paths" and isinstance(value, dict):
            entry.setdefault("paths", {})
            for child_key, child_value in value.items():
                if child_value:
                    entry["paths"][child_key] = child_value
            continue
        entry[key] = value
    return entry


def collect_replay_summaries(paths: list[str]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in paths:
        path = normalize_replay_path(raw)
        summary = load_json(path)
        for fingerprint, item in replay_lookup_from_summary(summary).items():
            merged[fingerprint] = item
    return merged


def collect_from_triage(entries: dict[str, dict[str, Any]], triage_paths: list[str], replay_lookup: dict[str, dict[str, Any]]) -> None:
    for raw in triage_paths:
        path = normalize_triage_path(raw)
        summary = load_json(path)
        if not isinstance(summary, dict):
            continue
        for group in as_list(summary.get("failure_groups")):
            if not isinstance(group, dict):
                continue
            fingerprint = as_str(group.get("fingerprint"))
            if not fingerprint:
                continue
            replay = replay_lookup.get(fingerprint, {})
            contact_candidates = as_list(group.get("representative_input_contact_candidates"))
            primary_contact = contact_candidates[0] if contact_candidates and isinstance(contact_candidates[0], dict) else {}
            topo_summary = group.get("representative_topo_track_summary")
            case_dir_text = as_str(group.get("representative_case_dir"))
            case_dir = Path(case_dir_text) if case_dir_text else None
            original_recipe = first_existing_path(group.get("recipe_paths"), path.parent)
            debug_geometry = case_dir / "debug_geometry" if case_dir is not None else None
            debug_geometry_index = case_dir / "report" / "debug_geometry_index.json" if case_dir is not None else None
            input_paths = case_input_asset_paths(case_dir)
            replay_recipe = resolve_existing_path(first_replay_attempt_path(replay, "recipe"), path.parent)
            replay_artifact = resolve_existing_path(first_replay_attempt_path(replay, "artifact_dir"), path.parent)
            update_entry(
                entries,
                fingerprint,
                {
                    "representative_case_id": group.get("representative_case_id"),
                    "api": (group.get("apis") or [""])[0] if isinstance(group.get("apis"), list) else "",
                    "reasons": group.get("reasons"),
                    "failure_count": group.get("count"),
                    "validation_failures": group.get("representative_validation_failures"),
                    "validation_oracle_details": group.get("representative_validation_oracle_details"),
                    "roundtrip_failures": group.get("representative_roundtrip_failures"),
                    "roundtrip_oracle_details": group.get("representative_roundtrip_oracle_details"),
                    "runner": group.get("representative_runner"),
                    "dsl": group.get("representative_dsl"),
                    "localized_inputs": group.get("representative_localized_inputs"),
                    "topo_track_policy": "diagnostic_when_modeling_fails",
                    "topo_track_diagnostic": topo_track_diagnostic(topo_summary),
                    "warnings": group.get("representative_warnings"),
                    "replay_status": replay.get("status", "unreplayed") if isinstance(replay, dict) else "unreplayed",
                    "primary_contact": primary_contact,
                    "primary_contact_label": contact_label(primary_contact),
                    "sources": [str(path)],
                    "paths": {
                        "triage_summary": str(path),
                        "representative_case_dir": str(case_dir) if case_dir is not None and case_dir.exists() else "",
                        "original_recipe": original_recipe,
                        "replay_recipe": replay_recipe,
                        "replay_artifact": replay_artifact,
                        "debug_geometry": str(debug_geometry) if debug_geometry is not None and debug_geometry.is_dir() else "",
                        "debug_geometry_index": str(debug_geometry_index) if debug_geometry_index is not None and debug_geometry_index.is_file() else "",
                        **input_paths,
                    },
                },
            )


def collect_from_bundle_indices(entries: dict[str, dict[str, Any]], bundle_paths: list[str], replay_lookup: dict[str, dict[str, Any]]) -> None:
    for raw in bundle_paths:
        index_path = normalize_bundle_index_path(raw)
        index = load_json(index_path)
        if not isinstance(index, dict):
            continue
        index_base = index_path.parent
        for bundle in as_list(index.get("bundles")):
            if not isinstance(bundle, dict):
                continue
            fingerprint = as_str(bundle.get("fingerprint"))
            if not fingerprint:
                continue
            manifest_path = Path(resolve_existing_path(bundle.get("bundle_manifest"), index_base))
            localization_path = Path(resolve_existing_path(bundle.get("localization_summary"), index_base))
            manifest = load_json(manifest_path)
            localization = load_json(localization_path)
            replay = replay_lookup.get(fingerprint, {})
            if isinstance(manifest, dict) and isinstance(manifest.get("replay"), dict):
                replay = {**replay, **manifest["replay"]}
            contact = first_contact(localization)
            copied = manifest.get("copied") if isinstance(manifest, dict) and isinstance(manifest.get("copied"), dict) else {}
            reports = copied.get("reports") if isinstance(copied.get("reports"), dict) else {}
            recipes = copied.get("recipes") if isinstance(copied.get("recipes"), dict) else {}
            inputs = copied.get("inputs") if isinstance(copied.get("inputs"), dict) else {}
            artifact_inputs = as_dict(manifest.get("artifact_inputs")) if isinstance(manifest, dict) else {}
            input_paths = bundle_input_asset_paths(inputs, artifact_inputs, index_base)
            topo_summary_path = resolve_existing_path(reports.get("topo_track_summary.json"), index_base)
            roundtrip_report_path = resolve_existing_path(reports.get("roundtrip_comparison.json"), index_base)
            manifest_roundtrip_failures = (manifest or {}).get("roundtrip_failures") if isinstance(manifest, dict) else []
            patch = {
                "representative_case_id": bundle.get("representative_case_id") or (manifest or {}).get("representative_case_id"),
                "api": (manifest or {}).get("api") if isinstance(manifest, dict) else "",
                "reasons": (manifest or {}).get("reasons") if isinstance(manifest, dict) else [],
                "status": (manifest or {}).get("status") if isinstance(manifest, dict) else {},
                "dsl": (manifest or {}).get("dsl") if isinstance(manifest, dict) else {},
                "topo_track_policy": ((manifest or {}).get("topo_track_policy") or "diagnostic_when_modeling_fails") if isinstance(manifest, dict) else "diagnostic_when_modeling_fails",
                "topo_track_diagnostic": topo_track_diagnostic_from_report(topo_summary_path),
                "replay_status": bundle.get("replay_status") or replay.get("status") or "unreplayed",
                "replay_attempt_count": replay.get("attempt_count"),
                "replay_returncodes": replay.get("returncodes"),
                "validation_failures": validation_failures_from_manifest(manifest),
                "validation_oracle_details": (manifest or {}).get("validation_oracle_details") if isinstance(manifest, dict) else [],
                "roundtrip_failures": manifest_roundtrip_failures or roundtrip_failures_from_report(roundtrip_report_path),
                "roundtrip_oracle_details": (manifest or {}).get("roundtrip_oracle_details") if isinstance(manifest, dict) else [],
                "runner": (manifest or {}).get("runner") if isinstance(manifest, dict) else {},
                "localized_inputs": localization.get("localized_inputs") if isinstance(localization, dict) else (manifest or {}).get("localized_inputs", []),
                "primary_contact": contact,
                "primary_contact_label": contact_label(contact),
                "sources": [str(index_path), str(manifest_path), str(localization_path)],
                "paths": {
                    "bundle_dir": resolve_existing_path(bundle.get("bundle_dir"), index_base),
                    "bundle_manifest": str(manifest_path),
                    "bug_report": resolve_existing_path(bundle.get("bug_report"), index_base),
                    "localization_summary": str(localization_path),
                    "zip": resolve_existing_path(bundle.get("zip"), index_base),
                    "preview": resolve_existing_path(copied.get("preview"), index_base) if isinstance(copied, dict) else "",
                    "debug_geometry": resolve_existing_path(copied.get("debug_geometry"), index_base) if isinstance(copied, dict) else "",
                    "debug_geometry_index": resolve_existing_path(reports.get("debug_geometry_index.json"), index_base),
                    "reproduce_script": resolve_existing_path(copied.get("reproduce_script"), index_base) if isinstance(copied, dict) else "",
                    "original_recipe": resolve_existing_path(recipes.get("original"), index_base),
                    "reduced_recipe": resolve_existing_path(recipes.get("reduced"), index_base),
                    "replay_recipe": resolve_existing_path(recipes.get("replay"), index_base),
                    **input_paths,
                },
            }
            update_entry(entries, fingerprint, patch)


def status_rank(status: str) -> int:
    order = {
        "stable_same_failure": 0,
        "stable_failure": 1,
        "flaky_same_failure": 2,
        "flaky": 3,
        "changed_failure": 4,
        "unverified_failure": 5,
        "unreplayed": 6,
        "not_reproduced": 7,
        "unavailable": 8,
    }
    return order.get(status, 9)


def build_summary(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    items = sorted(entries.values(), key=lambda item: (status_rank(as_str(item.get("replay_status"))), as_str(item.get("fingerprint"))))
    by_status = Counter(as_str(item.get("replay_status")) or "unknown" for item in items)
    by_api = Counter(as_str(item.get("api")) or "unknown" for item in items)
    return {
        "generated_at": now_iso_like(),
        "total": len(items),
        "by_replay_status": dict(sorted(by_status.items())),
        "by_api": dict(sorted(by_api.items())),
        "bugs": items,
    }


def markdown_report(registry: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# SGGK Bug Registry")
    lines.append("")
    lines.append(f"- Generated: `{registry.get('generated_at')}`")
    lines.append(f"- Total: `{registry.get('total')}`")
    lines.append(f"- Replay status: `{registry.get('by_replay_status')}`")
    lines.append(f"- APIs: `{registry.get('by_api')}`")
    lines.append("")
    lines.append("| fingerprint | status | case | API | failure | topo-track | primary contact | report |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for bug in registry.get("bugs", []):
        if not isinstance(bug, dict):
            continue
        paths = bug.get("paths") if isinstance(bug.get("paths"), dict) else {}
        report = as_str(paths.get("bug_report")) or as_str(paths.get("bundle_manifest")) or as_str(paths.get("triage_summary"))
        failures = ", ".join(as_list(bug.get("validation_failures"))[:2])
        if not failures:
            failures = ", ".join(as_list(bug.get("roundtrip_failures"))[:2])
        if not failures:
            failures = ", ".join(as_list(bug.get("reasons"))[:2])
        topo = bug.get("topo_track_diagnostic") if isinstance(bug.get("topo_track_diagnostic"), dict) else {}
        lines.append(
            "| `{fingerprint}` | `{status}` | `{case}` | `{api}` | {failure} | `{topo}` | {contact} | `{report}` |".format(
                fingerprint=bug.get("fingerprint"),
                status=bug.get("replay_status", ""),
                case=bug.get("representative_case_id", ""),
                api=bug.get("api", ""),
                failure=failures,
                topo=topo.get("status", ""),
                contact=as_str(bug.get("primary_contact_label")),
                report=report,
            )
        )
    lines.append("")
    lines.append("## Reproduction Assets")
    lines.append("")
    for bug in registry.get("bugs", []):
        if not isinstance(bug, dict):
            continue
        paths = bug.get("paths") if isinstance(bug.get("paths"), dict) else {}
        lines.append(f"### {bug.get('fingerprint')}")
        lines.append("")
        dsl = bug.get("dsl") if isinstance(bug.get("dsl"), dict) else {}
        if dsl.get("source_task_id") or dsl.get("source_ref"):
            lines.append(
                f"- source_task: `{dsl.get('source_task_id', '')}` source_ref=`{dsl.get('source_ref', '')}` risk=`{dsl.get('source_risk_id', '')}`"
            )
        for label in (
            "reproduce_script",
            "replay_recipe",
            "original_recipe",
            "representative_case_dir",
            "replay_artifact",
            "source_sgt",
            "source_step",
            "source_stp",
            "source_iges",
            "source_igs",
            "target_sgt",
            "tool_sgt",
            "preview",
            "debug_geometry",
            "debug_geometry_index",
            "zip",
        ):
            if paths.get(label):
                lines.append(f"- {label}: `{paths[label]}`")
        topo = bug.get("topo_track_diagnostic") if isinstance(bug.get("topo_track_diagnostic"), dict) else {}
        if topo:
            lines.append(f"- topo_track: `{topo.get('status')}` reason=`{topo.get('reason', '')}`")
        runner = bug.get("runner") if isinstance(bug.get("runner"), dict) else {}
        if runner:
            lines.append(
                f"- runner: returncode=`{runner.get('returncode')}` timed_out=`{runner.get('timed_out')}` elapsed=`{runner.get('elapsed_seconds')}`"
            )
        localized_inputs = as_list(bug.get("localized_inputs"))
        if localized_inputs:
            lines.append("- localized_inputs:")
            for item in localized_inputs[:5]:
                label = localized_input_label(item)
                if label:
                    lines.append(f"  - {label}")
        details = as_list(bug.get("validation_oracle_details"))
        if details:
            lines.append("- validation_oracle_details:")
            for detail in details[:3]:
                line = oracle_detail_label(detail)
                if line:
                    lines.append(f"  - {line}")
        roundtrip_details = as_list(bug.get("roundtrip_oracle_details"))
        if roundtrip_details:
            lines.append("- roundtrip_oracle_details:")
            for detail in roundtrip_details[:3]:
                line = oracle_detail_label(detail)
                if line:
                    lines.append(f"  - {line}")
        lines.append("")
    return "\n".join(lines)


def write_replay_recipe_list(registry: dict[str, Any], path: Path) -> None:
    lines: list[str] = []
    for bug in registry.get("bugs", []):
        if not isinstance(bug, dict):
            continue
        paths = bug.get("paths") if isinstance(bug.get("paths"), dict) else {}
        recipe = as_str(paths.get("replay_recipe")) or as_str(paths.get("original_recipe"))
        if recipe:
            lines.append(recipe)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def main() -> int:
    args = parse_args()
    replay_lookup = collect_replay_summaries(args.replay)
    entries: dict[str, dict[str, Any]] = {}
    collect_from_triage(entries, args.triage, replay_lookup)
    collect_from_bundle_indices(entries, args.bundle_index, replay_lookup)
    registry = build_summary(entries)
    out_dir = Path(args.out).resolve()
    write_json(out_dir / "bug_registry.json", registry)
    (out_dir / "bug_registry.md").write_text(markdown_report(registry), encoding="utf-8")
    write_replay_recipe_list(registry, out_dir / "registry_replay_recipes.txt")
    print(f"registry={out_dir / 'bug_registry.json'}")
    print(f"report={out_dir / 'bug_registry.md'}")
    print(f"replay_recipes={out_dir / 'registry_replay_recipes.txt'}")
    print(f"bugs={registry['total']} statuses={registry['by_replay_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
