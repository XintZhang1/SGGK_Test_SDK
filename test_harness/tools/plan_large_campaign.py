#!/usr/bin/env python3
"""Plan a sharded large SGGK campaign over local source and corpus inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


DEFAULT_DSLS = [
    "test_harness/dsl/tolerance_band_smoke.json",
    "test_harness/dsl/real_chain_tolerance_smoke.json",
    "test_harness/dsl/complex_surface_sweep_boolean_smoke.json",
]

PROFILE_DEFAULTS = {
    "smoke": {
        "matrix_presets": ["smoke"],
        "corpus_recut_presets": ["smoke"],
        "corpus_sgt_apis": ["check_sgt"],
        "corpus_limit": 10,
        "matrix_limit": 10,
        "dsl_limit": 20,
        "corpus_recut_source_limit": 5,
        "corpus_recut_limit": 20,
        "source_scan_max_findings": 120,
        "source_scan_max_seeds": 30,
        "source_task_max_tasks": 40,
    },
    "standard": {
        "matrix_presets": ["standard"],
        "corpus_recut_presets": ["standard"],
        "corpus_sgt_apis": ["check_sgt", "step_roundtrip", "iges_roundtrip"],
        "corpus_limit": 0,
        "matrix_limit": 0,
        "dsl_limit": 0,
        "corpus_recut_source_limit": 0,
        "corpus_recut_limit": 0,
        "source_scan_max_findings": 500,
        "source_scan_max_seeds": 120,
        "source_task_max_tasks": 120,
    },
    "stress": {
        "matrix_presets": ["standard", "stress"],
        "corpus_recut_presets": ["standard", "stress"],
        "corpus_sgt_apis": ["check_sgt", "step_roundtrip", "iges_roundtrip"],
        "corpus_limit": 0,
        "matrix_limit": 0,
        "dsl_limit": 0,
        "corpus_recut_source_limit": 0,
        "corpus_recut_limit": 0,
        "source_scan_max_findings": 2000,
        "source_scan_max_seeds": 400,
        "source_task_max_tasks": 400,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument("--out", required=True, help="Output directory for the plan and scripts")
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFAULTS), default="standard")
    parser.add_argument("--shards", type=int, default=4, help="Number of campaign shards")
    parser.add_argument("--jobs", type=int, default=2, help="Jobs per shard")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-case timeout in seconds")
    parser.add_argument("--dataset-root", action="append", default=[], help="Corpus file or directory to freeze into a dataset index")
    parser.add_argument("--dataset-list", action="append", default=[], help="Existing discover_corpus.py dataset index or path list")
    parser.add_argument("--source-root", action="append", default=[], help="Source root for source-risk scan/task packaging")
    parser.add_argument("--dsl", action="append", default=[], help="DSL file or directory to include; defaults to core DSL smoke/stress set")
    parser.add_argument("--include-artifacts", action="store_true", help="Include artifact directories during pre-discovery")
    parser.add_argument("--include-build", action="store_true", help="Include build directories during pre-discovery")
    parser.add_argument("--exclude-dir", action="append", default=[], help="Additional corpus discovery directory name to skip")
    parser.add_argument("--discover-limit", type=int, default=0, help="Discovery limit for the frozen dataset index")
    parser.add_argument("--hash-inputs", action="store_true", help="Hash discovered corpus files")
    parser.add_argument("--skip-dataset-audit", action="store_true", help="Generated preflight skips dataset audit")
    parser.add_argument("--dataset-audit-require-hashes", action="store_true", help="Generated preflight fails dataset audit when sha1 hashes are missing")
    parser.add_argument("--dataset-audit-fail-duplicate-ratio", type=float, default=-1.0, help="Generated preflight fails when duplicate-file ratio exceeds this value; negative disables")
    parser.add_argument("--profile-cad-features", action="store_true", help="Generated preflight profiles STEP/IGES feature complexity")
    parser.add_argument("--cad-feature-min-score", type=int, default=8, help="Minimum CAD feature score for generated complex subset")
    parser.add_argument("--require-cad-feature-profile", action="store_true", help="Generated preflight fails when CAD feature profile is unavailable or empty")
    parser.add_argument("--use-cad-feature-subset", action="store_true", help="Generate a complex STEP/IGES subset during planning and run shards on that subset")
    parser.add_argument("--hash-recipes", action="store_true", help="Hash recipes in campaign manifests")
    parser.add_argument("--bundle-zip", action="store_true", help="Zip failure bundles")
    parser.add_argument("--source-task-write-dsl-seeds", action="store_true", help="Write source-task seed DSL files in shard 0")
    parser.add_argument("--source-scan-each-shard", action="store_true", help="Run source scan/task packaging in every shard instead of shard 0 only")
    parser.add_argument("--materialize-bug-records", action="store_true", help="Merged collect script materializes bug-record drafts")
    parser.add_argument("--promote-bug-records", action="store_true", help="Merged collect script promotes bug-record drafts into portable artifact-local candidates")
    parser.add_argument("--replay-promoted-bug-records", action="store_true", help="Merged collect script materializes, replays, and classifies promoted bug-record candidates")
    parser.add_argument("--promoted-replay-timeout", type=float, default=60.0, help="Per-recipe timeout for promoted bug-record replay during collection")
    parser.add_argument("--promoted-replay-jobs", type=int, default=1, help="Parallel jobs for promoted bug-record replay during collection")
    parser.add_argument("--validate-recipes", action="store_true", help="Validate materialized bug-record replay recipes")
    parser.add_argument("--bug-record", action="append", default=[], help="Checked-in bug-record JSON file or directory forwarded to shard known-bug regression and preflight")
    parser.add_argument("--skip-oracle-coverage", action="store_true", help="Generated shard and collect commands skip oracle coverage")
    parser.add_argument("--oracle-coverage-min-kinds", type=int, default=1, help="Minimum oracle kinds per passed case for generated shard and merged oracle coverage")
    parser.add_argument("--skip-artifact-verify", action="store_true", help="Generated shard and collect commands skip artifact verification")
    parser.add_argument("--verify-allow-duplicate-inputs", action="store_true", help="Generated verifier command allows duplicate input groups")
    parser.add_argument("--verify-allow-duplicate-geometry", action="store_true", help="Generated verifier command allows duplicate full-geometry groups")
    parser.add_argument("--verify-allow-tolerance-mismatches", action="store_true", help="Generated verifier command allows geometry-audit tolerance mismatches")
    parser.add_argument("--fail-on-failures", action="store_true", help="Shard commands fail when aggregate failures are found")
    parser.add_argument("--known-bug-fail-on-fixed", action="store_true")
    parser.add_argument("--known-bug-fail-on-changed", action="store_true")
    parser.add_argument("--known-bug-fail-on-unavailable", action="store_true")
    parser.add_argument("--skip-known-bug-regression", action="store_true")
    parser.add_argument("--matrix-preset", action="append", choices=["smoke", "standard", "stress"], default=[], help="Override profile matrix preset(s)")
    parser.add_argument("--corpus-recut-preset", action="append", choices=["smoke", "standard", "stress"], default=[], help="Override profile corpus recut preset(s)")
    parser.add_argument("--corpus-sgt-api", action="append", choices=["check_sgt", "step_roundtrip", "iges_roundtrip"], default=[], help="Override profile SGT API list")
    parser.add_argument("--corpus-limit", type=int, default=-1, help="Override profile corpus limit; -1 uses profile")
    parser.add_argument("--matrix-limit", type=int, default=-1, help="Override profile matrix limit; -1 uses profile")
    parser.add_argument("--dsl-limit", type=int, default=-1, help="Override profile DSL limit; -1 uses profile")
    parser.add_argument("--corpus-recut-source-limit", type=int, default=-1, help="Override profile corpus recut source limit; -1 uses profile")
    parser.add_argument("--corpus-recut-limit", type=int, default=-1, help="Override profile corpus recut recipe limit; -1 uses profile")
    parser.add_argument("--corpus-recut-require-exact-bbox-probe", action="store_true", help="Require exact coordinate-plane bbox probes in shard corpus recut lanes")
    parser.add_argument("--corpus-recut-no-exact-bbox-probe", action="store_true", help="Disable exact coordinate-plane bbox probes in shard corpus recut lanes")
    parser.add_argument("--source-scan-max-findings", type=int, default=-1, help="Override profile source scan finding limit; -1 uses profile")
    parser.add_argument("--source-scan-max-seeds", type=int, default=-1, help="Override profile source scan seed limit; -1 uses profile")
    parser.add_argument("--source-task-max-tasks", type=int, default=-1, help="Override profile source task limit; -1 uses profile")
    parser.add_argument("--source-task-context-lines", type=int, default=12)
    parser.add_argument("--source-task-min-severity", choices=["critical", "high", "medium", "low"], default="medium")
    parser.add_argument("--reduce-stable-failures", action="store_true", help="Forward stable-failure reduction to shard campaigns")
    parser.add_argument("--reduction-limit", type=int, default=3, help="Maximum stable replay failures to reduce per shard when reduction is enabled; 0 means all")
    parser.add_argument("--reduction-max-trials", type=int, default=60, help="Maximum reducer trials per selected stable failure")
    parser.add_argument("--reduction-timeout", type=float, default=0.0, help="Per-reducer-trial timeout; 0 reuses shard --timeout")
    parser.add_argument("--reduction-min-dimension", type=float, default=0.01, help="Minimum positive dimension used by reduce_failure_recipe.py")
    parser.add_argument("--replay-reductions", action="store_true", help="Replay canonical merged reduced recipes during shard collection")
    parser.add_argument("--export-reduction-bug-record-drafts", action="store_true", help="Export editable bug-record drafts from merged reduced-recipe replay triage")
    parser.add_argument("--materialize-reduction-bug-records", action="store_true", help="Materialize reduced-replay bug-record drafts and classify them against reduced replay during collection")
    parser.add_argument("--reduction-replay-timeout", type=float, default=120.0, help="Per-recipe timeout for merged reduced-recipe replay")
    parser.add_argument("--reduction-replay-jobs", type=int, default=1, help="Parallel jobs for merged reduced-recipe replay")
    parser.add_argument("--reduction-replay-limit", type=int, default=0, help="Maximum canonical reduced recipes to replay during collection; 0 means all")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def ps_quote(value: str | Path) -> str:
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def command_text(cmd: list[str]) -> str:
    return " ".join(ps_quote(part) if any(ch.isspace() for ch in str(part)) else str(part) for part in cmd)


def script_lines_for_commands(commands: list[list[str]]) -> list[str]:
    lines = ["$ErrorActionPreference = 'Stop'", ""]
    for cmd in commands:
        for index, part in enumerate(cmd):
            prefix = "& " if index == 0 else "  "
            suffix = " `" if index < len(cmd) - 1 else ""
            lines.append(f"{prefix}{ps_quote(part)}{suffix}")
        lines.extend(["$code = $LASTEXITCODE", "if ($code -ne 0) { exit $code }", ""])
    return lines


def script_lines_for_command(cmd: list[str]) -> list[str]:
    return script_lines_for_commands([cmd])


def profile_value(args: argparse.Namespace, key: str) -> Any:
    explicit = getattr(args, key)
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    return PROFILE_DEFAULTS[args.profile][key]


def profile_list(args: argparse.Namespace, attr: str, key: str) -> list[str]:
    explicit = getattr(args, attr)
    return list(explicit) if explicit else list(PROFILE_DEFAULTS[args.profile][key])


def run_discovery(args: argparse.Namespace, script_dir: Path, out_dir: Path) -> dict[str, Any]:
    if not args.dataset_root:
        return {"skipped": True, "reason": "no dataset roots"}
    discovery_dir = out_dir / "discovery"
    index_path = discovery_dir / "dataset_index.json"
    cmd = [
        sys.executable,
        str(script_dir / "discover_corpus.py"),
        *args.dataset_root,
        "--out",
        str(index_path),
        "--paths-out",
        str(discovery_dir / "dataset_index.paths.txt"),
        "--report",
        str(discovery_dir / "dataset_index.md"),
    ]
    if args.hash_inputs:
        cmd.append("--hash-inputs")
    if args.include_artifacts:
        cmd.append("--include-artifacts")
    if args.include_build:
        cmd.append("--include-build")
    for exclude in args.exclude_dir:
        cmd.extend(["--exclude-dir", exclude])
    if args.discover_limit:
        cmd.extend(["--limit", str(args.discover_limit)])
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    index = None
    if index_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "index_path": str(index_path),
        "paths_path": str(discovery_dir / "dataset_index.paths.txt"),
        "report_path": str(discovery_dir / "dataset_index.md"),
        "total_files": index.get("total_files") if isinstance(index, dict) else None,
        "total_bytes": index.get("total_bytes") if isinstance(index, dict) else None,
        "by_extension": index.get("by_extension") if isinstance(index, dict) else {},
        "by_api": index.get("by_api") if isinstance(index, dict) else {},
        "duplicate_content_groups": len(index.get("duplicate_content_groups", [])) if isinstance(index, dict) else 0,
    }


def run_cad_feature_profile(
    args: argparse.Namespace,
    script_dir: Path,
    out_dir: Path,
    dataset_lists: list[str],
) -> dict[str, Any]:
    if not dataset_lists:
        return {"skipped": True, "ok": False, "reason": "no dataset lists"}
    profile_dir = out_dir / "cad_feature_profile"
    profile_path = profile_dir / "cad_feature_profile.json"
    report_path = profile_dir / "cad_feature_profile.md"
    paths_path = profile_dir / "complex_paths.txt"
    subset_path = profile_dir / "complex_dataset_index.json"
    cmd = [
        sys.executable,
        str(script_dir / "profile_cad_features.py"),
        "--out",
        str(profile_path),
        "--paths-out",
        str(paths_path),
        "--subset-out",
        str(subset_path),
        "--report",
        str(report_path),
        "--min-score",
        str(args.cad_feature_min_score),
    ]
    for dataset_list in dataset_lists:
        cmd.extend(["--dataset-list", dataset_list])
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    summary = None
    subset = None
    if profile_path.is_file():
        summary = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    if subset_path.is_file():
        subset = json.loads(subset_path.read_text(encoding="utf-8-sig"))
    complex_file_count = None
    total_files = None
    feature_totals: dict[str, Any] = {}
    if isinstance(summary, dict):
        complex_file_count = summary.get("complex_file_count")
        total_files = summary.get("total_files")
        feature_totals = summary.get("feature_totals", {})
    if complex_file_count is None and isinstance(subset, dict):
        complex_file_count = subset.get("total_files")
    if total_files is None and isinstance(subset, dict):
        total_files = subset.get("profile_summary", {}).get("total_files")
    return {
        "skipped": False,
        "command": cmd,
        "returncode": completed.returncode,
        "ok": completed.returncode == 0 and int(complex_file_count or 0) > 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "profile_path": str(profile_path),
        "report_path": str(report_path),
        "paths_path": str(paths_path),
        "subset_path": str(subset_path),
        "total_files": total_files,
        "complex_file_count": complex_file_count,
        "feature_totals": feature_totals,
        "source_dataset_lists": dataset_lists,
    }


def build_shard_command(
    args: argparse.Namespace,
    script_dir: Path,
    out_dir: Path,
    dataset_lists: list[str],
    shard_index: int,
) -> list[str]:
    cmd = [
        sys.executable,
        str(script_dir / "run_campaign.py"),
        "--runner",
        str(Path(args.runner).resolve()),
        "--out",
        str(out_dir / "shards" / f"shard_{shard_index}of{args.shards}"),
        "--jobs",
        str(args.jobs),
        "--timeout",
        str(args.timeout),
        "--shard-count",
        str(args.shards),
        "--shard-index",
        str(shard_index),
        "--triage-include-passed",
        "--resume",
    ]
    for dataset_list in dataset_lists:
        cmd.extend(["--dataset-list", dataset_list])
    source_roots_enabled = bool(args.source_root) and (args.source_scan_each_shard or shard_index == 0)
    for source_root in args.source_root if source_roots_enabled else []:
        cmd.extend(["--source-root", source_root])
    if args.source_root and not source_roots_enabled:
        cmd.append("--skip-source-scan")
    cmd.extend(["--source-scan-max-findings", str(profile_value(args, "source_scan_max_findings"))])
    cmd.extend(["--source-scan-max-seeds", str(profile_value(args, "source_scan_max_seeds"))])
    cmd.extend(["--source-task-max-tasks", str(profile_value(args, "source_task_max_tasks"))])
    cmd.extend(["--source-task-context-lines", str(args.source_task_context_lines)])
    cmd.extend(["--source-task-min-severity", args.source_task_min_severity])
    if args.source_task_write_dsl_seeds and source_roots_enabled:
        cmd.append("--source-task-write-dsl-seeds")
    for record in args.bug_record:
        cmd.extend(["--bug-record", record])
    if args.reduce_stable_failures:
        cmd.append("--reduce-stable-failures")
        cmd.extend(["--reduction-limit", str(args.reduction_limit)])
        cmd.extend(["--reduction-max-trials", str(args.reduction_max_trials)])
        if args.reduction_timeout:
            cmd.extend(["--reduction-timeout", str(args.reduction_timeout)])
        cmd.extend(["--reduction-min-dimension", str(args.reduction_min_dimension)])
    if args.include_artifacts:
        cmd.append("--discover-include-artifacts")
    if args.include_build:
        cmd.append("--discover-include-build")
    for exclude in args.exclude_dir:
        cmd.extend(["--discover-exclude-dir", exclude])
    if args.discover_limit:
        cmd.extend(["--discover-limit", str(args.discover_limit)])
    for api in profile_list(args, "corpus_sgt_api", "corpus_sgt_apis"):
        cmd.extend(["--corpus-sgt-api", api])
    for preset in profile_list(args, "matrix_preset", "matrix_presets"):
        cmd.extend(["--matrix-preset", preset])
    for preset in profile_list(args, "corpus_recut_preset", "corpus_recut_presets"):
        cmd.extend(["--corpus-recut-preset", preset])
    for dsl in args.dsl or DEFAULT_DSLS:
        cmd.extend(["--dsl", dsl])
    for option, key in (
        ("--corpus-limit", "corpus_limit"),
        ("--matrix-limit", "matrix_limit"),
        ("--dsl-limit", "dsl_limit"),
        ("--corpus-recut-source-limit", "corpus_recut_source_limit"),
        ("--corpus-recut-limit", "corpus_recut_limit"),
    ):
        value = profile_value(args, key)
        if value:
            cmd.extend([option, str(value)])
    if args.corpus_recut_require_exact_bbox_probe:
        cmd.append("--corpus-recut-require-exact-bbox-probe")
    if args.corpus_recut_no_exact_bbox_probe:
        cmd.append("--corpus-recut-no-exact-bbox-probe")
    if args.hash_inputs:
        cmd.append("--hash-inputs")
    if args.hash_recipes:
        cmd.append("--hash-recipes")
    if args.bundle_zip:
        cmd.append("--bundle-zip")
    if args.fail_on_failures:
        cmd.append("--fail-on-failures")
    if args.skip_known_bug_regression:
        cmd.append("--skip-known-bug-regression")
    if args.known_bug_fail_on_fixed:
        cmd.append("--known-bug-fail-on-fixed")
    if args.known_bug_fail_on_changed:
        cmd.append("--known-bug-fail-on-changed")
    if args.known_bug_fail_on_unavailable:
        cmd.append("--known-bug-fail-on-unavailable")
    if args.skip_oracle_coverage:
        cmd.append("--skip-oracle-coverage")
    elif args.oracle_coverage_min_kinds != 1:
        cmd.extend(["--oracle-coverage-min-kinds", str(args.oracle_coverage_min_kinds)])
    if args.skip_artifact_verify:
        cmd.append("--skip-artifact-verify")
    if args.verify_allow_duplicate_inputs:
        cmd.append("--artifact-verify-allow-duplicate-inputs")
    if args.verify_allow_duplicate_geometry:
        cmd.append("--artifact-verify-allow-duplicate-geometry")
    if args.verify_allow_tolerance_mismatches:
        cmd.append("--artifact-verify-allow-tolerance-mismatches")
    return cmd


def build_preflight_command(args: argparse.Namespace, script_dir: Path, out_dir: Path, dataset_lists: list[str]) -> list[str]:
    cmd = [
        sys.executable,
        str(script_dir / "preflight_campaign.py"),
        "--runner",
        str(Path(args.runner).resolve()),
        "--out",
        str(out_dir / "preflight"),
    ]
    if dataset_lists:
        for dataset_list in dataset_lists:
            cmd.extend(["--dataset-list", dataset_list])
    else:
        for root in args.dataset_root:
            cmd.extend(["--dataset-root", root])
        if args.discover_limit:
            cmd.extend(["--discover-limit", str(args.discover_limit)])
        else:
            cmd.extend(["--discover-limit", "20"])
    for source_root in args.source_root:
        cmd.extend(["--source-root", source_root])
    for dsl in args.dsl or DEFAULT_DSLS:
        cmd.extend(["--dsl", dsl])
    for record in args.bug_record:
        cmd.extend(["--bug-record", record])
    if args.skip_known_bug_regression:
        cmd.append("--skip-bug-record-check")
    if args.hash_inputs:
        cmd.append("--hash-inputs")
    if args.skip_dataset_audit:
        cmd.append("--skip-dataset-audit")
    if args.dataset_audit_require_hashes:
        cmd.append("--dataset-audit-require-hashes")
    if args.dataset_audit_fail_duplicate_ratio >= 0:
        cmd.extend(["--dataset-audit-fail-duplicate-ratio", str(args.dataset_audit_fail_duplicate_ratio)])
    if args.profile_cad_features or args.require_cad_feature_profile or args.use_cad_feature_subset:
        cmd.append("--profile-cad-features")
        cmd.extend(["--cad-feature-min-score", str(args.cad_feature_min_score)])
    if args.require_cad_feature_profile:
        cmd.append("--require-cad-feature-profile")
    if args.include_artifacts:
        cmd.append("--discover-include-artifacts")
    if args.include_build:
        cmd.append("--discover-include-build")
    for exclude in args.exclude_dir:
        cmd.extend(["--discover-exclude-dir", exclude])
    return cmd


def build_collect_command(args: argparse.Namespace, script_dir: Path, out_dir: Path) -> list[str]:
    cmd = [
        sys.executable,
        str(script_dir / "collect_campaign_shards.py"),
    ]
    for shard_index in range(args.shards):
        cmd.extend(["--campaign", str(out_dir / "shards" / f"shard_{shard_index}of{args.shards}")])
    cmd.extend(["--out", str(out_dir / "merged")])
    if args.materialize_bug_records:
        cmd.append("--materialize-bug-records")
    if args.promote_bug_records:
        cmd.append("--promote-bug-records")
    if args.replay_promoted_bug_records:
        cmd.append("--replay-promoted-bug-records")
        cmd.extend(["--promoted-replay-timeout", str(args.promoted_replay_timeout)])
        cmd.extend(["--promoted-replay-jobs", str(args.promoted_replay_jobs)])
    if args.validate_recipes:
        cmd.append("--validate-recipes")
    if args.skip_oracle_coverage:
        cmd.append("--skip-oracle-coverage")
    if args.oracle_coverage_min_kinds != 1:
        cmd.extend(["--oracle-coverage-min-kinds", str(args.oracle_coverage_min_kinds)])
    if args.replay_reductions or args.replay_promoted_bug_records:
        cmd.extend(["--runner", str(Path(args.runner).resolve())])
    if args.replay_reductions:
        cmd.append("--replay-reductions")
        cmd.extend(["--reduction-replay-timeout", str(args.reduction_replay_timeout)])
        cmd.extend(["--reduction-replay-jobs", str(args.reduction_replay_jobs)])
        if args.reduction_replay_limit:
            cmd.extend(["--reduction-replay-limit", str(args.reduction_replay_limit)])
    if args.export_reduction_bug_record_drafts:
        cmd.append("--export-reduction-bug-record-drafts")
    if args.materialize_reduction_bug_records:
        cmd.append("--materialize-reduction-bug-records")
    return cmd


def build_verify_command(args: argparse.Namespace, script_dir: Path, out_dir: Path) -> list[str]:
    if args.skip_artifact_verify:
        return []
    cmd = [
        sys.executable,
        str(script_dir / "verify_campaign_artifacts.py"),
        "--campaign",
        str(out_dir / "merged"),
        "--out",
        str(out_dir / "merged" / "campaign_verification"),
    ]
    if args.verify_allow_duplicate_inputs:
        cmd.append("--allow-duplicate-inputs")
    if args.verify_allow_duplicate_geometry:
        cmd.append("--allow-duplicate-geometry")
    if args.verify_allow_tolerance_mismatches:
        cmd.append("--allow-tolerance-mismatches")
    return cmd


def write_scripts(
    out_dir: Path,
    shard_commands: list[list[str]],
    collect_command: list[str],
    verify_command: list[str],
    preflight_command: list[str],
) -> dict[str, Any]:
    commands_dir = out_dir / "commands"
    command_records: list[dict[str, str]] = []
    preflight_path = commands_dir / "preflight.ps1"
    write_text(preflight_path, "\n".join(script_lines_for_command(preflight_command)))
    for index, cmd in enumerate(shard_commands):
        script_path = commands_dir / f"run_shard_{index}of{len(shard_commands)}.ps1"
        write_text(script_path, "\n".join(script_lines_for_command(cmd)))
        command_records.append({"name": f"run_shard_{index}of{len(shard_commands)}", "script": str(script_path), "command": command_text(cmd)})
    collect_path = commands_dir / "collect_shards.ps1"
    collect_commands = [collect_command] + ([verify_command] if verify_command else [])
    write_text(collect_path, "\n".join(script_lines_for_commands(collect_commands)))
    run_all_lines = ["$ErrorActionPreference = 'Stop'", ""]
    for record in command_records:
        run_all_lines.extend([f"& {ps_quote(record['script'])}", "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }", ""])
    run_all_lines.extend([f"& {ps_quote(collect_path)}", "exit $LASTEXITCODE", ""])
    run_all_path = commands_dir / "run_all_sequential.ps1"
    write_text(run_all_path, "\n".join(run_all_lines))
    run_all_preflight_lines = ["$ErrorActionPreference = 'Stop'", "", f"& {ps_quote(preflight_path)}", "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }", ""]
    run_all_preflight_lines.extend(run_all_lines[2:])
    run_all_with_preflight_path = commands_dir / "run_all_with_preflight.ps1"
    write_text(run_all_with_preflight_path, "\n".join(run_all_preflight_lines))
    post_commands = [command_text(collect_command)] + ([command_text(verify_command)] if verify_command else [])
    command_list = "\n".join([command_text(preflight_command)] + [record["command"] for record in command_records] + post_commands) + "\n"
    command_list_path = commands_dir / "command_list.txt"
    write_text(command_list_path, command_list)
    return {
        "commands_dir": str(commands_dir),
        "preflight": str(preflight_path),
        "run_all": str(run_all_path),
        "run_all_with_preflight": str(run_all_with_preflight_path),
        "collect": str(collect_path),
        "command_list": str(command_list_path),
        "shards": command_records,
        "preflight_command": command_text(preflight_command),
        "artifact_verify": command_text(verify_command) if verify_command else "",
    }


def write_report(path: Path, plan: dict[str, Any]) -> None:
    lines = [
        "# SGGK Large Campaign Plan",
        "",
        f"- Generated: `{plan['generated_at']}`",
        f"- Profile: `{plan['profile']}`",
        f"- Shards: `{plan['shards']}`",
        f"- Runner: `{plan['runner']}`",
        f"- Corpus recut exact probe: require=`{plan.get('corpus_recut_require_exact_bbox_probe')}` disabled=`{plan.get('corpus_recut_no_exact_bbox_probe')}`",
        f"- Stable-failure reduction: enabled=`{plan.get('reduce_stable_failures')}` limit=`{plan.get('reduction_limit')}` max_trials=`{plan.get('reduction_max_trials')}`",
        f"- Merged reduced-recipe replay: enabled=`{plan.get('replay_reductions')}` limit=`{plan.get('reduction_replay_limit')}` jobs=`{plan.get('reduction_replay_jobs')}`",
        f"- Reduced bug-record drafts: `{plan.get('export_reduction_bug_record_drafts')}`",
        f"- Materialized reduced bug records: `{plan.get('materialize_reduction_bug_records')}`",
        f"- Promoted bug records: `{plan.get('promote_bug_records')}`",
        f"- Promoted bug-record replay: `{plan.get('replay_promoted_bug_records')}` timeout=`{plan.get('promoted_replay_timeout')}` jobs=`{plan.get('promoted_replay_jobs')}`",
        f"- Preflight: `{plan['scripts']['preflight']}`",
        f"- Run all: `{plan['scripts']['run_all']}`",
        f"- Run all with preflight: `{plan['scripts']['run_all_with_preflight']}`",
        f"- Collect: `{plan['scripts']['collect']}`",
        f"- Merged oracle coverage: `{not plan.get('skip_oracle_coverage')}` min_kinds=`{plan.get('oracle_coverage_min_kinds')}`",
        f"- Dataset audit: enabled=`{not plan.get('skip_dataset_audit')}` require_hashes=`{plan.get('dataset_audit_require_hashes')}` duplicate_ratio_gate=`{plan.get('dataset_audit_fail_duplicate_ratio')}`",
        f"- CAD feature profile: enabled=`{plan.get('profile_cad_features')}` min_score=`{plan.get('cad_feature_min_score')}` required=`{plan.get('require_cad_feature_profile')}` use_subset=`{plan.get('use_cad_feature_subset')}`",
        f"- Artifact verification: `{bool(plan.get('verify_command'))}`",
        "",
        "## Dataset Lists",
        "",
        f"- Original dataset lists: `{plan.get('original_dataset_lists')}`",
        f"- Shard dataset lists: `{plan.get('shard_dataset_lists')}`",
        "",
        "## CAD Feature Profile",
        "",
    ]
    profile = plan.get("cad_feature_profile", {})
    if profile.get("skipped"):
        lines.append(f"- Skipped: `{profile.get('reason')}`")
    else:
        lines.append(f"- Profile: `{profile.get('profile_path')}`")
        lines.append(f"- Report: `{profile.get('report_path')}`")
        lines.append(f"- Complex subset: `{profile.get('subset_path')}`")
        lines.append(f"- Files: `{profile.get('total_files')}`")
        lines.append(f"- Complex files: `{profile.get('complex_file_count')}`")
        lines.append(f"- Feature totals: `{profile.get('feature_totals')}`")
    lines.extend(
        [
            "",
            "## Frozen Corpus Discovery",
            "",
        ]
    )
    discovery = plan.get("discovery", {})
    if discovery.get("skipped"):
        lines.append(f"- Skipped: `{discovery.get('reason')}`")
    else:
        lines.append(f"- Index: `{discovery.get('index_path')}`")
        lines.append(f"- Report: `{discovery.get('report_path')}`")
        lines.append(f"- Files: `{discovery.get('total_files')}`")
        lines.append(f"- Bytes: `{discovery.get('total_bytes')}`")
        lines.append(f"- By extension: `{discovery.get('by_extension')}`")
        lines.append(f"- Duplicate content groups: `{discovery.get('duplicate_content_groups')}`")
    lines.extend(["", "## Shard Commands", "", "| shard | script | output |", "| ---: | --- | --- |"])
    for shard in plan.get("shard_commands", []):
        lines.append(f"| {shard['shard_index']} | `{shard['script']}` | `{shard['out']}` |")
    lines.extend(
        [
            "",
            "## Output Contract",
            "",
            "- Run `commands/preflight.ps1` before long runs to check runner/extractor, DSL, checked-in bug records, dataset audit, dataset inputs, and source roots.",
            "- Each shard writes `campaign_report.md`, aggregate triage, bug registry, debug handoff, bug-record drafts, and known-bug regression unless disabled.",
            "- After all shards finish, run the collect script to produce merged `campaign_shards_report.md`, `oracle_coverage/` unless skipped, `bug_registry/`, `debug_handoff/`, optional promoted/materialized bug records, and `campaign_verification/` unless artifact verification was skipped.",
            "",
        ]
    )
    write_text(path, "\n".join(lines))


def main() -> int:
    args = parse_args()
    if args.shards <= 0:
        print("--shards must be >= 1", file=sys.stderr)
        return 2
    if args.jobs <= 0:
        print("--jobs must be >= 1", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be > 0", file=sys.stderr)
        return 2
    if args.corpus_recut_require_exact_bbox_probe and args.corpus_recut_no_exact_bbox_probe:
        print("--corpus-recut-require-exact-bbox-probe cannot be combined with --corpus-recut-no-exact-bbox-probe", file=sys.stderr)
        return 2
    if args.oracle_coverage_min_kinds < 0:
        print("--oracle-coverage-min-kinds must be >= 0", file=sys.stderr)
        return 2
    if args.dataset_audit_fail_duplicate_ratio > 1:
        print("--dataset-audit-fail-duplicate-ratio must be <= 1.0 or negative to disable", file=sys.stderr)
        return 2
    if args.cad_feature_min_score < 0:
        print("--cad-feature-min-score must be >= 0", file=sys.stderr)
        return 2
    if args.reduction_limit < 0:
        print("--reduction-limit must be >= 0", file=sys.stderr)
        return 2
    if args.reduction_max_trials <= 0:
        print("--reduction-max-trials must be >= 1", file=sys.stderr)
        return 2
    if args.reduction_timeout < 0:
        print("--reduction-timeout must be >= 0", file=sys.stderr)
        return 2
    if args.reduction_min_dimension <= 0:
        print("--reduction-min-dimension must be > 0", file=sys.stderr)
        return 2
    if args.reduction_replay_timeout <= 0:
        print("--reduction-replay-timeout must be > 0", file=sys.stderr)
        return 2
    if args.reduction_replay_jobs <= 0:
        print("--reduction-replay-jobs must be >= 1", file=sys.stderr)
        return 2
    if args.reduction_replay_limit < 0:
        print("--reduction-replay-limit must be >= 0", file=sys.stderr)
        return 2
    if args.export_reduction_bug_record_drafts and not args.replay_reductions:
        print("--export-reduction-bug-record-drafts requires --replay-reductions", file=sys.stderr)
        return 2
    if args.materialize_reduction_bug_records and not args.export_reduction_bug_record_drafts:
        print("--materialize-reduction-bug-records requires --export-reduction-bug-record-drafts", file=sys.stderr)
        return 2
    if args.replay_promoted_bug_records and not args.promote_bug_records:
        print("--replay-promoted-bug-records requires --promote-bug-records", file=sys.stderr)
        return 2
    if args.replay_promoted_bug_records and args.promoted_replay_jobs <= 0:
        print("--promoted-replay-jobs must be >= 1", file=sys.stderr)
        return 2
    if args.replay_promoted_bug_records and args.promoted_replay_timeout <= 0:
        print("--promoted-replay-timeout must be > 0", file=sys.stderr)
        return 2
    runner = Path(args.runner).resolve()
    if not runner.is_file():
        print(f"runner not found: {runner}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    script_dir = Path(__file__).resolve().parent
    discovery = run_discovery(args, script_dir, out_dir)
    if discovery.get("returncode") not in (None, 0):
        print(discovery.get("stderr", ""), file=sys.stderr)
        return 1

    dataset_lists = list(args.dataset_list)
    if not discovery.get("skipped") and discovery.get("index_path"):
        dataset_lists.insert(0, str(discovery["index_path"]))

    original_dataset_lists = list(dataset_lists)
    cad_feature_profile = {"skipped": True, "reason": "not requested"}
    shard_dataset_lists = list(dataset_lists)
    if args.use_cad_feature_subset:
        cad_feature_profile = run_cad_feature_profile(args, script_dir, out_dir, original_dataset_lists)
        if not cad_feature_profile.get("ok"):
            print(cad_feature_profile.get("stderr") or cad_feature_profile.get("reason") or "CAD feature profile did not produce a non-empty subset", file=sys.stderr)
            return 1
        shard_dataset_lists = [str(cad_feature_profile["subset_path"])]

    shard_commands = [build_shard_command(args, script_dir, out_dir, shard_dataset_lists, shard_index) for shard_index in range(args.shards)]
    collect_command = build_collect_command(args, script_dir, out_dir)
    verify_command = build_verify_command(args, script_dir, out_dir)
    preflight_command = build_preflight_command(args, script_dir, out_dir, shard_dataset_lists)
    scripts = write_scripts(out_dir, shard_commands, collect_command, verify_command, preflight_command)
    plan_shards = []
    for index, cmd in enumerate(shard_commands):
        plan_shards.append(
            {
                "shard_index": index,
                "script": scripts["shards"][index]["script"],
                "out": str(out_dir / "shards" / f"shard_{index}of{args.shards}"),
                "command": cmd,
            }
        )
    plan = {
        "generated_at": now_iso_like(),
        "profile": args.profile,
        "shards": args.shards,
        "runner": str(runner),
        "out": str(out_dir),
        "dataset_roots": args.dataset_root,
        "dataset_lists": shard_dataset_lists,
        "original_dataset_lists": original_dataset_lists,
        "shard_dataset_lists": shard_dataset_lists,
        "source_roots": args.source_root,
        "source_scan_each_shard": args.source_scan_each_shard,
        "corpus_recut_require_exact_bbox_probe": args.corpus_recut_require_exact_bbox_probe,
        "corpus_recut_no_exact_bbox_probe": args.corpus_recut_no_exact_bbox_probe,
        "reduce_stable_failures": args.reduce_stable_failures,
        "reduction_limit": args.reduction_limit,
        "reduction_max_trials": args.reduction_max_trials,
        "reduction_timeout": args.reduction_timeout,
        "reduction_min_dimension": args.reduction_min_dimension,
        "replay_reductions": args.replay_reductions,
        "export_reduction_bug_record_drafts": args.export_reduction_bug_record_drafts,
        "materialize_reduction_bug_records": args.materialize_reduction_bug_records,
        "promote_bug_records": args.promote_bug_records,
        "replay_promoted_bug_records": args.replay_promoted_bug_records,
        "promoted_replay_timeout": args.promoted_replay_timeout,
        "promoted_replay_jobs": args.promoted_replay_jobs,
        "reduction_replay_timeout": args.reduction_replay_timeout,
        "reduction_replay_jobs": args.reduction_replay_jobs,
        "reduction_replay_limit": args.reduction_replay_limit,
        "discovery": discovery,
        "skip_dataset_audit": args.skip_dataset_audit,
        "dataset_audit_require_hashes": args.dataset_audit_require_hashes,
        "dataset_audit_fail_duplicate_ratio": args.dataset_audit_fail_duplicate_ratio,
        "profile_cad_features": args.profile_cad_features,
        "cad_feature_min_score": args.cad_feature_min_score,
        "require_cad_feature_profile": args.require_cad_feature_profile,
        "use_cad_feature_subset": args.use_cad_feature_subset,
        "cad_feature_profile": cad_feature_profile,
        "preflight_command": preflight_command,
        "shard_commands": plan_shards,
        "collect_command": collect_command,
        "verify_command": verify_command,
        "skip_oracle_coverage": args.skip_oracle_coverage,
        "oracle_coverage_min_kinds": args.oracle_coverage_min_kinds,
        "skip_artifact_verify": args.skip_artifact_verify,
        "verify_allow_duplicate_inputs": args.verify_allow_duplicate_inputs,
        "verify_allow_duplicate_geometry": args.verify_allow_duplicate_geometry,
        "verify_allow_tolerance_mismatches": args.verify_allow_tolerance_mismatches,
        "scripts": scripts,
    }
    write_json(out_dir / "large_campaign_plan.json", plan)
    write_report(out_dir / "large_campaign_plan.md", plan)
    print(f"plan={out_dir / 'large_campaign_plan.json'}")
    print(f"report={out_dir / 'large_campaign_plan.md'}")
    print(f"preflight={scripts['preflight']}")
    print(f"run_all={scripts['run_all']}")
    print(f"run_all_with_preflight={scripts['run_all_with_preflight']}")
    print(f"collect={scripts['collect']}")
    print(f"shards={args.shards} dataset_lists={len(shard_dataset_lists)} original_dataset_lists={len(original_dataset_lists)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
