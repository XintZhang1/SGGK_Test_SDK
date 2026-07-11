#!/usr/bin/env python3
"""Collect multiple SGGK campaign shards into one merged report and registry."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

PATH_RE = re.compile(r"[A-Za-z]:\\[^\s`\"']+")
NUMBER_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign",
        action="append",
        required=True,
        help="Campaign output directory or campaign_summary.json. Can be passed more than once.",
    )
    parser.add_argument("--out", required=True, help="Output directory for merged shard summary/report")
    parser.add_argument("--runner", help="Path to sggk_case_runner.exe, required when --replay-reductions is set")
    parser.add_argument("--bug-prefix", default="merged_campaign", help="bug_id prefix for generated bug-record drafts")
    parser.add_argument("--skip-bug-registry", action="store_true", help="Do not run collect_bug_registry.py")
    parser.add_argument("--skip-debug-handoff", action="store_true", help="Do not build merged GUI-ready debug SGT handoff packs")
    parser.add_argument("--skip-bug-record-drafts", action="store_true", help="Do not run export_bug_record_drafts.py")
    parser.add_argument("--export-reduction-bug-record-drafts", action="store_true", help="Export editable bug-record drafts from reduced-recipe replay triage")
    parser.add_argument("--materialize-reduction-bug-records", action="store_true", help="Materialize reduced-replay bug-record drafts and classify them against reduced replay")
    parser.add_argument("--skip-oracle-coverage", action="store_true", help="Do not summarize merged validation/oracle coverage")
    parser.add_argument("--replay-reductions", action="store_true", help="Replay canonical merged reduced recipes with run_recipes.py")
    parser.add_argument("--reduction-replay-timeout", type=float, default=120.0, help="Per-recipe timeout for --replay-reductions")
    parser.add_argument("--reduction-replay-jobs", type=int, default=1, help="Parallel jobs for --replay-reductions")
    parser.add_argument("--reduction-replay-limit", type=int, default=0, help="Maximum canonical reduced recipes to replay; 0 means all")
    parser.add_argument(
        "--oracle-coverage-min-kinds",
        type=int,
        default=1,
        help="Minimum classified oracle kinds required for passed cases in the merged oracle coverage gate",
    )
    parser.add_argument(
        "--materialize-bug-records",
        action="store_true",
        help="Materialize merged bug-record drafts with record_bug_cases.py",
    )
    parser.add_argument(
        "--promote-bug-records",
        action="store_true",
        help="Promote merged bug-record drafts into artifact-local portable regression candidates",
    )
    parser.add_argument(
        "--replay-promoted-bug-records",
        action="store_true",
        help="Materialize, replay, and classify promoted bug-record candidates from the promoted root",
    )
    parser.add_argument("--promoted-replay-timeout", type=float, default=60.0, help="Per-recipe timeout for promoted bug-record replay")
    parser.add_argument("--promoted-replay-jobs", type=int, default=1, help="Parallel jobs for promoted bug-record replay")
    parser.add_argument(
        "--validate-recipes",
        action="store_true",
        help="Validate replay recipes when materializing bug records",
    )
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


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def normalize_path_key(raw: Any) -> str:
    text = as_str(raw)
    if not text:
        return ""
    try:
        return str(Path(text).resolve()).lower()
    except OSError:
        return str(Path(text)).lower()


def sanitize_name(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "record")).strip("._")
    return text or "record"


def normalize_failure_text(value: Any) -> str:
    text = str(value or "").lower()
    text = PATH_RE.sub("<path>", text)
    text = NUMBER_RE.sub("<num>", text)
    text = " ".join(text.split())
    return text[:240]


def validation_failure_keys(validation: dict[str, Any]) -> list[str]:
    failures = validation.get("failures")
    if not isinstance(failures, list):
        return []
    return sorted({text for item in failures if (text := normalize_failure_text(item))})


def topo_failure_keys(topo_check: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    bodies = topo_check.get("bodies")
    if not isinstance(bodies, list):
        return []
    for body in bodies:
        if not isinstance(body, dict) or body.get("ok") is not False:
            continue
        key = {
            "error_code": body.get("error_code"),
            "error_string": normalize_failure_text(body.get("error_string")),
        }
        keys.append(json.dumps(key, sort_keys=True, separators=(",", ":")))
    return sorted(set(keys))


def normalize_campaign_summary(raw: str) -> Path:
    path = Path(raw)
    return (path / "campaign_summary.json").resolve() if path.is_dir() else path.resolve()


def existing_path(raw: Any, fallback: Path | None = None) -> str:
    text = as_str(raw)
    if text and Path(text).exists():
        return str(Path(text).resolve())
    if fallback is not None and fallback.exists():
        return str(fallback.resolve())
    return ""


def section_path(summary: dict[str, Any], root: Path, section: str, key: str, fallback: Path) -> str:
    value = summary.get(section)
    if not isinstance(value, dict) or value.get("skipped"):
        return ""
    return existing_path(value.get(key), fallback)


def command_record(name: str, cmd: list[str], acceptable: set[int] | None = None, cwd: Path | None = None) -> dict[str, Any]:
    if acceptable is None:
        acceptable = {0}
    print(f"[campaign-shards] {name}")
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
    return {
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


def campaign_info(summary_path: Path) -> dict[str, Any]:
    summary = load_json(summary_path)
    root = summary_path.parent
    if not isinstance(summary, dict):
        return {
            "summary_path": str(summary_path),
            "root": str(root),
            "ok": False,
            "error": "campaign_summary_missing_or_invalid",
        }
    args = summary.get("args") if isinstance(summary.get("args"), dict) else {}
    lanes = []
    for lane in summary.get("lanes", []):
        if not isinstance(lane, dict):
            continue
        lanes.append(
            {
                "name": lane.get("name"),
                "type": lane.get("type"),
                "total": lane.get("total"),
                "executed": lane.get("executed"),
                "passed": lane.get("passed"),
                "failed": lane.get("failed"),
                "timed_out": lane.get("timed_out"),
                "empty_shard": bool(lane.get("empty_shard", False)),
                "out": lane.get("out"),
                "preview_out": lane.get("preview_out"),
                "dsl_check_report": lane.get("dsl_check_report"),
                "dsl_check_ok": lane.get("dsl_check_ok"),
                "dsl_check_recipe_count": lane.get("dsl_check_recipe_count"),
                "dsl_check_compile_failure_count": lane.get("dsl_check_compile_failure_count"),
                "dsl_check_validation_failure_count": lane.get("dsl_check_validation_failure_count"),
            }
        )
    aggregate = summary.get("aggregate_triage") if isinstance(summary.get("aggregate_triage"), dict) else {}
    raw_source_scan = summary.get("source_scan")
    source_scan = raw_source_scan if isinstance(raw_source_scan, dict) else {}
    source_tasks = source_scan.get("tasks") if isinstance(source_scan.get("tasks"), dict) else {}
    replay = summary.get("replay") if isinstance(summary.get("replay"), dict) else {}
    bundles = summary.get("bundles") if isinstance(summary.get("bundles"), dict) else {}
    registry = summary.get("bug_registry") if isinstance(summary.get("bug_registry"), dict) else {}
    drafts = summary.get("bug_record_drafts") if isinstance(summary.get("bug_record_drafts"), dict) else {}
    raw_dataset_audit = summary.get("dataset_audit")
    dataset_audit = raw_dataset_audit if isinstance(raw_dataset_audit, dict) else {}
    raw_known = summary.get("known_bug_regression")
    known = raw_known if isinstance(raw_known, dict) else {}
    raw_reductions = summary.get("reductions")
    reductions = raw_reductions if isinstance(raw_reductions, dict) else {}
    return {
        "summary_path": str(summary_path),
        "root": str(root),
        "ok": True,
        "started_at": summary.get("started_at"),
        "updated_at": summary.get("updated_at"),
        "runner": summary.get("runner"),
        "shard_count": args.get("shard_count"),
        "shard_index": args.get("shard_index"),
        "lanes": lanes,
        "source_scan": {
            "summary_path": section_path(summary, root, "source_scan", "summary_path", root / "source_scan" / "source_risk_report.json"),
            "report_path": section_path(summary, root, "source_scan", "report_path", root / "source_scan" / "source_risk_report.md"),
            "seed_path": section_path(summary, root, "source_scan", "seed_path", root / "source_scan" / "attack_seed_drafts.json"),
            "files_scanned": source_scan.get("files_scanned"),
            "findings": source_scan.get("findings"),
            "attack_seed_drafts": source_scan.get("attack_seed_drafts"),
            "attack_task_count": source_tasks.get("task_count"),
            "severity_counts": source_scan.get("severity_counts"),
            "category_counts": source_scan.get("category_counts"),
            "skipped": not isinstance(raw_source_scan, dict) or bool(source_scan.get("skipped", False)),
        },
        "aggregate_triage": {
            "summary_path": section_path(summary, root, "aggregate_triage", "summary_path", root / "triage" / "aggregate" / "triage_summary.json"),
            "report_path": section_path(summary, root, "aggregate_triage", "report_path", root / "triage" / "aggregate" / "triage_report.md"),
            "total_cases": aggregate.get("total_cases"),
            "failed_cases": aggregate.get("failed_cases"),
            "failure_group_count": aggregate.get("failure_group_count"),
            "command_failures": aggregate.get("command_failures"),
            "warning_cases": aggregate.get("warning_cases"),
        },
        "replay": {
            "summary_path": section_path(summary, root, "replay", "summary_path", root / "replay" / "aggregate" / "replay_summary.json"),
            "report_path": section_path(summary, root, "replay", "report_path", root / "replay" / "aggregate" / "replay_report.md"),
            "total": replay.get("total"),
            "stable_same_failure": replay.get("stable_same_failure") or replay.get("stable_failure"),
            "flaky_same_failure": replay.get("flaky_same_failure") or replay.get("flaky"),
            "changed_failure": replay.get("changed_failure"),
            "unverified_failure": replay.get("unverified_failure"),
            "not_reproduced": replay.get("not_reproduced"),
            "unavailable": replay.get("unavailable"),
            "skipped": bool(replay.get("skipped", False)),
        },
        "bundles": {
            "index_path": section_path(summary, root, "bundles", "index_path", root / "failure_bundles" / "bundle_index.json"),
            "report_path": section_path(summary, root, "bundles", "report_path", root / "failure_bundles" / "bundle_report.md"),
            "bundle_count": bundles.get("bundle_count"),
        },
        "bug_registry": {
            "summary_path": section_path(summary, root, "bug_registry", "summary_path", root / "bug_registry" / "bug_registry.json"),
            "report_path": section_path(summary, root, "bug_registry", "report_path", root / "bug_registry" / "bug_registry.md"),
            "total": registry.get("total"),
            "by_replay_status": registry.get("by_replay_status"),
        },
        "bug_record_drafts": {
            "draft_path": section_path(summary, root, "bug_record_drafts", "draft_path", root / "bug_record_drafts" / "drafts.json"),
            "record_count": drafts.get("record_count"),
        },
        "dataset_audit": {
            "summary_path": section_path(summary, root, "dataset_audit", "summary_path", root / "dataset_audit" / "dataset_audit.json"),
            "report_path": section_path(summary, root, "dataset_audit", "report_path", root / "dataset_audit" / "dataset_audit.md"),
            "ok": dataset_audit.get("ok"),
            "returncode": dataset_audit.get("returncode"),
            "total_files": dataset_audit.get("total_files"),
            "missing_files": dataset_audit.get("missing_files"),
            "empty_files": dataset_audit.get("empty_files"),
            "warning_count": dataset_audit.get("warning_count"),
            "error_count": dataset_audit.get("error_count"),
            "duplicate_content_group_count": dataset_audit.get("duplicate_content_group_count"),
            "hash_coverage_ratio": dataset_audit.get("hash_coverage_ratio"),
            "skipped": not isinstance(raw_dataset_audit, dict) or bool(dataset_audit.get("skipped", False)),
            "reason": dataset_audit.get("reason"),
        },
        "reductions": {
            "summary_path": section_path(summary, root, "reductions", "summary_path", root / "reductions" / "reduction_index.json"),
            "report_path": section_path(summary, root, "reductions", "report_path", root / "reductions" / "reduction_index.md"),
            "candidate_count": reductions.get("candidate_count"),
            "selected_count": reductions.get("selected_count"),
            "completed_count": reductions.get("completed_count"),
            "accepted_reduction_count": reductions.get("accepted_reduction_count"),
            "skipped": not isinstance(raw_reductions, dict) or bool(reductions.get("skipped", False)),
        },
        "known_bug_regression": {
            "registry_path": section_path(summary, root, "known_bug_regression", "registry_path", root / "known_bug_records" / "bug_registry.json"),
            "registry_report": section_path(summary, root, "known_bug_regression", "registry_report", root / "known_bug_records" / "bug_registry.md"),
            "replay_summary": section_path(summary, root, "known_bug_regression", "replay_summary", root / "known_bug_replay" / "recipe_summary.json"),
            "regression_summary": section_path(summary, root, "known_bug_regression", "regression_summary", root / "known_bug_regression" / "registry_regression.json"),
            "regression_report": section_path(summary, root, "known_bug_regression", "regression_report", root / "known_bug_regression" / "registry_regression.md"),
            "record_file_count": known.get("record_file_count"),
            "bug_count": known.get("bug_count"),
            "replay_total": known.get("replay_total"),
            "replay_failed": known.get("replay_failed"),
            "replay_timed_out": known.get("replay_timed_out"),
            "status_counts": known.get("status_counts"),
            "skipped": not isinstance(raw_known, dict) or bool(known.get("skipped", False)),
        },
        "oracle_coverage": summary.get("oracle_coverage") if isinstance(summary.get("oracle_coverage"), dict) else {},
    }


def summarize_campaigns(campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    lane_totals: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "campaign_count": 0,
        "total": 0,
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "timed_out": 0,
        "empty_shards": 0,
        "dsl_check_reports": 0,
        "dsl_check_failed_reports": 0,
        "dsl_check_recipes": 0,
        "dsl_check_compile_failures": 0,
        "dsl_check_validation_failures": 0,
    })
    aggregate = Counter()
    replay = Counter()
    source_scan = Counter()
    source_scan_severity = Counter()
    source_scan_category = Counter()
    known_bug_status = Counter()
    reductions = Counter()
    bundle_count = 0
    draft_count = 0
    known_bug_campaigns = 0
    known_bug_count = 0
    for campaign in campaigns:
        if not campaign.get("ok"):
            continue
        for lane in campaign.get("lanes", []):
            lane_name = as_str(lane.get("name")) or "unknown"
            item = lane_totals[lane_name]
            item["campaign_count"] += 1
            for key in ("total", "executed", "passed", "failed", "timed_out"):
                item[key] += as_int(lane.get(key))
            if lane.get("empty_shard"):
                item["empty_shards"] += 1
            if as_str(lane.get("dsl_check_report")):
                item["dsl_check_reports"] += 1
                if lane.get("dsl_check_ok") is False:
                    item["dsl_check_failed_reports"] += 1
                item["dsl_check_recipes"] += as_int(lane.get("dsl_check_recipe_count"))
                item["dsl_check_compile_failures"] += as_int(lane.get("dsl_check_compile_failure_count"))
                item["dsl_check_validation_failures"] += as_int(lane.get("dsl_check_validation_failure_count"))
        triage = campaign.get("aggregate_triage") if isinstance(campaign.get("aggregate_triage"), dict) else {}
        source = campaign.get("source_scan") if isinstance(campaign.get("source_scan"), dict) else {}
        if source and not source.get("skipped"):
            source_scan["campaign_count"] += 1
            for key in ("files_scanned", "findings", "attack_seed_drafts"):
                source_scan[key] += as_int(source.get(key))
            source_scan["attack_task_count"] += as_int(source.get("attack_task_count"))
            severity_counts = source.get("severity_counts") if isinstance(source.get("severity_counts"), dict) else {}
            for key, value in severity_counts.items():
                source_scan_severity[as_str(key)] += as_int(value)
            category_counts = source.get("category_counts") if isinstance(source.get("category_counts"), dict) else {}
            for key, value in category_counts.items():
                source_scan_category[as_str(key)] += as_int(value)
        for key in ("total_cases", "failed_cases", "failure_group_count", "command_failures", "warning_cases"):
            aggregate[key] += as_int(triage.get(key))
        replay_item = campaign.get("replay") if isinstance(campaign.get("replay"), dict) else {}
        for key in (
            "total",
            "stable_same_failure",
            "flaky_same_failure",
            "changed_failure",
            "unverified_failure",
            "not_reproduced",
            "unavailable",
        ):
            replay[key] += as_int(replay_item.get(key))
        bundles = campaign.get("bundles") if isinstance(campaign.get("bundles"), dict) else {}
        drafts = campaign.get("bug_record_drafts") if isinstance(campaign.get("bug_record_drafts"), dict) else {}
        reduction_item = campaign.get("reductions") if isinstance(campaign.get("reductions"), dict) else {}
        if reduction_item and not reduction_item.get("skipped"):
            reductions["campaign_count"] += 1
            for key in ("candidate_count", "selected_count", "completed_count", "accepted_reduction_count"):
                reductions[key] += as_int(reduction_item.get(key))
        bundle_count += as_int(bundles.get("bundle_count"))
        draft_count += as_int(drafts.get("record_count"))
        known = campaign.get("known_bug_regression") if isinstance(campaign.get("known_bug_regression"), dict) else {}
        if known and not known.get("skipped"):
            known_bug_campaigns += 1
            known_bug_count += as_int(known.get("bug_count"))
            status_counts = known.get("status_counts") if isinstance(known.get("status_counts"), dict) else {}
            for key, value in status_counts.items():
                known_bug_status[as_str(key)] += as_int(value)
    return {
        "lanes": dict(sorted(lane_totals.items())),
        "source_scan_raw_sum": dict(source_scan),
        "source_scan_severity_raw_sum": dict(sorted(source_scan_severity.items())),
        "source_scan_category_raw_sum": dict(sorted(source_scan_category.items())),
        "aggregate_triage_raw_sum": dict(aggregate),
        "replay_raw_sum": dict(replay),
        "bundle_count_raw_sum": bundle_count,
        "draft_count_raw_sum": draft_count,
        "reduction_raw_sum": dict(reductions),
        "known_bug_campaign_count": known_bug_campaigns,
        "known_bug_count_raw_sum": known_bug_count,
        "known_bug_status_raw_sum": dict(sorted(known_bug_status.items())),
    }


def write_dataset_audit_collection_report(index: dict[str, Any], path: Path) -> None:
    lines = [
        "# SGGK Merged Dataset Audit",
        "",
        f"- Generated: `{index.get('generated_at')}`",
        f"- OK: `{index.get('ok')}`",
        f"- Campaigns: `{index.get('campaign_count')}`",
        f"- Audited campaigns: `{index.get('audited_campaign_count')}`",
        f"- Skipped campaigns: `{index.get('skipped_campaign_count')}`",
        f"- Missing/legacy audit blocks: `{index.get('missing_block_count')}`",
        f"- Failed audited campaigns: `{index.get('failed_campaign_count')}`",
        f"- Files raw sum: `{index.get('total_files')}`",
        f"- Missing files raw sum: `{index.get('missing_files')}`",
        f"- Empty files raw sum: `{index.get('empty_files')}`",
        f"- Duplicate content groups raw sum: `{index.get('duplicate_content_group_count')}`",
        f"- Min hash coverage: `{index.get('min_hash_coverage_ratio')}`",
        f"- Weighted hash coverage: `{index.get('weighted_hash_coverage_ratio')}`",
        "",
        "| campaign | shard | status | files | missing | empty | errors | warnings | hash coverage | audit report |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for campaign in as_list(index.get("campaigns")):
        if not isinstance(campaign, dict):
            continue
        shard = ""
        if campaign.get("shard_count") is not None and campaign.get("shard_index") is not None:
            shard = f"{campaign.get('shard_index')}/{campaign.get('shard_count')}"
        lines.append(
            "| `{root}` | `{shard}` | `{status}` | `{files}` | `{missing}` | `{empty}` | `{errors}` | `{warnings}` | `{hash}` | `{report}` |".format(
                root=campaign.get("root", ""),
                shard=shard,
                status=campaign.get("status", ""),
                files=campaign.get("total_files", ""),
                missing=campaign.get("missing_files", ""),
                empty=campaign.get("empty_files", ""),
                errors=campaign.get("error_count", ""),
                warnings=campaign.get("warning_count", ""),
                hash=campaign.get("hash_coverage_ratio", ""),
                report=campaign.get("report_path", ""),
            )
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def collect_dataset_audits(out_dir: Path, campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    audited_count = 0
    skipped_count = 0
    missing_block_count = 0
    failed_count = 0
    totals = Counter()
    hash_weighted_sum = 0.0
    hash_weight = 0
    min_hash: float | None = None

    for campaign in campaigns:
        audit_block = campaign.get("dataset_audit") if isinstance(campaign.get("dataset_audit"), dict) else {}
        row: dict[str, Any] = {
            "root": campaign.get("root"),
            "summary_path": audit_block.get("summary_path"),
            "report_path": audit_block.get("report_path"),
            "shard_count": campaign.get("shard_count"),
            "shard_index": campaign.get("shard_index"),
        }
        if not audit_block or audit_block.get("skipped"):
            if audit_block and audit_block.get("summary_path"):
                skipped_count += 1
                row["status"] = "skipped"
                row["reason"] = audit_block.get("reason")
            else:
                missing_block_count += 1
                row["status"] = "missing_or_legacy"
            rows.append(row)
            continue

        summary_path = as_str(audit_block.get("summary_path"))
        audit = load_json(Path(summary_path)) if summary_path else None
        if not isinstance(audit, dict):
            failed_count += 1
            row["status"] = "audit_json_missing_or_invalid"
            row["ok"] = False
            rows.append(row)
            continue

        audited_count += 1
        audit_ok = audit_block.get("ok") is True and audit.get("ok") is True
        total_files = as_int(audit.get("total_files"))
        missing_files = as_int(audit.get("missing_files"))
        empty_files = as_int(audit.get("empty_files"))
        error_count = as_int(audit.get("error_count"))
        warning_count = as_int(audit.get("warning_count"))
        duplicate_groups = as_int(audit.get("duplicate_content_group_count"))
        hash_ratio = audit.get("hash_coverage_ratio")
        hash_ratio_value = as_float(hash_ratio, -1.0)
        if hash_ratio_value >= 0.0:
            min_hash = hash_ratio_value if min_hash is None else min(min_hash, hash_ratio_value)
            hash_weighted_sum += hash_ratio_value * max(total_files, 0)
            hash_weight += max(total_files, 0)
        row.update(
            {
                "status": "ok" if audit_ok else "failed",
                "ok": audit_ok,
                "total_files": total_files,
                "missing_files": missing_files,
                "empty_files": empty_files,
                "error_count": error_count,
                "warning_count": warning_count,
                "duplicate_content_group_count": duplicate_groups,
                "hash_coverage_ratio": hash_ratio,
            }
        )
        for key, value in (
            ("total_files", total_files),
            ("missing_files", missing_files),
            ("empty_files", empty_files),
            ("error_count", error_count),
            ("warning_count", warning_count),
            ("duplicate_content_group_count", duplicate_groups),
        ):
            totals[key] += value
        if not audit_ok or missing_files or empty_files:
            failed_count += 1
        rows.append(row)

    if audited_count == 0:
        return {
            "skipped": True,
            "reason": "no campaign dataset audit blocks",
            "campaign_count": len(campaigns),
            "missing_block_count": missing_block_count,
            "skipped_campaign_count": skipped_count,
        }

    audit_dir = out_dir / "dataset_audit"
    summary_path = audit_dir / "dataset_audit_collection.json"
    report_path = audit_dir / "dataset_audit_collection.md"
    payload = {
        "generated_at": now_iso_like(),
        "ok": failed_count == 0,
        "campaign_count": len(campaigns),
        "audited_campaign_count": audited_count,
        "skipped_campaign_count": skipped_count,
        "missing_block_count": missing_block_count,
        "failed_campaign_count": failed_count,
        "total_files": totals.get("total_files", 0),
        "missing_files": totals.get("missing_files", 0),
        "empty_files": totals.get("empty_files", 0),
        "error_count": totals.get("error_count", 0),
        "warning_count": totals.get("warning_count", 0),
        "duplicate_content_group_count": totals.get("duplicate_content_group_count", 0),
        "min_hash_coverage_ratio": min_hash,
        "weighted_hash_coverage_ratio": (hash_weighted_sum / hash_weight) if hash_weight else None,
        "campaigns": rows,
    }
    audit_dir.mkdir(parents=True, exist_ok=True)
    write_json(summary_path, payload)
    write_dataset_audit_collection_report(payload, report_path)
    return {
        "summary_path": str(summary_path),
        "report_path": str(report_path),
        "ok": payload["ok"],
        "campaign_count": payload["campaign_count"],
        "audited_campaign_count": payload["audited_campaign_count"],
        "skipped_campaign_count": payload["skipped_campaign_count"],
        "missing_block_count": payload["missing_block_count"],
        "failed_campaign_count": payload["failed_campaign_count"],
        "total_files": payload["total_files"],
        "missing_files": payload["missing_files"],
        "empty_files": payload["empty_files"],
        "error_count": payload["error_count"],
        "warning_count": payload["warning_count"],
        "duplicate_content_group_count": payload["duplicate_content_group_count"],
        "min_hash_coverage_ratio": payload["min_hash_coverage_ratio"],
        "weighted_hash_coverage_ratio": payload["weighted_hash_coverage_ratio"],
    }


def write_reduction_index_report(index: dict[str, Any], path: Path) -> None:
    lines = [
        "# SGGK Merged Campaign Reductions",
        "",
        f"- Generated: `{index.get('generated_at')}`",
        f"- Source campaigns with reductions: `{index.get('source_campaign_count')}`",
        f"- Candidate stable failures raw sum: `{index.get('candidate_count')}`",
        f"- Selected raw sum: `{index.get('selected_count')}`",
        f"- Completed raw sum: `{index.get('completed_count')}`",
        f"- Accepted reductions raw sum: `{index.get('accepted_reduction_count')}`",
        f"- Distinct fingerprints: `{index.get('distinct_fingerprint_count', '')}`",
        f"- Duplicate fingerprint groups: `{index.get('duplicate_fingerprint_group_count', '')}`",
        "",
        "## Fingerprint Groups",
        "",
        "| fingerprint | entries | completed | accepted | canonical reduced recipe | canonical report |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for group in index.get("fingerprint_groups", []):
        if not isinstance(group, dict):
            continue
        lines.append(
            "| `{fingerprint}` | {entries} | {completed} | {accepted} | `{recipe}` | `{report}` |".format(
                fingerprint=group.get("fingerprint", ""),
                entries=group.get("entry_count", ""),
                completed=group.get("completed_count", ""),
                accepted=group.get("accepted_reduction_count", ""),
                recipe=group.get("canonical_reduced_recipe", ""),
                report=group.get("canonical_report_path", ""),
            )
        )
    lines.extend(
        [
            "",
            "## Raw Reduction Entries",
            "",
        ]
    )
    lines.extend(
        [
        "| campaign | shard | fingerprint | case | status | accepted | trials | reduced recipe | report |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    for item in index.get("reductions", []):
        if not isinstance(item, dict):
            continue
        shard = ""
        if item.get("shard_index") is not None and item.get("shard_count") is not None:
            shard = f"{item.get('shard_index')}/{item.get('shard_count')}"
        lines.append(
            "| `{campaign}` | `{shard}` | `{fingerprint}` | `{case}` | `{status}` | {accepted} | {trials} | `{recipe}` | `{report}` |".format(
                campaign=item.get("source_campaign_root", ""),
                shard=shard,
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


def reduction_fingerprint(item: dict[str, Any], index: int) -> str:
    return as_str(item.get("fingerprint")) or f"missing_fingerprint_{index}"


def reduction_shard_ref(item: dict[str, Any]) -> str:
    if item.get("shard_index") is None or item.get("shard_count") is None:
        return ""
    return f"{item.get('shard_index')}/{item.get('shard_count')}"


def build_reduction_fingerprint_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, item in enumerate(items):
        grouped[reduction_fingerprint(item, index)].append((index, item))

    groups: list[dict[str, Any]] = []
    for fingerprint in sorted(grouped):
        entries = grouped[fingerprint]

        def canonical_score(pair: tuple[int, dict[str, Any]]) -> tuple[int, int, int, int]:
            index, item = pair
            return (
                1 if item.get("status") == "completed" else 0,
                as_int(item.get("accepted_reductions")),
                1 if as_str(item.get("reduced_recipe")) else 0,
                -index,
            )

        canonical_index, canonical = max(entries, key=canonical_score)
        status_counts: Counter[str] = Counter()
        source_roots: set[str] = set()
        shard_refs: set[str] = set()
        for _, item in entries:
            status_counts[as_str(item.get("status")) or "unknown"] += 1
            if as_str(item.get("source_campaign_root")):
                source_roots.add(as_str(item.get("source_campaign_root")))
            if reduction_shard_ref(item):
                shard_refs.add(reduction_shard_ref(item))
        groups.append(
            {
                "fingerprint": fingerprint,
                "entry_count": len(entries),
                "completed_count": sum(1 for _, item in entries if item.get("status") == "completed"),
                "accepted_reduction_count": sum(as_int(item.get("accepted_reductions")) for _, item in entries),
                "entry_indices": [index for index, _ in entries],
                "source_campaign_roots": sorted(source_roots),
                "shards": sorted(shard_refs),
                "canonical_entry_index": canonical_index,
                "canonical_reduced_recipe": as_str(canonical.get("reduced_recipe")),
                "canonical_report_path": as_str(canonical.get("report_path")),
                "canonical_summary_path": as_str(canonical.get("summary_path")),
                "status_counts": dict(sorted(status_counts.items())),
            }
        )
    return groups


def merge_reductions(out_dir: Path, campaigns: list[dict[str, Any]]) -> dict[str, Any] | None:
    reduction_sources = [
        campaign
        for campaign in campaigns
        if isinstance(campaign.get("reductions"), dict)
        and not campaign["reductions"].get("skipped")
        and as_str(campaign["reductions"].get("summary_path"))
    ]
    if not reduction_sources:
        return {"skipped": True, "reason": "no campaign reduction indexes"}

    reductions_root = out_dir / "reductions"
    merged_items: list[dict[str, Any]] = []
    candidate_count = 0
    selected_count = 0
    completed_count = 0
    accepted_reduction_count = 0
    source_errors: list[dict[str, Any]] = []

    for campaign in reduction_sources:
        block = campaign["reductions"]
        index_path = Path(as_str(block.get("summary_path")))
        index = load_json(index_path)
        if not isinstance(index, dict):
            source_errors.append(
                {
                    "campaign_root": campaign.get("root"),
                    "summary_path": str(index_path),
                    "error": "reduction_index_missing_or_invalid",
                }
            )
            continue
        candidate_count += as_int(index.get("candidate_count"))
        selected_count += as_int(index.get("selected_count"))
        completed_count += as_int(index.get("completed_count"))
        accepted_reduction_count += as_int(index.get("accepted_reduction_count"))
        for item in index.get("reductions", []):
            if not isinstance(item, dict):
                continue
            merged = dict(item)
            merged["source_campaign_root"] = campaign.get("root")
            merged["source_campaign_summary"] = campaign.get("summary_path")
            merged["source_reduction_index"] = str(index_path)
            merged["shard_count"] = campaign.get("shard_count")
            merged["shard_index"] = campaign.get("shard_index")
            merged_items.append(merged)

    fingerprint_groups = build_reduction_fingerprint_groups(merged_items)
    duplicate_fingerprint_group_count = sum(1 for group in fingerprint_groups if as_int(group.get("entry_count")) > 1)
    index_payload = {
        "generated_at": now_iso_like(),
        "source_campaign_count": len(reduction_sources),
        "candidate_count": candidate_count,
        "selected_count": selected_count,
        "completed_count": completed_count,
        "accepted_reduction_count": accepted_reduction_count,
        "distinct_fingerprint_count": len(fingerprint_groups),
        "duplicate_fingerprint_group_count": duplicate_fingerprint_group_count,
        "source_errors": source_errors,
        "fingerprint_groups": fingerprint_groups,
        "reductions": merged_items,
    }
    write_json(reductions_root / "reduction_index.json", index_payload)
    write_reduction_index_report(index_payload, reductions_root / "reduction_index.md")
    return {
        "out": str(reductions_root),
        "summary_path": str(reductions_root / "reduction_index.json"),
        "report_path": str(reductions_root / "reduction_index.md"),
        "source_campaign_count": len(reduction_sources),
        "candidate_count": candidate_count,
        "selected_count": selected_count,
        "completed_count": completed_count,
        "accepted_reduction_count": accepted_reduction_count,
        "distinct_fingerprint_count": len(fingerprint_groups),
        "duplicate_fingerprint_group_count": duplicate_fingerprint_group_count,
        "source_error_count": len(source_errors),
        "ok": not source_errors,
    }


def canonical_reduced_recipes(reduction_index: dict[str, Any]) -> list[str]:
    recipes: list[str] = []
    seen: set[str] = set()
    groups = reduction_index.get("fingerprint_groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            recipe = as_str(group.get("canonical_reduced_recipe"))
            if recipe and Path(recipe).is_file() and recipe not in seen:
                recipes.append(recipe)
                seen.add(recipe)
    if recipes:
        return recipes
    for item in reduction_index.get("reductions", []):
        if not isinstance(item, dict):
            continue
        recipe = as_str(item.get("reduced_recipe"))
        if recipe and Path(recipe).is_file() and recipe not in seen:
            recipes.append(recipe)
            seen.add(recipe)
    return recipes


def actual_replay_reasons(result: dict[str, Any], status: dict[str, Any], validation: dict[str, Any], topo_check: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if result.get("timed_out"):
        reasons.append("timed_out")
    if status:
        if status.get("succeeded") is False:
            reasons.append("api_failed")
    if validation and validation.get("ok") is False:
        reasons.append("validation_failed")
    if topo_failure_keys(topo_check):
        reasons.append("topology_invalid")
    if as_int(result.get("returncode")) != 0 and not reasons:
        reasons.append("runner_nonzero_exit")
    return sorted(set(reasons))


def replay_result_maps(recipe_summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result_by_recipe: dict[str, dict[str, Any]] = {}
    for result in as_list(recipe_summary.get("results")):
        if not isinstance(result, dict):
            continue
        key = normalize_path_key(result.get("recipe"))
        if key:
            result_by_recipe[key] = result
    return result_by_recipe


def replay_triage_group_maps(triage: dict[str, Any]) -> dict[str, dict[str, Any]]:
    group_by_recipe: dict[str, dict[str, Any]] = {}
    for group in as_list(triage.get("failure_groups")):
        if not isinstance(group, dict):
            continue
        for recipe in as_list(group.get("recipe_paths")):
            key = normalize_path_key(recipe)
            if key:
                group_by_recipe[key] = group
    return group_by_recipe


def predicate_preservation_mismatches(
    result: dict[str, Any],
    status: dict[str, Any],
    validation: dict[str, Any],
    topo_check: dict[str, Any],
    predicate: dict[str, Any],
    expected_returncode: int,
) -> tuple[list[str], list[str], list[str], list[str]]:
    mismatches: list[str] = []
    actual_reasons = actual_replay_reasons(result, status, validation, topo_check)
    expected_reasons = sorted({as_str(item) for item in as_list(predicate.get("reasons")) if as_str(item)})
    actual_returncode = as_int(result.get("returncode"))
    if actual_returncode == 0:
        mismatches.append("replay_returncode_zero")
    if expected_returncode and actual_returncode != expected_returncode:
        mismatches.append(f"returncode_changed:{expected_returncode}->{actual_returncode}")
    for reason in expected_reasons:
        if reason == "timed_out":
            if not result.get("timed_out"):
                mismatches.append("expected_timeout_missing")
        elif reason == "api_failed":
            if status.get("succeeded") is not False:
                mismatches.append("expected_api_failed_missing")
            expected_error = predicate.get("error_code")
            if isinstance(expected_error, int) and not isinstance(expected_error, bool):
                if status.get("error_code") != expected_error:
                    mismatches.append(f"api_error_code_changed:{expected_error}->{status.get('error_code')}")
        elif reason == "validation_failed":
            if validation.get("ok") is not False:
                mismatches.append("expected_validation_failed_missing")
            expected_validation = {as_str(item) for item in as_list(predicate.get("validation_failures")) if as_str(item)}
            actual_validation = set(validation_failure_keys(validation))
            if expected_validation and not expected_validation.issubset(actual_validation):
                mismatches.append("validation_failure_keys_missing")
        elif reason == "topology_invalid":
            actual_topo = set(topo_failure_keys(topo_check))
            expected_topo = {as_str(item) for item in as_list(predicate.get("topo_failures")) if as_str(item)}
            if not actual_topo:
                mismatches.append("expected_topology_invalid_missing")
            elif expected_topo and not expected_topo.intersection(actual_topo):
                mismatches.append("topology_failure_keys_changed")
        elif reason == "runner_nonzero_exit":
            if actual_returncode == 0:
                mismatches.append("expected_runner_nonzero_exit_missing")
        elif reason and reason not in actual_reasons:
            mismatches.append(f"expected_reason_missing:{reason}")
    return mismatches, expected_reasons, actual_reasons, validation_failure_keys(validation)


def write_reduction_replay_semantic_report(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# SGGK Reduced Replay Semantic Check",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- OK: `{payload.get('ok')}`",
        f"- Items: `{payload.get('item_count')}`",
        f"- Stable same failure: `{payload.get('stable_same_failure_count')}`",
        f"- Changed failure: `{payload.get('changed_failure_count')}`",
        f"- Not reproduced: `{payload.get('not_reproduced_count')}`",
        f"- Unavailable: `{payload.get('unavailable_count')}`",
        f"- Limited replay: `{payload.get('limited_replay')}`",
        "",
        "| status | reduction fingerprint | replay fingerprint | expected rc | actual rc | canonical recipe | notes |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for item in as_list(payload.get("items")):
        if not isinstance(item, dict):
            continue
        notes = "; ".join(as_list(item.get("mismatches")) or as_list(item.get("notes")))
        lines.append(
            "| `{status}` | `{original}` | `{replay}` | `{expected}` | `{actual}` | `{recipe}` | {notes} |".format(
                status=item.get("status", ""),
                original=item.get("reduction_fingerprint", ""),
                replay=item.get("replay_fingerprint", ""),
                expected=item.get("expected_returncode", ""),
                actual=item.get("actual_returncode", ""),
                recipe=item.get("canonical_reduced_recipe", ""),
                notes=notes,
            )
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_reduction_replay_semantic_check(
    reduction_index: dict[str, Any],
    recipe_summary: dict[str, Any],
    triage: dict[str, Any],
    recipes: list[str],
    out_dir: Path,
) -> dict[str, Any]:
    replayed_keys = {normalize_path_key(recipe) for recipe in recipes if normalize_path_key(recipe)}
    result_by_recipe = replay_result_maps(recipe_summary)
    group_by_recipe = replay_triage_group_maps(triage)
    all_groups = [group for group in as_list(reduction_index.get("fingerprint_groups")) if isinstance(group, dict)]
    groups = [
        group
        for group in all_groups
        if normalize_path_key(group.get("canonical_reduced_recipe")) in replayed_keys
    ]
    items: list[dict[str, Any]] = []
    for group in groups:
        recipe = as_str(group.get("canonical_reduced_recipe"))
        recipe_key = normalize_path_key(recipe)
        summary_path = as_str(group.get("canonical_summary_path"))
        reduction_summary = load_json(Path(summary_path)) if summary_path else None
        item: dict[str, Any] = {
            "reduction_fingerprint": group.get("fingerprint"),
            "canonical_reduced_recipe": recipe,
            "canonical_summary_path": summary_path,
            "mismatches": [],
            "notes": [],
        }
        if not recipe_key or not recipe:
            item["status"] = "unavailable"
            item["mismatches"].append("canonical_recipe_missing")
            items.append(item)
            continue
        result = result_by_recipe.get(recipe_key)
        triage_group = group_by_recipe.get(recipe_key)
        if not isinstance(reduction_summary, dict):
            item["status"] = "unavailable"
            item["mismatches"].append("reduction_summary_missing_or_invalid")
            items.append(item)
            continue
        predicate = reduction_summary.get("predicate") if isinstance(reduction_summary.get("predicate"), dict) else {}
        final_observation = (
            reduction_summary.get("final_observation")
            if isinstance(reduction_summary.get("final_observation"), dict)
            else {}
        )
        expected_returncode = as_int(final_observation.get("returncode"))
        item["expected_returncode"] = expected_returncode
        item["expected_reasons"] = as_list(predicate.get("reasons"))
        if not isinstance(result, dict):
            item["status"] = "unavailable"
            item["mismatches"].append("replay_result_missing")
            items.append(item)
            continue
        item["actual_returncode"] = result.get("returncode")
        item["timed_out"] = result.get("timed_out")
        item["case_id"] = result.get("case_id")
        item["artifact_dir"] = result.get("artifact_dir")
        artifact_dir = Path(as_str(result.get("artifact_dir")))
        status_report = load_json(artifact_dir / "report" / "status.json")
        validation_report = load_json(artifact_dir / "report" / "validation.json")
        topo_report = load_json(artifact_dir / "report" / "topo_check.json")
        status_report = status_report if isinstance(status_report, dict) else {}
        validation_report = validation_report if isinstance(validation_report, dict) else {}
        topo_report = topo_report if isinstance(topo_report, dict) else {}
        mismatches, expected_reasons, actual_reasons, actual_validation = predicate_preservation_mismatches(
            result,
            status_report,
            validation_report,
            topo_report,
            predicate,
            expected_returncode,
        )
        item["expected_reasons"] = expected_reasons
        item["actual_reasons"] = actual_reasons
        item["actual_validation_failures"] = actual_validation
        if isinstance(triage_group, dict):
            item["replay_fingerprint"] = triage_group.get("fingerprint")
            item["triage_reasons"] = triage_group.get("reasons")
            item["triage_representative_case_id"] = triage_group.get("representative_case_id")
        else:
            mismatches.append("replay_triage_group_missing")
        item["mismatches"] = mismatches
        if as_int(result.get("returncode")) == 0:
            item["status"] = "not_reproduced"
        elif mismatches:
            item["status"] = "changed_failure"
        else:
            item["status"] = "stable_same_failure"
        items.append(item)

    status_counts: Counter[str] = Counter(as_str(item.get("status")) or "unknown" for item in items)
    payload = {
        "generated_at": now_iso_like(),
        "ok": bool(items) and not any(item.get("status") != "stable_same_failure" for item in items),
        "item_count": len(items),
        "expected_canonical_count": len(all_groups),
        "replayed_recipe_count": len(replayed_keys),
        "limited_replay": bool(all_groups and len(replayed_keys) < len(all_groups)),
        "stable_same_failure_count": status_counts.get("stable_same_failure", 0),
        "changed_failure_count": status_counts.get("changed_failure", 0),
        "not_reproduced_count": status_counts.get("not_reproduced", 0),
        "unavailable_count": status_counts.get("unavailable", 0),
        "status_counts": dict(sorted(status_counts.items())),
        "items": items,
    }
    summary_path = out_dir / "semantic_check.json"
    report_path = out_dir / "semantic_check.md"
    write_json(summary_path, payload)
    write_reduction_replay_semantic_report(payload, report_path)
    return {
        "summary_path": str(summary_path),
        "report_path": str(report_path),
        "ok": payload.get("ok"),
        "item_count": payload.get("item_count"),
        "stable_same_failure_count": payload.get("stable_same_failure_count"),
        "changed_failure_count": payload.get("changed_failure_count"),
        "not_reproduced_count": payload.get("not_reproduced_count"),
        "unavailable_count": payload.get("unavailable_count"),
        "limited_replay": payload.get("limited_replay"),
    }


def replay_reductions(
    args: argparse.Namespace,
    script_dir: Path,
    out_dir: Path,
    reductions: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not args.replay_reductions:
        return {"skipped": True, "reason": "skip_replay_reductions"}
    if not isinstance(reductions, dict) or reductions.get("skipped") or not as_str(reductions.get("summary_path")):
        return {"skipped": True, "reason": "no merged reductions"}
    index = load_json(Path(as_str(reductions["summary_path"])))
    if not isinstance(index, dict):
        return {"skipped": True, "reason": "merged reduction index missing or invalid"}
    recipes = canonical_reduced_recipes(index)
    if args.reduction_replay_limit > 0:
        recipes = recipes[: args.reduction_replay_limit]
    if not recipes:
        return {"skipped": True, "reason": "no canonical reduced recipes"}

    replay_root = out_dir / "reduction_replay"
    recipe_list = replay_root / "canonical_reduced_recipes.txt"
    replay_out = replay_root / "runs"
    triage_out = replay_root / "triage"
    preview_out = replay_root / "previews"
    geometry_audit_out = replay_root / "geometry_audit"
    recipe_list.parent.mkdir(parents=True, exist_ok=True)
    recipe_list.write_text("\n".join(recipes) + "\n", encoding="utf-8")

    cmd = [
        sys.executable,
        str(script_dir / "run_recipes.py"),
        "--runner",
        as_str(args.runner),
        "--recipe-list",
        str(recipe_list),
        "--out",
        str(replay_out),
        "--timeout",
        str(args.reduction_replay_timeout),
        "--jobs",
        str(args.reduction_replay_jobs),
        "--hash-recipes",
        "--triage-out",
        str(triage_out),
        "--preview-out",
        str(preview_out),
        "--contact-sheet",
        str(preview_out / "contact.png"),
        "--geometry-audit-out",
        str(geometry_audit_out),
    ]
    record = command_record("replay_reductions", cmd, acceptable={0, 2})
    summary = load_json(replay_out / "recipe_summary.json")
    triage = load_json(triage_out / "triage_summary.json")
    geometry_audit = load_json(geometry_audit_out / "geometry_audit.json")
    result: dict[str, Any] = {
        "out": str(replay_root),
        "recipe_list": str(recipe_list),
        "recipe_count": len(recipes),
        "run_out": str(replay_out),
        "recipe_summary": str(replay_out / "recipe_summary.json"),
        "recipe_manifest": str(replay_out / "recipe_manifest.json"),
        "triage_summary": str(triage_out / "triage_summary.json"),
        "triage_report": str(triage_out / "triage_report.md"),
        "preview_out": str(preview_out),
        "contact_sheet": str(preview_out / "contact.png"),
        "geometry_audit_summary": str(geometry_audit_out / "geometry_audit.json"),
        "geometry_audit_report": str(geometry_audit_out / "geometry_audit.md"),
        "returncode": record["returncode"],
        "ok": record["ok"],
        "command": record,
    }
    if isinstance(summary, dict):
        result.update(
            {
                "total": summary.get("total"),
                "failed": summary.get("failed"),
                "timed_out": summary.get("timed_out"),
                "command_failures": summary.get("command_failures"),
            }
        )
    if isinstance(triage, dict):
        result["failure_group_count"] = triage.get("failure_group_count")
    if isinstance(geometry_audit, dict):
        result["geometry_audit_cases"] = geometry_audit.get("case_count")
        result["geometry_audit_duplicate_inputs"] = geometry_audit.get("duplicate_input_group_count")
        result["geometry_audit_tolerance_mismatches"] = geometry_audit.get("tolerance_mismatch_count")
    if isinstance(summary, dict) and isinstance(triage, dict):
        semantic = build_reduction_replay_semantic_check(index, summary, triage, recipes, replay_root)
        result["semantic_check_summary"] = semantic.get("summary_path")
        result["semantic_check_report"] = semantic.get("report_path")
        result["semantic_ok"] = semantic.get("ok")
        result["semantic_item_count"] = semantic.get("item_count")
        result["semantic_stable_same_failure_count"] = semantic.get("stable_same_failure_count")
        result["semantic_changed_failure_count"] = semantic.get("changed_failure_count")
        result["semantic_not_reproduced_count"] = semantic.get("not_reproduced_count")
        result["semantic_unavailable_count"] = semantic.get("unavailable_count")
        result["semantic_limited_replay"] = semantic.get("limited_replay")
    return result


def run_collect_bug_registry(script_dir: Path, out_dir: Path, campaigns: list[dict[str, Any]]) -> dict[str, Any] | None:
    triages = [as_str(c.get("aggregate_triage", {}).get("summary_path")) for c in campaigns if isinstance(c.get("aggregate_triage"), dict)]
    replays = [as_str(c.get("replay", {}).get("summary_path")) for c in campaigns if isinstance(c.get("replay"), dict)]
    bundles = [as_str(c.get("bundles", {}).get("index_path")) for c in campaigns if isinstance(c.get("bundles"), dict)]
    triages = [item for item in triages if item]
    replays = [item for item in replays if item]
    bundles = [item for item in bundles if item]
    if not triages and not bundles:
        return {"skipped": True, "reason": "no campaign triage or bundle index paths"}
    registry_out = out_dir / "bug_registry"
    cmd = [sys.executable, str(script_dir / "collect_bug_registry.py"), "--out", str(registry_out)]
    for item in triages:
        cmd.extend(["--triage", item])
    for item in replays:
        cmd.extend(["--replay", item])
    for item in bundles:
        cmd.extend(["--bundle-index", item])
    record = command_record("collect_bug_registry", cmd)
    registry = load_json(registry_out / "bug_registry.json")
    result = {
        "out": str(registry_out),
        "summary_path": str(registry_out / "bug_registry.json"),
        "report_path": str(registry_out / "bug_registry.md"),
        "replay_recipes": str(registry_out / "registry_replay_recipes.txt"),
        "returncode": record["returncode"],
        "ok": record["ok"],
        "command": record,
    }
    if isinstance(registry, dict):
        result["total"] = registry.get("total")
        result["by_replay_status"] = registry.get("by_replay_status")
        result["by_api"] = registry.get("by_api")
    return result


def run_export_bug_record_drafts(
    script_dir: Path,
    out_dir: Path,
    campaigns: list[dict[str, Any]],
    bug_prefix: str,
    debug_handoff: dict[str, Any] | None,
) -> dict[str, Any] | None:
    triages = [as_str(c.get("aggregate_triage", {}).get("summary_path")) for c in campaigns if isinstance(c.get("aggregate_triage"), dict)]
    replays = [as_str(c.get("replay", {}).get("summary_path")) for c in campaigns if isinstance(c.get("replay"), dict)]
    bundles = [as_str(c.get("bundles", {}).get("index_path")) for c in campaigns if isinstance(c.get("bundles"), dict)]
    triages = [item for item in triages if item]
    replays = [item for item in replays if item]
    bundles = [item for item in bundles if item]
    if not triages and not bundles:
        return {"skipped": True, "reason": "no campaign triage or bundle index paths"}
    draft_path = out_dir / "bug_record_drafts" / "drafts.json"
    cmd = [
        sys.executable,
        str(script_dir / "export_bug_record_drafts.py"),
        "--out",
        str(draft_path),
        "--bug-prefix",
        bug_prefix,
    ]
    for item in triages:
        cmd.extend(["--triage", item])
    for item in replays:
        cmd.extend(["--replay", item])
    for item in bundles:
        cmd.extend(["--bundle-index", item])
    if isinstance(debug_handoff, dict) and not debug_handoff.get("skipped") and as_str(debug_handoff.get("index_path")):
        cmd.extend(["--debug-handoff", as_str(debug_handoff["index_path"])])
    record = command_record("export_bug_record_drafts", cmd)
    drafts = load_json(draft_path)
    records = drafts.get("records") if isinstance(drafts, dict) else []
    return {
        "out": str(draft_path.parent),
        "draft_path": str(draft_path),
        "record_count": len(records) if isinstance(records, list) else 0,
        "returncode": record["returncode"],
        "ok": record["ok"],
        "command": record,
    }


def run_debug_handoff(
    script_dir: Path,
    out_dir: Path,
    campaigns: list[dict[str, Any]],
    bug_registry: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(bug_registry, dict) or not as_str(bug_registry.get("summary_path")):
        return {"skipped": True, "reason": "no merged bug registry"}
    handoff_out = out_dir / "debug_handoff"
    cmd = [
        sys.executable,
        str(script_dir / "build_debug_handoff.py"),
        "--registry",
        as_str(bug_registry["summary_path"]),
        "--out",
        str(handoff_out),
    ]
    preview_dirs: list[str] = []
    for campaign in campaigns:
        for lane in campaign.get("lanes", []):
            if isinstance(lane, dict) and as_str(lane.get("preview_out")):
                preview_dirs.append(as_str(lane["preview_out"]))
    for preview_dir in sorted(set(preview_dirs)):
        cmd.extend(["--preview-dir", preview_dir])
    record = command_record("build_debug_handoff", cmd)
    index = load_json(handoff_out / "debug_handoff_index.json")
    result = {
        "out": str(handoff_out),
        "index_path": str(handoff_out / "debug_handoff_index.json"),
        "report_path": str(handoff_out / "debug_handoff_report.md"),
        "returncode": record["returncode"],
        "ok": record["ok"],
        "command": record,
    }
    if isinstance(index, dict):
        result["pack_count"] = index.get("pack_count")
        result["debug_sgt_count"] = index.get("debug_sgt_count")
        result["focus_sgt_count"] = index.get("focus_sgt_count")
        result["input_sgt_count"] = index.get("input_sgt_count")
        result["by_api"] = index.get("by_api")
        result["topology_extractor"] = index.get("topology_extractor")
    return result


def run_record_bug_cases(
    script_dir: Path,
    out_dir: Path,
    drafts: dict[str, Any] | None,
    validate_recipes: bool,
    out_name: str = "bug_records_materialized",
    command_name: str = "record_bug_cases",
) -> dict[str, Any] | None:
    if not isinstance(drafts, dict) or not as_str(drafts.get("draft_path")):
        return None
    materialized_out = out_dir / out_name
    cmd = [
        sys.executable,
        str(script_dir / "record_bug_cases.py"),
        "--records",
        as_str(drafts["draft_path"]),
        "--out",
        str(materialized_out),
    ]
    if validate_recipes:
        cmd.append("--validate-recipes")
    record = command_record(command_name, cmd)
    registry = load_json(materialized_out / "bug_registry.json")
    result = {
        "out": str(materialized_out),
        "summary_path": str(materialized_out / "bug_registry.json"),
        "report_path": str(materialized_out / "bug_registry.md"),
        "replay_recipes": str(materialized_out / "registry_replay_recipes.txt"),
        "returncode": record["returncode"],
        "ok": record["ok"],
        "command": record,
    }
    if isinstance(registry, dict):
        result["total"] = registry.get("total")
        result["by_replay_status"] = registry.get("by_replay_status")
    return result


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


def run_promote_bug_records(
    script_dir: Path,
    out_dir: Path,
    drafts: dict[str, Any] | None,
    bug_prefix: str,
) -> dict[str, Any] | None:
    if not isinstance(drafts, dict) or not as_str(drafts.get("draft_path")):
        return {"skipped": True, "reason": "no merged bug-record drafts"}
    promotion_root = out_dir / "promoted_bug_records"
    registry_id = sanitize_name(f"{bug_prefix}_promoted")
    promoted_path = promotion_root / "test_harness" / "bug_records" / f"{registry_id}.json"
    fixture_root = promotion_root / "test_harness" / "fixtures" / "bug_records"
    cmd = [
        sys.executable,
        str(script_dir / "promote_bug_records.py"),
        "--records",
        as_str(drafts["draft_path"]),
        "--repo-root",
        str(promotion_root),
        "--fixture-root",
        "test_harness/fixtures/bug_records",
        "--out",
        str(promoted_path),
        "--registry-id",
        registry_id,
        "--description",
        "Portable promoted records from merged campaign discoveries.",
        "--overwrite",
    ]
    promote_record = command_record("promote_bug_records", cmd)
    promoted = load_json(promoted_path)

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
    audit_record = command_record("audit_promoted_bug_records", audit_cmd) if promoted_path.is_file() else None
    audit = load_json(audit_out / "bug_record_portability.json") if audit_record else None

    records = promoted.get("records") if isinstance(promoted, dict) else []
    result: dict[str, Any] = {
        "out": str(promotion_root),
        "record_path": str(promoted_path),
        "report_path": str(promoted_path.with_suffix(".md")),
        "fixture_root": str(fixture_root),
        "portability_summary_path": str(audit_out / "bug_record_portability.json"),
        "portability_report_path": str(audit_out / "bug_record_portability.md"),
        "registry_id": registry_id,
        "returncode": promote_record["returncode"],
        "ok": promote_record["ok"] and bool(audit_record and audit_record.get("ok")) and bool(isinstance(audit, dict) and audit.get("ok")),
        "command": promote_record,
        "commands": [audit_record] if isinstance(audit_record, dict) else [],
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


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def run_promoted_bug_record_replay(
    script_dir: Path,
    promoted: dict[str, Any] | None,
    runner: str,
    validate_recipes: bool,
    timeout: float,
    jobs: int,
) -> dict[str, Any]:
    if not isinstance(promoted, dict) or promoted.get("skipped"):
        return {"skipped": True, "reason": "no promoted bug records"}
    if not promoted.get("ok"):
        return {"skipped": True, "reason": "promotion did not complete successfully"}
    promotion_root = Path(as_str(promoted.get("out"))).resolve()
    record_path = Path(as_str(promoted.get("record_path"))).resolve()
    if not promotion_root.is_dir() or not record_path.is_file():
        return {"skipped": True, "reason": "promoted root or record file missing"}
    materialized = promotion_root / "materialized"
    replay_out = promotion_root / "replay"
    regression_out = promotion_root / "regression"
    record_arg = relative_to_root(record_path, promotion_root)
    materialize_cmd = [
        sys.executable,
        str(script_dir / "record_bug_cases.py"),
        "--records",
        record_arg,
        "--out",
        "materialized",
    ]
    if validate_recipes:
        materialize_cmd.append("--validate-recipes")
    materialize_record = command_record("materialize_promoted_bug_records", materialize_cmd, cwd=promotion_root)
    registry = load_json(materialized / "bug_registry.json")

    replay_cmd = [
        sys.executable,
        str(script_dir / "run_recipes.py"),
        "--runner",
        runner,
        "--recipe-list",
        "materialized/registry_replay_recipes.txt",
        "--out",
        "replay",
        "--triage-out",
        "replay_triage",
        "--timeout",
        str(timeout),
        "--jobs",
        str(jobs),
        "--triage-include-passed",
    ]
    replay_record = command_record("replay_promoted_bug_records", replay_cmd, acceptable={0, 1, 2}, cwd=promotion_root) if materialize_record["ok"] else None
    replay_summary = load_json(replay_out / "recipe_summary.json") if replay_record else None

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
    regression_record = (
        command_record("classify_promoted_bug_records", regression_cmd, cwd=promotion_root)
        if replay_record and replay_record.get("ok") and (replay_out / "recipe_summary.json").is_file()
        else None
    )
    regression = load_json(regression_out / "registry_regression.json") if regression_record else None

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
        "returncode": materialize_record["returncode"],
        "ok": bool(
            materialize_record.get("ok")
            and replay_record
            and replay_record.get("ok")
            and regression_record
            and regression_record.get("ok")
        ),
        "command": materialize_record,
        "commands": [item for item in (replay_record, regression_record) if isinstance(item, dict)],
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


def run_registry_regression(
    script_dir: Path,
    out_dir: Path,
    registry_path: str,
    recipe_summary: str,
    out_name: str,
    command_name: str,
) -> dict[str, Any]:
    regression_out = out_dir / out_name
    cmd = [
        sys.executable,
        str(script_dir / "check_bug_registry_regression.py"),
        "--registry",
        registry_path,
        "--recipe-summary",
        recipe_summary,
        "--out",
        str(regression_out),
    ]
    record = command_record(command_name, cmd)
    regression = load_json(regression_out / "registry_regression.json")
    result = {
        "out": str(regression_out),
        "summary_path": str(regression_out / "registry_regression.json"),
        "report_path": str(regression_out / "registry_regression.md"),
        "returncode": record["returncode"],
        "ok": record["ok"],
        "command": record,
    }
    if isinstance(regression, dict):
        result["total"] = regression.get("total")
        result["status_counts"] = regression.get("status_counts")
    return result


def run_reduction_bug_record_materialization(
    script_dir: Path,
    out_dir: Path,
    reduction_drafts: dict[str, Any] | None,
    reduction_replay: dict[str, Any] | None,
    validate_recipes: bool,
) -> dict[str, Any] | None:
    if not isinstance(reduction_drafts, dict) or reduction_drafts.get("skipped") or not as_str(reduction_drafts.get("draft_path")):
        return {"skipped": True, "reason": "no reduced bug-record drafts"}
    materialized = run_record_bug_cases(
        script_dir,
        out_dir,
        reduction_drafts,
        validate_recipes,
        out_name="reduction_bug_records_materialized",
        command_name="record_reduction_bug_cases",
    )
    result: dict[str, Any] = {
        "materialized": materialized,
        "commands": [],
    }
    if isinstance(materialized, dict):
        result.update(
            {
                "out": materialized.get("out"),
                "summary_path": materialized.get("summary_path"),
                "report_path": materialized.get("report_path"),
                "replay_recipes": materialized.get("replay_recipes"),
                "total": materialized.get("total"),
                "by_replay_status": materialized.get("by_replay_status"),
                "materialize_ok": materialized.get("ok"),
            }
        )
        if isinstance(materialized.get("command"), dict):
            result["commands"].append(materialized["command"])
    recipe_summary = as_str(reduction_replay.get("recipe_summary")) if isinstance(reduction_replay, dict) else ""
    registry_path = as_str(materialized.get("summary_path")) if isinstance(materialized, dict) else ""
    if not registry_path or not recipe_summary:
        result["regression"] = {"skipped": True, "reason": "missing registry or reduced replay summary"}
        result["ok"] = bool(result.get("materialize_ok"))
        return result
    regression = run_registry_regression(
        script_dir,
        out_dir,
        registry_path,
        recipe_summary,
        out_name="reduction_bug_regression",
        command_name="check_reduction_bug_regression",
    )
    result["regression"] = regression
    result["regression_summary_path"] = regression.get("summary_path")
    result["regression_report_path"] = regression.get("report_path")
    result["regression_status_counts"] = regression.get("status_counts")
    result["regression_total"] = regression.get("total")
    result["regression_ok"] = regression.get("ok")
    if isinstance(regression.get("command"), dict):
        result["commands"].append(regression["command"])
    result["ok"] = bool(result.get("materialize_ok")) and bool(regression.get("ok"))
    return result


def run_reduction_bug_record_drafts(
    script_dir: Path,
    out_dir: Path,
    reduction_replay: dict[str, Any] | None,
    bug_prefix: str,
) -> dict[str, Any] | None:
    if not isinstance(reduction_replay, dict) or reduction_replay.get("skipped"):
        return {"skipped": True, "reason": "no reduction replay"}
    triage_summary = as_str(reduction_replay.get("triage_summary"))
    if not triage_summary:
        return {"skipped": True, "reason": "no reduction replay triage"}
    draft_path = out_dir / "reduction_bug_record_drafts" / "drafts.json"
    cmd = [
        sys.executable,
        str(script_dir / "export_bug_record_drafts.py"),
        "--triage",
        triage_summary,
        "--out",
        str(draft_path),
        "--bug-prefix",
        f"{bug_prefix}_reduced",
    ]
    record = command_record("export_reduction_bug_record_drafts", cmd)
    drafts = load_json(draft_path)
    records = drafts.get("records") if isinstance(drafts, dict) else []
    if isinstance(drafts, dict) and isinstance(records, list):
        evidence = {
            "recipe_list": reduction_replay.get("recipe_list"),
            "recipe_summary": reduction_replay.get("recipe_summary"),
            "triage_summary": reduction_replay.get("triage_summary"),
            "triage_report": reduction_replay.get("triage_report"),
            "contact_sheet": reduction_replay.get("contact_sheet"),
            "geometry_audit_summary": reduction_replay.get("geometry_audit_summary"),
            "semantic_check_summary": reduction_replay.get("semantic_check_summary"),
            "semantic_check_report": reduction_replay.get("semantic_check_report"),
            "semantic_ok": reduction_replay.get("semantic_ok"),
            "semantic_stable_same_failure_count": reduction_replay.get("semantic_stable_same_failure_count"),
            "semantic_changed_failure_count": reduction_replay.get("semantic_changed_failure_count"),
            "semantic_not_reproduced_count": reduction_replay.get("semantic_not_reproduced_count"),
            "semantic_unavailable_count": reduction_replay.get("semantic_unavailable_count"),
            "recipe_count": reduction_replay.get("recipe_count"),
            "failed": reduction_replay.get("failed"),
            "failure_group_count": reduction_replay.get("failure_group_count"),
        }
        for record_item in records:
            if not isinstance(record_item, dict):
                continue
            replay = record_item.get("replay")
            if not isinstance(replay, dict):
                replay = {}
            replay["is_reduced_recipe"] = True
            replay["source"] = "canonical_reduction_replay"
            record_item["replay"] = replay
            record_item["replay_status"] = "stable_failure"
            record_item["reduction_replay_evidence"] = evidence
            notes = as_list(record_item.get("observations"))
            notes.append("Generated from collection-level canonical reduced-recipe replay; review before checking in.")
            record_item["observations"] = notes
        write_json(draft_path, drafts)
    return {
        "out": str(draft_path.parent),
        "draft_path": str(draft_path),
        "record_count": len(records) if isinstance(records, list) else 0,
        "returncode": record["returncode"],
        "ok": record["ok"],
        "command": record,
    }


def run_oracle_coverage(
    script_dir: Path,
    out_dir: Path,
    campaign_paths: list[Path],
    min_oracle_kinds: int,
) -> dict[str, Any] | None:
    coverage_out = out_dir / "oracle_coverage"
    cmd = [
        sys.executable,
        str(script_dir / "summarize_oracle_coverage.py"),
        "--out",
        str(coverage_out),
        "--fail-on-passed-missing-validation",
        "--min-oracle-kinds-per-passed-case",
        str(min_oracle_kinds),
    ]
    for campaign_path in campaign_paths:
        cmd.extend(["--campaign", str(campaign_path)])
    record = command_record("oracle_coverage", cmd)
    coverage = load_json(coverage_out / "oracle_coverage.json")
    result: dict[str, Any] = {
        "out": str(coverage_out),
        "summary_path": str(coverage_out / "oracle_coverage.json"),
        "report_path": str(coverage_out / "oracle_coverage.md"),
        "returncode": record["returncode"],
        "ok": record["ok"],
        "command": record,
    }
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


def markdown_report(summary: dict[str, Any]) -> str:
    def cell(value: Any) -> Any:
        return "" if value is None else value

    lines: list[str] = []
    lines.append("# SGGK Campaign Shard Collection")
    lines.append("")
    lines.append(f"- Generated: `{summary.get('generated_at')}`")
    lines.append(f"- Campaigns: `{summary.get('campaign_count')}`")
    lines.append(f"- Output: `{summary.get('out')}`")
    lines.append("")
    lines.append("## Campaigns")
    lines.append("")
    lines.append("| campaign | shard | source findings | source tasks | cases | failures | groups | replay stable | reductions | bundles | drafts | known bugs |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for campaign in summary.get("campaigns", []):
        if not isinstance(campaign, dict):
            continue
        triage = campaign.get("aggregate_triage") if isinstance(campaign.get("aggregate_triage"), dict) else {}
        source = campaign.get("source_scan") if isinstance(campaign.get("source_scan"), dict) else {}
        replay = campaign.get("replay") if isinstance(campaign.get("replay"), dict) else {}
        reductions = campaign.get("reductions") if isinstance(campaign.get("reductions"), dict) else {}
        bundles = campaign.get("bundles") if isinstance(campaign.get("bundles"), dict) else {}
        drafts = campaign.get("bug_record_drafts") if isinstance(campaign.get("bug_record_drafts"), dict) else {}
        known = campaign.get("known_bug_regression") if isinstance(campaign.get("known_bug_regression"), dict) else {}
        shard = ""
        if campaign.get("shard_count") is not None and campaign.get("shard_index") is not None:
            shard = f"{campaign.get('shard_index')}/{campaign.get('shard_count')}"
        lines.append(
            "| `{root}` | `{shard}` | `{source_findings}` | `{source_tasks}` | `{cases}` | `{failures}` | `{groups}` | `{stable}` | `{reductions}` | `{bundles}` | `{drafts}` | `{known}` |".format(
                root=campaign.get("root"),
                shard=shard,
                source_findings=cell(source.get("findings", "")),
                source_tasks=cell(source.get("attack_task_count", "")),
                cases=cell(triage.get("total_cases", "")),
                failures=cell(triage.get("failed_cases", "")),
                groups=cell(triage.get("failure_group_count", "")),
                stable=cell(replay.get("stable_same_failure", replay.get("stable_failure", ""))),
                reductions=cell(reductions.get("completed_count", "")),
                bundles=cell(bundles.get("bundle_count", "")),
                drafts=cell(drafts.get("record_count", "")),
                known=cell(known.get("status_counts", "")),
            )
        )
    lines.append("")
    source_totals = summary.get("totals", {}).get("source_scan_raw_sum") if isinstance(summary.get("totals"), dict) else {}
    if isinstance(source_totals, dict) and source_totals:
        lines.append("## Source Scan Totals")
        lines.append("")
        lines.append(f"- Campaigns with source scan: `{source_totals.get('campaign_count', 0)}`")
        lines.append(f"- Files scanned raw sum: `{source_totals.get('files_scanned', 0)}`")
        lines.append(f"- Findings raw sum: `{source_totals.get('findings', 0)}`")
        lines.append(f"- Attack seed drafts raw sum: `{source_totals.get('attack_seed_drafts', 0)}`")
        lines.append(f"- Attack tasks raw sum: `{source_totals.get('attack_task_count', 0)}`")
        severity = summary.get("totals", {}).get("source_scan_severity_raw_sum") if isinstance(summary.get("totals"), dict) else {}
        categories = summary.get("totals", {}).get("source_scan_category_raw_sum") if isinstance(summary.get("totals"), dict) else {}
        lines.append(f"- Severity raw sum: `{severity}`")
        lines.append(f"- Category raw sum: `{categories}`")
    lines.append("")
    lines.append("## Lane Totals")
    lines.append("")
    lines.append("| lane | shards | total | passed | failed | empty shards | DSL check reports | DSL check recipes | DSL check failures |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    lanes = summary.get("totals", {}).get("lanes") if isinstance(summary.get("totals"), dict) else {}
    for name, lane in lanes.items():
        if not isinstance(lane, dict):
            continue
        dsl_failures = as_int(lane.get("dsl_check_compile_failures")) + as_int(lane.get("dsl_check_validation_failures"))
        lines.append(
            f"| `{name}` | `{lane.get('campaign_count')}` | `{lane.get('total')}` | `{lane.get('passed')}` | `{lane.get('failed')}` | `{lane.get('empty_shards')}` | `{lane.get('dsl_check_reports', 0)}` | `{lane.get('dsl_check_recipes', 0)}` | `{dsl_failures}` |"
        )
    lines.append("")
    aggregate = summary.get("totals", {}).get("aggregate_triage_raw_sum") if isinstance(summary.get("totals"), dict) else {}
    if isinstance(aggregate, dict):
        lines.append("## Raw Aggregate Sum")
        lines.append("")
        for key in ("total_cases", "failed_cases", "failure_group_count", "command_failures", "warning_cases"):
            lines.append(f"- {key}: `{aggregate.get(key, 0)}`")
        lines.append("")
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    known_status = totals.get("known_bug_status_raw_sum") if isinstance(totals.get("known_bug_status_raw_sum"), dict) else {}
    if known_status or totals.get("known_bug_campaign_count"):
        lines.append("## Known Bug Regression")
        lines.append("")
        lines.append(f"- Campaigns with known-bug lane: `{totals.get('known_bug_campaign_count', 0)}`")
        lines.append(f"- Raw bug count sum: `{totals.get('known_bug_count_raw_sum', 0)}`")
        lines.append(f"- Raw status sum: `{known_status}`")
        lines.append("")
    dataset_audit = summary.get("dataset_audit")
    if isinstance(dataset_audit, dict):
        lines.append("## Dataset Audit")
        lines.append("")
        if dataset_audit.get("skipped"):
            lines.append(f"- Skipped: {dataset_audit.get('reason')}")
            lines.append(f"- Missing/legacy audit blocks: `{dataset_audit.get('missing_block_count', 0)}`")
        else:
            lines.append(f"- Ok: `{dataset_audit.get('ok')}`")
            lines.append(f"- Audited campaigns: `{dataset_audit.get('audited_campaign_count', 0)}`")
            lines.append(f"- Failed audited campaigns: `{dataset_audit.get('failed_campaign_count', 0)}`")
            lines.append(f"- Missing/legacy audit blocks: `{dataset_audit.get('missing_block_count', 0)}`")
            lines.append(f"- Files raw sum: `{dataset_audit.get('total_files', 0)}`")
            lines.append(f"- Missing files raw sum: `{dataset_audit.get('missing_files', 0)}`")
            lines.append(f"- Empty files raw sum: `{dataset_audit.get('empty_files', 0)}`")
            lines.append(f"- Duplicate content groups raw sum: `{dataset_audit.get('duplicate_content_group_count', 0)}`")
            lines.append(f"- Min hash coverage: `{dataset_audit.get('min_hash_coverage_ratio', '')}`")
            lines.append(f"- Weighted hash coverage: `{dataset_audit.get('weighted_hash_coverage_ratio', '')}`")
            lines.append(f"- Report: `{dataset_audit.get('report_path', '')}`")
        lines.append("")
    reductions = summary.get("reductions")
    if isinstance(reductions, dict):
        lines.append("## Merged Reductions")
        lines.append("")
        if reductions.get("skipped"):
            lines.append(f"- Skipped: {reductions.get('reason')}")
        else:
            lines.append(f"- Ok: `{reductions.get('ok')}`")
            lines.append(f"- Source campaigns: `{reductions.get('source_campaign_count', 0)}`")
            lines.append(f"- Candidate stable failures raw sum: `{reductions.get('candidate_count', 0)}`")
            lines.append(f"- Selected raw sum: `{reductions.get('selected_count', 0)}`")
            lines.append(f"- Completed raw sum: `{reductions.get('completed_count', 0)}`")
            lines.append(f"- Accepted reductions raw sum: `{reductions.get('accepted_reduction_count', 0)}`")
            lines.append(f"- Distinct fingerprints: `{reductions.get('distinct_fingerprint_count', '')}`")
            lines.append(f"- Duplicate fingerprint groups: `{reductions.get('duplicate_fingerprint_group_count', '')}`")
            lines.append(f"- Source errors: `{reductions.get('source_error_count', 0)}`")
            lines.append(f"- Summary: `{reductions.get('summary_path', '')}`")
            lines.append(f"- Report: `{reductions.get('report_path', '')}`")
        lines.append("")
    reduction_replay = summary.get("reduction_replay")
    if isinstance(reduction_replay, dict):
        lines.append("## Reduced Recipe Replay")
        lines.append("")
        if reduction_replay.get("skipped"):
            lines.append(f"- Skipped: {reduction_replay.get('reason')}")
        else:
            lines.append(f"- Ok: `{reduction_replay.get('ok')}`")
            lines.append(f"- Recipes: `{reduction_replay.get('recipe_count', 0)}`")
            lines.append(f"- Failed: `{reduction_replay.get('failed', '')}`")
            lines.append(f"- Timed out: `{reduction_replay.get('timed_out', '')}`")
            lines.append(f"- Failure groups: `{reduction_replay.get('failure_group_count', '')}`")
            lines.append(f"- Semantic ok: `{reduction_replay.get('semantic_ok', '')}`")
            lines.append(f"- Semantic stable same failure: `{reduction_replay.get('semantic_stable_same_failure_count', '')}`")
            lines.append(f"- Semantic changed failure: `{reduction_replay.get('semantic_changed_failure_count', '')}`")
            lines.append(f"- Semantic not reproduced: `{reduction_replay.get('semantic_not_reproduced_count', '')}`")
            lines.append(f"- Semantic unavailable: `{reduction_replay.get('semantic_unavailable_count', '')}`")
            lines.append(f"- Recipe list: `{reduction_replay.get('recipe_list', '')}`")
            lines.append(f"- Recipe summary: `{reduction_replay.get('recipe_summary', '')}`")
            lines.append(f"- Triage report: `{reduction_replay.get('triage_report', '')}`")
            lines.append(f"- Contact sheet: `{reduction_replay.get('contact_sheet', '')}`")
            lines.append(f"- Geometry audit: `{reduction_replay.get('geometry_audit_report', '')}`")
            lines.append(f"- Semantic check: `{reduction_replay.get('semantic_check_report', '')}`")
        lines.append("")
    reduction_drafts = summary.get("reduction_bug_record_drafts")
    if isinstance(reduction_drafts, dict):
        lines.append("## Reduced Bug Record Drafts")
        lines.append("")
        if reduction_drafts.get("skipped"):
            lines.append(f"- Skipped: {reduction_drafts.get('reason')}")
        else:
            lines.append(f"- Records: `{reduction_drafts.get('record_count', 0)}`")
            lines.append(f"- Drafts: `{reduction_drafts.get('draft_path', '')}`")
        lines.append("")
    reduction_materialized = summary.get("reduction_bug_records_materialized")
    if isinstance(reduction_materialized, dict):
        lines.append("## Materialized Reduced Bug Records")
        lines.append("")
        if reduction_materialized.get("skipped"):
            lines.append(f"- Skipped: {reduction_materialized.get('reason')}")
        else:
            lines.append(f"- Ok: `{reduction_materialized.get('ok')}`")
            lines.append(f"- Total: `{reduction_materialized.get('total', 0)}`")
            lines.append(f"- Replay status: `{reduction_materialized.get('by_replay_status', {})}`")
            lines.append(f"- Registry: `{reduction_materialized.get('summary_path', '')}`")
            lines.append(f"- Registry report: `{reduction_materialized.get('report_path', '')}`")
            lines.append(f"- Replay recipes: `{reduction_materialized.get('replay_recipes', '')}`")
            lines.append(f"- Regression status: `{reduction_materialized.get('regression_status_counts', {})}`")
            lines.append(f"- Regression report: `{reduction_materialized.get('regression_report_path', '')}`")
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
    oracle = summary.get("oracle_coverage")
    if isinstance(oracle, dict):
        lines.append("## Merged Oracle Coverage")
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
    registry = summary.get("bug_registry")
    if isinstance(registry, dict):
        lines.append("## Merged Bug Registry")
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
        lines.append("## Merged Debug Handoff")
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
        lines.append("## Merged Bug Record Drafts")
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
    materialized = summary.get("bug_records_materialized")
    if isinstance(materialized, dict):
        lines.append("## Materialized Bug Records")
        lines.append("")
        lines.append(f"- Total: `{materialized.get('total', 0)}`")
        lines.append(f"- Replay status: `{materialized.get('by_replay_status', {})}`")
        lines.append(f"- Report: `{materialized.get('report_path', '')}`")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.oracle_coverage_min_kinds < 0:
        print("--oracle-coverage-min-kinds must be >= 0", file=sys.stderr)
        return 1
    if args.reduction_replay_jobs <= 0:
        print("--reduction-replay-jobs must be >= 1", file=sys.stderr)
        return 1
    if args.reduction_replay_timeout <= 0:
        print("--reduction-replay-timeout must be > 0", file=sys.stderr)
        return 1
    if args.reduction_replay_limit < 0:
        print("--reduction-replay-limit must be >= 0", file=sys.stderr)
        return 1
    if args.export_reduction_bug_record_drafts and not args.replay_reductions:
        print("--export-reduction-bug-record-drafts requires --replay-reductions", file=sys.stderr)
        return 1
    if args.materialize_reduction_bug_records and not args.export_reduction_bug_record_drafts:
        print("--materialize-reduction-bug-records requires --export-reduction-bug-record-drafts", file=sys.stderr)
        return 1
    if args.promote_bug_records and args.skip_bug_record_drafts:
        print("--promote-bug-records requires bug-record drafts; remove --skip-bug-record-drafts", file=sys.stderr)
        return 1
    if args.replay_promoted_bug_records and not args.promote_bug_records:
        print("--replay-promoted-bug-records requires --promote-bug-records", file=sys.stderr)
        return 1
    if args.replay_promoted_bug_records and args.promoted_replay_jobs <= 0:
        print("--promoted-replay-jobs must be >= 1", file=sys.stderr)
        return 1
    if args.replay_promoted_bug_records and args.promoted_replay_timeout <= 0:
        print("--promoted-replay-timeout must be > 0", file=sys.stderr)
        return 1
    if args.replay_reductions or args.replay_promoted_bug_records:
        if not args.runner:
            print("--runner is required with replay options", file=sys.stderr)
            return 1
        runner = Path(args.runner).resolve()
        if not runner.is_file():
            print(f"runner not found: {runner}", file=sys.stderr)
            return 1
        args.runner = str(runner)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    script_dir = Path(__file__).resolve().parent

    campaign_paths = [normalize_campaign_summary(raw) for raw in args.campaign]
    campaigns = [campaign_info(path) for path in campaign_paths]
    totals = summarize_campaigns(campaigns)
    dataset_audit = collect_dataset_audits(out_dir, campaigns)

    bug_registry = None if args.skip_bug_registry else run_collect_bug_registry(script_dir, out_dir, campaigns)
    debug_handoff = None if args.skip_debug_handoff else run_debug_handoff(script_dir, out_dir, campaigns, bug_registry)
    drafts = None if args.skip_bug_record_drafts else run_export_bug_record_drafts(script_dir, out_dir, campaigns, args.bug_prefix, debug_handoff)
    promoted = run_promote_bug_records(script_dir, out_dir, drafts, args.bug_prefix) if args.promote_bug_records else {"skipped": True, "reason": "skip_promote_bug_records"}
    promoted_replay = (
        run_promoted_bug_record_replay(script_dir, promoted, args.runner, args.validate_recipes, args.promoted_replay_timeout, args.promoted_replay_jobs)
        if args.replay_promoted_bug_records
        else {"skipped": True, "reason": "skip_replay_promoted_bug_records"}
    )
    reductions = merge_reductions(out_dir, campaigns)
    reduction_replay = replay_reductions(args, script_dir, out_dir, reductions)
    reduction_drafts = (
        run_reduction_bug_record_drafts(script_dir, out_dir, reduction_replay, args.bug_prefix)
        if args.export_reduction_bug_record_drafts
        else {"skipped": True, "reason": "skip_export_reduction_bug_record_drafts"}
    )
    materialized = None
    if args.materialize_bug_records:
        materialized = run_record_bug_cases(script_dir, out_dir, drafts, args.validate_recipes)
    reduction_materialized = (
        run_reduction_bug_record_materialization(script_dir, out_dir, reduction_drafts, reduction_replay, args.validate_recipes)
        if args.materialize_reduction_bug_records
        else {"skipped": True, "reason": "skip_materialize_reduction_bug_records"}
    )
    oracle_coverage = (
        {"skipped": True, "reason": "skip_oracle_coverage"}
        if args.skip_oracle_coverage
        else run_oracle_coverage(script_dir, out_dir, campaign_paths, args.oracle_coverage_min_kinds)
    )

    command_records = []
    for section in (bug_registry, debug_handoff, drafts, promoted, promoted_replay, reduction_replay, reduction_drafts, materialized, reduction_materialized, oracle_coverage):
        if isinstance(section, dict) and isinstance(section.get("command"), dict):
            command_records.append(section["command"])
        if isinstance(section, dict):
            for nested_command in as_list(section.get("commands")):
                if isinstance(nested_command, dict):
                    command_records.append(nested_command)

    summary = {
        "generated_at": now_iso_like(),
        "out": str(out_dir),
        "campaign_count": len(campaigns),
        "campaigns": campaigns,
        "totals": totals,
        "dataset_audit": dataset_audit,
        "bug_registry": bug_registry,
        "debug_handoff": debug_handoff,
        "bug_record_drafts": drafts,
        "bug_records_promoted": promoted,
        "bug_records_promoted_replay": promoted_replay,
        "reductions": reductions,
        "reduction_replay": reduction_replay,
        "reduction_bug_record_drafts": reduction_drafts,
        "bug_records_materialized": materialized,
        "reduction_bug_records_materialized": reduction_materialized,
        "oracle_coverage": oracle_coverage,
        "commands": command_records,
    }
    summary_path = out_dir / "campaign_shards_summary.json"
    report_path = out_dir / "campaign_shards_report.md"
    write_json(summary_path, summary)
    report_path.write_text(markdown_report(summary), encoding="utf-8")
    print(f"summary={summary_path}")
    print(f"report={report_path}")

    if any(isinstance(campaign, dict) and not campaign.get("ok") for campaign in campaigns):
        return 1
    if isinstance(reductions, dict) and not reductions.get("skipped") and reductions.get("ok") is False:
        return 1
    if any(isinstance(record, dict) and not record.get("ok") for record in command_records):
        return 1
    if isinstance(dataset_audit, dict) and not dataset_audit.get("skipped") and dataset_audit.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
