#!/usr/bin/env python3
"""Run an end-to-end SGGK corpus/generated-test campaign."""

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument("--out", default="artifacts/campaign", help="Campaign output root")
    parser.add_argument("--dataset-root", action="append", default=[], help="Corpus file or directory to discover")
    parser.add_argument("--dataset-list", action="append", default=[], help="Existing discover_corpus.py JSON or path list")
    parser.add_argument("--source-root", action="append", default=[], help="Source file or directory to scan with scan_source_risks.py")
    parser.add_argument("--source-scan-include-ext", default="", help="Comma-separated source extensions for scan_source_risks.py")
    parser.add_argument("--source-scan-exclude-dir", action="append", default=[], help="Directory name to skip during source-risk scanning")
    parser.add_argument("--source-scan-max-findings", type=int, default=500, help="Maximum source-risk findings to emit")
    parser.add_argument("--source-scan-max-seeds", type=int, default=120, help="Maximum source-risk attack seed drafts to emit")
    parser.add_argument("--source-task-max-tasks", type=int, default=120, help="Maximum source-attack tasks to build from source scan; 0 means all")
    parser.add_argument("--source-task-context-lines", type=int, default=12, help="Source context radius for source-attack tasks")
    parser.add_argument("--source-task-min-severity", choices=["critical", "high", "medium", "low"], default="medium")
    parser.add_argument("--source-task-write-dsl-seeds", action="store_true", help="Write review-required seed DSL files for source-attack tasks")
    parser.add_argument("--discover-include-artifacts", action="store_true", help="Pass --include-artifacts to discover_corpus.py")
    parser.add_argument("--discover-include-build", action="store_true", help="Pass --include-build to discover_corpus.py")
    parser.add_argument("--discover-exclude-dir", action="append", default=[], help="Additional directory name to skip during discovery")
    parser.add_argument("--discover-limit", type=int, default=0, help="Maximum files to write in the discovery index; 0 means all")
    parser.add_argument("--skip-dataset-audit", action="store_true", help="Do not audit frozen corpus dataset lists in the campaign artifacts")
    parser.add_argument("--dataset-audit-require-hashes", action="store_true", help="Fail dataset audit when sha1 hashes are missing")
    parser.add_argument("--dataset-audit-fail-duplicate-ratio", type=float, default=-1.0, help="Fail dataset audit when duplicate-file ratio exceeds this value; negative disables")
    parser.add_argument("--jobs", type=int, default=2, help="Parallel runner processes for recipe/corpus lanes")
    parser.add_argument("--shard-count", type=int, default=1, help="Total number of stable shards for corpus and recipe lanes")
    parser.add_argument("--shard-index", type=int, default=0, help="Zero-based shard index for corpus and recipe lanes")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-case timeout in seconds")
    parser.add_argument("--corpus-limit", type=int, default=0, help="Maximum corpus files to run; 0 means all")
    parser.add_argument(
        "--corpus-preserve-input-order",
        action="store_true",
        help="Preserve explicit corpus dataset-list order before applying --corpus-limit",
    )
    parser.add_argument(
        "--corpus-sgt-api",
        action="append",
        choices=["check_sgt", "step_roundtrip", "iges_roundtrip"],
        help="API to run for .sgt corpus files. Can repeat; default is check_sgt.",
    )
    parser.add_argument("--corpus-source-body-index", type=int, default=0, help="Body index used by SGT roundtrip corpus APIs")
    parser.add_argument("--corpus-step-app-protocol", choices=["AP203", "AP214", "AP242"], default="AP203")
    parser.add_argument("--corpus-step-surface-to-bspline", action="store_true")
    parser.add_argument("--corpus-step-curve-to-bspline", action="store_true")
    parser.add_argument("--corpus-step-spcurve-in-wire-to-bspline", action="store_true")
    parser.add_argument("--corpus-iges-face-only-mode", action="store_true")
    parser.add_argument("--corpus-iges-write-sgk-specified-data", action="store_true")
    parser.add_argument("--corpus-roundtrip-abs-tol", type=float, default=0.01)
    parser.add_argument("--corpus-roundtrip-rel-tol", type=float, default=1e-5)
    parser.add_argument("--matrix-limit", type=int, default=0, help="Maximum matrix recipes to generate/run; 0 means all")
    parser.add_argument("--dsl-limit", type=int, default=0, help="Maximum compiled recipes to run per DSL lane; 0 means all")
    parser.add_argument("--corpus-recut-limit", type=int, default=0, help="Maximum corpus recut recipes to generate/run; 0 means all")
    parser.add_argument("--corpus-recut-source-limit", type=int, default=0, help="Maximum SGT corpus sources to recut; 0 means all")
    parser.add_argument("--matrix-preset", action="append", choices=["smoke", "standard", "stress"], help="Matrix preset; can repeat")
    parser.add_argument("--corpus-recut-preset", action="append", choices=["smoke", "standard", "stress"], help="Corpus recut preset; can repeat")
    parser.add_argument(
        "--corpus-recut-use",
        choices=["auto", "original", "artifacts", "both"],
        default="auto",
        help="Corpus recut sources: original dataset SGTs, corpus result artifacts, both, or auto=artifacts when available else original",
    )
    parser.add_argument("--dsl", action="append", default=[], help="DSL file or directory to compile and run; can repeat")
    parser.add_argument("--skip-corpus", action="store_true", help="Skip corpus discovery/running")
    parser.add_argument("--skip-source-scan", action="store_true", help="Skip source-risk scanning even when --source-root is provided")
    parser.add_argument("--skip-source-attack-tasks", action="store_true", help="Skip building source-attack task JSONL from source-risk scan output")
    parser.add_argument("--skip-corpus-recut", action="store_true", help="Skip loaded-SGT corpus recut recipe lanes")
    parser.add_argument("--skip-corpus-recut-artifacts", action="store_true", help="Do not recut SGT result artifacts produced by the corpus lane")
    parser.add_argument("--skip-matrix", action="store_true", help="Skip generated boolean matrix lanes")
    parser.add_argument("--skip-dsl", action="store_true", help="Skip DSL compile/run lanes")
    parser.add_argument("--skip-aggregate-triage", action="store_true", help="Skip triage across all campaign lanes")
    parser.add_argument("--skip-replay", action="store_true", help="Skip replaying aggregate regression seeds")
    parser.add_argument("--probe-topotrack-crashes", action="store_true", help="Probe runner crashes that may be topo-track-only diagnostics")
    parser.add_argument("--topotrack-probe-timeout", type=float, default=60.0, help="Per-recipe timeout for topo-track crash probes")
    parser.add_argument("--topotrack-probe-jobs", type=int, default=1, help="Parallel jobs for topo-track crash probes")
    parser.add_argument("--topotrack-probe-limit", type=int, default=0, help="Maximum selected topo-track crash cases per lane; 0 means all")
    parser.add_argument("--skip-bundles", action="store_true", help="Skip exporting failure bundles")
    parser.add_argument("--skip-bug-registry", action="store_true", help="Skip collecting triage/replay/bundle outputs into a bug registry")
    parser.add_argument("--skip-debug-handoff", action="store_true", help="Skip GUI-ready debug SGT handoff pack generation")
    parser.add_argument("--skip-bug-record-drafts", action="store_true", help="Skip exporting editable bug-record drafts")
    parser.add_argument("--skip-known-bug-regression", action="store_true", help="Skip replaying checked-in bug records")
    parser.add_argument("--promote-bug-records", action="store_true", help="Promote generated bug-record drafts into artifact-local portable candidates")
    parser.add_argument(
        "--replay-promoted-bug-records",
        action="store_true",
        help="Materialize, replay, and classify promoted bug-record candidates from the promoted root",
    )
    parser.add_argument("--promoted-replay-timeout", type=float, default=60.0, help="Per-recipe timeout for promoted bug-record replay")
    parser.add_argument("--promoted-replay-jobs", type=int, default=1, help="Parallel jobs for promoted bug-record replay")
    parser.add_argument(
        "--bug-record",
        action="append",
        default=[],
        help="Checked-in bug-record JSON file or directory. Defaults to test_harness/bug_records when omitted.",
    )
    parser.add_argument("--known-bug-fail-on-fixed", action="store_true", help="Return campaign failure when a known bug no longer reproduces")
    parser.add_argument("--known-bug-fail-on-changed", action="store_true", help="Return campaign failure when a known bug fails differently")
    parser.add_argument("--known-bug-fail-on-unavailable", action="store_true", help="Return campaign failure when a known-bug replay recipe is unavailable")
    parser.add_argument("--bug-record-prefix", default="campaign", help="bug_id prefix for generated bug-record drafts")
    parser.add_argument("--bundle-zip", action="store_true", help="Create zip archives for exported failure bundles")
    parser.add_argument("--skip-oracle-coverage", action="store_true", help="Do not summarize validation/oracle coverage at the end")
    parser.add_argument(
        "--oracle-coverage-min-kinds",
        type=int,
        default=1,
        help="Minimum classified oracle kinds required for passed cases in the oracle coverage gate",
    )
    parser.add_argument("--skip-artifact-verify", action="store_true", help="Do not run campaign artifact verification at the end")
    parser.add_argument(
        "--artifact-verify-allow-duplicate-inputs",
        action="store_true",
        help="Do not fail artifact verification on geometry duplicate input groups",
    )
    parser.add_argument(
        "--artifact-verify-allow-duplicate-geometry",
        action="store_true",
        help="Do not fail artifact verification on full-geometry duplicate groups",
    )
    parser.add_argument(
        "--artifact-verify-allow-tolerance-mismatches",
        action="store_true",
        help="Do not fail artifact verification on geometry-audit tolerance mismatches",
    )
    parser.add_argument(
        "--artifact-verify-expect-known-bug-status",
        action="append",
        default=[],
        help="Require a known-bug regression status key during artifact verification; can repeat",
    )
    parser.add_argument("--no-preview", action="store_true", help="Skip preview/contact-sheet rendering for recipe lanes")
    parser.add_argument("--no-geometry-audit", action="store_true", help="Skip bbox geometry audit for recipe lanes")
    parser.add_argument("--geometry-audit-fail-on-duplicates", action="store_true", help="Fail recipe lanes when same-boolean duplicate input geometry is found")
    parser.add_argument("--geometry-audit-fail-on-tolerance-mismatch", action="store_true", help="Fail recipe lanes when inferred tolerance offsets mismatch")
    parser.add_argument("--resume", action="store_true", help="Resume previous passing cases in lane outputs")
    parser.add_argument("--hash-inputs", action="store_true", help="Hash discovered and corpus inputs")
    parser.add_argument("--hash-recipes", action="store_true", help="Hash recipe inputs in recipe lane manifests")
    parser.add_argument("--corpus-recut-topo-track", action="store_true", help="Enable SDK topo tracking in generated corpus recut recipes")
    parser.add_argument(
        "--corpus-recut-sample-input-properties",
        action="store_true",
        help="Sample target/tool input properties in generated corpus recut recipes",
    )
    parser.add_argument(
        "--corpus-recut-require-exact-bbox-probe",
        action="store_true",
        help="Skip corpus recut sources whose coordinate-plane exact bbox probe fails",
    )
    parser.add_argument(
        "--corpus-recut-no-exact-bbox-probe",
        action="store_true",
        help="Disable coordinate-plane exact bbox probing for corpus recut generation",
    )
    parser.add_argument("--triage-include-passed", action="store_true", help="Include passed cases in triage summaries")
    parser.add_argument("--replay-retries", type=int, default=3, help="Replay attempts per aggregate seed")
    parser.add_argument("--replay-limit", type=int, default=0, help="Maximum aggregate seeds to replay; 0 means all")
    parser.add_argument("--reduce-stable-failures", action="store_true", help="Run reduce_failure_recipe.py for stable replay failures with flat recipes")
    parser.add_argument("--reduction-limit", type=int, default=3, help="Maximum stable replay failures to reduce when --reduce-stable-failures is set; 0 means all")
    parser.add_argument("--reduction-max-trials", type=int, default=60, help="Maximum reducer trials per selected stable failure")
    parser.add_argument("--reduction-timeout", type=float, default=0.0, help="Per-reducer-trial timeout; 0 reuses --timeout")
    parser.add_argument("--reduction-min-dimension", type=float, default=0.01, help="Minimum positive dimension used by reduce_failure_recipe.py")
    parser.add_argument(
        "--fail-on-failures",
        action="store_true",
        help="Return exit code 2 when aggregate triage finds failures or replay finds stable failures",
    )
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def sanitize_name(value: str) -> str:
    result = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    result = "_".join(part for part in result.split("_") if part)
    return result or "lane"


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def run_command(name: str, cmd: list[str], acceptable: set[int] | None = None, cwd: Path | None = None) -> dict[str, Any]:
    if acceptable is None:
        acceptable = {0}
    print(f"[campaign] {name}")
    print("  " + " ".join(cmd))
    if cwd is not None:
        print(f"  cwd={cwd}")
    started = time.perf_counter()
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
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
    record = {
        "name": name,
        "command": cmd,
        "returncode": completed.returncode,
        "acceptable": sorted(acceptable),
        "ok": completed.returncode in acceptable,
        "elapsed_seconds": elapsed,
        "cwd": str(cwd) if cwd is not None else "",
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    return record


def append_common_run_flags(cmd: list[str], args: argparse.Namespace) -> None:
    cmd.extend(["--timeout", str(args.timeout), "--jobs", str(args.jobs)])
    if args.shard_count != 1 or args.shard_index != 0:
        cmd.extend(["--shard-count", str(args.shard_count), "--shard-index", str(args.shard_index)])
    if args.resume:
        cmd.append("--resume")


def summarize_lane(lane: dict[str, Any]) -> dict[str, Any]:
    summary_path = Path(lane.get("summary_path", ""))
    summary = read_json(summary_path) if str(summary_path) else None
    if not isinstance(summary, dict):
        return lane
    for key in ("total", "executed", "skipped", "passed", "failed", "timed_out"):
        if key in summary:
            lane[key] = summary[key]
    if summary.get("empty_shard"):
        lane["empty_shard"] = True
        lane["preview_out"] = ""
        lane["contact_sheet"] = ""
        lane["geometry_audit_out"] = ""
    triage = summary.get("triage")
    if isinstance(triage, dict):
        lane["triage_returncode"] = triage.get("returncode")
        lane["triage_out"] = triage.get("out")
    preview = summary.get("preview")
    if isinstance(preview, dict):
        lane["preview_returncode"] = preview.get("returncode")
        lane["preview_out"] = preview.get("out")
        lane["contact_sheet"] = preview.get("contact_sheet")
    geometry_audit = summary.get("geometry_audit")
    if isinstance(geometry_audit, dict):
        lane["geometry_audit_returncode"] = geometry_audit.get("returncode")
        lane["geometry_audit_out"] = geometry_audit.get("out")
        audit_out = Path(str(geometry_audit.get("out") or ""))
        audit_summary = read_json(audit_out / "geometry_audit.json") if str(audit_out) else None
        if isinstance(audit_summary, dict):
            lane["geometry_audit_report"] = str(audit_out / "geometry_audit.md")
            lane["geometry_audit_cases"] = audit_summary.get("total_cases")
            lane["geometry_audit_duplicate_inputs"] = len(audit_summary.get("same_boolean_duplicate_input_groups", []))
            lane["geometry_audit_duplicate_geometry"] = len(audit_summary.get("duplicate_geometry_groups", []))
            lane["geometry_audit_tolerance_mismatches"] = len(audit_summary.get("tolerance_mismatches", []))
    return lane


def run_source_scan(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if args.skip_source_scan:
        return {"skipped": True, "reason": "skip_source_scan"}
    if not args.source_root:
        return None
    scan_out = out_root / "source_scan"
    cmd = [
        sys.executable,
        str(script_dir / "scan_source_risks.py"),
        *args.source_root,
        "--out",
        str(scan_out),
        "--max-findings",
        str(args.source_scan_max_findings),
        "--max-seeds",
        str(args.source_scan_max_seeds),
    ]
    if args.source_scan_include_ext:
        cmd.extend(["--include-ext", args.source_scan_include_ext])
    for name in args.source_scan_exclude_dir:
        cmd.extend(["--exclude-dir", name])
    record = run_command("scan_source_risks", cmd)
    command_records.append(record)
    report_path = scan_out / "source_risk_report.json"
    report = read_json(report_path)
    result: dict[str, Any] = {
        "out": str(scan_out),
        "summary_path": str(report_path),
        "report_path": str(scan_out / "source_risk_report.md"),
        "seed_path": str(scan_out / "attack_seed_drafts.json"),
        "source_files_path": str(scan_out / "source_risk_files.txt"),
        "returncode": record["returncode"],
        "ok": record["ok"],
    }
    if isinstance(report, dict):
        scan = report.get("scan") if isinstance(report.get("scan"), dict) else {}
        result["files_scanned"] = scan.get("files_scanned")
        result["findings"] = len(report.get("findings", [])) if isinstance(report.get("findings"), list) else scan.get("emitted_findings")
        result["attack_seed_drafts"] = len(report.get("attack_seed_drafts", [])) if isinstance(report.get("attack_seed_drafts"), list) else None
        result["severity_counts"] = report.get("severity_counts")
        result["category_counts"] = report.get("category_counts")
        result["candidate_truncated"] = scan.get("candidate_truncated")
    if record["ok"] and not args.skip_source_attack_tasks:
        task_out = out_root / "source_attack_tasks"
        task_cmd = [
            sys.executable,
            str(script_dir / "build_source_attack_tasks.py"),
            str(report_path),
            "--out",
            str(task_out),
            "--max-tasks",
            str(args.source_task_max_tasks),
            "--context-lines",
            str(args.source_task_context_lines),
            "--min-severity",
            args.source_task_min_severity,
        ]
        if args.source_task_write_dsl_seeds:
            task_cmd.append("--write-dsl-seeds")
        task_record = run_command("build_source_attack_tasks", task_cmd)
        command_records.append(task_record)
        task_payload = read_json(task_out / "source_attack_tasks.json")
        result["tasks"] = {
            "out": str(task_out),
            "json": str(task_out / "source_attack_tasks.json"),
            "jsonl": str(task_out / "source_attack_tasks.jsonl"),
            "manifest": str(task_out / "source_attack_task_manifest.md"),
            "ids": str(task_out / "source_attack_task_ids.txt"),
            "returncode": task_record["returncode"],
            "ok": task_record["ok"],
            "task_count": task_payload.get("task_count") if isinstance(task_payload, dict) else None,
        }
        result["ok"] = bool(result.get("ok")) and task_record["ok"]
    return result


def run_corpus_lane(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    out_root: Path,
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if args.skip_corpus:
        return None
    dataset_lists = list(args.dataset_list)
    discovery_path = out_root / "discovery" / "dataset_index.json"
    if args.dataset_root:
        cmd = [
            sys.executable,
            str(script_dir / "discover_corpus.py"),
            *args.dataset_root,
            "--out",
            str(discovery_path),
        ]
        if args.hash_inputs:
            cmd.append("--hash-inputs")
        if args.discover_include_artifacts:
            cmd.append("--include-artifacts")
        if args.discover_include_build:
            cmd.append("--include-build")
        if args.discover_limit:
            cmd.extend(["--limit", str(args.discover_limit)])
        for name in args.discover_exclude_dir:
            cmd.extend(["--exclude-dir", name])
        record = run_command("discover_corpus", cmd)
        command_records.append(record)
        if not record["ok"]:
            return {"name": "corpus", "type": "corpus", "skipped": True, "error": "discover_corpus_failed"}
        dataset_lists.append(str(discovery_path))
    if not dataset_lists:
        return {"name": "corpus", "type": "corpus", "skipped": True, "reason": "no dataset roots or lists"}

    run_out = out_root / "runs" / "corpus"
    triage_out = out_root / "triage" / "corpus"
    cmd = [
        sys.executable,
        str(script_dir / "run_corpus.py"),
        "--runner",
        str(runner),
        "--out",
        str(run_out),
        "--triage-out",
        str(triage_out),
    ]
    for dataset_list in dataset_lists:
        cmd.extend(["--dataset-list", dataset_list])
    append_common_run_flags(cmd, args)
    if args.corpus_limit:
        cmd.extend(["--limit", str(args.corpus_limit)])
    if args.corpus_preserve_input_order:
        cmd.append("--preserve-input-order")
    for api in args.corpus_sgt_api or []:
        cmd.extend(["--sgt-api", api])
    cmd.extend(["--source-body-index", str(args.corpus_source_body_index)])
    cmd.extend(["--step-app-protocol", args.corpus_step_app_protocol])
    if args.corpus_step_surface_to_bspline:
        cmd.append("--step-surface-to-bspline")
    if args.corpus_step_curve_to_bspline:
        cmd.append("--step-curve-to-bspline")
    if args.corpus_step_spcurve_in_wire_to_bspline:
        cmd.append("--step-spcurve-in-wire-to-bspline")
    if args.corpus_iges_face_only_mode:
        cmd.append("--iges-face-only-mode")
    if args.corpus_iges_write_sgk_specified_data:
        cmd.append("--iges-write-sgk-specified-data")
    cmd.extend(["--roundtrip-abs-tol", str(args.corpus_roundtrip_abs_tol)])
    cmd.extend(["--roundtrip-rel-tol", str(args.corpus_roundtrip_rel_tol)])
    if args.hash_inputs:
        cmd.append("--hash-inputs")
    if args.triage_include_passed:
        cmd.append("--triage-include-passed")
    record = run_command("run_corpus", cmd, acceptable={0, 2})
    command_records.append(record)
    return summarize_lane(
        {
            "name": "corpus",
            "type": "corpus",
            "out": str(run_out),
            "summary_path": str(run_out / "corpus_summary.json"),
            "triage_out": str(triage_out),
            "returncode": record["returncode"],
            "ok": record["ok"],
        }
    )


def run_matrix_lanes(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    out_root: Path,
    command_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if args.skip_matrix:
        return []
    lanes: list[dict[str, Any]] = []
    presets = args.matrix_preset or ["smoke"]
    for preset in presets:
        lane_name = f"matrix_{preset}"
        recipe_dir = out_root / "recipes" / lane_name
        manifest_path = out_root / "recipes" / f"{lane_name}_manifest.json"
        cmd = [
            sys.executable,
            str(script_dir / "generate_boolean_matrix.py"),
            "--out",
            str(recipe_dir),
            "--preset",
            preset,
            "--case-prefix",
            f"campaign_{preset}",
            "--manifest",
            str(manifest_path),
        ]
        if args.matrix_limit:
            cmd.extend(["--limit", str(args.matrix_limit)])
        record = run_command(f"generate_{lane_name}", cmd)
        command_records.append(record)
        if not record["ok"]:
            lanes.append({"name": lane_name, "type": "recipe", "skipped": True, "error": "generate_failed"})
            continue
        lanes.append(run_recipe_lane(args, script_dir, runner, out_root, command_records, lane_name, recipe_dir, limit=0))
    return lanes


def write_corpus_artifact_sgt_list(corpus_lane: dict[str, Any] | None, out_root: Path) -> str:
    if not isinstance(corpus_lane, dict) or not corpus_lane.get("out"):
        return ""
    run_out = Path(str(corpus_lane["out"]))
    if not run_out.is_dir():
        return ""
    result_paths = sorted(
        (path.resolve() for path in run_out.rglob("result_*.sgt") if path.is_file() and path.parent.name == "output"),
        key=lambda item: str(item).lower(),
    )
    if not result_paths:
        return ""
    list_path = out_root / "discovery" / "corpus_artifact_sgt.paths.txt"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text("\n".join(str(path) for path in result_paths) + "\n", encoding="utf-8")
    return str(list_path)


def run_corpus_recut_lanes(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    out_root: Path,
    command_records: list[dict[str, Any]],
    dataset_lists: list[str],
) -> list[dict[str, Any]]:
    if args.skip_corpus_recut:
        return []
    if not dataset_lists:
        return [{"name": "corpus_recut", "type": "recipe", "skipped": True, "reason": "no dataset roots or lists"}]
    lanes: list[dict[str, Any]] = []
    presets = args.corpus_recut_preset or ["smoke"]
    for preset in presets:
        lane_name = f"corpus_recut_{preset}"
        recipe_dir = out_root / "recipes" / lane_name
        manifest_path = out_root / "recipes" / f"{lane_name}_manifest.json"
        cmd = [
            sys.executable,
            str(script_dir / "generate_corpus_recut_matrix.py"),
            "--out",
            str(recipe_dir),
            "--preset",
            preset,
            "--case-prefix",
            f"campaign_corpus_recut_{preset}",
            "--manifest",
            str(manifest_path),
            "--runner",
            str(runner),
        ]
        for dataset_list in dataset_lists:
            cmd.extend(["--dataset-list", dataset_list])
        if args.corpus_recut_source_limit:
            cmd.extend(["--source-limit", str(args.corpus_recut_source_limit)])
        if args.corpus_recut_limit:
            cmd.extend(["--limit", str(args.corpus_recut_limit)])
        if args.corpus_recut_topo_track:
            cmd.append("--topo-track")
        if args.corpus_recut_sample_input_properties:
            cmd.append("--sample-input-properties")
        if args.corpus_recut_require_exact_bbox_probe:
            cmd.append("--require-exact-bbox-probe")
        if args.corpus_recut_no_exact_bbox_probe:
            cmd.append("--no-exact-bbox-probe")
        record = run_command(f"generate_{lane_name}", cmd)
        command_records.append(record)
        if not record["ok"]:
            lanes.append({"name": lane_name, "type": "recipe", "skipped": True, "error": "generate_failed"})
            continue
        manifest = read_json(manifest_path)
        recipe_count = manifest.get("recipe_count", 0) if isinstance(manifest, dict) else 0
        if not recipe_count:
            lanes.append(
                {
                    "name": lane_name,
                    "type": "recipe",
                    "skipped": True,
                    "reason": "no SGT corpus recut recipes generated",
                    "manifest_path": str(manifest_path),
                }
            )
            continue
        lane = run_recipe_lane(args, script_dir, runner, out_root, command_records, lane_name, recipe_dir, limit=0)
        if isinstance(manifest, dict):
            lane["generator_manifest_path"] = str(manifest_path)
            lane["generator_report_path"] = str(manifest_path.with_suffix(".md"))
            lane["generated_source_count"] = manifest.get("used_source_count")
            lane["generated_skipped_source_count"] = len(manifest.get("skipped_sources", [])) if isinstance(manifest.get("skipped_sources"), list) else 0
            lane["generated_recipe_count"] = manifest.get("recipe_count")
            exact_probe = manifest.get("exact_bbox_probe") if isinstance(manifest.get("exact_bbox_probe"), dict) else {}
            if exact_probe:
                lane["exact_bbox_probe_enabled"] = exact_probe.get("enabled")
                lane["exact_bbox_probe_require"] = exact_probe.get("require")
                lane["exact_bbox_probe_failure_count"] = exact_probe.get("failure_count")
                lane["exact_bbox_probe_bbox_sources"] = exact_probe.get("bbox_sources")
        lanes.append(lane)
    return lanes


def run_dsl_lanes(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    out_root: Path,
    command_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if args.skip_dsl:
        return []
    lanes: list[dict[str, Any]] = []
    dsl_paths = args.dsl or DEFAULT_DSLS
    for raw_path in dsl_paths:
        dsl_path = Path(raw_path)
        lane_name = f"dsl_{sanitize_name(dsl_path.stem)}"
        check_report = out_root / "dsl_checks" / f"{lane_name}.json"
        check_cmd = [
            sys.executable,
            str(script_dir / "compile_attack_dsl.py"),
            str(dsl_path),
            "--check",
            "--report",
            str(check_report),
        ]
        check_record = run_command(f"check_{lane_name}", check_cmd)
        command_records.append(check_record)
        check_summary = read_json(check_report)
        check_lane_fields = {
            "dsl": str(dsl_path),
            "dsl_check_report": str(check_report),
            "dsl_check_returncode": check_record["returncode"],
            "dsl_check_ok": check_record["ok"],
        }
        if isinstance(check_summary, dict):
            check_lane_fields.update(
                {
                    "dsl_check_file_count": check_summary.get("file_count"),
                    "dsl_check_recipe_count": check_summary.get("recipe_count"),
                    "dsl_check_compile_failure_count": check_summary.get("compile_failure_count"),
                    "dsl_check_validation_failure_count": check_summary.get("validation_failure_count"),
                }
            )
        if not check_record["ok"]:
            lanes.append(
                {
                    "name": lane_name,
                    "type": "recipe",
                    "skipped": True,
                    "error": "dsl_check_failed",
                    **check_lane_fields,
                }
            )
            continue
        recipe_dir = out_root / "recipes" / lane_name
        cmd = [
            sys.executable,
            str(script_dir / "compile_attack_dsl.py"),
            str(dsl_path),
            "--out",
            str(recipe_dir),
        ]
        record = run_command(f"compile_{lane_name}", cmd)
        command_records.append(record)
        if not record["ok"]:
            lanes.append(
                {
                    "name": lane_name,
                    "type": "recipe",
                    "skipped": True,
                    "error": "compile_failed",
                    **check_lane_fields,
                }
            )
            continue
        lane = run_recipe_lane(args, script_dir, runner, out_root, command_records, lane_name, recipe_dir, limit=args.dsl_limit)
        lane.update(check_lane_fields)
        lanes.append(lane)
    return lanes


def run_recipe_lane(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    out_root: Path,
    command_records: list[dict[str, Any]],
    lane_name: str,
    recipe_dir: Path,
    limit: int,
) -> dict[str, Any]:
    run_out = out_root / "runs" / lane_name
    triage_out = out_root / "triage" / lane_name
    preview_out = out_root / "previews" / lane_name
    geometry_audit_out = out_root / "geometry_audit" / lane_name
    cmd = [
        sys.executable,
        str(script_dir / "run_recipes.py"),
        "--runner",
        str(runner),
        "--recipe",
        str(recipe_dir),
        "--out",
        str(run_out),
        "--triage-out",
        str(triage_out),
    ]
    append_common_run_flags(cmd, args)
    if limit:
        cmd.extend(["--limit", str(limit)])
    if args.hash_recipes:
        cmd.append("--hash-recipes")
    if args.triage_include_passed:
        cmd.append("--triage-include-passed")
    if not args.no_preview:
        cmd.extend(["--preview-out", str(preview_out), "--contact-sheet", str(preview_out / "contact.png")])
    if not args.no_geometry_audit:
        cmd.extend(["--geometry-audit-out", str(geometry_audit_out)])
        if args.geometry_audit_fail_on_duplicates:
            cmd.append("--geometry-audit-fail-on-duplicates")
        if args.geometry_audit_fail_on_tolerance_mismatch:
            cmd.append("--geometry-audit-fail-on-tolerance-mismatch")
    record = run_command(f"run_{lane_name}", cmd, acceptable={0, 2})
    command_records.append(record)
    return summarize_lane(
        {
            "name": lane_name,
            "type": "recipe",
            "recipe_dir": str(recipe_dir),
            "out": str(run_out),
            "summary_path": str(run_out / "recipe_summary.json"),
            "triage_out": str(triage_out),
            "preview_out": str(preview_out) if not args.no_preview else "",
            "contact_sheet": str(preview_out / "contact.png") if not args.no_preview else "",
            "geometry_audit_out": str(geometry_audit_out) if not args.no_geometry_audit else "",
            "returncode": record["returncode"],
            "ok": record["ok"],
        }
    )


def run_aggregate_triage(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    lanes: list[dict[str, Any]],
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if args.skip_aggregate_triage:
        return None
    roots = [lane["out"] for lane in lanes if isinstance(lane, dict) and lane.get("out")]
    if not roots:
        return {"skipped": True, "reason": "no lane roots"}
    aggregate_out = out_root / "triage" / "aggregate"
    cmd = [sys.executable, str(script_dir / "triage_artifacts.py"), *roots, "--out", str(aggregate_out)]
    if args.triage_include_passed:
        cmd.append("--include-passed")
    record = run_command("triage_aggregate", cmd, acceptable={0, 2})
    command_records.append(record)
    triage_summary = read_json(aggregate_out / "triage_summary.json")
    result: dict[str, Any] = {
        "out": str(aggregate_out),
        "summary_path": str(aggregate_out / "triage_summary.json"),
        "report_path": str(aggregate_out / "triage_report.md"),
        "seeds_path": str(aggregate_out / "regression_seeds.json"),
        "returncode": record["returncode"],
        "ok": record["ok"],
    }
    if isinstance(triage_summary, dict):
        for key in ("total_cases", "passed_cases", "failed_cases", "failure_group_count", "warning_cases", "command_failures"):
            result[key] = triage_summary.get(key)
    return result


def run_replay(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    out_root: Path,
    aggregate: dict[str, Any] | None,
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if args.skip_replay or not aggregate or not aggregate.get("seeds_path"):
        return None
    seeds_path = Path(str(aggregate["seeds_path"]))
    seeds = read_json(seeds_path)
    if not isinstance(seeds, list) or not seeds:
        return {"skipped": True, "reason": "no aggregate seeds"}
    replay_out = out_root / "replay" / "aggregate"
    cmd = [
        sys.executable,
        str(script_dir / "replay_regression_seeds.py"),
        "--runner",
        str(runner),
        "--seeds",
        str(seeds_path),
        "--out",
        str(replay_out),
        "--retries",
        str(args.replay_retries),
        "--timeout",
        str(args.timeout),
    ]
    if args.replay_limit:
        cmd.extend(["--limit", str(args.replay_limit)])
    record = run_command("replay_aggregate", cmd, acceptable={0, 2})
    command_records.append(record)
    replay_summary = read_json(replay_out / "replay_summary.json")
    result: dict[str, Any] = {
        "out": str(replay_out),
        "summary_path": str(replay_out / "replay_summary.json"),
        "report_path": str(replay_out / "replay_report.md"),
        "returncode": record["returncode"],
        "ok": record["ok"],
    }
    if isinstance(replay_summary, dict):
        for key in (
            "total",
            "stable_same_failure",
            "flaky_same_failure",
            "changed_failure",
            "unverified_failure",
            "not_reproduced",
            "unavailable",
        ):
            result[key] = replay_summary.get(key)
    return result


def write_topotrack_probe_index_report(index: dict[str, Any], path: Path) -> None:
    lines = [
        "# SGGK Topo-track Crash Probe Index",
        "",
        f"- Generated: `{index.get('generated_at')}`",
        f"- Lane count: `{index.get('lane_count')}`",
        f"- Selected cases: `{index.get('selected_count')}`",
        f"- Classification counts: `{index.get('classification_counts')}`",
        "",
        "| lane | selected | classifications | report |",
        "| --- | ---: | --- | --- |",
    ]
    for item in index.get("lanes", []):
        if not isinstance(item, dict):
            continue
        lines.append(
            "| `{lane}` | {selected} | `{counts}` | `{report}` |".format(
                lane=item.get("lane", ""),
                selected=item.get("selected_count", 0),
                counts=item.get("classification_counts", {}),
                report=item.get("report_path", ""),
            )
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run_topotrack_probe(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    out_root: Path,
    lanes: list[dict[str, Any]],
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not args.probe_topotrack_crashes:
        return None
    probe_root = out_root / "topotrack_probe"
    lane_results: list[dict[str, Any]] = []
    classification_counts: dict[str, int] = {}
    merged_results: list[dict[str, Any]] = []
    selected_count = 0
    probed_count = 0
    for lane in lanes:
        if not isinstance(lane, dict) or lane.get("skipped") or lane.get("type") != "recipe":
            continue
        lane_name = str(lane.get("name") or "lane")
        summary_path = Path(str(lane.get("summary_path") or ""))
        if not summary_path.is_file():
            continue
        lane_summary = read_json(summary_path)
        if isinstance(lane_summary, dict) and int(lane_summary.get("failed") or 0) <= 0:
            continue
        lane_out = probe_root / sanitize_name(lane_name)
        cmd = [
            sys.executable,
            str(script_dir / "probe_topotrack_crashes.py"),
            "--runner",
            str(runner),
            "--summary",
            str(summary_path),
            "--out",
            str(lane_out),
            "--timeout",
            str(args.topotrack_probe_timeout),
            "--jobs",
            str(args.topotrack_probe_jobs),
        ]
        if args.topotrack_probe_limit:
            cmd.extend(["--limit", str(args.topotrack_probe_limit)])
        record = run_command(f"topotrack_probe_{sanitize_name(lane_name)}", cmd)
        command_records.append(record)
        probed_count += 1
        probe_summary_path = lane_out / "topotrack_probe_summary.json"
        probe_report_path = lane_out / "topotrack_probe_report.md"
        probe_summary = read_json(probe_summary_path)
        lane_result: dict[str, Any] = {
            "lane": lane_name,
            "out": str(lane_out),
            "summary_path": str(probe_summary_path),
            "report_path": str(probe_report_path),
            "returncode": record["returncode"],
            "ok": record["ok"],
        }
        if isinstance(probe_summary, dict):
            lane_selected = int(probe_summary.get("selected_count") or 0)
            selected_count += lane_selected
            lane_counts = probe_summary.get("classification_counts")
            if isinstance(lane_counts, dict):
                lane_result["classification_counts"] = lane_counts
                for key, value in lane_counts.items():
                    classification_counts[str(key)] = classification_counts.get(str(key), 0) + int(value or 0)
            lane_result["selected_count"] = lane_selected
            lane_result["skipped_count"] = probe_summary.get("skipped_count")
            for item in probe_summary.get("results", []):
                if isinstance(item, dict):
                    merged_results.append({**item, "campaign_lane": lane_name})
        lane_results.append(lane_result)
    if not lane_results:
        return {"skipped": True, "reason": "no failed recipe lanes with summaries"}
    index = {
        "generated_at": now_iso_like(),
        "out": str(probe_root),
        "lane_count": probed_count,
        "selected_count": selected_count,
        "classification_counts": classification_counts,
        "results": merged_results,
        "lanes": lane_results,
    }
    write_json(probe_root / "topotrack_probe_index.json", index)
    write_topotrack_probe_index_report(index, probe_root / "topotrack_probe_index.md")
    return {
        "out": str(probe_root),
        "summary_path": str(probe_root / "topotrack_probe_index.json"),
        "report_path": str(probe_root / "topotrack_probe_index.md"),
        "lane_count": probed_count,
        "selected_count": selected_count,
        "classification_counts": classification_counts,
        "topotrack_only_modeling_ok": classification_counts.get("topotrack_only_modeling_ok", 0),
        "lanes": lane_results,
    }


def write_reduction_index_report(index: dict[str, Any], path: Path) -> None:
    lines = [
        "# SGGK Campaign Reductions",
        "",
        f"- Generated: `{index.get('generated_at')}`",
        f"- Candidate stable failures: `{index.get('candidate_count')}`",
        f"- Selected: `{index.get('selected_count')}`",
        f"- Completed: `{index.get('completed_count')}`",
        f"- Accepted reductions: `{index.get('accepted_reduction_count')}`",
        "",
        "| fingerprint | case | status | accepted | trials | reduced recipe | report |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for item in index.get("reductions", []):
        if not isinstance(item, dict):
            continue
        lines.append(
            "| `{fingerprint}` | `{case}` | `{status}` | {accepted} | {trials} | `{recipe}` | `{report}` |".format(
                fingerprint=item.get("fingerprint", ""),
                case=item.get("representative_case_id", ""),
                status=item.get("status", ""),
                accepted=item.get("accepted_reductions", ""),
                trials=item.get("trials", ""),
                recipe=item.get("reduced_recipe", ""),
                report=item.get("report_path", ""),
            )
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run_reductions(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    out_root: Path,
    replay: dict[str, Any] | None,
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not args.reduce_stable_failures:
        return None
    if not replay or replay.get("skipped") or not replay.get("summary_path"):
        return {"skipped": True, "reason": "replay unavailable"}
    replay_summary = read_json(Path(str(replay["summary_path"])))
    if not isinstance(replay_summary, dict):
        return {"skipped": True, "reason": "replay summary missing"}
    reductions_root = out_root / "reductions"
    timeout = args.reduction_timeout if args.reduction_timeout > 0 else args.timeout
    cmd = [
        sys.executable,
        str(script_dir / "reduce_replay_failures.py"),
        "--runner",
        str(runner),
        "--replay",
        str(replay["summary_path"]),
        "--out",
        str(reductions_root),
        "--limit",
        str(args.reduction_limit),
        "--timeout",
        str(timeout),
        "--max-trials",
        str(args.reduction_max_trials),
        "--min-dimension",
        str(args.reduction_min_dimension),
    ]
    record = run_command("reduce_stable_replay_failures", cmd, acceptable={0, 2})
    command_records.append(record)
    index_payload = read_json(reductions_root / "reduction_index.json")
    if not isinstance(index_payload, dict):
        return {
            "out": str(reductions_root),
            "skipped": False,
            "failed": True,
            "reason": "hardened reduction batch did not produce reduction_index.json",
        }
    write_reduction_index_report(index_payload, reductions_root / "reduction_index.md")
    return {
        "out": str(reductions_root),
        "summary_path": str(reductions_root / "reduction_index.json"),
        "report_path": str(reductions_root / "reduction_index.md"),
        "candidate_count": index_payload["candidate_count"],
        "selected_count": index_payload["selected_count"],
        "completed_count": index_payload["completed_count"],
        "accepted_reduction_count": index_payload["accepted_reduction_count"],
    }


def run_bundle_export(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    aggregate: dict[str, Any] | None,
    replay: dict[str, Any] | None,
    reductions: dict[str, Any] | None,
    topotrack_probe: dict[str, Any] | None,
    lanes: list[dict[str, Any]],
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if (
        args.skip_bundles
        or not aggregate
        or not replay
        or aggregate.get("skipped")
        or replay.get("skipped")
        or not aggregate.get("out")
        or not replay.get("out")
    ):
        return None
    bundle_out = out_root / "failure_bundles"
    cmd = [
        sys.executable,
        str(script_dir / "export_failure_bundles.py"),
        "--triage",
        str(aggregate["out"]),
        "--replay",
        str(replay["out"]),
        "--out",
        str(bundle_out),
    ]
    if isinstance(reductions, dict) and reductions.get("summary_path"):
        cmd.extend(["--reductions", str(reductions["summary_path"])])
    if (
        isinstance(topotrack_probe, dict)
        and not topotrack_probe.get("skipped")
        and topotrack_probe.get("summary_path")
    ):
        cmd.extend(["--topotrack-probe", str(topotrack_probe["summary_path"])])
    for lane in lanes:
        preview_out = lane.get("preview_out") if isinstance(lane, dict) else ""
        if preview_out:
            cmd.extend(["--preview-dir", str(preview_out)])
    if args.bundle_zip:
        cmd.append("--zip")
    record = run_command("export_failure_bundles", cmd)
    command_records.append(record)
    bundle_index = read_json(bundle_out / "bundle_index.json")
    count = 0
    if isinstance(bundle_index, dict) and isinstance(bundle_index.get("bundles"), list):
        count = len(bundle_index["bundles"])
    return {
        "out": str(bundle_out),
        "index_path": str(bundle_out / "bundle_index.json"),
        "report_path": str(bundle_out / "bundle_report.md"),
        "bundle_count": count,
        "returncode": record["returncode"],
        "ok": record["ok"],
    }


def run_bug_registry(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    bundles: dict[str, Any] | None,
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if args.skip_bug_registry:
        return None
    cmd = [
        sys.executable,
        str(script_dir / "collect_bug_registry.py"),
        "--out",
        str(out_root / "bug_registry"),
    ]
    if isinstance(bundles, dict) and bundles.get("index_path"):
        cmd.extend(["--bundle-index", str(bundles["index_path"])])
    if "--bundle-index" not in cmd:
        return {"skipped": True, "reason": "no stable failure-bundle index"}
    record = run_command("collect_bug_registry", cmd)
    command_records.append(record)
    registry_out = out_root / "bug_registry"
    registry = read_json(registry_out / "bug_registry.json")
    result = {
        "out": str(registry_out),
        "summary_path": str(registry_out / "bug_registry.json"),
        "report_path": str(registry_out / "bug_registry.md"),
        "replay_recipes": str(registry_out / "registry_replay_recipes.txt"),
        "returncode": record["returncode"],
        "ok": record["ok"],
    }
    if isinstance(registry, dict):
        result["total"] = registry.get("total")
        result["by_replay_status"] = registry.get("by_replay_status")
        result["by_api"] = registry.get("by_api")
    return result


def run_debug_handoff(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    aggregate: dict[str, Any] | None,
    bug_registry: dict[str, Any] | None,
    lanes: list[dict[str, Any]],
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if args.skip_debug_handoff:
        return None
    cmd = [
        sys.executable,
        str(script_dir / "build_debug_handoff.py"),
        "--out",
        str(out_root / "debug_handoff"),
    ]
    if isinstance(bug_registry, dict) and not bug_registry.get("skipped") and bug_registry.get("summary_path"):
        cmd.extend(["--registry", str(bug_registry["summary_path"])])
    elif isinstance(aggregate, dict) and not aggregate.get("skipped") and aggregate.get("summary_path"):
        cmd.extend(["--triage", str(aggregate["summary_path"])])
    else:
        return {"skipped": True, "reason": "no bug registry or aggregate triage"}
    for lane in lanes:
        preview_out = lane.get("preview_out") if isinstance(lane, dict) else ""
        if preview_out:
            cmd.extend(["--preview-dir", str(preview_out)])
    record = run_command("build_debug_handoff", cmd)
    command_records.append(record)
    out_dir = out_root / "debug_handoff"
    index = read_json(out_dir / "debug_handoff_index.json")
    result = {
        "out": str(out_dir),
        "index_path": str(out_dir / "debug_handoff_index.json"),
        "report_path": str(out_dir / "debug_handoff_report.md"),
        "returncode": record["returncode"],
        "ok": record["ok"],
    }
    if isinstance(index, dict):
        result["pack_count"] = index.get("pack_count")
        result["debug_sgt_count"] = index.get("debug_sgt_count")
        result["focus_sgt_count"] = index.get("focus_sgt_count")
        result["input_sgt_count"] = index.get("input_sgt_count")
        result["by_api"] = index.get("by_api")
        result["topology_extractor"] = index.get("topology_extractor")
    return result


def run_bug_record_drafts(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    bundles: dict[str, Any] | None,
    debug_handoff: dict[str, Any] | None,
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if args.skip_bug_record_drafts:
        return None
    cmd = [
        sys.executable,
        str(script_dir / "export_bug_record_drafts.py"),
        "--out",
        str(out_root / "bug_record_drafts" / "drafts.json"),
        "--bug-prefix",
        str(args.bug_record_prefix),
    ]
    if isinstance(bundles, dict) and bundles.get("index_path"):
        cmd.extend(["--bundle-index", str(bundles["index_path"])])
    if isinstance(debug_handoff, dict) and not debug_handoff.get("skipped") and debug_handoff.get("index_path"):
        cmd.extend(["--debug-handoff", str(debug_handoff["index_path"])])
    if "--bundle-index" not in cmd:
        return {"skipped": True, "reason": "no stable failure-bundle index"}
    record = run_command("export_bug_record_drafts", cmd)
    command_records.append(record)
    draft_path = out_root / "bug_record_drafts" / "drafts.json"
    drafts = read_json(draft_path)
    records = drafts.get("records") if isinstance(drafts, dict) else []
    return {
        "out": str(draft_path.parent),
        "draft_path": str(draft_path),
        "record_count": len(records) if isinstance(records, list) else 0,
        "returncode": record["returncode"],
        "ok": record["ok"],
    }


def string_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def collect_fixture_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            refs.update(collect_fixture_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(collect_fixture_refs(child))
    elif isinstance(value, str):
        normalized = value.replace("\\", "/")
        for marker in ("test_harness/fixtures/bug_records/", "fixtures/bug_records/"):
            if marker in normalized:
                refs.add(normalized[normalized.index(marker) + len(marker) :])
                break
    return refs


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def run_promote_bug_records(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    drafts: dict[str, Any] | None,
    command_records: list[dict[str, Any]],
) -> dict[str, Any]:
    if not args.promote_bug_records:
        return {"skipped": True, "reason": "skip_promote_bug_records"}
    if not isinstance(drafts, dict) or not string_value(drafts.get("draft_path")):
        return {"skipped": True, "reason": "no bug-record drafts"}
    if drafts.get("ok") is False:
        return {"skipped": True, "reason": "bug-record draft export failed"}
    promotion_root = out_root / "promoted_bug_records"
    registry_id = sanitize_name(f"{args.bug_record_prefix}_promoted")
    promoted_path = promotion_root / "test_harness" / "bug_records" / f"{registry_id}.json"
    fixture_root = promotion_root / "test_harness" / "fixtures" / "bug_records"
    cmd = [
        sys.executable,
        str(script_dir / "promote_bug_records.py"),
        "--records",
        string_value(drafts["draft_path"]),
        "--repo-root",
        str(promotion_root),
        "--fixture-root",
        "test_harness/fixtures/bug_records",
        "--out",
        str(promoted_path),
        "--registry-id",
        registry_id,
        "--description",
        "Portable promoted records from direct campaign discoveries.",
        "--overwrite",
    ]
    promote = run_command("promote_bug_records", cmd)
    command_records.append(promote)
    promoted = read_json(promoted_path)

    audit_out = promotion_root / "portability_audit"
    audit_cmd = [
        sys.executable,
        str(script_dir / "audit_bug_record_portability.py"),
        "--records",
        str(promoted_path),
        "--repo-root",
        str(promotion_root),
        "--out",
        str(audit_out),
    ]
    audit_record: dict[str, Any] | None = None
    audit: Any = None
    if promoted_path.is_file():
        audit_record = run_command("audit_promoted_bug_records", audit_cmd)
        command_records.append(audit_record)
        audit = read_json(audit_out / "bug_record_portability.json")

    records = promoted.get("records") if isinstance(promoted, dict) else []
    result: dict[str, Any] = {
        "out": str(promotion_root),
        "record_path": str(promoted_path),
        "report_path": str(promoted_path.with_suffix(".md")),
        "fixture_root": str(fixture_root),
        "portability_summary_path": str(audit_out / "bug_record_portability.json"),
        "portability_report_path": str(audit_out / "bug_record_portability.md"),
        "registry_id": registry_id,
        "returncode": promote["returncode"],
        "ok": promote["ok"] and bool(audit_record and audit_record.get("ok")) and bool(isinstance(audit, dict) and audit.get("ok")),
    }
    if isinstance(records, list):
        result["record_count"] = len(records)
    if isinstance(promoted, dict):
        result["copied_asset_count"] = len(collect_fixture_refs(promoted))
    if isinstance(audit, dict):
        result["portability_ok"] = audit.get("ok")
        result["portability_errors"] = audit.get("error_count")
        result["portability_warnings"] = audit.get("warning_count")
    return result


def run_promoted_bug_record_replay(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    promoted: dict[str, Any] | None,
    command_records: list[dict[str, Any]],
) -> dict[str, Any]:
    if not args.replay_promoted_bug_records:
        return {"skipped": True, "reason": "skip_replay_promoted_bug_records"}
    if not isinstance(promoted, dict) or promoted.get("skipped"):
        return {"skipped": True, "reason": "no promoted bug records"}
    if not promoted.get("ok"):
        return {"skipped": True, "reason": "promotion did not complete successfully"}
    promotion_root = Path(string_value(promoted.get("out"))).resolve()
    record_path = Path(string_value(promoted.get("record_path"))).resolve()
    if not promotion_root.is_dir() or not record_path.is_file():
        return {"skipped": True, "reason": "promoted root or record file missing"}

    materialized = promotion_root / "materialized"
    replay_out = promotion_root / "replay"
    regression_out = promotion_root / "regression"
    materialize_cmd = [
        sys.executable,
        str(script_dir / "record_bug_cases.py"),
        "--records",
        relative_to_root(record_path, promotion_root),
        "--out",
        "materialized",
        "--validate-recipes",
    ]
    materialize = run_command("materialize_promoted_bug_records", materialize_cmd, cwd=promotion_root)
    command_records.append(materialize)
    registry = read_json(materialized / "bug_registry.json")

    replay_cmd = [
        sys.executable,
        str(script_dir / "run_recipes.py"),
        "--runner",
        str(runner),
        "--recipe-list",
        "materialized/registry_replay_recipes.txt",
        "--out",
        "replay",
        "--triage-out",
        "replay_triage",
        "--timeout",
        str(args.promoted_replay_timeout),
        "--jobs",
        str(args.promoted_replay_jobs),
        "--triage-include-passed",
    ]
    replay_record: dict[str, Any] | None = None
    replay_summary: Any = None
    if materialize["ok"]:
        replay_record = run_command("replay_promoted_bug_records", replay_cmd, acceptable={0, 1, 2}, cwd=promotion_root)
        command_records.append(replay_record)
        replay_summary = read_json(replay_out / "recipe_summary.json")

    regression_cmd = [
        sys.executable,
        str(script_dir / "check_bug_registry_regression.py"),
        "--registry",
        "materialized",
        "--recipe-summary",
        "replay/recipe_summary.json",
        "--out",
        "regression",
    ]
    regression_record: dict[str, Any] | None = None
    regression: Any = None
    if replay_record and replay_record.get("ok") and (replay_out / "recipe_summary.json").is_file():
        regression_record = run_command("classify_promoted_bug_records", regression_cmd, cwd=promotion_root)
        command_records.append(regression_record)
        regression = read_json(regression_out / "registry_regression.json")

    result: dict[str, Any] = {
        "out": str(promotion_root),
        "materialized_out": str(materialized),
        "registry_path": str(materialized / "bug_registry.json"),
        "registry_report": str(materialized / "bug_registry.md"),
        "replay_recipes": str(materialized / "registry_replay_recipes.txt"),
        "replay_out": str(replay_out),
        "replay_summary": str(replay_out / "recipe_summary.json"),
        "replay_triage": str(promotion_root / "replay_triage"),
        "regression_out": str(regression_out),
        "regression_summary_path": str(regression_out / "registry_regression.json"),
        "regression_report_path": str(regression_out / "registry_regression.md"),
        "returncode": materialize["returncode"],
        "ok": bool(
            materialize.get("ok")
            and replay_record
            and replay_record.get("ok")
            and regression_record
            and regression_record.get("ok")
        ),
    }
    if isinstance(registry, dict):
        result["total"] = registry.get("total")
        result["by_replay_status"] = registry.get("by_replay_status")
    if isinstance(replay_summary, dict):
        result["replay_total"] = replay_summary.get("total")
        result["replay_passed"] = replay_summary.get("passed")
        result["replay_failed"] = replay_summary.get("failed")
        result["replay_timed_out"] = replay_summary.get("timed_out")
    if isinstance(regression, dict):
        result["regression_total"] = regression.get("total")
        result["regression_status_counts"] = regression.get("status_counts")
    return result


def collect_bug_record_files(args: argparse.Namespace, script_dir: Path) -> list[Path]:
    raw_paths = args.bug_record or [str(script_dir.parent / "bug_records")]
    files: set[Path] = set()
    for raw in raw_paths:
        path = Path(raw)
        if path.is_file():
            files.add(path.resolve())
        elif path.is_dir():
            files.update(item.resolve() for item in path.rglob("*.json") if item.is_file())
    return sorted(files, key=lambda item: str(item).lower())


def run_known_bug_regression(
    args: argparse.Namespace,
    script_dir: Path,
    runner: Path,
    out_root: Path,
    command_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if args.skip_known_bug_regression:
        return None
    records = collect_bug_record_files(args, script_dir)
    if not records:
        return {"skipped": True, "reason": "no checked-in bug records"}

    registry_out = out_root / "known_bug_records"
    materialize_cmd = [
        sys.executable,
        str(script_dir / "record_bug_cases.py"),
        "--out",
        str(registry_out),
        "--validate-recipes",
    ]
    for record_path in records:
        materialize_cmd.extend(["--records", str(record_path)])
    materialize = run_command("known_bug_record_materialize", materialize_cmd)
    command_records.append(materialize)
    result: dict[str, Any] = {
        "record_files": [str(path) for path in records],
        "record_file_count": len(records),
        "out": str(registry_out),
        "registry_path": str(registry_out / "bug_registry.json"),
        "registry_report": str(registry_out / "bug_registry.md"),
        "replay_recipes": str(registry_out / "registry_replay_recipes.txt"),
        "materialize_returncode": materialize["returncode"],
        "materialize_ok": materialize["ok"],
    }
    registry = read_json(registry_out / "bug_registry.json")
    if isinstance(registry, dict):
        result["bug_count"] = registry.get("total")
        result["by_replay_status"] = registry.get("by_replay_status")
    if not materialize["ok"]:
        return result

    replay_out = out_root / "known_bug_replay"
    replay_triage_out = out_root / "known_bug_replay_triage"
    replay_preview_out = out_root / "known_bug_replay_preview"
    replay_geometry_audit_out = out_root / "known_bug_replay_geometry_audit"
    replay_cmd = [
        sys.executable,
        str(script_dir / "run_recipes.py"),
        "--runner",
        str(runner),
        "--recipe-list",
        str(registry_out / "registry_replay_recipes.txt"),
        "--out",
        str(replay_out),
        "--timeout",
        str(args.timeout),
        "--jobs",
        str(args.jobs),
        "--triage-out",
        str(replay_triage_out),
    ]
    if args.resume:
        replay_cmd.append("--resume")
    if args.hash_recipes:
        replay_cmd.append("--hash-recipes")
    if not args.no_preview:
        replay_cmd.extend(["--preview-out", str(replay_preview_out), "--contact-sheet", str(replay_preview_out / "contact.png")])
    if not args.no_geometry_audit:
        replay_cmd.extend(["--geometry-audit-out", str(replay_geometry_audit_out)])
    replay = run_command("known_bug_replay", replay_cmd, acceptable={0, 2})
    command_records.append(replay)
    result.update(
        {
            "replay_out": str(replay_out),
            "replay_summary": str(replay_out / "recipe_summary.json"),
            "replay_returncode": replay["returncode"],
            "replay_ok": replay["ok"],
            "replay_triage_out": str(replay_triage_out),
            "replay_preview_out": str(replay_preview_out) if not args.no_preview else "",
            "replay_contact_sheet": str(replay_preview_out / "contact.png") if not args.no_preview else "",
            "replay_geometry_audit_out": str(replay_geometry_audit_out) if not args.no_geometry_audit else "",
        }
    )
    replay_summary = read_json(replay_out / "recipe_summary.json")
    if isinstance(replay_summary, dict):
        for key in ("total", "executed", "passed", "failed", "timed_out"):
            if key in replay_summary:
                result[f"replay_{key}"] = replay_summary[key]
        triage = replay_summary.get("triage") if isinstance(replay_summary.get("triage"), dict) else {}
        if triage:
            result["replay_triage_returncode"] = triage.get("returncode")
            result["replay_triage_summary"] = str(replay_triage_out / "triage_summary.json")
            result["replay_triage_report"] = str(replay_triage_out / "triage_report.md")
        preview = replay_summary.get("preview") if isinstance(replay_summary.get("preview"), dict) else {}
        if preview:
            result["replay_preview_returncode"] = preview.get("returncode")
            result["replay_contact_sheet"] = preview.get("contact_sheet") or result.get("replay_contact_sheet", "")
        geometry_audit = replay_summary.get("geometry_audit") if isinstance(replay_summary.get("geometry_audit"), dict) else {}
        if geometry_audit:
            result["replay_geometry_audit_returncode"] = geometry_audit.get("returncode")
            result["replay_geometry_audit_report"] = str(replay_geometry_audit_out / "geometry_audit.md")
            audit_summary = read_json(replay_geometry_audit_out / "geometry_audit.json")
            if isinstance(audit_summary, dict):
                result["replay_geometry_audit_cases"] = audit_summary.get("total_cases")
                result["replay_geometry_audit_duplicate_inputs"] = len(audit_summary.get("same_boolean_duplicate_input_groups", []))
                result["replay_geometry_audit_tolerance_mismatches"] = len(audit_summary.get("tolerance_mismatches", []))
    if not replay["ok"]:
        return result

    regression_out = out_root / "known_bug_regression"
    regression_cmd = [
        sys.executable,
        str(script_dir / "check_bug_registry_regression.py"),
        "--registry",
        str(registry_out),
        "--recipe-summary",
        str(replay_out / "recipe_summary.json"),
        "--out",
        str(regression_out),
    ]
    if args.known_bug_fail_on_fixed:
        regression_cmd.append("--fail-on-fixed")
    if args.known_bug_fail_on_changed:
        regression_cmd.append("--fail-on-changed")
    if args.known_bug_fail_on_unavailable:
        regression_cmd.append("--fail-on-unavailable")
    regression = run_command("known_bug_regression", regression_cmd, acceptable={0, 2})
    command_records.append(regression)
    result.update(
        {
            "regression_out": str(regression_out),
            "regression_summary": str(regression_out / "registry_regression.json"),
            "regression_report": str(regression_out / "registry_regression.md"),
            "regression_returncode": regression["returncode"],
            "regression_ok": regression["ok"],
        }
    )
    regression_summary = read_json(regression_out / "registry_regression.json")
    if isinstance(regression_summary, dict):
        result["status_counts"] = regression_summary.get("status_counts")
        result["regression_total"] = regression_summary.get("total")
    if not args.skip_debug_handoff:
        handoff_out = out_root / "known_bug_debug_handoff"
        handoff_cmd = [
            sys.executable,
            str(script_dir / "build_debug_handoff.py"),
            "--registry",
            str(registry_out),
            "--triage",
            str(replay_triage_out),
            "--out",
            str(handoff_out),
        ]
        if not args.no_preview:
            handoff_cmd.extend(["--preview-dir", str(replay_preview_out)])
        handoff = run_command("known_bug_debug_handoff", handoff_cmd)
        command_records.append(handoff)
        result["debug_handoff_out"] = str(handoff_out)
        result["debug_handoff_index"] = str(handoff_out / "debug_handoff_index.json")
        result["debug_handoff_report"] = str(handoff_out / "debug_handoff_report.md")
        result["debug_handoff_returncode"] = handoff["returncode"]
        result["debug_handoff_ok"] = handoff["ok"]
        handoff_index = read_json(handoff_out / "debug_handoff_index.json")
        if isinstance(handoff_index, dict):
            result["debug_handoff_pack_count"] = handoff_index.get("pack_count")
            result["debug_handoff_debug_sgt_count"] = handoff_index.get("debug_sgt_count")
            result["debug_handoff_focus_sgt_count"] = handoff_index.get("focus_sgt_count")
            result["debug_handoff_input_sgt_count"] = handoff_index.get("input_sgt_count")
            result["debug_handoff_topology_extractor"] = handoff_index.get("topology_extractor")
    return result


def run_artifact_verification(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    command_records: list[dict[str, Any]],
) -> dict[str, Any]:
    if args.skip_artifact_verify:
        return {"skipped": True, "reason": "skip_artifact_verify"}
    verify_out = out_root / "campaign_verification"
    cmd = [
        sys.executable,
        str(script_dir / "verify_campaign_artifacts.py"),
        "--campaign",
        str(out_root),
        "--out",
        str(verify_out),
    ]
    if args.artifact_verify_allow_duplicate_inputs:
        cmd.append("--allow-duplicate-inputs")
    if args.artifact_verify_allow_duplicate_geometry:
        cmd.append("--allow-duplicate-geometry")
    if args.artifact_verify_allow_tolerance_mismatches:
        cmd.append("--allow-tolerance-mismatches")
    for status in args.artifact_verify_expect_known_bug_status:
        cmd.extend(["--expect-known-bug-status", status])

    record = run_command("artifact_verification", cmd)
    command_records.append(record)
    result: dict[str, Any] = {
        "out": str(verify_out),
        "summary_path": str(verify_out / "campaign_verification.json"),
        "report_path": str(verify_out / "campaign_verification.md"),
        "returncode": record["returncode"],
        "ok": record["ok"],
    }
    verification = read_json(verify_out / "campaign_verification.json")
    if isinstance(verification, dict):
        verifier_ok = bool(verification.get("ok"))
        result.update(
            {
                "verifier_ok": verifier_ok,
                "ok": record["ok"] and verifier_ok,
                "error_count": verification.get("error_count"),
                "warning_count": verification.get("warning_count"),
                "check_count": verification.get("check_count"),
            }
        )
    else:
        result["error"] = "campaign_verification_summary_missing_or_invalid"
        result["ok"] = False
    return result


def run_oracle_coverage(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    command_records: list[dict[str, Any]],
) -> dict[str, Any]:
    if args.skip_oracle_coverage:
        return {"skipped": True, "reason": "skip_oracle_coverage"}
    coverage_out = out_root / "oracle_coverage"
    cmd = [
        sys.executable,
        str(script_dir / "summarize_oracle_coverage.py"),
        "--campaign",
        str(out_root),
        "--out",
        str(coverage_out),
        "--fail-on-passed-missing-validation",
        "--min-oracle-kinds-per-passed-case",
        str(args.oracle_coverage_min_kinds),
    ]
    record = run_command("oracle_coverage", cmd)
    command_records.append(record)
    result: dict[str, Any] = {
        "out": str(coverage_out),
        "summary_path": str(coverage_out / "oracle_coverage.json"),
        "report_path": str(coverage_out / "oracle_coverage.md"),
        "returncode": record["returncode"],
        "ok": record["ok"],
    }
    coverage = read_json(coverage_out / "oracle_coverage.json")
    if isinstance(coverage, dict):
        coverage_ok = bool(coverage.get("ok"))
        result.update(
            {
                "coverage_ok": coverage_ok,
                "ok": record["ok"] and coverage_ok,
                "total_cases": coverage.get("total_cases"),
                "validation_present": coverage.get("validation_present"),
                "validation_missing": coverage.get("validation_missing"),
                "passed_cases": coverage.get("passed_cases"),
                "passed_missing_validation": coverage.get("passed_missing_validation"),
                "passed_below_min_oracle_kinds": coverage.get("passed_below_min_oracle_kinds"),
                "oracle_kind_count": len(coverage.get("oracle_counts", {})) if isinstance(coverage.get("oracle_counts"), dict) else 0,
                "gate_failures": coverage.get("gate_failures"),
            }
        )
    else:
        result["error"] = "oracle_coverage_summary_missing_or_invalid"
        result["ok"] = False
    return result


def run_dataset_audit(
    args: argparse.Namespace,
    script_dir: Path,
    out_root: Path,
    dataset_lists: list[str],
    command_records: list[dict[str, Any]],
) -> dict[str, Any]:
    if args.skip_dataset_audit:
        return {"skipped": True, "reason": "skip_dataset_audit"}
    if not dataset_lists:
        return {"skipped": True, "reason": "no dataset lists"}
    audit_out = out_root / "dataset_audit"
    cmd = [
        sys.executable,
        str(script_dir / "audit_corpus_dataset.py"),
        "--out",
        str(audit_out),
    ]
    if args.dataset_audit_require_hashes:
        cmd.append("--require-hashes")
    if args.dataset_audit_fail_duplicate_ratio >= 0:
        cmd.extend(["--fail-duplicate-ratio", str(args.dataset_audit_fail_duplicate_ratio)])
    for dataset_list in dataset_lists:
        cmd.extend(["--dataset-list", dataset_list])
    record = run_command("dataset_audit", cmd)
    command_records.append(record)
    result: dict[str, Any] = {
        "out": str(audit_out),
        "summary_path": str(audit_out / "dataset_audit.json"),
        "report_path": str(audit_out / "dataset_audit.md"),
        "returncode": record["returncode"],
        "ok": record["ok"],
    }
    audit = read_json(audit_out / "dataset_audit.json")
    if isinstance(audit, dict):
        audit_ok = bool(audit.get("ok"))
        result.update(
            {
                "audit_ok": audit_ok,
                "ok": record["ok"] and audit_ok,
                "total_files": audit.get("total_files"),
                "existing_files": audit.get("existing_files"),
                "missing_files": audit.get("missing_files"),
                "empty_files": audit.get("empty_files"),
                "error_count": audit.get("error_count"),
                "warning_count": audit.get("warning_count"),
                "hash_coverage_ratio": audit.get("hash_coverage_ratio"),
                "duplicate_content_group_count": audit.get("duplicate_content_group_count"),
                "duplicate_file_ratio": audit.get("duplicate_file_ratio"),
            }
        )
    else:
        result["error"] = "dataset_audit_summary_missing_or_invalid"
        result["ok"] = False
    return result


def write_report(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# SGGK Campaign Report",
        "",
        f"- Started: `{summary.get('started_at')}`",
        f"- Updated: `{summary.get('updated_at')}`",
        f"- Runner: `{summary.get('runner')}`",
        f"- Output: `{summary.get('out_root')}`",
        "",
    ]

    source_scan = summary.get("source_scan")
    if isinstance(source_scan, dict):
        lines.extend(["## Source Scan", ""])
        if source_scan.get("skipped"):
            lines.append(f"- Skipped: {source_scan.get('reason')}")
        else:
            for key in ("files_scanned", "findings", "attack_seed_drafts"):
                if key in source_scan:
                    lines.append(f"- {key}: `{source_scan[key]}`")
            if source_scan.get("candidate_truncated") is not None:
                lines.append(f"- Candidate truncated: `{source_scan.get('candidate_truncated')}`")
            lines.append(f"- Report: `{source_scan.get('report_path', '')}`")
            lines.append(f"- Seeds: `{source_scan.get('seed_path', '')}`")
            tasks = source_scan.get("tasks") if isinstance(source_scan.get("tasks"), dict) else {}
            if tasks:
                lines.append(f"- Attack tasks: `{tasks.get('task_count', '')}`")
                lines.append(f"- Task manifest: `{tasks.get('manifest', '')}`")
                lines.append(f"- Task JSONL: `{tasks.get('jsonl', '')}`")
        lines.append("")

    lines.extend(
        [
        "## Lanes",
        "",
        ]
    )
    for lane in summary.get("lanes", []):
        lines.append(f"### {lane.get('name')}")
        lines.append("")
        if lane.get("dsl_check_report"):
            lines.append(f"- DSL check: `{lane.get('dsl_check_report')}`")
            lines.append(f"- DSL check ok: `{lane.get('dsl_check_ok')}`")
            lines.append(f"- DSL check recipes: `{lane.get('dsl_check_recipe_count', '')}`")
            lines.append(f"- DSL check compile failures: `{lane.get('dsl_check_compile_failure_count', '')}`")
            lines.append(f"- DSL check validation failures: `{lane.get('dsl_check_validation_failure_count', '')}`")
        if lane.get("skipped"):
            lines.append(f"- Skipped: {lane.get('reason') or lane.get('error')}")
        else:
            lines.append(f"- Output: `{lane.get('out', '')}`")
            for key in ("total", "executed", "passed", "failed", "timed_out"):
                if key in lane:
                    lines.append(f"- {key}: `{lane[key]}`")
            if lane.get("empty_shard"):
                lines.append("- Empty shard: `true`")
            if lane.get("contact_sheet"):
                lines.append(f"- Contact sheet: `{lane['contact_sheet']}`")
            if lane.get("generator_manifest_path"):
                lines.append(f"- Generator manifest: `{lane['generator_manifest_path']}`")
            if lane.get("generated_recipe_count") is not None:
                lines.append(f"- Generated recipes: `{lane.get('generated_recipe_count')}`")
            if lane.get("generated_source_count") is not None:
                lines.append(f"- Generated sources: `{lane.get('generated_source_count')}`")
            if lane.get("generated_skipped_source_count") is not None:
                lines.append(f"- Skipped sources: `{lane.get('generated_skipped_source_count')}`")
            if lane.get("exact_bbox_probe_enabled") is not None:
                lines.append(f"- Exact bbox probe: enabled=`{lane.get('exact_bbox_probe_enabled')}` require=`{lane.get('exact_bbox_probe_require')}` failures=`{lane.get('exact_bbox_probe_failure_count')}`")
                lines.append(f"- Exact bbox sources: `{lane.get('exact_bbox_probe_bbox_sources', {})}`")
            if lane.get("geometry_audit_report"):
                lines.append(f"- Geometry audit: `{lane['geometry_audit_report']}`")
                lines.append(f"- Geometry duplicate inputs: `{lane.get('geometry_audit_duplicate_inputs', '')}`")
                lines.append(f"- Geometry tolerance mismatches: `{lane.get('geometry_audit_tolerance_mismatches', '')}`")
        lines.append("")

    topotrack_probe = summary.get("topotrack_probe")
    if isinstance(topotrack_probe, dict):
        lines.append("## Topo-track Crash Probe")
        lines.append("")
        if topotrack_probe.get("skipped"):
            lines.append(f"- Skipped: {topotrack_probe.get('reason')}")
        else:
            lines.append(f"- Lanes: `{topotrack_probe.get('lane_count', 0)}`")
            lines.append(f"- Selected cases: `{topotrack_probe.get('selected_count', 0)}`")
            lines.append(f"- Classification counts: `{topotrack_probe.get('classification_counts', {})}`")
            lines.append(f"- Topotrack-only modeling-ok: `{topotrack_probe.get('topotrack_only_modeling_ok', 0)}`")
            lines.append(f"- Report: `{topotrack_probe.get('report_path', '')}`")
        lines.append("")

    aggregate = summary.get("aggregate_triage")
    if isinstance(aggregate, dict):
        lines.append("## Aggregate Triage")
        lines.append("")
        if aggregate.get("skipped"):
            lines.append(f"- Skipped: {aggregate.get('reason')}")
        else:
            for key in ("total_cases", "failed_cases", "failure_group_count", "command_failures", "warning_cases"):
                if key in aggregate:
                    lines.append(f"- {key}: `{aggregate[key]}`")
            lines.append(f"- Report: `{aggregate.get('report_path', '')}`")
        lines.append("")

    replay = summary.get("replay")
    if isinstance(replay, dict):
        lines.append("## Replay")
        lines.append("")
        if replay.get("skipped"):
            lines.append(f"- Skipped: {replay.get('reason')}")
        else:
            for key in (
                "total",
                "stable_same_failure",
                "flaky_same_failure",
                "changed_failure",
                "unverified_failure",
                "not_reproduced",
                "unavailable",
            ):
                if key in replay:
                    lines.append(f"- {key}: `{replay[key]}`")
            lines.append(f"- Report: `{replay.get('report_path', '')}`")
        lines.append("")

    reductions = summary.get("reductions")
    if isinstance(reductions, dict):
        lines.append("## Reductions")
        lines.append("")
        if reductions.get("skipped"):
            lines.append(f"- Skipped: {reductions.get('reason')}")
        else:
            lines.append(f"- Candidates: `{reductions.get('candidate_count', 0)}`")
            lines.append(f"- Selected: `{reductions.get('selected_count', 0)}`")
            lines.append(f"- Completed: `{reductions.get('completed_count', 0)}`")
            lines.append(f"- Accepted reductions: `{reductions.get('accepted_reduction_count', 0)}`")
            lines.append(f"- Report: `{reductions.get('report_path', '')}`")
        lines.append("")

    bundles = summary.get("bundles")
    if isinstance(bundles, dict):
        lines.append("## Bundles")
        lines.append("")
        lines.append(f"- Count: `{bundles.get('bundle_count', 0)}`")
        lines.append(f"- Report: `{bundles.get('report_path', '')}`")
        lines.append("")

    registry = summary.get("bug_registry")
    if isinstance(registry, dict):
        lines.append("## Bug Registry")
        lines.append("")
        if registry.get("skipped"):
            lines.append(f"- Skipped: {registry.get('reason')}")
        else:
            lines.append(f"- Total: `{registry.get('total', 0)}`")
            lines.append(f"- Replay status: `{registry.get('by_replay_status', {})}`")
            lines.append(f"- Report: `{registry.get('report_path', '')}`")
            lines.append(f"- Replay recipes: `{registry.get('replay_recipes', '')}`")
        lines.append("")

    debug_handoff = summary.get("debug_handoff")
    if isinstance(debug_handoff, dict):
        lines.append("## Debug Handoff")
        lines.append("")
        if debug_handoff.get("skipped"):
            lines.append(f"- Skipped: {debug_handoff.get('reason')}")
        else:
            lines.append(f"- Packs: `{debug_handoff.get('pack_count', 0)}`")
            lines.append(f"- Debug SGTs: `{debug_handoff.get('debug_sgt_count', 0)}`")
            lines.append(f"- Focus topology SGTs: `{debug_handoff.get('focus_sgt_count', 0)}`")
            lines.append(f"- Input SGTs: `{debug_handoff.get('input_sgt_count', 0)}`")
            if debug_handoff.get("topology_extractor"):
                lines.append(f"- Topology extractor: `{debug_handoff.get('topology_extractor')}`")
            lines.append(f"- Report: `{debug_handoff.get('report_path', '')}`")
        lines.append("")

    drafts = summary.get("bug_record_drafts")
    if isinstance(drafts, dict):
        lines.append("## Bug Record Drafts")
        lines.append("")
        if drafts.get("skipped"):
            lines.append(f"- Skipped: {drafts.get('reason')}")
        else:
            lines.append(f"- Records: `{drafts.get('record_count', 0)}`")
            lines.append(f"- Drafts: `{drafts.get('draft_path', '')}`")
        lines.append("")

    promoted = summary.get("bug_records_promoted")
    if isinstance(promoted, dict):
        lines.append("## Promoted Bug Records")
        lines.append("")
        if promoted.get("skipped"):
            lines.append(f"- Skipped: {promoted.get('reason')}")
        else:
            lines.append(f"- Ok: `{promoted.get('ok')}`")
            lines.append(f"- Records: `{promoted.get('record_count', 0)}`")
            lines.append(f"- Copied assets: `{promoted.get('copied_asset_count', 0)}`")
            lines.append(f"- Registry: `{promoted.get('record_path', '')}`")
            lines.append(f"- Report: `{promoted.get('report_path', '')}`")
            lines.append(f"- Fixture root: `{promoted.get('fixture_root', '')}`")
            lines.append(f"- Portability ok: `{promoted.get('portability_ok')}` errors=`{promoted.get('portability_errors')}` warnings=`{promoted.get('portability_warnings')}`")
            lines.append(f"- Portability report: `{promoted.get('portability_report_path', '')}`")
        lines.append("")

    promoted_replay = summary.get("bug_records_promoted_replay")
    if isinstance(promoted_replay, dict):
        lines.append("## Promoted Bug Record Replay")
        lines.append("")
        if promoted_replay.get("skipped"):
            lines.append(f"- Skipped: {promoted_replay.get('reason')}")
        else:
            lines.append(f"- Ok: `{promoted_replay.get('ok')}`")
            lines.append(f"- Total: `{promoted_replay.get('total', 0)}`")
            lines.append(f"- Replay status: `{promoted_replay.get('by_replay_status', {})}`")
            lines.append(f"- Replay failed: `{promoted_replay.get('replay_failed', '')}`")
            lines.append(f"- Replay timed out: `{promoted_replay.get('replay_timed_out', '')}`")
            lines.append(f"- Registry: `{promoted_replay.get('registry_path', '')}`")
            lines.append(f"- Replay summary: `{promoted_replay.get('replay_summary', '')}`")
            lines.append(f"- Regression status: `{promoted_replay.get('regression_status_counts', {})}`")
            lines.append(f"- Regression report: `{promoted_replay.get('regression_report_path', '')}`")
        lines.append("")

    known = summary.get("known_bug_regression")
    if isinstance(known, dict):
        lines.append("## Known Bug Regression")
        lines.append("")
        if known.get("skipped"):
            lines.append(f"- Skipped: {known.get('reason')}")
        else:
            lines.append(f"- Bug-record files: `{known.get('record_file_count', 0)}`")
            lines.append(f"- Bugs: `{known.get('bug_count', 0)}`")
            lines.append(f"- Replay: total=`{known.get('replay_total', '')}` passed=`{known.get('replay_passed', '')}` failed=`{known.get('replay_failed', '')}` timed_out=`{known.get('replay_timed_out', '')}`")
            lines.append(f"- Regression status: `{known.get('status_counts', {})}`")
            lines.append(f"- Registry: `{known.get('registry_report', '')}`")
            lines.append(f"- Regression report: `{known.get('regression_report', '')}`")
            if known.get("replay_triage_report"):
                lines.append(f"- Replay triage: `{known.get('replay_triage_report')}`")
            if known.get("replay_contact_sheet"):
                lines.append(f"- Replay contact sheet: `{known.get('replay_contact_sheet')}`")
            if known.get("replay_geometry_audit_report"):
                lines.append(f"- Replay geometry audit: `{known.get('replay_geometry_audit_report')}`")
                lines.append(f"- Replay geometry duplicate inputs: `{known.get('replay_geometry_audit_duplicate_inputs', '')}`")
                lines.append(f"- Replay geometry tolerance mismatches: `{known.get('replay_geometry_audit_tolerance_mismatches', '')}`")
            if known.get("debug_handoff_report"):
                lines.append(f"- Debug handoff: `{known.get('debug_handoff_report')}`")
            lines.append(f"- Debug handoff packs: `{known.get('debug_handoff_pack_count', 0)}` focus SGTs=`{known.get('debug_handoff_focus_sgt_count', 0)}` input SGTs=`{known.get('debug_handoff_input_sgt_count', 0)}`")
        lines.append("")

    dataset_audit = summary.get("dataset_audit")
    if isinstance(dataset_audit, dict):
        lines.append("## Dataset Audit")
        lines.append("")
        if dataset_audit.get("skipped"):
            lines.append(f"- Skipped: {dataset_audit.get('reason')}")
        else:
            lines.append(f"- Ok: `{dataset_audit.get('ok')}`")
            lines.append(f"- Files: `{dataset_audit.get('total_files', '')}`")
            lines.append(f"- Missing files: `{dataset_audit.get('missing_files', '')}`")
            lines.append(f"- Empty files: `{dataset_audit.get('empty_files', '')}`")
            lines.append(f"- Errors: `{dataset_audit.get('error_count', '')}`")
            lines.append(f"- Warnings: `{dataset_audit.get('warning_count', '')}`")
            lines.append(f"- Hash coverage: `{dataset_audit.get('hash_coverage_ratio', '')}`")
            lines.append(f"- Duplicate content groups: `{dataset_audit.get('duplicate_content_group_count', '')}`")
            lines.append(f"- Summary: `{dataset_audit.get('summary_path', '')}`")
            lines.append(f"- Report: `{dataset_audit.get('report_path', '')}`")
        lines.append("")

    oracle = summary.get("oracle_coverage")
    if isinstance(oracle, dict):
        lines.append("## Oracle Coverage")
        lines.append("")
        if oracle.get("skipped"):
            lines.append(f"- Skipped: {oracle.get('reason')}")
        else:
            lines.append(f"- Ok: `{oracle.get('ok')}`")
            lines.append(f"- Cases: `{oracle.get('total_cases', '')}`")
            lines.append(f"- Passed cases: `{oracle.get('passed_cases', '')}`")
            lines.append(f"- Validation present: `{oracle.get('validation_present', '')}`")
            lines.append(f"- Validation missing: `{oracle.get('validation_missing', '')}`")
            lines.append(f"- Passed missing validation: `{oracle.get('passed_missing_validation', '')}`")
            lines.append(f"- Passed below min oracle kinds: `{oracle.get('passed_below_min_oracle_kinds', '')}`")
            lines.append(f"- Oracle kinds: `{oracle.get('oracle_kind_count', '')}`")
            lines.append(f"- Gate failures: `{oracle.get('gate_failures', [])}`")
            lines.append(f"- Summary: `{oracle.get('summary_path', '')}`")
            lines.append(f"- Report: `{oracle.get('report_path', '')}`")
        lines.append("")

    verification = summary.get("artifact_verification")
    if isinstance(verification, dict):
        lines.append("## Artifact Verification")
        lines.append("")
        if verification.get("skipped"):
            lines.append(f"- Skipped: {verification.get('reason')}")
        else:
            lines.append(f"- Ok: `{verification.get('ok')}`")
            lines.append(f"- Errors: `{verification.get('error_count', '')}`")
            lines.append(f"- Warnings: `{verification.get('warning_count', '')}`")
            lines.append(f"- Checks: `{verification.get('check_count', '')}`")
            lines.append(f"- Summary: `{verification.get('summary_path', '')}`")
            lines.append(f"- Report: `{verification.get('report_path', '')}`")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.jobs <= 0:
        print("--jobs must be >= 1", file=sys.stderr)
        return 1
    if args.discover_limit < 0:
        print("--discover-limit must be >= 0", file=sys.stderr)
        return 1
    if args.shard_count <= 0:
        print("--shard-count must be >= 1", file=sys.stderr)
        return 1
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        print("--shard-index must satisfy 0 <= index < shard-count", file=sys.stderr)
        return 1
    if args.timeout <= 0:
        print("--timeout must be > 0", file=sys.stderr)
        return 1
    if args.corpus_recut_require_exact_bbox_probe and args.corpus_recut_no_exact_bbox_probe:
        print("--corpus-recut-require-exact-bbox-probe cannot be combined with --corpus-recut-no-exact-bbox-probe", file=sys.stderr)
        return 1
    if args.replay_retries <= 0:
        print("--replay-retries must be >= 1", file=sys.stderr)
        return 1
    if args.topotrack_probe_timeout <= 0:
        print("--topotrack-probe-timeout must be > 0", file=sys.stderr)
        return 1
    if args.topotrack_probe_jobs <= 0:
        print("--topotrack-probe-jobs must be >= 1", file=sys.stderr)
        return 1
    if args.topotrack_probe_limit < 0:
        print("--topotrack-probe-limit must be >= 0", file=sys.stderr)
        return 1
    if args.reduction_limit < 0:
        print("--reduction-limit must be >= 0", file=sys.stderr)
        return 1
    if args.reduction_max_trials <= 0:
        print("--reduction-max-trials must be >= 1", file=sys.stderr)
        return 1
    if args.reduction_timeout < 0:
        print("--reduction-timeout must be >= 0", file=sys.stderr)
        return 1
    if args.reduction_min_dimension <= 0:
        print("--reduction-min-dimension must be > 0", file=sys.stderr)
        return 1
    if args.source_scan_max_findings < 0:
        print("--source-scan-max-findings must be >= 0", file=sys.stderr)
        return 1
    if args.source_scan_max_seeds < 0:
        print("--source-scan-max-seeds must be >= 0", file=sys.stderr)
        return 1
    if args.source_task_max_tasks < 0:
        print("--source-task-max-tasks must be >= 0", file=sys.stderr)
        return 1
    if args.source_task_context_lines < 0:
        print("--source-task-context-lines must be >= 0", file=sys.stderr)
        return 1
    if args.corpus_source_body_index < 0:
        print("--corpus-source-body-index must be >= 0", file=sys.stderr)
        return 1
    if args.corpus_roundtrip_abs_tol <= 0:
        print("--corpus-roundtrip-abs-tol must be > 0", file=sys.stderr)
        return 1
    if args.corpus_roundtrip_rel_tol <= 0:
        print("--corpus-roundtrip-rel-tol must be > 0", file=sys.stderr)
        return 1
    if args.oracle_coverage_min_kinds < 0:
        print("--oracle-coverage-min-kinds must be >= 0", file=sys.stderr)
        return 1
    if args.replay_promoted_bug_records and not args.promote_bug_records:
        print("--replay-promoted-bug-records requires --promote-bug-records", file=sys.stderr)
        return 1
    if args.promoted_replay_timeout <= 0:
        print("--promoted-replay-timeout must be > 0", file=sys.stderr)
        return 1
    if args.promoted_replay_jobs <= 0:
        print("--promoted-replay-jobs must be >= 1", file=sys.stderr)
        return 1
    if args.dataset_audit_fail_duplicate_ratio > 1:
        print("--dataset-audit-fail-duplicate-ratio must be <= 1.0 or negative to disable", file=sys.stderr)
        return 1

    runner = Path(args.runner).resolve()
    if not runner.is_file():
        print(f"runner not found: {runner}", file=sys.stderr)
        return 1

    started_at = now_iso_like()
    script_dir = Path(__file__).resolve().parent
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    command_records: list[dict[str, Any]] = []
    lanes: list[dict[str, Any]] = []
    original_corpus_recut_dataset_lists = list(args.dataset_list)

    source_scan = run_source_scan(args, script_dir, out_root, command_records)
    corpus_lane = run_corpus_lane(args, script_dir, runner, out_root, command_records)
    if corpus_lane is not None:
        lanes.append(corpus_lane)
    discovery_path = out_root / "discovery" / "dataset_index.json"
    if args.dataset_root and discovery_path.is_file():
        original_corpus_recut_dataset_lists.append(str(discovery_path))
    dataset_audit = run_dataset_audit(args, script_dir, out_root, original_corpus_recut_dataset_lists, command_records)
    artifact_list = ""
    if not args.skip_corpus_recut_artifacts:
        artifact_list = write_corpus_artifact_sgt_list(corpus_lane, out_root)

    if args.corpus_recut_use == "original":
        corpus_recut_dataset_lists = original_corpus_recut_dataset_lists
    elif args.corpus_recut_use == "artifacts":
        corpus_recut_dataset_lists = [artifact_list] if artifact_list else []
    elif args.corpus_recut_use == "both":
        corpus_recut_dataset_lists = list(original_corpus_recut_dataset_lists)
        if artifact_list:
            corpus_recut_dataset_lists.append(artifact_list)
    else:
        corpus_recut_dataset_lists = [artifact_list] if artifact_list else original_corpus_recut_dataset_lists
    lanes.extend(run_corpus_recut_lanes(args, script_dir, runner, out_root, command_records, corpus_recut_dataset_lists))
    lanes.extend(run_matrix_lanes(args, script_dir, runner, out_root, command_records))
    lanes.extend(run_dsl_lanes(args, script_dir, runner, out_root, command_records))

    topotrack_probe = run_topotrack_probe(args, script_dir, runner, out_root, lanes, command_records)
    aggregate = run_aggregate_triage(args, script_dir, out_root, lanes, command_records)
    replay = run_replay(args, script_dir, runner, out_root, aggregate, command_records)
    reductions = run_reductions(args, script_dir, runner, out_root, replay, command_records)
    bundles = run_bundle_export(
        args,
        script_dir,
        out_root,
        aggregate,
        replay,
        reductions,
        topotrack_probe,
        lanes,
        command_records,
    )
    bug_registry = run_bug_registry(args, script_dir, out_root, bundles, command_records)
    debug_handoff = run_debug_handoff(args, script_dir, out_root, aggregate, bug_registry, lanes, command_records)
    bug_record_drafts = run_bug_record_drafts(
        args,
        script_dir,
        out_root,
        bundles,
        debug_handoff,
        command_records,
    )
    bug_records_promoted = run_promote_bug_records(args, script_dir, out_root, bug_record_drafts, command_records)
    bug_records_promoted_replay = run_promoted_bug_record_replay(args, script_dir, runner, bug_records_promoted, command_records)
    known_bug_regression = run_known_bug_regression(args, script_dir, runner, out_root, command_records)

    summary = {
        "started_at": started_at,
        "updated_at": now_iso_like(),
        "runner": str(runner),
        "out_root": str(out_root),
        "args": vars(args),
        "source_scan": source_scan,
        "lanes": lanes,
        "topotrack_probe": topotrack_probe,
        "aggregate_triage": aggregate,
        "replay": replay,
        "reductions": reductions,
        "bundles": bundles,
        "bug_registry": bug_registry,
        "debug_handoff": debug_handoff,
        "bug_record_drafts": bug_record_drafts,
        "bug_records_promoted": bug_records_promoted,
        "bug_records_promoted_replay": bug_records_promoted_replay,
        "known_bug_regression": known_bug_regression,
        "dataset_audit": dataset_audit,
        "commands": command_records,
    }
    summary_path = out_root / "campaign_summary.json"
    report_path = out_root / "campaign_report.md"
    write_json(summary_path, summary)
    write_report(summary, report_path)
    oracle_coverage = run_oracle_coverage(args, script_dir, out_root, command_records)
    summary["oracle_coverage"] = oracle_coverage
    summary["commands"] = command_records
    summary["updated_at"] = now_iso_like()
    write_json(summary_path, summary)
    write_report(summary, report_path)
    artifact_verification = run_artifact_verification(args, script_dir, out_root, command_records)
    summary["artifact_verification"] = artifact_verification
    summary["commands"] = command_records
    summary["updated_at"] = now_iso_like()
    write_json(summary_path, summary)
    write_report(summary, report_path)
    print(f"summary={summary_path}")
    print(f"report={report_path}")

    infrastructure_failed = any(
        not record.get("ok")
        for record in command_records
        if record.get("name") != "artifact_verification"
    )
    if infrastructure_failed:
        return 1
    known_bug_changed = False
    if isinstance(known_bug_regression, dict):
        counts = known_bug_regression.get("status_counts") if isinstance(known_bug_regression.get("status_counts"), dict) else {}
        known_bug_changed = (
            (args.known_bug_fail_on_fixed and int(counts.get("fixed_or_not_reproduced") or 0) > 0)
            or (args.known_bug_fail_on_changed and int(counts.get("changed_failure") or 0) > 0)
            or (args.known_bug_fail_on_unavailable and int(counts.get("unavailable") or 0) > 0)
        )
    if known_bug_changed:
        return 2
    if args.fail_on_failures:
        aggregate_failed = isinstance(aggregate, dict) and (
            int(aggregate.get("failed_cases") or 0) > 0 or int(aggregate.get("command_failures") or 0) > 0
        )
        stable_replay = isinstance(replay, dict) and int(
            replay.get("stable_same_failure") or replay.get("stable_failure") or 0
        ) > 0
        if aggregate_failed or stable_replay:
            return 2
    if isinstance(artifact_verification, dict) and not artifact_verification.get("skipped") and not artifact_verification.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
