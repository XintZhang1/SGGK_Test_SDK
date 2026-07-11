#!/usr/bin/env python3
"""Export triaged SGGK failures as handoff-ready bug bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
from typing import Any


KEY_REPORTS = [
    "status.json",
    "validation.json",
    "topo_check.json",
    "topo_track_summary.json",
    "topo_track.json",
    "input_provenance.json",
    "input_topology_index.json",
    "debug_geometry_index.json",
    "data_exchange.json",
    "source_properties.json",
    "properties.json",
    "roundtrip_comparison.json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--triage",
        required=True,
        help="triage_summary.json or directory produced by triage_artifacts.py",
    )
    parser.add_argument(
        "--replay",
        help="Optional replay_summary.json or directory produced by replay_regression_seeds.py",
    )
    parser.add_argument(
        "--reductions",
        help="Optional reduction_index.json or reductions directory; matching reduced recipes become canonical reproducers.",
    )
    parser.add_argument(
        "--preview-dir",
        action="append",
        default=[],
        help="Optional preview directory containing <case_id>.png files. Can be passed more than once.",
    )
    parser.add_argument("--out", default="artifacts/failure_bundles", help="Bundle output directory")
    parser.add_argument("--limit", type=int, default=0, help="Maximum failure groups to export; 0 means all")
    parser.add_argument(
        "--include-full-artifact",
        action="store_true",
        help="Copy the full representative artifact directory. By default only key reports and inputs are copied.",
    )
    parser.add_argument("--zip", action="store_true", help="Also create <bundle>.zip archives")
    return parser.parse_args()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def resolve_summary_path(raw: str, filename: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        path = path / filename
    return path


def sanitize_name(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "bundle")).strip("._")
    return text or "bundle"


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def first_dict(items: Any, key: str, value: str) -> dict[str, Any]:
    if not isinstance(items, list):
        return {}
    for item in items:
        if isinstance(item, dict) and as_str(item.get(key)) == value:
            return item
    return {}


def copy_file(src: str | Path, dst: Path) -> str:
    source = Path(src)
    if not source.is_file():
        return ""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dst)
    return str(dst)


def copy_optional_tree(src: str | Path, dst: Path) -> str:
    source = Path(src)
    if not source.is_dir():
        return ""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(source, dst)
    return str(dst)


def path_for_powershell(path: Path) -> str:
    return str(path).replace("'", "''")


def write_reproduce_script(bundle_dir: Path, recipe_path: str, runner: str = "") -> str:
    if not recipe_path:
        return ""
    script_path = bundle_dir / "reproduce.ps1"
    recipe_name = Path(recipe_path).name
    default_runner = runner or str((Path.cwd() / "build" / "test_harness" / "Release" / "sggk_case_runner.exe").resolve())
    content = "\n".join(
        [
            "param(",
            f"  [string]$Runner = '{path_for_powershell(Path(default_runner))}',",
            "  [string]$Out = (Join-Path $PSScriptRoot 'repro')",
            ")",
            "",
            "$RecipeDir = Join-Path $PSScriptRoot 'recipes'",
            "$Recipe = Join-Path $RecipeDir '" + recipe_name.replace("'", "''") + "'",
            "if (!(Test-Path -LiteralPath $Runner)) {",
            "  Write-Error \"Runner not found: $Runner\"",
            "  exit 1",
            "}",
            "if (!(Test-Path -LiteralPath $Recipe)) {",
            "  Write-Error \"Recipe not found: $Recipe\"",
            "  exit 1",
            "}",
            "& $Runner --recipe $Recipe --out $Out",
            "exit $LASTEXITCODE",
            "",
        ]
    )
    write_text(script_path, content)
    return str(script_path)


def rewrite_replay_recipe_inputs(recipe_path: str, copied_inputs: dict[str, str]) -> None:
    if not recipe_path:
        return
    path = Path(recipe_path)
    try:
        recipe = read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if not isinstance(recipe, dict):
        return
    if recipe.get("target_kind") == "loaded_sgt" and copied_inputs.get("target_sgt"):
        recipe["target_source_file"] = str(Path(copied_inputs["target_sgt"]).resolve())
    if recipe.get("tool_kind") == "loaded_sgt" and copied_inputs.get("tool_sgt"):
        recipe["tool_source_file"] = str(Path(copied_inputs["tool_sgt"]).resolve())
    if recipe.get("api") in {"check_sgt", "step_import", "iges_import", "step_roundtrip", "iges_roundtrip"}:
        for key in ("source_sgt", "source_step", "source_stp", "source_iges", "source_igs"):
            if copied_inputs.get(key):
                recipe["source_file"] = str(Path(copied_inputs[key]).resolve())
                break
    write_json(path, recipe)


def find_preview(case_id: str, preview_dirs: list[str]) -> str:
    for raw_dir in preview_dirs:
        preview_dir = Path(raw_dir)
        for candidate in [preview_dir / f"{case_id}.png", preview_dir / sanitize_name(case_id) / "preview.png"]:
            if candidate.is_file():
                return str(candidate)
    return ""


def replay_by_fingerprint(replay_summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in replay_summary.get("results", []):
        if isinstance(item, dict) and as_str(item.get("fingerprint")):
            result[as_str(item["fingerprint"])] = item
    return result


def seed_by_fingerprint(triage_summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in triage_summary.get("regression_seeds", []):
        if isinstance(item, dict) and as_str(item.get("fingerprint")):
            result[as_str(item["fingerprint"])] = item
    return result


def reduction_by_fingerprint(reduction_index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in reduction_index.get("reductions", []):
        if isinstance(item, dict) and as_str(item.get("fingerprint")):
            result[as_str(item["fingerprint"])] = item
    return result


def format_ref(ref: Any) -> str:
    if not isinstance(ref, dict):
        return ""
    role = as_str(ref.get("role"))
    topo_type = as_str(ref.get("type"))
    topo_id = ref.get("id")
    local_index = ref.get("local_index")
    op = as_str(ref.get("terminal_operation"))
    op_text = f" op=`{op}`" if op else ""
    return f"{role} {topo_type}#{topo_id}[{local_index}]{op_text}"


def contact_line(candidate: Any) -> str:
    if not isinstance(candidate, dict):
        return ""
    distance = candidate.get("bbox_distance")
    distance_text = f"{distance:.8g}" if isinstance(distance, (int, float)) and not isinstance(distance, bool) else str(distance)
    return (
        f"{format_ref(candidate.get('target'))} <-> {format_ref(candidate.get('tool'))} "
        f"bbox_gap={distance_text} axis_gaps={candidate.get('axis_gaps')} "
        f"axis_overlaps={candidate.get('axis_overlaps')}"
    )


def oracle_detail_line(detail: Any) -> str:
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
        pieces.append(f"`{check_id}`")
    if detail.get("check_kind"):
        pieces.append(f"kind={detail.get('check_kind')}")
    if detail.get("actual_extreme") is None and (detail.get("expected") is not None or detail.get("actual") is not None):
        pieces.append(f"expected={detail.get('expected')} actual={detail.get('actual')}")
    if detail.get("actual_extreme") is not None:
        pieces.append(
            f"axis={detail.get('axis')} side={detail.get('side')} "
            f"actual_extreme={detail.get('actual_extreme')} probe={detail.get('probe_coordinate')}"
        )
    if detail.get("success") is not None:
        pieces.append(f"success={detail.get('success')}")
    for key in ("role", "role_a", "role_b", "body_index", "body_index_a", "body_index_b", "face_index"):
        if detail.get(key) is not None:
            pieces.append(f"{key}={detail.get(key)}")
    for key in ("actual_face", "topology_a", "topology_b", "target"):
        if detail.get(key):
            pieces.append(f"{key}={detail.get(key)}")
    for key in ("uv", "point", "point_a", "point_b", "sub_clashes", "metric_failures", "debug_geometry"):
        if detail.get(key):
            pieces.append(f"{key}={detail.get(key)}")
    return "; ".join(pieces)


def ref_summary(ref: Any) -> dict[str, Any]:
    if not isinstance(ref, dict):
        return {}
    return {
        "role": ref.get("role"),
        "type": ref.get("type"),
        "id": ref.get("id"),
        "local_index": ref.get("local_index"),
        "terminal_operation": ref.get("terminal_operation"),
        "operation_chain": ref.get("operation_chain", []),
    }


def build_localization_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    contacts = manifest.get("input_contact_candidates") if isinstance(manifest.get("input_contact_candidates"), list) else []
    first_contact = contacts[0] if contacts and isinstance(contacts[0], dict) else {}
    copied = manifest.get("copied") if isinstance(manifest.get("copied"), dict) else {}
    return {
        "fingerprint": manifest.get("fingerprint"),
        "representative_case_id": manifest.get("representative_case_id"),
        "api": manifest.get("api"),
        "status": manifest.get("status", {}),
        "dsl": manifest.get("dsl", {}),
        "replay": manifest.get("replay", {}),
        "primary_contact": {
            "target": ref_summary(first_contact.get("target") if isinstance(first_contact, dict) else {}),
            "tool": ref_summary(first_contact.get("tool") if isinstance(first_contact, dict) else {}),
            "bbox_distance": first_contact.get("bbox_distance") if isinstance(first_contact, dict) else None,
            "axis_gaps": first_contact.get("axis_gaps") if isinstance(first_contact, dict) else [],
            "axis_overlaps": first_contact.get("axis_overlaps") if isinstance(first_contact, dict) else [],
            "target_locator": first_contact.get("target_locator") if isinstance(first_contact, dict) else {},
            "tool_locator": first_contact.get("tool_locator") if isinstance(first_contact, dict) else {},
        },
        "contact_candidates": contacts[:8],
        "validation_oracle_details": manifest.get("validation_oracle_details", [])[:8],
        "roundtrip_oracle_details": manifest.get("roundtrip_oracle_details", [])[:8],
        "localized_inputs": manifest.get("localized_inputs", [])[:8],
        "recipes": copied.get("recipes", {}) if isinstance(copied.get("recipes"), dict) else {},
        "inputs": copied.get("inputs", {}) if isinstance(copied.get("inputs"), dict) else {},
    }


def copy_bundle_files(
    bundle_dir: Path,
    group: dict[str, Any],
    failure: dict[str, Any],
    seed: dict[str, Any],
    replay: dict[str, Any],
    reduction: dict[str, Any],
    preview_dirs: list[str],
    include_full_artifact: bool,
) -> dict[str, Any]:
    copied: dict[str, Any] = {"reports": {}, "inputs": {}, "recipes": {}}
    case_id = as_str(group.get("representative_case_id")) or as_str(failure.get("case_id"))
    case_dir = Path(as_str(group.get("representative_case_dir")) or as_str(failure.get("case_dir")))

    recipe_paths = group.get("recipe_paths") if isinstance(group.get("recipe_paths"), list) else []
    if recipe_paths:
        copied["recipes"]["original"] = copy_file(as_str(recipe_paths[0]), bundle_dir / "recipes" / "original_recipe.json")

    artifact_inputs = seed.get("artifact_inputs") if isinstance(seed.get("artifact_inputs"), dict) else {}
    for key, src in artifact_inputs.items():
        if isinstance(src, str) and src:
            copied["inputs"][key] = copy_file(src, bundle_dir / "input" / Path(src).name)

    attempts = replay.get("attempts") if isinstance(replay.get("attempts"), list) else []
    if attempts:
        first_attempt = attempts[0] if isinstance(attempts[0], dict) else {}
        copied["recipes"]["replay"] = copy_file(as_str(first_attempt.get("recipe")), bundle_dir / "recipes" / "replay_recipe.json")
        rewrite_replay_recipe_inputs(copied["recipes"]["replay"], copied["inputs"])

    reduced_recipe = as_str(reduction.get("reduced_recipe"))
    if reduced_recipe:
        copied["recipes"]["reduced"] = copy_file(reduced_recipe, bundle_dir / "recipes" / "reduced_recipe.json")
        rewrite_replay_recipe_inputs(copied["recipes"]["reduced"], copied["inputs"])

    report_dir = case_dir / "report"
    for name in KEY_REPORTS:
        copied_path = copy_file(report_dir / name, bundle_dir / "report" / name)
        if copied_path:
            copied["reports"][name] = copied_path

    preview = find_preview(case_id, preview_dirs)
    if preview:
        copied["preview"] = copy_file(preview, bundle_dir / "preview.png")

    debug_geometry = copy_optional_tree(case_dir / "debug_geometry", bundle_dir / "debug_geometry")
    if debug_geometry:
        copied["debug_geometry"] = debug_geometry

    if include_full_artifact:
        copied["full_artifact"] = copy_optional_tree(case_dir, bundle_dir / "full_artifact")

    recipes = copied.get("recipes") if isinstance(copied.get("recipes"), dict) else {}
    copied["reproduce_script"] = write_reproduce_script(
        bundle_dir,
        as_str(recipes.get("reduced") or recipes.get("replay") or recipes.get("original")),
    )

    return copied


def build_manifest(
    group: dict[str, Any],
    failure: dict[str, Any],
    seed: dict[str, Any],
    replay: dict[str, Any],
    reduction: dict[str, Any],
    copied: dict[str, Any],
) -> dict[str, Any]:
    attempts = replay.get("attempts") if isinstance(replay.get("attempts"), list) else []
    status = failure.get("status") if isinstance(failure.get("status"), dict) else {}
    runner = failure.get("corpus") if isinstance(failure.get("corpus"), dict) else {}
    return {
        "fingerprint": group.get("fingerprint"),
        "representative_case_id": group.get("representative_case_id"),
        "representative_case_dir": group.get("representative_case_dir"),
        "api": failure.get("api"),
        "reasons": group.get("reasons", []),
        "status": status,
        "failure_signature": seed.get("failure_signature", {}),
        "runner": {
            "summary_path": runner.get("summary_path"),
            "returncode": runner.get("returncode"),
            "timed_out": runner.get("timed_out", False),
            "elapsed_seconds": runner.get("elapsed_seconds"),
            "stderr": runner.get("stderr", ""),
        },
        "dsl": failure.get("dsl", {}),
        "replay": {
            "status": replay.get("status"),
            "attempt_count": replay.get("attempt_count", len(attempts)),
            "returncodes": [item.get("returncode") for item in attempts if isinstance(item, dict)],
            "first_artifact_dir": as_str(attempts[0].get("artifact_dir")) if attempts and isinstance(attempts[0], dict) else "",
        },
        "reduction": {
            "status": reduction.get("status"),
            "accepted_reductions": reduction.get("accepted_reductions"),
            "trials": reduction.get("trials"),
            "summary_path": reduction.get("summary_path"),
            "final_artifact_dir": reduction.get("final_artifact_dir"),
        },
        "recipe_paths": group.get("recipe_paths", []),
        "case_dirs": group.get("case_dirs", []),
        "artifact_inputs": seed.get("artifact_inputs", {}),
        "validation_failures": group.get("representative_validation_failures", [])[:8],
        "validation_oracle_details": group.get("representative_validation_oracle_details", [])[:8],
        "roundtrip_failures": group.get("representative_roundtrip_failures", [])[:8],
        "roundtrip_oracle_details": group.get("representative_roundtrip_oracle_details", [])[:8],
        "localized_inputs": group.get("representative_localized_inputs", [])[:8],
        "input_contact_candidates": group.get("representative_input_contact_candidates", [])[:8],
        "copied": copied,
    }


def write_bug_report(manifest: dict[str, Any], path: Path) -> None:
    status = manifest.get("status") if isinstance(manifest.get("status"), dict) else {}
    runner = manifest.get("runner") if isinstance(manifest.get("runner"), dict) else {}
    replay = manifest.get("replay") if isinstance(manifest.get("replay"), dict) else {}
    dsl = manifest.get("dsl") if isinstance(manifest.get("dsl"), dict) else {}
    copied = manifest.get("copied") if isinstance(manifest.get("copied"), dict) else {}
    lines: list[str] = [
        f"# {manifest.get('representative_case_id')}",
        "",
        f"- Fingerprint: `{manifest.get('fingerprint')}`",
        f"- API: `{manifest.get('api')}`",
        f"- Reasons: {', '.join(manifest.get('reasons', []))}",
        f"- SDK status: succeeded={status.get('succeeded')} error_code={status.get('error_code')} error_message=`{status.get('error_message', '')}`",
        f"- Runner: returncode={runner.get('returncode')} timed_out={runner.get('timed_out')} elapsed={runner.get('elapsed_seconds')}",
        f"- Replay: `{replay.get('status', '')}` attempts={replay.get('attempt_count', 0)} returncodes={replay.get('returncodes', [])}",
    ]
    if dsl:
        lines.extend(
            [
                f"- DSL: source=`{dsl.get('source', '')}` case=`{dsl.get('case_id', '')}` variant=`{dsl.get('variant', '')}`",
                f"- Hypothesis: {dsl.get('hypothesis', '')}",
            ]
        )
    lines.append("")
    lines.append("## Localization")
    lines.append("")
    contacts = manifest.get("input_contact_candidates", [])
    if contacts:
        for candidate in contacts[:8]:
            line = contact_line(candidate)
            if line:
                lines.append(f"- {line}")
    else:
        lines.append("- No contact candidates recorded.")
    lines.append("")
    details = manifest.get("validation_oracle_details") if isinstance(manifest.get("validation_oracle_details"), list) else []
    if details:
        lines.append("## Validation Oracle Details")
        lines.append("")
        for detail in details[:8]:
            line = oracle_detail_line(detail)
            if line:
                lines.append(f"- {line}")
        lines.append("")
    roundtrip_details = manifest.get("roundtrip_oracle_details") if isinstance(manifest.get("roundtrip_oracle_details"), list) else []
    if roundtrip_details:
        lines.append("## Roundtrip Oracle Details")
        lines.append("")
        for detail in roundtrip_details[:8]:
            line = oracle_detail_line(detail)
            if line:
                lines.append(f"- {line}")
        lines.append("")
    lines.append("## Files")
    lines.append("")
    recipes = copied.get("recipes") if isinstance(copied.get("recipes"), dict) else {}
    for label, copied_path in recipes.items():
        if copied_path:
            lines.append(f"- {label} recipe: `{copied_path}`")
    inputs = copied.get("inputs") if isinstance(copied.get("inputs"), dict) else {}
    for label, copied_path in inputs.items():
        if copied_path:
            lines.append(f"- {label}: `{copied_path}`")
    if copied.get("preview"):
        lines.append(f"- preview: `{copied.get('preview')}`")
    if copied.get("debug_geometry"):
        lines.append(f"- debug geometry: `{copied.get('debug_geometry')}`")
    if copied.get("localization_summary"):
        lines.append(f"- localization summary: `{copied.get('localization_summary')}`")
    if copied.get("reproduce_script"):
        lines.append(f"- reproduce script: `{copied.get('reproduce_script')}`")
    if copied.get("zip"):
        lines.append(f"- zip: `{copied.get('zip')}`")
    if copied.get("full_artifact"):
        lines.append(f"- full artifact: `{copied.get('full_artifact')}`")
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    replay_recipe = recipes.get("reduced") or recipes.get("replay")
    original_recipe = recipes.get("original")
    recipe = replay_recipe or original_recipe
    if recipe:
        lines.append("```powershell")
        if copied.get("reproduce_script"):
            lines.append(f"& '{copied.get('reproduce_script')}'")
        else:
            lines.append(".\\build\\test_harness\\Release\\sggk_case_runner.exe `")
            lines.append(f"  --recipe {recipe} `")
            lines.append("  --out .\\artifacts\\repro")
        lines.append("```")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_index_report(index: list[dict[str, Any]], path: Path) -> None:
    lines = ["# SGGK Failure Bundles", ""]
    lines.append(f"- Bundles: {len(index)}")
    lines.append("")
    for item in index:
        lines.append(f"## {item['representative_case_id']}")
        lines.append("")
        lines.append(f"- Fingerprint: `{item['fingerprint']}`")
        lines.append(f"- Replay: `{item.get('replay_status', '')}`")
        lines.append(f"- Bundle: `{item['bundle_dir']}`")
        lines.append(f"- Report: `{item['bug_report']}`")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.limit < 0:
        print("--limit must be >= 0")
        return 1

    triage_path = resolve_summary_path(args.triage, "triage_summary.json")
    triage = read_json(triage_path)
    if not isinstance(triage, dict):
        print(f"triage summary root must be object: {triage_path}")
        return 1

    replay: dict[str, Any] = {}
    if args.replay:
        replay_path = resolve_summary_path(args.replay, "replay_summary.json")
        replay_value = read_json(replay_path)
        if isinstance(replay_value, dict):
            replay = replay_value

    reductions: dict[str, Any] = {}
    if args.reductions:
        reductions_path = resolve_summary_path(args.reductions, "reduction_index.json")
        reductions_value = read_json(reductions_path)
        if isinstance(reductions_value, dict):
            reductions = reductions_value

    replay_lookup = replay_by_fingerprint(replay)
    reduction_lookup = reduction_by_fingerprint(reductions)
    seed_lookup = seed_by_fingerprint(triage)
    failures = triage.get("failures") if isinstance(triage.get("failures"), list) else []
    groups = triage.get("failure_groups") if isinstance(triage.get("failure_groups"), list) else []
    if args.limit:
        groups = groups[: args.limit]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    index: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        fingerprint = as_str(group.get("fingerprint"))
        case_id = as_str(group.get("representative_case_id"))
        failure = first_dict(failures, "fingerprint", fingerprint) or first_dict(failures, "case_id", case_id)
        seed = seed_lookup.get(fingerprint, {})
        replay_result = replay_lookup.get(fingerprint, {})
        reduction_result = reduction_lookup.get(fingerprint, {})
        bundle_dir = out_dir / f"{sanitize_name(fingerprint)}_{sanitize_name(case_id)}"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        copied = copy_bundle_files(
            bundle_dir,
            group,
            failure,
            seed,
            replay_result,
            reduction_result,
            args.preview_dir,
            args.include_full_artifact,
        )
        manifest = build_manifest(group, failure, seed, replay_result, reduction_result, copied)
        manifest_path = bundle_dir / "bundle_manifest.json"
        report_path = bundle_dir / "bug_report.md"
        localization_path = bundle_dir / "localization_summary.json"
        copied["localization_summary"] = str(localization_path)
        manifest["copied"] = copied
        write_json(manifest_path, manifest)
        write_json(localization_path, build_localization_summary(manifest))
        write_bug_report(manifest, report_path)
        zip_path = ""
        if args.zip:
            zip_path = shutil.make_archive(str(bundle_dir), "zip", root_dir=bundle_dir)
            copied["zip"] = zip_path
            manifest["copied"] = copied
            write_json(manifest_path, manifest)
            write_bug_report(manifest, report_path)
        index.append(
            {
                "fingerprint": fingerprint,
                "representative_case_id": case_id,
                "replay_status": replay_result.get("status", ""),
                "bundle_dir": str(bundle_dir),
                "bundle_manifest": str(manifest_path),
                "localization_summary": str(localization_path),
                "bug_report": str(report_path),
                "zip": zip_path,
            }
        )
        print(f"bundle={bundle_dir}")

    write_json(
        out_dir / "bundle_index.json",
        {"triage": str(triage_path), "replay": args.replay or "", "reductions": args.reductions or "", "bundles": index},
    )
    write_index_report(index, out_dir / "bundle_report.md")
    print(f"index={out_dir / 'bundle_index.json'}")
    print(f"report={out_dir / 'bundle_report.md'}")
    print(f"bundles={len(index)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
