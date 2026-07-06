#!/usr/bin/env python3
"""Preflight-check SGGK campaign inputs before a large run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


try:
    from run_campaign import DEFAULT_DSLS
except Exception:  # noqa: BLE001
    DEFAULT_DSLS = [
        "test_harness/dsl/tolerance_band_smoke.json",
        "test_harness/dsl/real_chain_tolerance_smoke.json",
        "test_harness/dsl/complex_surface_sweep_boolean_smoke.json",
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", default="", help="Path to sggk_case_runner.exe")
    parser.add_argument("--topology-extractor", default="", help="Optional sggk_topology_extract executable")
    parser.add_argument("--out", default="artifacts/campaign_preflight", help="Preflight output directory")
    parser.add_argument("--dataset-root", action="append", default=[], help="Corpus file or directory to discover")
    parser.add_argument("--dataset-list", action="append", default=[], help="Existing dataset_index.json or path list")
    parser.add_argument("--source-root", action="append", default=[], help="Source root intended for source-risk scanning")
    parser.add_argument("--dsl", action="append", default=[], help="DSL file or directory to check; defaults to campaign defaults")
    parser.add_argument("--bug-record", action="append", default=[], help="Bug-record JSON file or directory; defaults to test_harness/bug_records when present")
    parser.add_argument("--discover-limit", type=int, default=20, help="Maximum files for preflight discovery; 0 means all")
    parser.add_argument("--hash-inputs", action="store_true", help="Hash discovered dataset files during preflight")
    parser.add_argument("--discover-include-artifacts", action="store_true")
    parser.add_argument("--discover-include-build", action="store_true")
    parser.add_argument("--discover-exclude-dir", action="append", default=[])
    parser.add_argument("--skip-dsl-check", action="store_true")
    parser.add_argument("--skip-bug-record-check", action="store_true")
    parser.add_argument("--skip-dataset-discovery", action="store_true")
    parser.add_argument("--skip-dataset-audit", action="store_true")
    parser.add_argument("--dataset-audit-require-hashes", action="store_true")
    parser.add_argument("--dataset-audit-fail-duplicate-ratio", type=float, default=-1.0)
    parser.add_argument("--profile-cad-features", action="store_true", help="Profile STEP/IGES feature complexity and write complex subset index")
    parser.add_argument("--cad-feature-min-score", type=int, default=8, help="Minimum score for complex CAD feature subset")
    parser.add_argument("--require-cad-feature-profile", action="store_true", help="Fail preflight when CAD feature profile fails or finds no complex files")
    parser.add_argument("--warn-only", action="store_true", help="Return zero even when required checks fail")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def resolve_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


class Preflight:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.checks: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []

    def add(self, severity: str, kind: str, message: str, path: Any = "") -> None:
        self.checks.append(
            {
                "severity": severity,
                "kind": kind,
                "message": message,
                "path": str(path) if path else "",
            }
        )

    def ok(self, kind: str, message: str, path: Any = "") -> None:
        self.add("ok", kind, message, path)

    def warning(self, kind: str, message: str, path: Any = "") -> None:
        self.add("warning", kind, message, path)

    def error(self, kind: str, message: str, path: Any = "") -> None:
        self.add("error", kind, message, path)

    def require_file(self, raw: str, label: str) -> Path | None:
        if not raw:
            self.error("missing_path", f"{label} path is not set")
            return None
        path = resolve_path(raw)
        if not path.is_file():
            self.error("missing_file", f"{label} does not exist", path)
            return None
        if path.stat().st_size <= 0:
            self.error("empty_file", f"{label} is empty", path)
            return None
        self.ok("file", f"{label} exists", path)
        return path

    def require_any_path(self, raw: str, label: str) -> Path | None:
        if not raw:
            self.error("missing_path", f"{label} path is not set")
            return None
        path = resolve_path(raw)
        if not path.exists():
            self.error("missing_path", f"{label} does not exist", path)
            return None
        self.ok("path", f"{label} exists", path)
        return path

    def optional_any_path(self, raw: str, label: str) -> Path | None:
        if not raw:
            self.warning("missing_optional_path", f"{label} path is not set")
            return None
        path = resolve_path(raw)
        if not path.exists():
            self.warning("missing_optional_path", f"{label} does not exist", path)
            return None
        self.ok("path", f"{label} exists", path)
        return path

    def run_command(self, name: str, cmd: list[str], report_path: Path | None = None) -> tuple[int, Any]:
        completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        record = {
            "name": name,
            "command": cmd,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "report_path": str(report_path) if report_path else "",
        }
        self.commands.append(record)
        data = None
        if report_path and report_path.is_file():
            try:
                data = read_json(report_path)
            except Exception as exc:  # noqa: BLE001
                self.error("invalid_json", f"{name} report is not readable JSON: {exc}", report_path)
        return completed.returncode, data


def detect_topology_extractor(explicit: str) -> str:
    candidates: list[Path] = []
    if explicit:
        candidates.append(resolve_path(explicit))
    for config in ("Release", "RelWithDebInfo", "Debug"):
        candidates.append(Path.cwd() / "build" / "test_harness" / config / "sggk_topology_extract.exe")
        candidates.append(Path.cwd() / "build" / "test_harness" / config / "sggk_topology_extract")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def default_bug_records() -> list[str]:
    path = Path("test_harness/bug_records")
    return [str(path)] if path.is_dir() and list(path.glob("*.json")) else []


def check_runner(preflight: Preflight, runner: str) -> str:
    path = preflight.require_file(runner, "runner")
    return str(path) if path else ""


def check_topology_extractor(preflight: Preflight, explicit: str) -> str:
    extractor = detect_topology_extractor(explicit)
    if extractor:
        preflight.ok("topology_extractor", "topology extractor available", extractor)
    else:
        preflight.warning("topology_extractor_missing", "topology extractor not found; debug handoff focus SGT export will be diagnostic-only")
    return extractor


def check_paths(preflight: Preflight, values: list[str], label: str, required: bool) -> list[str]:
    resolved: list[str] = []
    for index, value in enumerate(values):
        path = preflight.require_any_path(value, f"{label} {index}") if required else preflight.optional_any_path(value, f"{label} {index}")
        if path:
            resolved.append(str(path))
    if not values and required:
        preflight.error("missing_path", f"no {label} paths were provided")
    elif not values:
        preflight.warning("missing_optional_path", f"no {label} paths were provided")
    return resolved


def check_dsl(preflight: Preflight, dsl_paths: list[str]) -> dict[str, Any]:
    report_path = preflight.out_dir / "dsl_check.json"
    cmd = [sys.executable, str(Path("test_harness/tools/compile_attack_dsl.py")), "--check", "--report", str(report_path), *dsl_paths]
    returncode, report = preflight.run_command("dsl_check", cmd, report_path)
    if returncode != 0:
        preflight.error("dsl_check_failed", f"DSL check command returned {returncode}", report_path)
    elif isinstance(report, dict) and report.get("ok") is True:
        preflight.ok("dsl_check", f"DSL check ok files={report.get('file_count')} recipes={report.get('recipe_count')}", report_path)
    else:
        preflight.error("dsl_check_failed", "DSL check report is missing or not ok", report_path)
    if isinstance(report, dict):
        compile_failures = as_int(report.get("compile_failure_count"))
        validation_failures = as_int(report.get("validation_failure_count"))
        if compile_failures or validation_failures:
            preflight.error("dsl_check_failures", f"DSL check failures compile={compile_failures} validation={validation_failures}", report_path)
        else:
            preflight.ok("dsl_check_failures", "DSL check failures compile=0 validation=0", report_path)
        return report
    return {}


def check_bug_records(preflight: Preflight, record_paths: list[str]) -> dict[str, Any]:
    audit_out = preflight.out_dir / "bug_record_portability"
    audit_cmd = [
        sys.executable,
        str(Path("test_harness/tools/audit_bug_record_portability.py")),
        "--out",
        str(audit_out),
    ]
    for path in record_paths:
        audit_cmd.extend(["--records", path])
    audit_returncode, audit = preflight.run_command("bug_record_portability", audit_cmd, audit_out / "bug_record_portability.json")
    if audit_returncode != 0:
        preflight.error("bug_record_portability_failed", f"bug-record portability audit returned {audit_returncode}", audit_out / "bug_record_portability.json")
    elif isinstance(audit, dict) and audit.get("ok") is True:
        preflight.ok(
            "bug_record_portability",
            f"bug-record portability ok files={audit.get('file_count')} warnings={audit.get('warning_count')}",
            audit_out / "bug_record_portability.json",
        )
    else:
        preflight.error("bug_record_portability_failed", "bug-record portability report is missing or not ok", audit_out / "bug_record_portability.json")

    out_dir = preflight.out_dir / "bug_records_materialized"
    cmd = [sys.executable, str(Path("test_harness/tools/record_bug_cases.py"))]
    for path in record_paths:
        cmd.extend(["--records", path])
    cmd.extend(["--out", str(out_dir), "--validate-recipes"])
    returncode, _ = preflight.run_command("bug_records", cmd, out_dir / "bug_registry.json")
    registry_path = out_dir / "bug_registry.json"
    registry = read_json(registry_path) if registry_path.is_file() else {}
    bug_count = len(as_list(registry.get("bugs"))) if isinstance(registry, dict) else 0
    if returncode != 0:
        preflight.error("bug_records_failed", f"bug-record materialization returned {returncode}", registry_path)
    else:
        preflight.ok("bug_records", f"bug records materialized bugs={bug_count}", registry_path)
    if bug_count <= 0:
        preflight.warning("bug_records_empty", "bug-record materialization produced zero bugs", registry_path)
    return registry if isinstance(registry, dict) else {}


def check_dataset_discovery(preflight: Preflight, args: argparse.Namespace, dataset_roots: list[str]) -> dict[str, Any]:
    out_path = preflight.out_dir / "discovery" / "dataset_index.json"
    report_path = preflight.out_dir / "discovery" / "dataset_index.md"
    paths_path = preflight.out_dir / "discovery" / "dataset_paths.txt"
    cmd = [
        sys.executable,
        str(Path("test_harness/tools/discover_corpus.py")),
        *dataset_roots,
        "--out",
        str(out_path),
        "--report",
        str(report_path),
        "--paths-out",
        str(paths_path),
        "--limit",
        str(args.discover_limit),
    ]
    if args.hash_inputs:
        cmd.append("--hash-inputs")
    if args.discover_include_artifacts:
        cmd.append("--include-artifacts")
    if args.discover_include_build:
        cmd.append("--include-build")
    for name in args.discover_exclude_dir:
        cmd.extend(["--exclude-dir", name])
    returncode, index = preflight.run_command("dataset_discovery", cmd, out_path)
    total_files = as_int(index.get("total_files")) if isinstance(index, dict) else 0
    if returncode != 0:
        preflight.error("dataset_discovery_failed", f"dataset discovery returned {returncode}", out_path)
    elif total_files > 0:
        preflight.ok("dataset_discovery", f"dataset discovery found files={total_files}", out_path)
    else:
        preflight.warning("dataset_discovery_empty", "dataset discovery found zero files", out_path)
    return index if isinstance(index, dict) else {}


def check_dataset_audit(preflight: Preflight, args: argparse.Namespace, dataset_lists: list[str]) -> dict[str, Any]:
    out_dir = preflight.out_dir / "dataset_audit"
    cmd = [
        sys.executable,
        str(Path("test_harness/tools/audit_corpus_dataset.py")),
        "--out",
        str(out_dir),
        "--fail-duplicate-ratio",
        str(args.dataset_audit_fail_duplicate_ratio),
    ]
    if args.dataset_audit_require_hashes:
        cmd.append("--require-hashes")
    for path in dataset_lists:
        cmd.extend(["--dataset-list", path])
    returncode, audit = preflight.run_command("dataset_audit", cmd, out_dir / "dataset_audit.json")
    if returncode != 0:
        preflight.error("dataset_audit_failed", f"dataset audit returned {returncode}", out_dir / "dataset_audit.json")
    elif isinstance(audit, dict) and audit.get("ok") is True:
        message = (
            f"dataset audit ok files={audit.get('total_files')} "
            f"duplicates={audit.get('duplicate_content_group_count')} "
            f"warnings={audit.get('warning_count')}"
        )
        if as_int(audit.get("warning_count")):
            preflight.warning("dataset_audit_warnings", message, out_dir / "dataset_audit.json")
        else:
            preflight.ok("dataset_audit", message, out_dir / "dataset_audit.json")
    else:
        preflight.error("dataset_audit_failed", "dataset audit report is missing or not ok", out_dir / "dataset_audit.json")
    return audit if isinstance(audit, dict) else {}


def check_cad_feature_profile(preflight: Preflight, args: argparse.Namespace, dataset_lists: list[str]) -> dict[str, Any]:
    out_dir = preflight.out_dir / "cad_feature_profile"
    profile_path = out_dir / "cad_feature_profile.json"
    subset_path = out_dir / "complex_dataset_index.json"
    cmd = [
        sys.executable,
        str(Path("test_harness/tools/profile_cad_features.py")),
        "--out",
        str(profile_path),
        "--paths-out",
        str(out_dir / "complex_paths.txt"),
        "--subset-out",
        str(subset_path),
        "--report",
        str(out_dir / "cad_feature_profile.md"),
        "--min-score",
        str(args.cad_feature_min_score),
    ]
    for path in dataset_lists:
        cmd.extend(["--dataset-list", path])
    returncode, profile = preflight.run_command("cad_feature_profile", cmd, profile_path)
    result: dict[str, Any] = {
        "summary_path": str(profile_path),
        "report_path": str(out_dir / "cad_feature_profile.md"),
        "paths_path": str(out_dir / "complex_paths.txt"),
        "subset_path": str(subset_path),
        "returncode": returncode,
        "ok": False,
    }
    if isinstance(profile, dict):
        result.update(
            {
                "total_files": profile.get("total_files"),
                "profiled_files": profile.get("profiled_files"),
                "complex_file_count": profile.get("complex_file_count"),
                "feature_totals": profile.get("feature_totals"),
            }
        )
    complex_count = as_int(result.get("complex_file_count"))
    if returncode != 0:
        message = f"CAD feature profile returned {returncode}"
        if args.require_cad_feature_profile:
            preflight.error("cad_feature_profile_failed", message, profile_path)
        else:
            preflight.warning("cad_feature_profile_failed", message, profile_path)
        return result
    if not isinstance(profile, dict):
        message = "CAD feature profile report is missing or invalid"
        if args.require_cad_feature_profile:
            preflight.error("cad_feature_profile_missing", message, profile_path)
        else:
            preflight.warning("cad_feature_profile_missing", message, profile_path)
        return result
    if complex_count <= 0:
        message = "CAD feature profile found no complex STEP/IGES files"
        if args.require_cad_feature_profile:
            preflight.error("cad_feature_profile_empty", message, profile_path)
        else:
            preflight.warning("cad_feature_profile_empty", message, profile_path)
        return result
    preflight.ok("cad_feature_profile", f"CAD feature profile complex files={complex_count}", profile_path)
    audit_out = out_dir / "dataset_audit"
    audit_cmd = [
        sys.executable,
        str(Path("test_harness/tools/audit_corpus_dataset.py")),
        "--out",
        str(audit_out),
        "--fail-duplicate-ratio",
        str(args.dataset_audit_fail_duplicate_ratio),
        "--dataset-list",
        str(subset_path),
    ]
    if args.dataset_audit_require_hashes:
        audit_cmd.append("--require-hashes")
    audit_returncode, audit = preflight.run_command("cad_feature_subset_audit", audit_cmd, audit_out / "dataset_audit.json")
    result["subset_audit"] = {
        "summary_path": str(audit_out / "dataset_audit.json"),
        "report_path": str(audit_out / "dataset_audit.md"),
        "returncode": audit_returncode,
        "ok": bool(isinstance(audit, dict) and audit.get("ok") is True and audit_returncode == 0),
    }
    if isinstance(audit, dict):
        result["subset_audit"].update(
            {
                "total_files": audit.get("total_files"),
                "missing_files": audit.get("missing_files"),
                "empty_files": audit.get("empty_files"),
                "error_count": audit.get("error_count"),
                "warning_count": audit.get("warning_count"),
                "hash_coverage_ratio": audit.get("hash_coverage_ratio"),
                "duplicate_content_group_count": audit.get("duplicate_content_group_count"),
            }
        )
    if audit_returncode != 0 or not isinstance(audit, dict) or audit.get("ok") is not True:
        preflight.error("cad_feature_subset_audit_failed", f"complex subset audit returned {audit_returncode}", audit_out / "dataset_audit.json")
        result["ok"] = False
    else:
        message = (
            f"complex subset audit ok files={audit.get('total_files')} "
            f"hash={audit.get('hash_coverage_ratio')} warnings={audit.get('warning_count')}"
        )
        if as_int(audit.get("warning_count")):
            preflight.warning("cad_feature_subset_audit_warnings", message, audit_out / "dataset_audit.json")
        else:
            preflight.ok("cad_feature_subset_audit", message, audit_out / "dataset_audit.json")
        result["ok"] = True
    return result


def markdown_report(result: dict[str, Any]) -> str:
    lines = [
        "# SGGK Campaign Preflight",
        "",
        f"- Generated: `{result.get('generated_at')}`",
        f"- OK: `{result.get('ok')}`",
        f"- Errors: `{result.get('error_count')}`",
        f"- Warnings: `{result.get('warning_count')}`",
        "",
        "## Inputs",
        "",
    ]
    inputs = result.get("inputs", {}) if isinstance(result.get("inputs"), dict) else {}
    for key in ("runner", "topology_extractor", "dsl_paths", "bug_record_paths", "dataset_roots", "dataset_lists", "source_roots"):
        lines.append(f"- `{key}`: `{inputs.get(key)}`")
    cad_profile = result.get("cad_feature_profile") if isinstance(result.get("cad_feature_profile"), dict) else {}
    if cad_profile:
        lines.extend(
            [
                "",
                "## CAD Feature Profile",
                "",
                f"- Profile: `{cad_profile.get('summary_path', '')}`",
                f"- Report: `{cad_profile.get('report_path', '')}`",
                f"- Complex subset: `{cad_profile.get('subset_path', '')}`",
                f"- Complex files: `{cad_profile.get('complex_file_count', 0)}`",
                f"- Subset audit: `{cad_profile.get('subset_audit', {}).get('summary_path', '') if isinstance(cad_profile.get('subset_audit'), dict) else ''}`",
            ]
        )
    lines.extend(["", "## Checks", "", "| severity | kind | message | path |", "| --- | --- | --- | --- |"])
    for check in as_list(result.get("checks")):
        if not isinstance(check, dict):
            continue
        lines.append(
            "| `{severity}` | `{kind}` | {message} | `{path}` |".format(
                severity=check.get("severity", ""),
                kind=check.get("kind", ""),
                message=str(check.get("message", "")).replace("|", "\\|"),
                path=check.get("path", ""),
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    preflight = Preflight(out_dir)

    runner = check_runner(preflight, args.runner) if args.runner else ""
    if not args.runner:
        preflight.warning("runner_missing", "runner path not provided; preflight cannot prove executable availability")
    extractor = check_topology_extractor(preflight, args.topology_extractor)

    dsl_paths = args.dsl or list(DEFAULT_DSLS)
    resolved_dsls = check_paths(preflight, dsl_paths, "DSL", required=True)
    bug_record_paths = args.bug_record or default_bug_records()
    resolved_bug_records = check_paths(preflight, bug_record_paths, "bug record", required=False)
    dataset_roots = check_paths(preflight, args.dataset_root, "dataset root", required=False) if args.dataset_root else []
    dataset_lists = check_paths(preflight, args.dataset_list, "dataset list", required=False) if args.dataset_list else []
    source_roots = check_paths(preflight, args.source_root, "source root", required=False)

    dsl_report: dict[str, Any] = {}
    if args.skip_dsl_check:
        preflight.warning("dsl_check_skipped", "DSL check skipped by argument")
    elif resolved_dsls:
        dsl_report = check_dsl(preflight, resolved_dsls)

    bug_registry: dict[str, Any] = {}
    if args.skip_bug_record_check:
        preflight.warning("bug_record_check_skipped", "bug-record check skipped by argument")
    elif resolved_bug_records:
        bug_registry = check_bug_records(preflight, resolved_bug_records)

    discovery: dict[str, Any] = {}
    dataset_audit: dict[str, Any] = {}
    cad_feature_profile: dict[str, Any] = {}
    if args.skip_dataset_discovery:
        preflight.warning("dataset_discovery_skipped", "dataset discovery skipped by argument")
    elif dataset_roots:
        discovery = check_dataset_discovery(preflight, args, dataset_roots)
    elif dataset_lists:
        preflight.ok("dataset_list", f"existing dataset list(s) provided count={len(dataset_lists)}")
    else:
        preflight.warning("dataset_missing", "no dataset root or dataset list provided")

    audit_inputs: list[str] = []
    if discovery:
        audit_inputs = [str(out_dir / "discovery" / "dataset_index.json")]
    elif dataset_lists:
        audit_inputs = dataset_lists
    if args.skip_dataset_audit:
        preflight.warning("dataset_audit_skipped", "dataset audit skipped by argument")
    elif audit_inputs:
        dataset_audit = check_dataset_audit(preflight, args, audit_inputs)
    if args.profile_cad_features or args.require_cad_feature_profile:
        if audit_inputs:
            cad_feature_profile = check_cad_feature_profile(preflight, args, audit_inputs)
        else:
            message = "CAD feature profile requested but no dataset inputs were available"
            if args.require_cad_feature_profile:
                preflight.error("cad_feature_profile_missing_inputs", message)
            else:
                preflight.warning("cad_feature_profile_missing_inputs", message)

    errors = [check for check in preflight.checks if check.get("severity") == "error"]
    warnings = [check for check in preflight.checks if check.get("severity") == "warning"]
    result = {
        "generated_at": now_iso_like(),
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "inputs": {
            "runner": runner,
            "topology_extractor": extractor,
            "dsl_paths": resolved_dsls,
            "bug_record_paths": resolved_bug_records,
            "dataset_roots": dataset_roots,
            "dataset_lists": dataset_lists,
            "source_roots": source_roots,
        },
        "dsl_check": dsl_report,
        "bug_registry": {
            "path": str(out_dir / "bug_records_materialized" / "bug_registry.json") if bug_registry else "",
            "bug_count": len(as_list(bug_registry.get("bugs"))) if bug_registry else 0,
        },
        "dataset_discovery": {
            "path": str(out_dir / "discovery" / "dataset_index.json") if discovery else "",
            "total_files": discovery.get("total_files") if discovery else 0,
            "duplicate_content_group_count": discovery.get("duplicate_content_group_count") if discovery else 0,
        },
        "dataset_audit": {
            "path": str(out_dir / "dataset_audit" / "dataset_audit.json") if dataset_audit else "",
            "total_files": dataset_audit.get("total_files") if dataset_audit else 0,
            "error_count": dataset_audit.get("error_count") if dataset_audit else 0,
            "warning_count": dataset_audit.get("warning_count") if dataset_audit else 0,
            "hash_coverage_ratio": dataset_audit.get("hash_coverage_ratio") if dataset_audit else 0,
            "duplicate_content_group_count": dataset_audit.get("duplicate_content_group_count") if dataset_audit else 0,
        },
        "cad_feature_profile": cad_feature_profile,
        "commands": preflight.commands,
        "checks": preflight.checks,
    }
    json_path = out_dir / "preflight_report.json"
    md_path = out_dir / "preflight_report.md"
    write_json(json_path, result)
    write_text(md_path, markdown_report(result))
    print(f"summary={json_path}")
    print(f"report={md_path}")
    print(f"ok={result['ok']} errors={result['error_count']} warnings={result['warning_count']}")
    if errors and not args.warn_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
