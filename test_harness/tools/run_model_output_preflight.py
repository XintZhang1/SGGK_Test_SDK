#!/usr/bin/env python3
"""Run deterministic preflight gates for staged model output.

This command validates interface example packs, diagnostics, provenance,
optional regression assets, input readiness, and saved model-output contracts.
It is provider-neutral and SDK-free: model execution belongs to the authoring
gateway, while this preflight only consumes staged JSON and local metadata.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from subprocess import run
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="artifacts/model_output_preflight", help="Output directory for preflight evidence")
    parser.add_argument("--model-output-root", default="artifacts/model_outputs", help="Saved model-output JSON root")
    parser.add_argument(
        "--model-output-provenance-root",
        default="artifacts/model_output_provenance",
        help="Saved-output provenance sidecar root",
    )
    parser.add_argument("--asset", action="append", default=[], help="Regression asset to include in provenance validation")
    parser.add_argument(
        "--interface-form",
        action="append",
        default=[],
        help="Specific interface intake form to validate; repeatable. Defaults to the manifest forms.",
    )
    parser.add_argument(
        "--interface-forms-manifest",
        default="test_harness/forms/interface_distillation/00_manifest.json",
        help="Manifest to pass to validate_interface_intake_readiness.py when --interface-form is not used",
    )
    parser.add_argument(
        "--interface-forms-root",
        default="test_harness/forms/interface_distillation",
        help="Forms root to pass to validate_interface_intake_readiness.py",
    )
    parser.add_argument("--skip-distillation", action="store_true", help="Skip SDK-free saved-output matrix check")
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 after writing reports")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def rel_display(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def command_text(command: list[str]) -> str:
    return " ".join(command)


def evaluate_report(label: str, report: Any) -> tuple[bool | None, str]:
    """Apply gate-specific semantics to a structured child report.

    Most fixed gates publish a top-level ``ok`` field.  The interface
    distillation summary is intentionally a matrix/report rather than a gate
    envelope, so a zero child return code alone is not enough: an already
    saved model output may have normalized successfully and still fail its
    fixed DSL/recipe gate.
    """

    if not isinstance(report, dict):
        return None, ""
    if report.get("ok") is False:
        return False, "report_declared_failure"
    if label != "saved_output_matrix":
        value = report.get("ok")
        return value if isinstance(value, bool) else None, ""

    matrix = report.get("model_output_matrix")
    if not isinstance(matrix, dict):
        return False, "saved_output_matrix_missing"
    counts = matrix.get("counts")
    if not isinstance(counts, dict):
        return False, "saved_output_matrix_counts_missing"

    blocking_counts = {
        "normalized_failed": int(counts.get("normalized_failed") or 0),
        "gate_failed": int(counts.get("gate_failed") or 0),
    }
    rows = matrix.get("rows") if isinstance(matrix.get("rows"), list) else []
    blocking_counts["saved_output_provenance_missing"] = sum(
        1
        for row in rows
        if isinstance(row, dict) and row.get("saved_output") and not row.get("provenance_found")
    )
    blocking_counts["saved_output_provenance_unknown"] = sum(
        1
        for row in rows
        if isinstance(row, dict) and row.get("saved_output") and not row.get("provenance_source_known")
    )
    stage_counts = (
        report.get("stage_counts")
        if isinstance(report.get("stage_counts"), dict)
        else {}
    )
    blocking_counts["model_output_check_failed"] = int(stage_counts.get("model_output_check_failed") or 0)
    failures = {key: value for key, value in blocking_counts.items() if value > 0}
    if failures:
        detail = ", ".join(f"{key}={value}" for key, value in sorted(failures.items()))
        return False, f"saved_output_matrix_failures: {detail}"
    return True, ""


def run_gate(label: str, command: list[str], *, report_path: Path | None = None) -> dict[str, Any]:
    started = now_iso_like()
    completed = run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    loaded_report: Any = None
    report_error = ""
    if report_path is not None:
        try:
            loaded_report = read_json(report_path)
        except FileNotFoundError:
            report_error = "report_missing"
        except json.JSONDecodeError as exc:
            report_error = f"report_invalid_json: {exc}"
        except OSError as exc:
            report_error = f"report_read_error: {exc}"
    report_ok, report_policy_error = evaluate_report(label, loaded_report)
    ok = (
        completed.returncode == 0
        and report_error == ""
        and report_policy_error == ""
        and report_ok is not False
    )
    return {
        "label": label,
        "ok": ok,
        "started_at": started,
        "finished_at": now_iso_like(),
        "returncode": completed.returncode,
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "report_path": rel_display(report_path) if report_path is not None else "",
        "report_ok": report_ok,
        "report_error": report_error,
        "report_policy_error": report_policy_error,
        "report": loaded_report if isinstance(loaded_report, dict) else {},
    }


def compact_gate(record: dict[str, Any]) -> dict[str, Any]:
    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    compact: dict[str, Any] = {
        "label": record.get("label"),
        "ok": record.get("ok"),
        "returncode": record.get("returncode"),
        "report_path": record.get("report_path"),
    }
    if record.get("report_policy_error"):
        compact["report_policy_error"] = record.get("report_policy_error")
    if isinstance(report.get("counts"), dict):
        compact["counts"] = report.get("counts")
    if report.get("scanned_file_count") is not None:
        compact["scanned_file_count"] = report.get("scanned_file_count")
    if report.get("pack_count") is not None:
        compact["pack_count"] = report.get("pack_count")
    if report.get("record_count") is not None:
        compact["record_count"] = report.get("record_count")
    if isinstance(report.get("readiness_counts"), dict):
        compact["readiness_counts"] = report.get("readiness_counts")
    if report.get("asset_count") is not None:
        compact["asset_count"] = report.get("asset_count")
    if report.get("action_count") is not None:
        compact["action_count"] = report.get("action_count")
    if isinstance(report.get("asset_state_counts"), dict):
        compact["asset_state_counts"] = report.get("asset_state_counts")
    if isinstance(report.get("model_output_matrix"), dict):
        matrix = report["model_output_matrix"]
        compact["matrix_counts"] = matrix.get("counts", {})
        compact["matrix_boundary"] = matrix.get("boundary", "")
        compact["diagnostic_catalog_summary"] = matrix.get("diagnostic_catalog_summary", {})
    if isinstance(report.get("summary"), dict):
        compact["bundle_summary"] = report.get("summary")
    if isinstance(report.get("record_counts"), dict):
        compact["record_counts"] = report.get("record_counts")
    if isinstance(report.get("by_action_state"), dict):
        compact["by_action_state"] = report.get("by_action_state")
    if isinstance(report.get("next_action_queue"), list):
        compact["action_queue_items"] = len(report.get("next_action_queue", []))
    return compact


def build_commands(args: argparse.Namespace, out_root: Path) -> list[tuple[str, list[str], Path | None]]:
    commands: list[tuple[str, list[str], Path | None]] = []
    example_report = out_root / "interface_example_packs" / "report.json"
    commands.append(
        (
            "interface_example_packs",
            [
                sys.executable,
                "test_harness/tools/validate_interface_example_packs.py",
                "--report",
                str(example_report),
                "--markdown",
                str(out_root / "interface_example_packs" / "report.md"),
            ],
            example_report,
        )
    )

    intake_report = out_root / "interface_intake_readiness" / "report.json"
    intake_cmd = [
        sys.executable,
        "test_harness/tools/validate_interface_intake_readiness.py",
        "--report",
        str(intake_report),
        "--markdown",
        str(out_root / "interface_intake_readiness" / "report.md"),
    ]
    if args.interface_form:
        for form in args.interface_form:
            intake_cmd.extend(["--form", form])
    else:
        intake_cmd.extend(
            [
                "--manifest",
                args.interface_forms_manifest,
                "--forms-root",
                args.interface_forms_root,
            ]
        )
    commands.append(
        (
            "interface_intake_readiness",
            intake_cmd,
            intake_report,
        )
    )

    input_asset_report = out_root / "input_assets" / "report.json"
    input_asset_cmd = [
        sys.executable,
        "test_harness/tools/validate_input_assets.py",
        "--report",
        str(input_asset_report),
        "--markdown",
        str(out_root / "input_assets" / "report.md"),
    ]
    if args.interface_form:
        for form in args.interface_form:
            input_asset_cmd.extend(["--form", form])
    else:
        input_asset_cmd.extend(
            [
                "--manifest",
                args.interface_forms_manifest,
                "--forms-root",
                args.interface_forms_root,
            ]
        )
    commands.append(("input_assets", input_asset_cmd, input_asset_report))

    diagnostic_report = out_root / "diagnostic_catalog" / "report.json"
    commands.append(
        (
            "diagnostic_catalog",
            [
                sys.executable,
                "test_harness/tools/validate_diagnostic_catalog.py",
                "--report",
                str(diagnostic_report),
                "--markdown",
                str(out_root / "diagnostic_catalog" / "report.md"),
            ],
            diagnostic_report,
        )
    )

    provenance_report = out_root / "provenance" / "report.json"
    provenance_cmd = [
        sys.executable,
        "test_harness/tools/validate_provenance_metadata.py",
        "--model-output-provenance-root",
        args.model_output_provenance_root,
        "--report",
        str(provenance_report),
        "--markdown",
        str(out_root / "provenance" / "report.md"),
    ]
    model_output_root = repo_path(args.model_output_root)
    provenance_root = repo_path(args.model_output_provenance_root)
    if model_output_root.is_dir():
        for sidecar in sorted(model_output_root.glob("*.provenance.json")):
            if sidecar.parent.resolve() != provenance_root.resolve():
                provenance_cmd.extend(["--path", str(sidecar)])
    for asset in args.asset:
        provenance_cmd.extend(["--asset", asset])
    commands.append(("provenance", provenance_cmd, provenance_report))

    if args.asset:
        regression_asset_report = out_root / "regression_assets" / "report.json"
        regression_asset_cmd = [
            sys.executable,
            "test_harness/tools/validate_regression_assets.py",
            "--report",
            str(regression_asset_report),
            "--markdown",
            str(out_root / "regression_assets" / "report.md"),
        ]
        for asset in args.asset:
            regression_asset_cmd.extend(["--asset", asset])
        commands.append(("regression_assets", regression_asset_cmd, regression_asset_report))

    if not args.skip_distillation:
        distillation_out = out_root / "interface_distillation_check"
        distillation_summary = distillation_out / "interface_distillation_summary.json"
        distillation_cmd = [
            sys.executable,
            "test_harness/tools/run_interface_distillation.py",
            "--out",
            str(distillation_out),
            "--model-output-root",
            args.model_output_root,
            "--model-output-provenance-root",
            args.model_output_provenance_root,
            "--check-model-outputs",
            "--fail-on-failures",
        ]
        commands.append(("saved_output_matrix", distillation_cmd, distillation_summary))
    return commands


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    out_root = repo_path(args.out)
    gates = [run_gate(label, command, report_path=report_path) for label, command, report_path in build_commands(args, out_root)]
    blockers = [gate for gate in gates if not gate.get("ok")]
    return {
        "ok": not blockers,
        "generated_at": now_iso_like(),
        "out_root": rel_display(out_root),
        "boundary": {
            "model_calls": False,
            "direct_api_calls": False,
            "runs_sdk": False,
            "applies_patches": False,
            "commits_changes": False,
            "production_flow": "deterministic_model_output_preflight",
        },
        "inputs": {
            "model_output_root": args.model_output_root,
            "model_output_provenance_root": args.model_output_provenance_root,
            "assets": args.asset,
            "interface_forms": args.interface_form,
            "interface_forms_manifest": args.interface_forms_manifest,
            "interface_forms_root": args.interface_forms_root,
            "skip_distillation": bool(args.skip_distillation),
        },
        "gates": gates,
        "gate_summary": [compact_gate(gate) for gate in gates],
        "blocker_count": len(blockers),
        "blockers": [compact_gate(gate) for gate in blockers],
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Model Output Preflight Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- OK: `{report.get('ok')}`",
        f"- Output root: `{report.get('out_root')}`",
        "- Boundary: `deterministic_model_output_preflight`",
        "",
        "## Gates",
        "",
        "| gate | ok | returncode | report | counts |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for gate in report.get("gate_summary", []):
        counts = gate.get("readiness_counts") or gate.get("counts") or gate.get("matrix_counts") or gate.get("bundle_summary") or {}
        lines.append(
            f"| `{gate.get('label')}` | `{gate.get('ok')}` | {gate.get('returncode')} | "
            f"`{gate.get('report_path')}` | `{counts}` |"
        )
    lines.extend(["", "## Notes", ""])
    lines.append("- This preflight never calls a model API; the authoring gateway is a separate stage.")
    lines.append("- It does not pass a runner to `run_interface_distillation.py`, so it remains SDK-free.")
    lines.append("- Diagnostic catalog validation only scans static source/catalog files and helps repair prompts use stable codes.")
    lines.append("- Input asset readiness only checks form paths and local files; missing assets become structured diagnostics.")
    lines.append("- Missing model outputs are reported as pending for the gateway task scheduler.")
    if report.get("inputs", {}).get("assets"):
        lines.append("- Regression asset health checks only read saved asset manifests, provenance, replay plans, and triage links.")
    if report.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        for gate in report["blockers"]:
            lines.append(f"- `{gate.get('label')}` failed; inspect `{gate.get('report_path')}`.")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    out_root = repo_path(args.out)
    report = build_report(args)
    write_json(out_root / "preflight_report.json", report)
    write_text(out_root / "preflight_report.md", markdown_report(report))
    print(json.dumps({"ok": report["ok"], "blocker_count": report["blocker_count"], "out": report["out_root"]}, indent=2))
    return 0 if report["ok"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
