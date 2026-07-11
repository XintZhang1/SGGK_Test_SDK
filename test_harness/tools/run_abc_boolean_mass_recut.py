#!/usr/bin/env python3
"""Plan, run, and report a large ABC loaded-SGT boolean recut campaign.

This is a fixed-code wrapper for the distillation workflow:

developer form -> generated large ABC recut recipes -> SDK run -> triage ->
bug-only report with explicit unsupported failures filtered out.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_UNSUPPORTED_PATTERNS = [
    "Not accepted type!",
    "wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument("--dataset", action="append", default=[], help="Imported SGT file or directory")
    parser.add_argument("--dataset-list", action="append", default=[], help="Text/JSON list of imported SGT files")
    parser.add_argument("--out", default="artifacts/abc_boolean_mass_recut", help="Campaign output root")
    parser.add_argument("--target-cases", type=int, default=100000, help="Recipes to generate before sharding")
    parser.add_argument("--preset", choices=["smoke", "standard", "stress"], default="stress")
    parser.add_argument("--case-prefix", default="abc_mass_recut")
    parser.add_argument("--source-limit", type=int, default=0, help="Optional maximum SGT sources to scan")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--resume-mode", choices=["passed", "completed"], default="completed")
    parser.add_argument("--plan-only", action="store_true", help="Generate recipes/report only; do not execute SDK cases")
    parser.add_argument("--require-exact-bbox-probe", action="store_true")
    parser.add_argument("--no-exact-bbox-probe", action="store_true")
    parser.add_argument("--sample-input-properties", action="store_true")
    parser.add_argument("--topo-track", action="store_true")
    parser.add_argument(
        "--unsupported-pattern",
        action="append",
        default=[],
        help="Additional literal text that means the kernel explicitly does not support this case.",
    )
    parser.add_argument("--max-bug-groups", type=int, default=30)
    parser.add_argument("--preview-limit", type=int, default=0, help="0 disables preview generation")
    parser.add_argument("--geometry-audit", action="store_true", help="Run geometry audit for executed shard")
    parser.add_argument("--hash-recipes", action="store_true")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def recipes_per_source(preset: str) -> int:
    if preset == "smoke":
        return 3
    if preset == "standard":
        return 30
    return 75


def validate_args(args: argparse.Namespace) -> None:
    if not args.dataset and not args.dataset_list:
        raise ValueError("pass at least one --dataset or --dataset-list")
    if args.target_cases <= 0:
        raise ValueError("--target-cases must be positive")
    if args.shard_count <= 0:
        raise ValueError("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise ValueError("--shard-index must satisfy 0 <= index < shard-count")
    if args.jobs <= 0:
        raise ValueError("--jobs must be >= 1")
    if args.timeout <= 0:
        raise ValueError("--timeout must be > 0")
    if args.max_bug_groups < 0:
        raise ValueError("--max-bug-groups must be >= 0")
    if args.preview_limit < 0:
        raise ValueError("--preview-limit must be >= 0")
    if args.require_exact_bbox_probe and args.no_exact_bbox_probe:
        raise ValueError("--require-exact-bbox-probe cannot be combined with --no-exact-bbox-probe")


def run_command(name: str, cmd: list[str], acceptable: set[int] | None = None) -> dict[str, Any]:
    if acceptable is None:
        acceptable = {0}
    print(f"[abc-mass] {name}")
    print("  " + " ".join(cmd))
    started = time.perf_counter()
    completed = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    elapsed = time.perf_counter() - started
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return {
        "name": name,
        "command": cmd,
        "returncode": completed.returncode,
        "acceptable": sorted(acceptable),
        "ok": completed.returncode in acceptable,
        "elapsed_seconds": elapsed,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def generate_recipes(args: argparse.Namespace, out_root: Path, command_records: list[dict[str, Any]]) -> dict[str, Any] | None:
    recipe_dir = out_root / "recipes"
    manifest_path = out_root / "recipes_manifest.json"
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "generate_corpus_recut_matrix.py"),
        "--out",
        str(recipe_dir),
        "--preset",
        args.preset,
        "--case-prefix",
        args.case_prefix,
        "--manifest",
        str(manifest_path),
        "--runner",
        str(Path(args.runner)),
        "--limit",
        str(args.target_cases),
        "--probe-out",
        str(out_root / "exact_bbox_probes"),
    ]
    for dataset in args.dataset:
        cmd.extend(["--dataset", dataset])
    for dataset_list in args.dataset_list:
        cmd.extend(["--dataset-list", dataset_list])
    if args.source_limit:
        cmd.extend(["--source-limit", str(args.source_limit)])
    if args.require_exact_bbox_probe:
        cmd.append("--require-exact-bbox-probe")
    if args.no_exact_bbox_probe:
        cmd.append("--no-exact-bbox-probe")
    if args.sample_input_properties:
        cmd.append("--sample-input-properties")
    if args.topo_track:
        cmd.append("--topo-track")
    record = run_command("generate_recipes", cmd)
    command_records.append(record)
    if not record["ok"]:
        return None
    manifest = read_json(manifest_path)
    return manifest if isinstance(manifest, dict) else None


def run_shard(args: argparse.Namespace, out_root: Path, command_records: list[dict[str, Any]]) -> dict[str, Any] | None:
    run_out = out_root / "run" / f"shard_{args.shard_index:04d}_of_{args.shard_count:04d}"
    triage_out = out_root / "triage" / f"shard_{args.shard_index:04d}_of_{args.shard_count:04d}_raw"
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "run_recipes.py"),
        "--runner",
        str(Path(args.runner)),
        "--recipe",
        str(out_root / "recipes"),
        "--out",
        str(run_out),
        "--triage-out",
        str(triage_out),
        "--timeout",
        str(args.timeout),
        "--jobs",
        str(args.jobs),
        "--shard-count",
        str(args.shard_count),
        "--shard-index",
        str(args.shard_index),
    ]
    if args.resume:
        cmd.append("--resume")
        cmd.extend(["--resume-mode", args.resume_mode])
    if args.hash_recipes:
        cmd.append("--hash-recipes")
    if args.preview_limit:
        cmd.extend(["--preview-out", str(out_root / "preview" / f"shard_{args.shard_index:04d}_of_{args.shard_count:04d}")])
        cmd.extend(["--preview-limit", str(args.preview_limit)])
    if args.geometry_audit:
        cmd.extend(["--geometry-audit-out", str(out_root / "geometry_audit" / f"shard_{args.shard_index:04d}_of_{args.shard_count:04d}")])
    record = run_command("run_shard", cmd, acceptable={0, 2})
    command_records.append(record)
    summary = read_json(run_out / "recipe_summary.json")
    return summary if isinstance(summary, dict) else None


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


def classify_bug_groups(
    triage_summary: dict[str, Any] | None,
    unsupported_patterns: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(triage_summary, dict):
        return [], []
    candidate_groups: list[dict[str, Any]] = []
    unsupported_groups: list[dict[str, Any]] = []
    lowered_patterns = [pattern.lower() for pattern in unsupported_patterns if pattern]
    for group in triage_summary.get("failure_groups", []):
        if not isinstance(group, dict):
            continue
        text = group_text(group)
        matched = [pattern for pattern in lowered_patterns if pattern in text]
        item = dict(group)
        if matched:
            item["unsupported_match"] = matched
            unsupported_groups.append(item)
        else:
            candidate_groups.append(item)
    return candidate_groups, unsupported_groups


def load_triage_summary(out_root: Path, args: argparse.Namespace) -> dict[str, Any] | None:
    triage_path = out_root / "triage" / f"shard_{args.shard_index:04d}_of_{args.shard_count:04d}_raw" / "triage_summary.json"
    triage = read_json(triage_path)
    return triage if isinstance(triage, dict) else None


def write_bug_report(
    path: Path,
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any] | None,
    run_summary: dict[str, Any] | None,
    triage_summary: dict[str, Any] | None,
    candidate_groups: list[dict[str, Any]],
    unsupported_groups: list[dict[str, Any]],
    command_records: list[dict[str, Any]],
) -> None:
    manifest = manifest or {}
    run_summary = run_summary or {}
    triage_summary = triage_summary or {}
    lines = ["# ABC Boolean Mass Recut Bug Report", ""]
    lines.append(f"- Generated: `{now_iso_like()}`")
    lines.append(f"- Preset: `{args.preset}`")
    lines.append(f"- Target recipes: `{args.target_cases}`")
    lines.append(f"- Generated recipes: `{manifest.get('recipe_count', 0)}`")
    lines.append(f"- Sources used: `{manifest.get('used_source_count', 0)}`")
    lines.append(f"- Shard: `{args.shard_index}/{args.shard_count}`")
    expected_shard = math.ceil(int(manifest.get("recipe_count", args.target_cases) or args.target_cases) / args.shard_count)
    lines.append(f"- Expected shard size: `~{expected_shard}`")
    lines.append(f"- Executed: `{run_summary.get('executed', 0)}`")
    lines.append(f"- Passed: `{run_summary.get('passed', 0)}`")
    lines.append(f"- Failed before filtering: `{triage_summary.get('failed_cases', 0)}`")
    lines.append(f"- Unsupported groups filtered: `{len(unsupported_groups)}`")
    lines.append(f"- Candidate bug groups: `{len(candidate_groups)}`")
    lines.append("")
    lines.append("## Unsupported Filter")
    lines.append("")
    lines.append("These groups are not counted as bugs because the kernel explicitly reports unsupported or currently not allowed behavior.")
    if unsupported_groups:
        for group in unsupported_groups[: args.max_bug_groups or None]:
            lines.append(
                f"- `{group.get('fingerprint')}` count `{group.get('count')}` "
                f"match `{', '.join(group.get('unsupported_match', []))}` "
                f"case `{group.get('representative_case_id')}`"
            )
    else:
        lines.append("- None in this shard.")
    lines.append("")
    lines.append("## Candidate Bugs")
    lines.append("")
    if not candidate_groups:
        lines.append("No candidate bug groups remained after unsupported filtering.")
    else:
        for index, group in enumerate(candidate_groups[: args.max_bug_groups or None], start=1):
            runner = group.get("representative_runner") if isinstance(group.get("representative_runner"), dict) else {}
            components = group.get("fingerprint_components") if isinstance(group.get("fingerprint_components"), dict) else {}
            status = components.get("error_message") if isinstance(components.get("error_message"), str) else ""
            lines.append(f"### {index}. `{group.get('fingerprint')}`")
            lines.append("")
            lines.append(f"- Count: `{group.get('count')}`")
            lines.append(f"- APIs: `{', '.join(group.get('apis', []))}`")
            lines.append(f"- Reasons: `{', '.join(group.get('reasons', []))}`")
            lines.append(f"- Representative case: `{group.get('representative_case_id')}`")
            lines.append(f"- Representative dir: `{group.get('representative_case_dir')}`")
            if runner:
                lines.append(f"- Runner return code: `{runner.get('returncode')}` timed_out `{runner.get('timed_out')}`")
            if status:
                lines.append(f"- Normalized error: `{status}`")
            validation = group.get("representative_validation_failures")
            if isinstance(validation, list) and validation:
                lines.append("- Validation failures:")
                for failure in validation[:5]:
                    lines.append(f"  - `{failure}`")
            lines.append("")
    lines.append("## Commands")
    lines.append("")
    for record in command_records:
        lines.append(f"- `{record.get('name')}` rc `{record.get('returncode')}` ok `{record.get('ok')}` elapsed `{record.get('elapsed_seconds', 0):.2f}s`")
    write_text(path, "\n".join(lines) + "\n")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    command_records: list[dict[str, Any]] = []
    manifest = generate_recipes(args, out_root, command_records)
    if manifest is None:
        write_json(out_root / "abc_boolean_mass_recut_summary.json", {"ok": False, "commands": command_records})
        return 2

    run_summary: dict[str, Any] | None = None
    triage_summary: dict[str, Any] | None = None
    if not args.plan_only:
        run_summary = run_shard(args, out_root, command_records)
        triage_summary = load_triage_summary(out_root, args)

    unsupported_patterns = DEFAULT_UNSUPPORTED_PATTERNS + list(args.unsupported_pattern)
    candidate_groups, unsupported_groups = classify_bug_groups(triage_summary, unsupported_patterns)
    summary = {
        "ok": True,
        "generated_at": now_iso_like(),
        "target_cases": args.target_cases,
        "preset": args.preset,
        "recipes_per_source": recipes_per_source(args.preset),
        "generated_recipe_count": manifest.get("recipe_count"),
        "used_source_count": manifest.get("used_source_count"),
        "shard_count": args.shard_count,
        "shard_index": args.shard_index,
        "plan_only": args.plan_only,
        "executed": run_summary.get("executed") if isinstance(run_summary, dict) else 0,
        "passed": run_summary.get("passed") if isinstance(run_summary, dict) else 0,
        "failed_raw": triage_summary.get("failed_cases") if isinstance(triage_summary, dict) else 0,
        "candidate_bug_group_count": len(candidate_groups),
        "unsupported_group_count": len(unsupported_groups),
        "unsupported_patterns": unsupported_patterns,
        "paths": {
            "recipes": str(out_root / "recipes"),
            "recipe_manifest": str(out_root / "recipes_manifest.json"),
            "run_summary": str(out_root / "run" / f"shard_{args.shard_index:04d}_of_{args.shard_count:04d}" / "recipe_summary.json"),
            "triage_summary": str(out_root / "triage" / f"shard_{args.shard_index:04d}_of_{args.shard_count:04d}_raw" / "triage_summary.json"),
            "bug_report": str(out_root / "abc_boolean_mass_recut_bug_report.md"),
        },
        "commands": command_records,
    }
    write_json(out_root / "abc_boolean_mass_recut_summary.json", summary)
    write_bug_report(
        out_root / "abc_boolean_mass_recut_bug_report.md",
        args=args,
        manifest=manifest,
        run_summary=run_summary,
        triage_summary=triage_summary,
        candidate_groups=candidate_groups,
        unsupported_groups=unsupported_groups,
        command_records=command_records,
    )
    print(json.dumps(summary["paths"], indent=2))
    print(
        "generated={generated} executed={executed} candidate_bug_groups={bugs} unsupported_groups={unsupported}".format(
            generated=summary["generated_recipe_count"],
            executed=summary["executed"],
            bugs=summary["candidate_bug_group_count"],
            unsupported=summary["unsupported_group_count"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
