#!/usr/bin/env python3
"""Materialize hand-written SGGK bug records into a replayable bug registry."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any

from compile_attack_dsl import DslError, compile_dsl_file
from validate_recipe import validate_file


INPUT_ASSET_KEYS = (
    "source_sgt",
    "source_step",
    "source_stp",
    "source_iges",
    "source_igs",
    "target_sgt",
    "tool_sgt",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--records",
        action="append",
        required=True,
        help="Bug-record JSON file or directory containing *.json records. Can be passed more than once.",
    )
    parser.add_argument("--out", required=True, help="Output directory for bug_registry.json/md and emitted recipes")
    parser.add_argument("--validate-recipes", action="store_true", help="Validate emitted or referenced replay recipes")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sanitize_name(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "record")).strip("._")
    return text or "record"


def stable_hash(value: Any, length: int = 16) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:length]


def resolve_path(raw: Any, base: Path) -> str:
    text = as_str(raw)
    if not text:
        return ""
    path = Path(text)
    return str(path if path.is_absolute() else (base / path).resolve())


def expand_record_files(values: list[str]) -> list[Path]:
    result: list[Path] = []
    for raw in values:
        path = Path(raw).resolve()
        if path.is_dir():
            files = sorted(item for item in path.glob("*.json") if item.is_file())
            if not files:
                raise ValueError(f"record directory has no JSON files: {path}")
            result.extend(files)
        else:
            result.append(path)
    return result


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = as_str(value)
        if text:
            return text
    return ""


def contact_label(contact: Any) -> str:
    if not isinstance(contact, dict):
        return ""
    target = contact.get("target") if isinstance(contact.get("target"), dict) else {}
    tool = contact.get("tool") if isinstance(contact.get("tool"), dict) else {}
    if not target and not tool:
        return ""
    target_label = f"{as_str(target.get('role'))} {as_str(target.get('type'))}#{target.get('id')}[{target.get('local_index')}]"
    tool_label = f"{as_str(tool.get('role'))} {as_str(tool.get('type'))}#{tool.get('id')}[{tool.get('local_index')}]"
    return f"{target_label} <-> {tool_label} gap={contact.get('bbox_distance')} overlaps={contact.get('axis_overlaps')}"


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
    check_id = as_str(detail.get("id"))
    pieces = [kind]
    if check_id:
        pieces.append(check_id)
    if detail.get("check_kind"):
        pieces.append(f"kind={detail.get('check_kind')}")
    if detail.get("actual_extreme") is None and (detail.get("expected") is not None or detail.get("actual") is not None):
        pieces.append(f"expected={detail.get('expected')} actual={detail.get('actual')}")
    if detail.get("actual_extreme") is not None:
        pieces.append(
            f"axis={detail.get('axis')} side={detail.get('side')} "
            f"expected={detail.get('expected')} actual_extreme={detail.get('actual_extreme')} "
            f"probe={detail.get('probe_coordinate')}"
        )
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


def records_from_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = load_json(path)
    if isinstance(root, list):
        return {}, [item for item in root if isinstance(item, dict)]
    if not isinstance(root, dict):
        raise ValueError(f"record file root must be an object or list: {path}")
    records = root.get("records")
    if not isinstance(records, list):
        raise ValueError(f"record file must contain a records array: {path}")
    return as_dict(root.get("defaults")), [item for item in records if isinstance(item, dict)]


def replay_spec(record: dict[str, Any]) -> dict[str, Any]:
    replay = as_dict(record.get("replay"))
    if replay:
        return replay
    if "recipe" in record:
        return {"recipe": record.get("recipe")}
    if "recipe_path" in record:
        return {"recipe_path": record.get("recipe_path")}
    return {}


def has_dsl_replay(spec: dict[str, Any]) -> bool:
    return bool(as_dict(spec.get("dsl")) or first_nonempty(
        spec.get("dsl_path"),
        spec.get("dsl_file"),
        spec.get("dsl_source"),
    ))


def dsl_recipe_selector(record: dict[str, Any], spec: dict[str, Any]) -> dict[str, str]:
    record_dsl = as_dict(record.get("dsl"))
    return {
        "compiled_case_id": first_nonempty(
            spec.get("compiled_case_id"),
            spec.get("case_id"),
            spec.get("compiled_recipe_case_id"),
            record.get("representative_case_id"),
        ),
        "dsl_case_id": first_nonempty(
            spec.get("dsl_case_id"),
            spec.get("source_case_id"),
            record_dsl.get("case_id"),
        ),
        "dsl_variant": first_nonempty(
            spec.get("dsl_variant"),
            spec.get("variant"),
            record_dsl.get("variant"),
        ),
    }


def select_compiled_dsl_recipe(
    recipes: list[dict[str, Any]],
    selector: dict[str, str],
    label: str,
) -> dict[str, Any]:
    if not recipes:
        raise ValueError(f"{label}: DSL compiled to no recipes")
    compiled_case_id = selector.get("compiled_case_id", "")
    dsl_case_id = selector.get("dsl_case_id", "")
    dsl_variant = selector.get("dsl_variant", "")

    matches: list[dict[str, Any]]
    if compiled_case_id:
        matches = [recipe for recipe in recipes if as_str(recipe.get("case_id")) == compiled_case_id]
    elif dsl_case_id:
        matches = [recipe for recipe in recipes if as_str(recipe.get("dsl_case_id")) == dsl_case_id]
        if dsl_variant:
            matches = [recipe for recipe in matches if as_str(recipe.get("dsl_variant")) == dsl_variant]
    elif len(recipes) == 1:
        return dict(recipes[0])
    else:
        raise ValueError(
            f"{label}: DSL compiled to {len(recipes)} recipes; specify replay.case_id or replay.dsl_case_id"
        )

    if len(matches) != 1:
        selector_text = {key: value for key, value in selector.items() if value}
        raise ValueError(f"{label}: DSL selector matched {len(matches)} recipes: {selector_text}")
    return dict(matches[0])


def emit_dsl_replay_recipe(
    record: dict[str, Any],
    spec: dict[str, Any],
    record_file: Path,
    out_dir: Path,
    provisional_fingerprint: str,
) -> tuple[str, dict[str, Any]]:
    recipe_name = sanitize_name(first_nonempty(record.get("bug_id"), record.get("representative_case_id"), provisional_fingerprint))
    inline_dsl = as_dict(spec.get("dsl"))
    if inline_dsl:
        dsl_path = out_dir / "dsl" / f"{recipe_name}.json"
        write_json(dsl_path, inline_dsl)
    else:
        dsl_path_text = first_nonempty(spec.get("dsl_path"), spec.get("dsl_file"), spec.get("dsl_source"))
        dsl_path = Path(resolve_path(dsl_path_text, record_file.parent))

    if not dsl_path.is_file():
        raise ValueError(f"{record_file}: replay DSL path not found: {dsl_path}")

    try:
        recipes = compile_dsl_file(dsl_path)
    except DslError as exc:
        raise ValueError(f"{record_file}: DSL replay compile failed: {exc}") from exc

    recipe = select_compiled_dsl_recipe(recipes, dsl_recipe_selector(record, spec), str(record_file))
    if "case_id" not in recipe:
        recipe["case_id"] = first_nonempty(record.get("representative_case_id"), record.get("bug_id"), provisional_fingerprint)
    recipe_path = out_dir / "recipes" / f"{recipe_name}.json"
    write_json(recipe_path, recipe)
    return str(recipe_path.resolve()), recipe


def infer_dsl(record: dict[str, Any], recipe: dict[str, Any] | None = None) -> dict[str, Any]:
    dsl = as_dict(record.get("dsl"))
    if dsl:
        return dsl
    source = first_nonempty(record.get("dsl_source"), recipe.get("dsl_source") if recipe else "")
    case_id = first_nonempty(record.get("dsl_case_id"), recipe.get("dsl_case_id") if recipe else "")
    variant = first_nonempty(record.get("dsl_variant"), recipe.get("dsl_variant") if recipe else "")
    hypothesis = first_nonempty(record.get("hypothesis"), recipe.get("hypothesis") if recipe else "")
    source_ref = first_nonempty(record.get("source_ref"), recipe.get("source_ref") if recipe else "")
    source_task_id = first_nonempty(record.get("source_task_id"), recipe.get("source_task_id") if recipe else "")
    source_task_path = first_nonempty(record.get("source_task_path"), recipe.get("source_task_path") if recipe else "")
    source_risk_id = first_nonempty(record.get("source_risk_id"), recipe.get("source_risk_id") if recipe else "")
    source_risk_family = first_nonempty(record.get("source_risk_family"), recipe.get("source_risk_family") if recipe else "")
    source_risk_categories = first_nonempty(record.get("source_risk_categories"), recipe.get("source_risk_categories") if recipe else "")
    if not any((source, case_id, variant, hypothesis, source_ref, source_task_id, source_risk_id)):
        return {}
    return {
        "source": source,
        "case_id": case_id,
        "variant": variant,
        "hypothesis": hypothesis,
        "source_ref": source_ref,
        "source_task_id": source_task_id,
        "source_task_path": source_task_path,
        "source_risk_id": source_risk_id,
        "source_risk_family": source_risk_family,
        "source_risk_categories": source_risk_categories,
    }


def fingerprint_for(record: dict[str, Any], recipe: dict[str, Any] | None, recipe_path: str) -> str:
    explicit = as_str(record.get("fingerprint"))
    if explicit:
        return explicit
    return stable_hash(
        {
            "bug_id": record.get("bug_id"),
            "title": record.get("title"),
            "api": record.get("api") or (recipe or {}).get("api"),
            "validation_failures": record.get("validation_failures"),
            "roundtrip_failures": record.get("roundtrip_failures"),
            "recipe_path": recipe_path,
            "recipe": recipe,
        }
    )


def emit_recipe(
    record: dict[str, Any],
    defaults: dict[str, Any],
    record_file: Path,
    out_dir: Path,
    provisional_fingerprint: str,
) -> tuple[str, dict[str, Any] | None]:
    spec = replay_spec(record)
    inline_recipe = spec.get("recipe")
    if isinstance(inline_recipe, dict):
        recipe = dict(inline_recipe)
        if "case_id" not in recipe:
            recipe["case_id"] = first_nonempty(record.get("representative_case_id"), record.get("bug_id"), provisional_fingerprint)
        recipe_name = sanitize_name(first_nonempty(record.get("bug_id"), recipe.get("case_id"), provisional_fingerprint))
        recipe_path = out_dir / "recipes" / f"{recipe_name}.json"
        write_json(recipe_path, recipe)
        return str(recipe_path.resolve()), recipe
    if has_dsl_replay(spec):
        return emit_dsl_replay_recipe(record, spec, record_file, out_dir, provisional_fingerprint)
    recipe_path = first_nonempty(spec.get("recipe_path"), defaults.get("recipe_path"), record.get("replay_recipe"))
    if recipe_path:
        return resolve_path(recipe_path, record_file.parent), None
    return "", None


def build_bug(
    record: dict[str, Any],
    defaults: dict[str, Any],
    record_file: Path,
    out_dir: Path,
) -> dict[str, Any]:
    provisional_fingerprint = first_nonempty(record.get("fingerprint"), record.get("bug_id"), record.get("representative_case_id"))
    recipe_path, inline_recipe = emit_recipe(record, defaults, record_file, out_dir, provisional_fingerprint)
    fingerprint = fingerprint_for(record, inline_recipe, recipe_path)
    api = first_nonempty(record.get("api"), inline_recipe.get("api") if inline_recipe else "", defaults.get("api"))
    replay_status = first_nonempty(record.get("replay_status"), defaults.get("replay_status"), "stable_failure")
    topo_policy = first_nonempty(record.get("topo_track_policy"), defaults.get("topo_track_policy"), "diagnostic_when_modeling_fails")
    expected = as_dict(record.get("expected"))
    dsl = infer_dsl(record, inline_recipe)
    primary_contact = as_dict(record.get("primary_contact"))
    runner = as_dict(defaults.get("runner"))
    runner.update(as_dict(record.get("runner")))
    observations = []
    observations.extend(as_list(defaults.get("observations")))
    observations.extend(as_list(record.get("observations")))
    if as_str(record.get("notes")):
        observations.append(as_str(record.get("notes")))
    validation_oracle_details = []
    validation_oracle_details.extend(as_list(defaults.get("validation_oracle_details")))
    validation_oracle_details.extend(as_list(record.get("validation_oracle_details")))
    roundtrip_oracle_details = []
    roundtrip_oracle_details.extend(as_list(defaults.get("roundtrip_oracle_details")))
    roundtrip_oracle_details.extend(as_list(record.get("roundtrip_oracle_details")))
    localized_inputs = []
    localized_inputs.extend(as_list(defaults.get("localized_inputs")))
    localized_inputs.extend(as_list(record.get("localized_inputs")))
    debug_handoff = {}
    debug_handoff.update(as_dict(defaults.get("debug_handoff")))
    debug_handoff.update(as_dict(record.get("debug_handoff")))

    paths = {
        "record_file": str(record_file.resolve()),
        "replay_recipe": recipe_path,
        "original_recipe": resolve_path(record.get("original_recipe"), record_file.parent),
        "representative_case_dir": resolve_path(record.get("representative_case_dir"), record_file.parent),
        "replay_artifact": resolve_path(record.get("replay_artifact"), record_file.parent),
        "bundle_dir": resolve_path(record.get("bundle_dir"), record_file.parent),
        "bundle_manifest": resolve_path(record.get("bundle_manifest"), record_file.parent),
        "localization_summary": resolve_path(record.get("localization_summary"), record_file.parent),
        "zip": resolve_path(record.get("zip"), record_file.parent),
        "reproduce_script": resolve_path(record.get("reproduce_script"), record_file.parent),
        "dsl_source": resolve_path(dsl.get("source"), record_file.parent) if dsl.get("source") else "",
        "bug_report": resolve_path(record.get("bug_report"), record_file.parent),
        "preview": resolve_path(record.get("preview"), record_file.parent),
        "debug_geometry": resolve_path(record.get("debug_geometry"), record_file.parent),
        "debug_geometry_index": resolve_path(record.get("debug_geometry_index"), record_file.parent),
        "debug_handoff_index": resolve_path(first_nonempty(debug_handoff.get("debug_handoff_index"), record.get("debug_handoff_index")), record_file.parent),
        "debug_handoff_report": resolve_path(first_nonempty(debug_handoff.get("debug_handoff_report"), record.get("debug_handoff_report")), record_file.parent),
        "debug_handoff_pack": resolve_path(first_nonempty(debug_handoff.get("pack_dir"), record.get("debug_handoff_pack")), record_file.parent),
        "debug_handoff_readme": resolve_path(first_nonempty(debug_handoff.get("readme"), record.get("debug_handoff_readme")), record_file.parent),
        "debug_handoff_manifest": resolve_path(first_nonempty(debug_handoff.get("manifest"), record.get("debug_handoff_manifest")), record_file.parent),
        "visual_index": resolve_path(first_nonempty(debug_handoff.get("visual_index"), record.get("visual_index")), record_file.parent),
        "visual_index_json": resolve_path(first_nonempty(debug_handoff.get("visual_index_json"), record.get("visual_index_json")), record_file.parent),
        "focus_index": resolve_path(first_nonempty(debug_handoff.get("focus_index"), record.get("focus_index")), record_file.parent),
        "focus_index_json": resolve_path(first_nonempty(debug_handoff.get("focus_index_json"), record.get("focus_index_json")), record_file.parent),
        "sgt_paths": resolve_path(first_nonempty(debug_handoff.get("sgt_paths"), record.get("sgt_paths")), record_file.parent),
        "open_folder": resolve_path(first_nonempty(debug_handoff.get("open_folder"), record.get("open_folder")), record_file.parent),
        "open_in_gui": resolve_path(first_nonempty(debug_handoff.get("open_in_gui"), record.get("open_in_gui")), record_file.parent),
        **{key: resolve_path(record.get(key), record_file.parent) for key in INPUT_ASSET_KEYS},
    }
    paths = {key: value for key, value in paths.items() if value}
    resolved_debug_handoff = dict(debug_handoff)
    for source_key, path_key in (
        ("debug_handoff_index", "debug_handoff_index"),
        ("debug_handoff_report", "debug_handoff_report"),
        ("pack_dir", "debug_handoff_pack"),
        ("readme", "debug_handoff_readme"),
        ("manifest", "debug_handoff_manifest"),
        ("visual_index", "visual_index"),
        ("visual_index_json", "visual_index_json"),
        ("focus_index", "focus_index"),
        ("focus_index_json", "focus_index_json"),
        ("sgt_paths", "sgt_paths"),
        ("open_folder", "open_folder"),
        ("open_in_gui", "open_in_gui"),
    ):
        if paths.get(path_key):
            resolved_debug_handoff[source_key] = paths[path_key]
    resolved_debug_handoff = {key: value for key, value in resolved_debug_handoff.items() if value not in ("", None, [], {})}

    return {
        "fingerprint": fingerprint,
        "bug_id": first_nonempty(record.get("bug_id"), fingerprint),
        "title": as_str(record.get("title")),
        "representative_case_id": first_nonempty(record.get("representative_case_id"), (inline_recipe or {}).get("case_id")),
        "api": api,
        "reasons": as_list(record.get("reasons")) or as_list(defaults.get("reasons")),
        "validation_failures": as_list(record.get("validation_failures")),
        "validation_oracle_details": validation_oracle_details,
        "roundtrip_failures": as_list(record.get("roundtrip_failures")),
        "roundtrip_oracle_details": roundtrip_oracle_details,
        "localized_inputs": localized_inputs,
        "replay_status": replay_status,
        "runner": runner,
        "topo_track_policy": topo_policy,
        "topo_track_diagnostic": as_dict(record.get("topo_track_diagnostic")) or as_dict(defaults.get("topo_track_diagnostic")),
        "topo_track_required": bool(record.get("topo_track_required", defaults.get("topo_track_required", False))),
        "modeling_failure_required": bool(record.get("modeling_failure_required", defaults.get("modeling_failure_required", True))),
        "expected": expected,
        "dsl": dsl,
        "primary_contact": primary_contact,
        "primary_contact_label": first_nonempty(record.get("primary_contact_label"), contact_label(primary_contact)),
        "debug_handoff": resolved_debug_handoff,
        "sources": [str(record_file.resolve())],
        "observations": observations,
        "paths": paths,
    }


def status_rank(status: str) -> int:
    order = {
        "stable_failure": 0,
        "recorded": 1,
        "flaky": 2,
        "unreplayed": 3,
        "not_reproduced": 4,
        "unavailable": 5,
    }
    return order.get(status, 9)


def build_registry(bugs: list[dict[str, Any]]) -> dict[str, Any]:
    items = sorted(bugs, key=lambda item: (status_rank(as_str(item.get("replay_status"))), as_str(item.get("fingerprint"))))
    by_status = Counter(as_str(item.get("replay_status")) or "unknown" for item in items)
    by_api = Counter(as_str(item.get("api")) or "unknown" for item in items)
    return {
        "generated_at": now_iso_like(),
        "source": "bug_records",
        "total": len(items),
        "by_replay_status": dict(sorted(by_status.items())),
        "by_api": dict(sorted(by_api.items())),
        "bugs": items,
    }


def markdown_report(registry: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# SGGK Bug Records")
    lines.append("")
    lines.append(f"- Generated: `{registry.get('generated_at')}`")
    lines.append(f"- Total: `{registry.get('total')}`")
    lines.append(f"- Replay status: `{registry.get('by_replay_status')}`")
    lines.append(f"- APIs: `{registry.get('by_api')}`")
    lines.append("")
    lines.append("| fingerprint | bug id | case | API | expected failure | topo-track | recipe |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for bug in registry.get("bugs", []):
        if not isinstance(bug, dict):
            continue
        paths = as_dict(bug.get("paths"))
        failures = ", ".join(as_list(bug.get("validation_failures"))[:2])
        if not failures:
            failures = ", ".join(as_list(bug.get("roundtrip_failures"))[:2])
        topo = as_dict(bug.get("topo_track_diagnostic"))
        topo_label = as_str(topo.get("status")) or as_str(bug.get("topo_track_policy"))
        lines.append(
            "| `{fingerprint}` | `{bug_id}` | `{case}` | `{api}` | {failure} | `{policy}` | `{recipe}` |".format(
                fingerprint=bug.get("fingerprint"),
                bug_id=bug.get("bug_id", ""),
                case=bug.get("representative_case_id", ""),
                api=bug.get("api", ""),
                failure=failures,
                policy=topo_label,
                recipe=paths.get("replay_recipe", ""),
            )
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- `diagnostic_when_modeling_fails` means missing or incomplete topo-track is reported as diagnostic context when the modeling/result oracle fails, but it is not treated as the primary bug by itself.")
    lines.append("- `registry_replay_recipes.txt` can be passed directly to `run_recipes.py --recipe-list`.")
    lines.append("")
    bugs_with_assets = [
        bug for bug in registry.get("bugs", [])
        if isinstance(bug, dict) and as_dict(bug.get("paths"))
    ]
    if bugs_with_assets:
        lines.append("## Reproduction Assets")
        lines.append("")
    for bug in bugs_with_assets:
        paths = as_dict(bug.get("paths"))
        dsl = as_dict(bug.get("dsl"))
        lines.append(f"### {bug.get('fingerprint')}")
        lines.append("")
        if dsl.get("source_task_id") or dsl.get("source_ref") or dsl.get("source_risk_id"):
            lines.append(
                "- source_task: `{task}` source_ref=`{source_ref}` risk=`{risk}`".format(
                    task=dsl.get("source_task_id", ""),
                    source_ref=dsl.get("source_ref", ""),
                    risk=dsl.get("source_risk_id", ""),
                )
            )
            if dsl.get("source_task_path"):
                lines.append(f"- source_task_path: `{dsl.get('source_task_path')}`")
        for label in (
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
            "debug_handoff_index",
            "debug_handoff_report",
            "debug_handoff_pack",
            "debug_handoff_readme",
            "debug_handoff_manifest",
            "visual_index",
            "visual_index_json",
            "focus_index",
            "focus_index_json",
            "sgt_paths",
            "open_folder",
            "open_in_gui",
            "bug_report",
            "bundle_manifest",
            "localization_summary",
            "reproduce_script",
            "zip",
        ):
            if paths.get(label):
                lines.append(f"- {label}: `{paths[label]}`")
        handoff = as_dict(bug.get("debug_handoff"))
        if handoff:
            lines.append(
                "- debug_handoff_sgts: debug=`{debug}` focus=`{focus}` input=`{input}`".format(
                    debug=handoff.get("debug_sgt_count", 0),
                    focus=handoff.get("focus_sgt_count", 0),
                    input=handoff.get("input_sgt_count", 0),
                )
            )
        topo = as_dict(bug.get("topo_track_diagnostic"))
        if topo:
            lines.append(f"- topo_track: `{topo.get('status')}` reason=`{topo.get('reason', '')}`")
        runner = as_dict(bug.get("runner"))
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
        lines.append("")
    bugs_with_oracle_details = [
        bug for bug in registry.get("bugs", [])
        if isinstance(bug, dict)
        and (as_list(bug.get("validation_oracle_details")) or as_list(bug.get("roundtrip_oracle_details")))
    ]
    if bugs_with_oracle_details:
        lines.append("## Oracle Details")
        lines.append("")
    for bug in bugs_with_oracle_details:
        details = as_list(bug.get("validation_oracle_details")) + as_list(bug.get("roundtrip_oracle_details"))
        lines.append(f"### {bug.get('fingerprint')}")
        lines.append("")
        for detail in details[:3]:
            line = oracle_detail_label(detail)
            if line:
                lines.append(f"- {line}")
        lines.append("")
    return "\n".join(lines)


def write_replay_recipe_list(registry: dict[str, Any], path: Path) -> None:
    lines: list[str] = []
    for bug in registry.get("bugs", []):
        if not isinstance(bug, dict):
            continue
        recipe = as_str(as_dict(bug.get("paths")).get("replay_recipe"))
        if recipe:
            lines.append(recipe)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def validate_replay_recipes(registry: dict[str, Any]) -> int:
    failures = 0
    for bug in registry.get("bugs", []):
        if not isinstance(bug, dict):
            continue
        recipe = as_str(as_dict(bug.get("paths")).get("replay_recipe"))
        if not recipe:
            continue
        errors = validate_file(Path(recipe))
        if errors:
            failures += 1
            print(f"FAIL {recipe}", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
    return failures


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    bugs: list[dict[str, Any]] = []
    try:
        record_files = expand_record_files(args.records)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for record_file in record_files:
        defaults, records = records_from_file(record_file)
        for record in records:
            bugs.append(build_bug(record, defaults, record_file, out_dir))

    registry = build_registry(bugs)
    write_json(out_dir / "bug_registry.json", registry)
    (out_dir / "bug_registry.md").write_text(markdown_report(registry), encoding="utf-8")
    write_replay_recipe_list(registry, out_dir / "registry_replay_recipes.txt")
    print(f"registry={out_dir / 'bug_registry.json'}")
    print(f"report={out_dir / 'bug_registry.md'}")
    print(f"replay_recipes={out_dir / 'registry_replay_recipes.txt'}")
    print(f"bugs={registry['total']} statuses={registry['by_replay_status']}")

    if args.validate_recipes:
        failures = validate_replay_recipes(registry)
        if failures:
            print(f"recipe validation failed for {failures} record(s)", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
