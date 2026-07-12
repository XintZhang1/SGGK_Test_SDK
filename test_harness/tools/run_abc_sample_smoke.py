#!/usr/bin/env python3
"""Run a focused ABC sample smoke over a fetch_abc_dataset.py output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-root", required=True, help="Output root produced by fetch_abc_dataset.py")
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument("--out", default="", help="Smoke output directory; defaults to <fetch-root>/sample_smoke")
    parser.add_argument("--top-import-limit", type=int, default=48, help="Top complex STEP/IGES files to import")
    parser.add_argument("--recut-source-limit", type=int, default=12, help="Imported SGT sources used for recut matrix")
    parser.add_argument("--recut-limit", type=int, default=36, help="Maximum recut recipes")
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-case timeout")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel runner jobs")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse completed corpus and recipe cases after an interrupted smoke run",
    )
    parser.add_argument("--fail-on-import-failure", action="store_true", help="Stop when top-complex import has failures")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def run_tool(cmd: list[str]) -> dict[str, Any]:
    print("+ " + " ".join(cmd), flush=True)
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr, flush=True)
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None


def compact_corpus_counts(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {"available": False}
    return {
        "available": True,
        "total": data.get("total"),
        "executed": data.get("executed"),
        "passed": data.get("passed"),
        "failed": data.get("failed"),
        "timed_out": data.get("timed_out"),
    }


def compact_recipe_counts(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {"available": False}
    return {
        "available": True,
        "total": data.get("total"),
        "executed": data.get("executed"),
        "passed": data.get("passed"),
        "failed": data.get("failed"),
        "timed_out": data.get("timed_out"),
    }


def compact_triage_counts(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {"available": False}
    return {
        "available": True,
        "total_cases": data.get("total_cases"),
        "passed_cases": data.get("passed_cases"),
        "failed_cases": data.get("failed_cases"),
        "failure_group_count": data.get("failure_group_count"),
        "warning_cases": data.get("warning_cases"),
        "command_failures": data.get("command_failures"),
    }


def compact_audit(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {"available": False}
    return {
        "available": True,
        "ok": data.get("ok"),
        "files": data.get("total_files", data.get("files")),
        "errors": data.get("error_count", data.get("errors")),
        "warnings": data.get("warning_count", data.get("warnings")),
    }


def compact_feature_profile(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {"available": False}
    return {
        "available": True,
        "total_files": data.get("total_files"),
        "profiled_files": data.get("profiled_files"),
        "complex_file_count": data.get("complex_file_count"),
        "feature_totals": data.get("feature_totals", {}),
    }


def compact_geometry_audit(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {"available": False}
    exact_bbox = data.get("exact_input_bbox")
    if not isinstance(exact_bbox, dict):
        exact_bbox = {}
    return {
        "available": True,
        "total_cases": data.get("total_cases"),
        "exact_input_bbox_enabled": exact_bbox.get("enabled"),
        "duplicate_geometry_group_count": len(data.get("duplicate_geometry_groups") or []),
        "same_boolean_duplicate_input_group_count": len(data.get("same_boolean_duplicate_input_groups") or []),
        "tolerance_mismatch_count": len(data.get("tolerance_mismatches") or []),
    }


def markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# ABC Sample Smoke",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Fetch root: `{summary.get('fetch_root')}`",
        f"- Output root: `{summary.get('out_root')}`",
        f"- Runner: `{summary.get('runner')}`",
        "",
        "## Results",
        "",
    ]
    audit = summary.get("dataset_audit", {})
    profile = summary.get("feature_profile", {})
    top_import = summary.get("top_complex_import", {})
    top_triage = summary.get("top_complex_import_triage", {})
    recut = summary.get("recut_run", {})
    recut_triage = summary.get("recut_triage", {})
    recut_geometry = summary.get("recut_geometry_audit", {})
    lines.extend(
        [
            f"- Dataset audit: ok=`{audit.get('ok')}` files=`{audit.get('files')}` errors=`{audit.get('errors')}` warnings=`{audit.get('warnings')}`",
            f"- Feature profile: files=`{profile.get('total_files')}` complex=`{profile.get('complex_file_count')}`",
            f"- Top-complex import: passed=`{top_import.get('passed')}` failed=`{top_import.get('failed')}` timed_out=`{top_import.get('timed_out')}`",
            f"- Top-complex triage: failures=`{top_triage.get('failed_cases')}` groups=`{top_triage.get('failure_group_count')}` command_failures=`{top_triage.get('command_failures')}`",
            f"- Recut run: passed=`{recut.get('passed')}` failed=`{recut.get('failed')}` timed_out=`{recut.get('timed_out')}`",
            f"- Recut triage: failures=`{recut_triage.get('failed_cases')}` groups=`{recut_triage.get('failure_group_count')}` command_failures=`{recut_triage.get('command_failures')}`",
            f"- Recut geometry audit: cases=`{recut_geometry.get('total_cases')}` tolerance_mismatches=`{recut_geometry.get('tolerance_mismatch_count')}` duplicate_geometry_groups=`{recut_geometry.get('duplicate_geometry_group_count')}` duplicate_input_groups=`{recut_geometry.get('same_boolean_duplicate_input_group_count')}`",
            "",
            "## Artifacts",
            "",
        ]
    )
    for key in (
        "dataset_audit_report",
        "top_complex_triage_report",
        "top_complex_preview_contact",
        "recut_manifest_report",
        "recut_triage_report",
        "recut_geometry_audit_report",
        "recut_preview_contact",
    ):
        value = summary.get(key)
        if value:
            lines.append(f"- `{key}`: `{value}`")
    lines.append("")
    return "\n".join(lines)


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def main() -> int:
    args = parse_args()
    fetch_root = Path(args.fetch_root)
    out_root = Path(args.out) if args.out else fetch_root / "sample_smoke"
    runner = Path(args.runner)
    script_dir = Path(__file__).resolve().parent
    dataset_index = fetch_root / "dataset_index.json"
    complex_index = fetch_root / "complex_dataset_index.json"
    feature_profile = fetch_root / "cad_feature_profile.json"
    require_file(dataset_index, "dataset index")
    require_file(complex_index, "complex dataset index")
    require_file(runner, "runner")

    out_root.mkdir(parents=True, exist_ok=True)
    commands: dict[str, Any] = {}

    dataset_audit_dir = out_root / "dataset_audit"
    commands["dataset_audit"] = run_tool(
        [
            sys.executable,
            str(script_dir / "audit_corpus_dataset.py"),
            "--dataset-list",
            str(dataset_index),
            "--out",
            str(dataset_audit_dir),
            "--require-hashes",
        ]
    )
    if not commands["dataset_audit"]["ok"]:
        write_json(out_root / "abc_sample_smoke_summary.json", {"generated_at": now_iso_like(), "commands": commands})
        return 1

    top_import_dir = out_root / "top_complex_import"
    top_triage_dir = out_root / "top_complex_import_triage"
    top_import_command = [
            sys.executable,
            str(script_dir / "run_corpus.py"),
            "--runner",
            str(runner),
            "--dataset-list",
            str(complex_index),
            "--out",
            str(top_import_dir),
            "--limit",
            str(args.top_import_limit),
            "--preserve-input-order",
            "--timeout",
            str(args.timeout),
            "--triage-out",
            str(top_triage_dir),
            "--triage-include-passed",
            "--jobs",
            str(args.jobs),
        ]
    if args.resume:
        top_import_command.extend(["--resume", "--resume-mode", "completed"])
    commands["top_complex_import"] = run_tool(top_import_command)
    if args.fail_on_import_failure and not commands["top_complex_import"]["ok"]:
        write_json(out_root / "abc_sample_smoke_summary.json", {"generated_at": now_iso_like(), "commands": commands})
        return 1

    top_preview_dir = out_root / "top_complex_import_preview"
    commands["top_complex_preview"] = run_tool(
        [
            sys.executable,
            str(script_dir / "render_case_preview.py"),
            str(top_import_dir),
            "--out-dir",
            str(top_preview_dir),
            "--contact-sheet",
            str(top_preview_dir / "contact.png"),
        ]
    )

    recut_recipe_dir = out_root / "top_complex_recut_recipes"
    recut_manifest = out_root / "top_complex_recut_manifest.json"
    commands["generate_recut"] = run_tool(
        [
            sys.executable,
            str(script_dir / "generate_corpus_recut_matrix.py"),
            "--dataset",
            str(top_import_dir),
            "--out",
            str(recut_recipe_dir),
            "--case-prefix",
            "abc_sample_recut",
            "--preset",
            "smoke",
            "--source-limit",
            str(args.recut_source_limit),
            "--limit",
            str(args.recut_limit),
            "--runner",
            str(runner),
            "--require-exact-bbox-probe",
            "--manifest",
            str(recut_manifest),
        ]
    )
    if not commands["generate_recut"]["ok"]:
        write_json(out_root / "abc_sample_smoke_summary.json", {"generated_at": now_iso_like(), "commands": commands})
        return 1

    recut_run_dir = out_root / "top_complex_recut_run"
    recut_triage_dir = out_root / "top_complex_recut_triage"
    recut_preview_dir = out_root / "top_complex_recut_preview"
    recut_geometry_audit_dir = out_root / "top_complex_recut_geometry_audit"
    recut_command = [
            sys.executable,
            str(script_dir / "run_recipes.py"),
            "--runner",
            str(runner),
            "--recipe",
            str(recut_recipe_dir),
            "--out",
            str(recut_run_dir),
            "--jobs",
            str(args.jobs),
            "--timeout",
            str(args.timeout),
            "--triage-out",
            str(recut_triage_dir),
            "--triage-include-passed",
            "--preview-out",
            str(recut_preview_dir),
            "--contact-sheet",
            str(recut_preview_dir / "contact.png"),
            "--geometry-audit-out",
            str(recut_geometry_audit_dir),
        ]
    if args.resume:
        recut_command.extend(["--resume", "--resume-mode", "completed"])
    commands["run_recut"] = run_tool(recut_command)

    summary = {
        "generated_at": now_iso_like(),
        "fetch_root": str(fetch_root),
        "out_root": str(out_root),
        "runner": str(runner),
        "dataset_index": str(dataset_index),
        "complex_dataset_index": str(complex_index),
        "dataset_audit": compact_audit(dataset_audit_dir / "dataset_audit.json"),
        "feature_profile": compact_feature_profile(feature_profile),
        "top_complex_import": compact_corpus_counts(top_import_dir / "corpus_summary.json"),
        "top_complex_import_triage": compact_triage_counts(top_triage_dir / "triage_summary.json"),
        "recut_run": compact_recipe_counts(recut_run_dir / "recipe_summary.json"),
        "recut_triage": compact_triage_counts(recut_triage_dir / "triage_summary.json"),
        "recut_geometry_audit": compact_geometry_audit(recut_geometry_audit_dir / "geometry_audit.json"),
        "commands": commands,
        "dataset_audit_report": str(dataset_audit_dir / "dataset_audit.md"),
        "top_complex_triage_report": str(top_triage_dir / "triage_report.md"),
        "top_complex_preview_contact": str(top_preview_dir / "contact.png"),
        "recut_manifest_report": str(recut_manifest.with_suffix(".md")),
        "recut_triage_report": str(recut_triage_dir / "triage_report.md"),
        "recut_geometry_audit_report": str(recut_geometry_audit_dir / "geometry_audit.md"),
        "recut_preview_contact": str(recut_preview_dir / "contact.png"),
    }
    write_json(out_root / "abc_sample_smoke_summary.json", summary)
    write_text(out_root / "abc_sample_smoke_report.md", markdown_report(summary))
    print(f"summary={out_root / 'abc_sample_smoke_summary.json'}")
    print(f"report={out_root / 'abc_sample_smoke_report.md'}")
    return 0 if commands["run_recut"]["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
