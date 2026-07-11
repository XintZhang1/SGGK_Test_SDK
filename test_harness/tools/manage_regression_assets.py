#!/usr/bin/env python3
"""Create and compare compact regression assets from SGGK campaign runs.

The asset pack is intentionally local-only. It keeps replayable recipes and
compact triage evidence, while referring to the committed harness code by git
commit instead of copying SDK/build artifacts.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_UNSUPPORTED_PATTERNS = [
    "Not accepted type!",
    "wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="Materialize a compact regression asset pack from a run")
    snapshot.add_argument("--campaign", required=True, help="Campaign/run root, abc summary, recipe_summary.json, corpus_summary.json, or triage dir")
    snapshot.add_argument("--out", required=True, help="Output directory for the asset pack")
    snapshot.add_argument("--asset-id", default="", help="Stable asset id; defaults to output directory name")
    snapshot.add_argument("--sdk-version", default="", help="SDK/kernel version label for the baseline")
    snapshot.add_argument("--dataset-label", default="", help="Dataset label/path used by this run")
    snapshot.add_argument("--source-type", default="", help="Registered provenance source type from interface_capabilities.json")
    snapshot.add_argument("--source-label", default="", help="Human-readable source/run label")
    snapshot.add_argument("--model", default="", help="Model label that produced the saved JSON, if applicable")
    snapshot.add_argument("--form", default="", help="Interface form path or id that led to this asset")
    snapshot.add_argument("--prompt-pack", default="", help="Prompt pack or prompt path used to produce the saved JSON")
    snapshot.add_argument("--model-output", default="", help="Saved model-output JSON path used for the run")
    snapshot.add_argument("--fingerprint", action="append", default=[], help="Known bug/failure fingerprint associated with this asset")
    snapshot.add_argument("--max-cases", type=int, default=5000, help="Maximum recipes to copy; 0 means no cap")
    snapshot.add_argument("--pass-sample", type=int, default=2000, help="Maximum passed cases to keep; 0 keeps none")
    snapshot.add_argument(
        "--unsupported-pattern",
        action="append",
        default=[],
        help="Additional literal text that means this failure is explicitly unsupported.",
    )
    snapshot.add_argument(
        "--no-default-unsupported-patterns",
        action="store_true",
        help="Do not apply the built-in SGGK unsupported message patterns.",
    )

    compare = subparsers.add_parser("compare", help="Compare a new replay/run against a baseline asset pack")
    compare.add_argument("--asset", required=True, help="Asset pack directory or asset_manifest.json")
    compare.add_argument("--new-run", required=True, help="New run root, campaign root, recipe_summary.json, or corpus_summary.json")
    compare.add_argument("--new-triage", default="", help="New triage root or triage_summary.json")
    compare.add_argument("--out", required=True, help="Output directory for regression_comparison.json/md")
    compare.add_argument("--new-sdk-version", default="", help="SDK/kernel version label for the new run")
    compare.add_argument(
        "--unsupported-pattern",
        action="append",
        default=[],
        help="Additional unsupported-message pattern for the new run.",
    )

    annotate = subparsers.add_parser("annotate", help="Add or update provenance metadata on an existing asset pack")
    annotate.add_argument("--asset", required=True, help="Asset pack directory or asset_manifest.json")
    annotate.add_argument("--source-type", default="", help="Registered provenance source type from interface_capabilities.json")
    annotate.add_argument("--source-label", default="", help="Human-readable source/run label")
    annotate.add_argument("--model", default="", help="Model label that produced the saved JSON, if applicable")
    annotate.add_argument("--form", default="", help="Interface form path or id that led to this asset")
    annotate.add_argument("--prompt-pack", default="", help="Prompt pack or prompt path used to produce the saved JSON")
    annotate.add_argument("--model-output", default="", help="Saved model-output JSON path used for the run")
    annotate.add_argument("--fingerprint", action="append", default=[], help="Known bug/failure fingerprint associated with this asset")
    annotate.add_argument("--note", action="append", default=[], help="Human-reviewed provenance note")

    plan = subparsers.add_parser("plan-replay", help="Write a deterministic replay/compare plan for an asset")
    plan.add_argument("--asset", required=True, help="Asset pack directory or asset_manifest.json")
    plan.add_argument("--out", required=True, help="Output directory for replay_plan.json/md")
    plan.add_argument("--runner", default=".\\build\\test_harness\\Release\\sggk_case_runner.exe", help="Runner path for the replay command")
    plan.add_argument("--replay-out", default="", help="Replay run output directory; defaults under --out")
    plan.add_argument("--triage-out", default="", help="Replay triage output directory; defaults under --out")
    plan.add_argument("--compare-out", default="", help="Regression compare output directory; defaults under --out")
    plan.add_argument("--new-sdk-version", default="", help="SDK/kernel version label for replay comparison")
    plan.add_argument("--jobs", type=int, default=1)
    plan.add_argument("--timeout", type=float, default=180.0)
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_json(path: Path) -> Any | None:
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def clean_string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str) and item]


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: Any, length: int = 16) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:length]


def normalize_key(raw: Any) -> str:
    text = as_str(raw)
    if not text:
        return ""
    return str(Path(text).resolve()).lower()


def relpath(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


def git_value(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def read_case_id(recipe_path: Path) -> str:
    recipe = load_json(recipe_path)
    if isinstance(recipe, dict) and as_str(recipe.get("case_id")):
        return as_str(recipe["case_id"])
    return recipe_path.stem


def normalize_summary_path(raw: str, preferred_name: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        return path / preferred_name
    return path


def first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def campaign_paths(raw: str) -> dict[str, str]:
    path = Path(raw)
    root = path if path.is_dir() else path.parent
    abc_summary_path = path if path.name == "abc_boolean_mass_recut_summary.json" else root / "abc_boolean_mass_recut_summary.json"
    abc_summary = load_json(abc_summary_path)
    if isinstance(abc_summary, dict):
        paths = as_dict(abc_summary.get("paths"))
        return {
            "campaign_root": str(abc_summary_path.parent),
            "campaign_summary": str(abc_summary_path),
            "recipe_summary": as_str(paths.get("run_summary")),
            "triage_summary": as_str(paths.get("triage_summary")),
            "recipe_manifest": as_str(paths.get("recipe_manifest")),
            "bug_report": as_str(paths.get("bug_report")),
            "recipes": as_str(paths.get("recipes")),
        }

    recipe_summary = first_existing(
        [
            path if path.name == "recipe_summary.json" else root / "recipe_summary.json",
            path if path.name == "corpus_summary.json" else root / "corpus_summary.json",
            root / "run" / "recipe_summary.json",
            root / "run" / "corpus_summary.json",
            root / "runs" / "corpus" / "corpus_summary.json",
        ]
    )
    triage_summary = first_existing(
        [
            path if path.name == "triage_summary.json" else root / "triage_summary.json",
            root / "triage" / "triage_summary.json",
        ]
    )
    if recipe_summary is None and root.is_dir():
        matches = sorted(
            [*root.rglob("recipe_summary.json"), *root.rglob("corpus_summary.json")],
            key=lambda item: str(item).lower(),
        )
        recipe_summary = matches[0] if matches else None
    if triage_summary is None and root.is_dir():
        matches = sorted(root.rglob("triage_summary.json"), key=lambda item: str(item).lower())
        triage_summary = matches[0] if matches else None
    return {
        "campaign_root": str(root),
        "campaign_summary": "",
            "recipe_summary": str(recipe_summary) if recipe_summary else "",
        "triage_summary": str(triage_summary) if triage_summary else "",
        "recipe_manifest": str(root / "recipes_manifest.json") if (root / "recipes_manifest.json").is_file() else "",
        "bug_report": str(root / "abc_boolean_mass_recut_bug_report.md") if (root / "abc_boolean_mass_recut_bug_report.md").is_file() else "",
        "recipes": str(root / "recipes") if (root / "recipes").is_dir() else "",
    }


def group_text(group: dict[str, Any]) -> str:
    parts = [
        group.get("fingerprint"),
        group.get("reasons"),
        group.get("representative_runner"),
        group.get("fingerprint_components"),
        group.get("representative_validation_failures"),
        group.get("representative_roundtrip_failures"),
    ]
    return json.dumps(parts, ensure_ascii=False, sort_keys=True).lower()


def unsupported_patterns(*, include_defaults: bool, extra: list[str]) -> list[str]:
    patterns = list(DEFAULT_UNSUPPORTED_PATTERNS) if include_defaults else []
    patterns.extend(extra)
    return [item for item in patterns if item]


def is_unsupported_group(group: dict[str, Any], patterns: list[str]) -> bool:
    text = group_text(group)
    return any(pattern.lower() in text for pattern in patterns)


def group_by_recipe(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for group in groups:
        for recipe in as_list(group.get("recipe_paths")):
            key = normalize_key(recipe)
            if key:
                lookup[key] = group
    return lookup


def group_by_case_id(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for group in groups:
        for case_id in as_list(group.get("case_ids")):
            if as_str(case_id):
                lookup[as_str(case_id)] = group
        representative = as_str(group.get("representative_case_id"))
        if representative:
            lookup.setdefault(representative, group)
    return lookup


def load_failure_groups(triage_summary_path: str) -> list[dict[str, Any]]:
    triage = load_json(Path(triage_summary_path)) if triage_summary_path else None
    if not isinstance(triage, dict):
        return []
    return [item for item in as_list(triage.get("failure_groups")) if isinstance(item, dict)]


def result_recipe(result: dict[str, Any]) -> str:
    return as_str(result.get("recipe"))


def result_case_id(result: dict[str, Any]) -> str:
    return as_str(result.get("case_id"))


def result_failed(result: dict[str, Any]) -> bool:
    return bool(result.get("timed_out")) or as_int(result.get("returncode")) != 0


def even_sample(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[0]]
    result: list[dict[str, Any]] = []
    last = len(items) - 1
    for index in range(limit):
        result.append(items[round(index * last / (limit - 1))])
    return result


def role_ref_label(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return "{role} {typ}#{idx}[{local}]".format(
        role=as_str(value.get("role")),
        typ=as_str(value.get("type")),
        idx=value.get("id"),
        local=value.get("local_index"),
    ).strip()


def contact_label(contact: Any) -> str:
    if not isinstance(contact, dict):
        return ""
    target = role_ref_label(contact.get("target"))
    tool = role_ref_label(contact.get("tool"))
    if not target and not tool:
        return ""
    return f"{target} <-> {tool} gap={contact.get('bbox_distance')} overlaps={contact.get('axis_overlaps')}"


def localized_label(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    pieces = [
        role_ref_label(item),
        f"count={item.get('count')}",
    ]
    if as_str(item.get("terminal_operation")):
        pieces.append(f"op={as_str(item.get('terminal_operation'))}")
    if item.get("track_type_counts"):
        pieces.append(f"track={item.get('track_type_counts')}")
    return " ".join(piece for piece in pieces if piece)


def track_digest(group: dict[str, Any]) -> dict[str, Any]:
    topo_summary = as_dict(group.get("representative_topo_track_summary"))
    localized = [item for item in as_list(group.get("representative_localized_inputs")) if isinstance(item, dict)]
    contacts = [item for item in as_list(group.get("representative_input_contact_candidates")) if isinstance(item, dict)]
    if localized:
        return {
            "source": "topo_track",
            "status": "localized",
            "topo_track_summary": topo_summary,
            "items": localized[:5],
            "summary": [localized_label(item) for item in localized[:3] if localized_label(item)],
        }
    if contacts:
        return {
            "source": "input_bbox_contact_fallback",
            "status": "contact_candidates",
            "topo_track_summary": topo_summary,
            "items": contacts[:5],
            "summary": [contact_label(item) for item in contacts[:3] if contact_label(item)],
        }
    if topo_summary:
        return {
            "source": "topo_track",
            "status": "no_localized_inputs",
            "topo_track_summary": topo_summary,
            "summary": [as_str(topo_summary.get("reason"))] if as_str(topo_summary.get("reason")) else [],
        }
    return {"source": "none", "status": "unavailable", "summary": []}


def copy_recipe(original: Path, dest_dir: Path, index: int) -> Path:
    case_id = read_case_id(original)
    safe_stem = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in case_id).strip("._") or original.stem
    dest = dest_dir / f"{index:06d}_{safe_stem}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original, dest)
    return dest


def select_results(results: list[dict[str, Any]], groups_for_recipe: dict[str, dict[str, Any]], max_cases: int, pass_sample: int) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    for result in results:
        recipe = result_recipe(result)
        grouped_failure = bool(recipe and normalize_key(recipe) in groups_for_recipe)
        if result_failed(result) or grouped_failure:
            failed.append(result)
        else:
            passed.append(result)
    sampled_passed = even_sample(passed, pass_sample) if pass_sample else []
    selected = failed + sampled_passed
    if max_cases > 0 and len(selected) > max_cases:
        selected = failed + sampled_passed[: max(0, max_cases - len(failed))]
    return selected


def source_code_refs() -> dict[str, Any]:
    return {
        "git_branch": git_value(["branch", "--show-current"]),
        "git_commit": git_value(["rev-parse", "HEAD"]),
        "git_commit_short": git_value(["rev-parse", "--short=12", "HEAD"]),
        "tools": [
            "test_harness/tools/discover_corpus.py",
            "test_harness/tools/profile_cad_features.py",
            "test_harness/tools/run_corpus.py",
            "test_harness/tools/run_abc_boolean_mass_recut.py",
            "test_harness/tools/generate_corpus_recut_matrix.py",
            "test_harness/tools/run_recipes.py",
            "test_harness/tools/triage_artifacts.py",
            "test_harness/tools/manage_regression_assets.py",
        ],
        "forms": [
            "test_harness/forms/interface_distillation/02_step_import_abc_complex.json",
            "test_harness/forms/interface_distillation/10_step_roundtrip_imported_sgt.json",
            "test_harness/forms/interface_distillation/13_check_sgt_replay.json",
            "test_harness/forms/interface_distillation/14_iges_import_abc_complex.json",
            "test_harness/forms/interface_distillation/15_boolean_abc_mass_recut.json",
        ],
    }


def provenance_from_args(args: argparse.Namespace, *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    fingerprints = clean_string_list([*as_list(existing.get("fingerprints")), *getattr(args, "fingerprint", [])])
    result = {
        "schema_version": 1,
        "updated_at": now_iso_like(),
        "source_type": getattr(args, "source_type", "") or existing.get("source_type", ""),
        "source_label": getattr(args, "source_label", "") or existing.get("source_label", ""),
        "model": getattr(args, "model", "") or existing.get("model", ""),
        "form": getattr(args, "form", "") or existing.get("form", ""),
        "prompt_pack": getattr(args, "prompt_pack", "") or existing.get("prompt_pack", ""),
        "model_output": getattr(args, "model_output", "") or existing.get("model_output", ""),
        "fingerprints": sorted(dict.fromkeys(fingerprints)),
        "notes": clean_string_list([*as_list(existing.get("notes")), *getattr(args, "note", [])]),
        "boundary": {
            "model_calls": False,
            "direct_api_calls": False,
            "production_flow": "saved_json_or_run_artifacts_to_regression_asset",
        },
    }
    return {key: value for key, value in result.items() if value not in ("", [], {})}


def build_bug_registry(asset_id: str, cases: list[dict[str, Any]], groups: dict[str, dict[str, Any]]) -> dict[str, Any]:
    bugs: dict[str, dict[str, Any]] = {}
    for case in cases:
        if case.get("baseline_status") != "candidate_bug":
            continue
        fingerprint = as_str(case.get("baseline_fingerprint"))
        if not fingerprint or fingerprint in bugs:
            continue
        group = groups.get(fingerprint, {})
        runner = as_dict(group.get("representative_runner"))
        bugs[fingerprint] = {
            "fingerprint": fingerprint,
            "bug_id": f"{asset_id}_{fingerprint}",
            "representative_case_id": group.get("representative_case_id") or case.get("case_id"),
            "api": (as_list(group.get("apis")) or [case.get("api", "")])[0],
            "replay_status": "stable_failure",
            "reasons": group.get("reasons", []),
            "validation_failures": group.get("representative_validation_failures", []),
            "roundtrip_failures": group.get("representative_roundtrip_failures", []),
            "expected": {
                "returncode": runner.get("returncode", case.get("baseline_returncode")),
                "runner_timeout": bool(runner.get("timed_out", case.get("baseline_timed_out", False))),
            },
            "topo_track_policy": "diagnostic_when_modeling_fails",
            "topo_track_required": False,
            "track": case.get("track", {}),
            "paths": {
                "replay_recipe": case.get("asset_recipe"),
                "original_recipe": case.get("original_recipe"),
                "representative_case_dir": group.get("representative_case_dir"),
            },
        }
    items = sorted(bugs.values(), key=lambda item: as_str(item.get("fingerprint")))
    return {
        "generated_at": now_iso_like(),
        "asset_id": asset_id,
        "total": len(items),
        "by_api": dict(sorted(Counter(as_str(item.get("api")) or "unknown" for item in items).items())),
        "bugs": items,
    }


def snapshot_report(manifest: dict[str, Any], bug_registry: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# SGGK Regression Asset")
    lines.append("")
    lines.append(f"- Asset: `{manifest.get('asset_id')}`")
    lines.append(f"- Generated: `{manifest.get('generated_at')}`")
    lines.append(f"- SDK baseline: `{manifest.get('sdk_version')}`")
    lines.append(f"- Dataset: `{manifest.get('dataset_label')}`")
    provenance = as_dict(manifest.get("provenance"))
    if provenance:
        lines.append(f"- Source type: `{provenance.get('source_type', '')}`")
        lines.append(f"- Model: `{provenance.get('model', '')}`")
        lines.append(f"- Form: `{provenance.get('form', '')}`")
        lines.append(f"- Prompt/model output: `{provenance.get('prompt_pack', '')}` / `{provenance.get('model_output', '')}`")
        lines.append(f"- Known fingerprints: `{provenance.get('fingerprints', [])}`")
    lines.append(f"- Campaign root: `{manifest.get('campaign', {}).get('campaign_root')}`")
    lines.append(f"- Source commit: `{manifest.get('source_code', {}).get('git_commit_short')}`")
    lines.append(f"- Preserved cases: `{manifest.get('case_count')}`")
    lines.append(f"- Baseline status counts: `{manifest.get('baseline_status_counts')}`")
    lines.append(f"- Candidate bug fingerprints: `{bug_registry.get('total')}`")
    lines.append("")
    lines.append("## Replay")
    lines.append("")
    lines.append("Run the saved cases against a newer SDK build:")
    lines.append("")
    lines.append("```powershell")
    lines.append("python .\\test_harness\\tools\\run_recipes.py `")
    lines.append("  --runner .\\build\\test_harness\\Release\\sggk_case_runner.exe `")
    lines.append(f"  --recipe-list {manifest.get('paths', {}).get('regression_recipe_list')} `")
    lines.append("  --out .\\artifacts\\regression_replay\\run `")
    lines.append("  --triage-out .\\artifacts\\regression_replay\\triage `")
    lines.append("  --jobs 1 `")
    lines.append("  --timeout 180")
    lines.append("```")
    lines.append("")
    lines.append("Then compare:")
    lines.append("")
    lines.append("```powershell")
    lines.append("python .\\test_harness\\tools\\manage_regression_assets.py compare `")
    lines.append(f"  --asset {manifest.get('paths', {}).get('asset_manifest')} `")
    lines.append("  --new-run .\\artifacts\\regression_replay\\run\\recipe_summary.json `")
    lines.append("  --new-triage .\\artifacts\\regression_replay\\triage\\triage_summary.json `")
    lines.append("  --out .\\artifacts\\regression_compare")
    lines.append("```")
    lines.append("")
    lines.append("## Candidate Bugs")
    lines.append("")
    if not bug_registry.get("bugs"):
        lines.append("No candidate bugs were preserved in this asset.")
    for bug in as_list(bug_registry.get("bugs")):
        if not isinstance(bug, dict):
            continue
        track = as_dict(bug.get("track"))
        lines.append(f"### {bug.get('fingerprint')}")
        lines.append("")
        lines.append(f"- Case: `{bug.get('representative_case_id')}`")
        lines.append(f"- Reasons: `{', '.join(as_list(bug.get('reasons')))}`")
        lines.append(f"- Replay recipe: `{as_dict(bug.get('paths')).get('replay_recipe')}`")
        lines.append(f"- Track: `{track.get('source')}` / `{track.get('status')}`")
        for item in as_list(track.get("summary"))[:3]:
            lines.append(f"  - {item}")
        lines.append("")
    return "\n".join(lines) + "\n"


def run_snapshot(args: argparse.Namespace) -> int:
    paths = campaign_paths(args.campaign)
    recipe_summary_path = Path(paths["recipe_summary"]) if paths.get("recipe_summary") else None
    if recipe_summary_path is None or not recipe_summary_path.is_file():
        print(f"recipe_summary.json not found for campaign: {args.campaign}")
        return 1
    recipe_summary = load_json(recipe_summary_path)
    if not isinstance(recipe_summary, dict):
        print(f"invalid recipe summary: {recipe_summary_path}")
        return 1
    triage_summary_path = paths.get("triage_summary", "")
    groups = load_failure_groups(triage_summary_path)
    patterns = unsupported_patterns(include_defaults=not args.no_default_unsupported_patterns, extra=args.unsupported_pattern)
    groups_for_recipe = group_by_recipe(groups)
    groups_by_fingerprint = {as_str(group.get("fingerprint")): group for group in groups if as_str(group.get("fingerprint"))}
    selected = select_results(
        [item for item in as_list(recipe_summary.get("results")) if isinstance(item, dict)],
        groups_for_recipe,
        args.max_cases,
        args.pass_sample,
    )

    out_dir = Path(args.out).resolve()
    asset_id = args.asset_id or out_dir.name
    recipe_out = out_dir / "recipes" / "regression_cases"
    cases: list[dict[str, Any]] = []
    for index, result in enumerate(selected, start=1):
        original_recipe_text = result_recipe(result)
        original_recipe = Path(original_recipe_text)
        if not original_recipe.is_file():
            continue
        copied = copy_recipe(original_recipe, recipe_out, index)
        group = groups_for_recipe.get(normalize_key(original_recipe_text), {})
        baseline_status = "passed"
        if group:
            baseline_status = "known_unsupported" if is_unsupported_group(group, patterns) else "candidate_bug"
        elif result_failed(result):
            baseline_status = "failed_unclassified"
        case_id = result_case_id(result) or read_case_id(copied)
        cases.append(
            {
                "index": len(cases) + 1,
                "case_id": case_id,
                "api": (as_list(group.get("apis")) or [as_str(result.get("api"))])[0],
                "baseline_status": baseline_status,
                "baseline_fingerprint": group.get("fingerprint"),
                "baseline_reasons": group.get("reasons", []),
                "baseline_returncode": result.get("returncode"),
                "baseline_timed_out": bool(result.get("timed_out", False)),
                "original_recipe": str(original_recipe),
                "asset_recipe": str(copied),
                "asset_recipe_rel": relpath(copied, out_dir),
                "asset_recipe_sha1": file_sha1(copied),
                "baseline_artifact_dir": result.get("artifact_dir"),
                "track": track_digest(group) if group else {"source": "none", "status": "not_failed_at_baseline", "summary": []},
            }
        )

    regression_recipe_list = out_dir / "regression_recipe_list.txt"
    regression_recipe_list.parent.mkdir(parents=True, exist_ok=True)
    regression_recipe_list.write_text("".join(f"{case['asset_recipe']}\n" for case in cases), encoding="utf-8")

    manifest = {
        "schema": "sggk.regression_asset.v1",
        "asset_id": asset_id,
        "generated_at": now_iso_like(),
        "sdk_version": args.sdk_version,
        "dataset_label": args.dataset_label,
        "provenance": provenance_from_args(args),
        "source_code": source_code_refs(),
        "campaign": paths,
        "recipe_summary": str(recipe_summary_path),
        "triage_summary": triage_summary_path,
        "unsupported_patterns": patterns,
        "baseline_total_results": len(as_list(recipe_summary.get("results"))),
        "case_count": len(cases),
        "baseline_status_counts": dict(sorted(Counter(as_str(case.get("baseline_status")) for case in cases).items())),
        "cases": cases,
        "paths": {
            "asset_manifest": str(out_dir / "asset_manifest.json"),
            "asset_report": str(out_dir / "asset_report.md"),
            "bug_registry": str(out_dir / "bug_registry.json"),
            "regression_recipe_list": str(regression_recipe_list),
        },
    }
    bug_registry = build_bug_registry(asset_id, cases, groups_by_fingerprint)
    write_json(out_dir / "asset_manifest.json", manifest)
    write_json(out_dir / "asset_provenance.json", manifest.get("provenance", {}))
    write_json(out_dir / "bug_registry.json", bug_registry)
    (out_dir / "asset_report.md").write_text(snapshot_report(manifest, bug_registry), encoding="utf-8")
    print(f"asset={out_dir / 'asset_manifest.json'}")
    print(f"report={out_dir / 'asset_report.md'}")
    print(f"recipes={regression_recipe_list}")
    print(f"cases={len(cases)} status_counts={manifest['baseline_status_counts']}")
    return 0


def normalize_asset_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        return path / "asset_manifest.json"
    return path


def result_lookup(summary: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_recipe: dict[str, dict[str, Any]] = {}
    by_case: dict[str, dict[str, Any]] = {}
    for result in as_list(summary.get("results")):
        if not isinstance(result, dict):
            continue
        recipe_key = normalize_key(result.get("recipe"))
        if recipe_key:
            by_recipe[recipe_key] = result
        case_id = result_case_id(result)
        if case_id:
            by_case[case_id] = result
    return by_recipe, by_case


def group_lookup_for_compare(triage_summary_path: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    groups = load_failure_groups(triage_summary_path)
    return group_by_recipe(groups), group_by_case_id(groups)


def compare_status(
    baseline_case: dict[str, Any],
    result: dict[str, Any] | None,
    new_group: dict[str, Any],
    patterns: list[str],
) -> dict[str, Any]:
    base_status = as_str(baseline_case.get("baseline_status"))
    base_fp = as_str(baseline_case.get("baseline_fingerprint"))
    if result is None:
        return {"status": "unavailable", "reason": "case was not found in the new run"}

    failed = result_failed(result) or bool(new_group)
    new_fp = as_str(new_group.get("fingerprint"))
    common = {
        "new_returncode": result.get("returncode"),
        "new_timed_out": bool(result.get("timed_out", False)),
        "new_fingerprint": new_fp,
        "new_reasons": new_group.get("reasons", []),
        "new_artifact_dir": result.get("artifact_dir"),
        "new_track": track_digest(new_group) if new_group else {"source": "none", "status": "not_failed", "summary": []},
    }

    if base_status == "candidate_bug":
        if not failed:
            return {**common, "status": "fixed", "reason": "baseline candidate bug now passes"}
        if new_fp and new_fp == base_fp:
            return {**common, "status": "still_failing", "reason": "same failure fingerprint reproduced"}
        return {**common, "status": "changed_failure", "reason": "baseline bug still fails but fingerprint changed"}

    if base_status == "passed":
        if failed:
            return {**common, "status": "new_issue", "reason": "baseline passing case now fails"}
        return {**common, "status": "still_passing", "reason": "baseline passing case still passes"}

    if base_status == "known_unsupported":
        if not failed:
            return {**common, "status": "unsupported_now_passes", "reason": "previous unsupported case now passes"}
        if is_unsupported_group(new_group, patterns):
            return {**common, "status": "still_unsupported", "reason": "explicit unsupported response remains"}
        return {**common, "status": "new_candidate_from_unsupported", "reason": "unsupported response changed into a candidate failure"}

    if not failed:
        return {**common, "status": "fixed", "reason": "baseline unclassified failure now passes"}
    if new_fp and new_fp == base_fp:
        return {**common, "status": "still_failing", "reason": "same unclassified failure fingerprint reproduced"}
    return {**common, "status": "changed_failure", "reason": "baseline unclassified failure changed"}


def compare_report(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# SGGK Regression Comparison")
    lines.append("")
    lines.append(f"- Generated: `{summary.get('generated_at')}`")
    lines.append(f"- Asset: `{summary.get('asset_id')}`")
    lines.append(f"- Baseline SDK: `{summary.get('baseline_sdk_version')}`")
    lines.append(f"- New SDK: `{summary.get('new_sdk_version')}`")
    lines.append(f"- Total compared: `{summary.get('total')}`")
    lines.append(f"- Status counts: `{summary.get('status_counts')}`")
    lines.append("")

    sections = [
        ("Fixed", {"fixed"}),
        ("New Issues", {"new_issue", "new_candidate_from_unsupported", "new_issue_outside_asset"}),
        ("Changed Failures", {"changed_failure"}),
        ("Still Failing", {"still_failing"}),
        ("Unsupported Changes", {"unsupported_now_passes", "still_unsupported"}),
        ("Unavailable", {"unavailable"}),
    ]
    for title, statuses in sections:
        items = [item for item in as_list(summary.get("results")) if isinstance(item, dict) and item.get("status") in statuses]
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("- None.")
            lines.append("")
            continue
        lines.append("| case | baseline | new status | fingerprint | reason | track |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in items:
            track = as_dict(item.get("new_track"))
            track_text = "; ".join(as_list(track.get("summary"))[:2]) or as_str(track.get("status"))
            lines.append(
                "| `{case}` | `{base}` | `{status}` | `{fp}` | {reason} | {track} |".format(
                    case=item.get("case_id"),
                    base=item.get("baseline_status"),
                    status=item.get("status"),
                    fp=item.get("new_fingerprint") or item.get("baseline_fingerprint", ""),
                    reason=as_str(item.get("reason")),
                    track=track_text.replace("|", "\\|"),
                )
            )
        lines.append("")
    return "\n".join(lines)


def run_compare(args: argparse.Namespace) -> int:
    asset_path = normalize_asset_path(args.asset).resolve()
    manifest = load_json(asset_path)
    if not isinstance(manifest, dict):
        print(f"invalid asset manifest: {asset_path}")
        return 1
    new_paths = campaign_paths(args.new_run)
    recipe_summary_path = Path(new_paths["recipe_summary"]) if new_paths.get("recipe_summary") else normalize_summary_path(args.new_run, "recipe_summary.json")
    recipe_summary = load_json(recipe_summary_path)
    if not isinstance(recipe_summary, dict):
        print(f"invalid new recipe summary: {recipe_summary_path}")
        return 1
    triage_summary_path = args.new_triage or new_paths.get("triage_summary", "")
    if triage_summary_path:
        triage_summary_path = str(normalize_summary_path(triage_summary_path, "triage_summary.json"))
    by_recipe, by_case = result_lookup(recipe_summary)
    new_groups_by_recipe, new_groups_by_case = group_lookup_for_compare(triage_summary_path)
    patterns = [*as_list(manifest.get("unsupported_patterns")), *args.unsupported_pattern]

    results: list[dict[str, Any]] = []
    seen_recipe_keys: set[str] = set()
    seen_case_ids: set[str] = set()
    for case in as_list(manifest.get("cases")):
        if not isinstance(case, dict):
            continue
        recipe_key = normalize_key(case.get("asset_recipe"))
        case_id = as_str(case.get("case_id"))
        result = by_recipe.get(recipe_key) if recipe_key else None
        if result is None and case_id:
            result = by_case.get(case_id)
        result_recipe_key = normalize_key(result.get("recipe")) if isinstance(result, dict) else recipe_key
        new_group = {}
        if result_recipe_key:
            new_group = new_groups_by_recipe.get(result_recipe_key, {})
            seen_recipe_keys.add(result_recipe_key)
        if not new_group and result is not None:
            new_group = new_groups_by_case.get(result_case_id(result), {})
        if case_id:
            seen_case_ids.add(case_id)
        classification = compare_status(case, result, new_group, patterns)
        results.append(
            {
                "case_id": case_id,
                "asset_recipe": case.get("asset_recipe"),
                "baseline_status": case.get("baseline_status"),
                "baseline_fingerprint": case.get("baseline_fingerprint"),
                "baseline_reasons": case.get("baseline_reasons", []),
                **classification,
            }
        )

    for recipe_key, group in sorted(new_groups_by_recipe.items()):
        if recipe_key in seen_recipe_keys:
            continue
        representative = as_str(group.get("representative_case_id"))
        if representative in seen_case_ids:
            continue
        results.append(
            {
                "case_id": representative,
                "asset_recipe": "",
                "baseline_status": "outside_asset",
                "baseline_fingerprint": "",
                "status": "new_issue_outside_asset",
                "reason": "new run contains a failing recipe not present in the asset manifest",
                "new_fingerprint": group.get("fingerprint"),
                "new_reasons": group.get("reasons", []),
                "new_track": track_digest(group),
            }
        )

    status_counts = Counter(as_str(item.get("status")) for item in results)
    summary = {
        "schema": "sggk.regression_comparison.v1",
        "generated_at": now_iso_like(),
        "asset_id": manifest.get("asset_id"),
        "asset_manifest": str(asset_path),
        "new_recipe_summary": str(recipe_summary_path),
        "new_triage_summary": triage_summary_path,
        "baseline_sdk_version": manifest.get("sdk_version"),
        "new_sdk_version": args.new_sdk_version,
        "total": len(results),
        "status_counts": dict(sorted(status_counts.items())),
        "results": results,
    }
    out_dir = Path(args.out).resolve()
    write_json(out_dir / "regression_comparison.json", summary)
    (out_dir / "regression_comparison.md").write_text(compare_report(summary), encoding="utf-8")
    print(f"summary={out_dir / 'regression_comparison.json'}")
    print(f"report={out_dir / 'regression_comparison.md'}")
    print(f"total={summary['total']} status_counts={summary['status_counts']}")
    return 0


def load_asset_manifest(raw: str) -> tuple[Path, dict[str, Any]]:
    asset_path = normalize_asset_path(raw).resolve()
    manifest = load_json(asset_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"invalid asset manifest: {asset_path}")
    return asset_path, manifest


def run_annotate(args: argparse.Namespace) -> int:
    try:
        asset_path, manifest = load_asset_manifest(args.asset)
    except ValueError as exc:
        print(str(exc))
        return 1
    provenance = provenance_from_args(args, existing=as_dict(manifest.get("provenance")))
    manifest["provenance"] = provenance
    write_json(asset_path, manifest)
    write_json(asset_path.parent / "asset_provenance.json", provenance)
    bug_registry = load_json(asset_path.parent / "bug_registry.json")
    if not isinstance(bug_registry, dict):
        bug_registry = {"bugs": []}
    write_text(asset_path.parent / "asset_report.md", snapshot_report(manifest, bug_registry))
    print(f"asset={asset_path}")
    print(f"provenance={asset_path.parent / 'asset_provenance.json'}")
    print(f"source_type={provenance.get('source_type', '')} model={provenance.get('model', '')} fingerprints={provenance.get('fingerprints', [])}")
    return 0


def powershell_command(command: list[str]) -> str:
    lines: list[str] = []
    for index, item in enumerate(command):
        suffix = " `" if index < len(command) - 1 else ""
        lines.append(f"  {item}{suffix}" if index else f"{item}{suffix}")
    return "\n".join(lines)


def replay_plan_report(plan: dict[str, Any]) -> str:
    lines = [
        "# Regression Asset Replay Plan",
        "",
        f"- asset_id: `{plan.get('asset_id')}`",
        f"- asset_manifest: `{plan.get('asset_manifest')}`",
        f"- source_type: `{as_dict(plan.get('provenance')).get('source_type', '')}`",
        f"- model: `{as_dict(plan.get('provenance')).get('model', '')}`",
        f"- new_sdk_version: `{plan.get('new_sdk_version')}`",
        "",
        "## Replay",
        "",
        "```powershell",
        powershell_command(as_list(plan.get("replay_command"))),
        "```",
        "",
        "## Compare",
        "",
        "```powershell",
        powershell_command(as_list(plan.get("compare_command"))),
        "```",
        "",
        "The plan is deterministic metadata only; generating it does not run the SDK.",
        "",
    ]
    return "\n".join(lines)


def run_plan_replay(args: argparse.Namespace) -> int:
    try:
        asset_path, manifest = load_asset_manifest(args.asset)
    except ValueError as exc:
        print(str(exc))
        return 1
    recipe_list = as_str(as_dict(manifest.get("paths")).get("regression_recipe_list"))
    if not recipe_list:
        print("asset manifest has no paths.regression_recipe_list")
        return 1
    out_dir = Path(args.out).resolve()
    replay_out = args.replay_out or str(out_dir / "run")
    triage_out = args.triage_out or str(out_dir / "triage")
    compare_out = args.compare_out or str(out_dir / "compare")
    replay_command = [
        "python .\\test_harness\\tools\\run_recipes.py",
        f"--runner {args.runner}",
        f"--recipe-list {recipe_list}",
        f"--out {replay_out}",
        f"--triage-out {triage_out}",
        f"--jobs {args.jobs}",
        f"--timeout {args.timeout:g}",
    ]
    compare_command = [
        "python .\\test_harness\\tools\\manage_regression_assets.py compare",
        f"--asset {asset_path}",
        f"--new-run {Path(replay_out) / 'recipe_summary.json'}",
        f"--new-triage {Path(triage_out) / 'triage_summary.json'}",
        f"--out {compare_out}",
    ]
    if args.new_sdk_version:
        compare_command.append(f"--new-sdk-version {args.new_sdk_version}")
    plan = {
        "schema": "sggk.regression_replay_plan.v1",
        "generated_at": now_iso_like(),
        "asset_id": manifest.get("asset_id"),
        "asset_manifest": str(asset_path),
        "provenance": manifest.get("provenance", {}),
        "recipe_list": recipe_list,
        "runner": args.runner,
        "replay_out": replay_out,
        "triage_out": triage_out,
        "compare_out": compare_out,
        "new_sdk_version": args.new_sdk_version,
        "replay_command": replay_command,
        "compare_command": compare_command,
        "boundary": {
            "model_calls": False,
            "direct_api_calls": False,
            "runs_sdk": False,
            "applies_patches": False,
            "commits_changes": False,
        },
    }
    write_json(out_dir / "replay_plan.json", plan)
    write_text(out_dir / "replay_plan.md", replay_plan_report(plan))
    print(f"plan={out_dir / 'replay_plan.json'}")
    print(f"report={out_dir / 'replay_plan.md'}")
    print(f"asset_id={plan['asset_id']} runs_sdk={plan['boundary']['runs_sdk']}")
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "snapshot":
        return run_snapshot(args)
    if args.command == "compare":
        return run_compare(args)
    if args.command == "annotate":
        return run_annotate(args)
    if args.command == "plan-replay":
        return run_plan_replay(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
