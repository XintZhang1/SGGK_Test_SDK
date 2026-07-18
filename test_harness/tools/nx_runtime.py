#!/usr/bin/env python3
"""Detect, probe, or invoke the Siemens NX Python runtime safely."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_harness.nx import (  # noqa: E402
    NxJournalPolicyError,
    detect_nx_environment,
    execute_nx_journal,
    probe_nx_python,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="Optional JSON report path")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    detect = subparsers.add_parser("detect", help="Perform static, side-effect-free discovery")
    detect.add_argument("--nx-root", action="append", default=[])

    probe = subparsers.add_parser("probe", help="Launch the bundled isolated NXOpen probe")
    probe.add_argument("--nx-root", action="append", default=[])
    probe.add_argument("--timeout", type=float, default=120.0)

    measure_step = subparsers.add_parser(
        "measure-step",
        help="Run the fixed ABC STEP measurement journal",
    )
    measure_step.add_argument("--nx-root", action="append", default=[])
    measure_step.add_argument("--step", required=True, help="Existing .step/.stp input")
    measure_step.add_argument("--measurement-out", required=True, help="NX measurement JSON output")
    measure_step.add_argument("--timeout", type=float, default=300.0)

    boolean_measure = subparsers.add_parser(
        "boolean-measure",
        help="Run the fixed Parasolid boolean measurement journal",
    )
    boolean_measure.add_argument("--nx-root", action="append", default=[])
    boolean_measure.add_argument("--target", required=True, help="Existing target .step/.stp input")
    boolean_measure.add_argument("--tool", required=True, help="Existing tool .step/.stp input")
    boolean_measure.add_argument(
        "--operation",
        dest="boolean_operation",
        required=True,
        choices=["unite", "subtract", "intersect"],
        help="Parasolid boolean operation",
    )
    boolean_measure.add_argument("--measurement-out", required=True, help="NX boolean measurement JSON output")
    boolean_measure.add_argument("--result-step", default="", help="Optional result .step export path")
    boolean_measure.add_argument("--timeout", type=float, default=300.0)

    run = subparsers.add_parser("run", help="Run an allow-listed Python journal")
    run.add_argument("--nx-root", action="append", default=[])
    run.add_argument("--journal", required=True)
    run.add_argument("--allow-root", action="append", required=True)
    run.add_argument("--arg", action="append", default=[])
    run.add_argument("--timeout", type=float, default=300.0)
    return parser


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.operation == "detect":
        return detect_nx_environment(explicit_roots=args.nx_root)
    if args.operation == "probe":
        return probe_nx_python(explicit_roots=args.nx_root, timeout_seconds=args.timeout)
    if args.operation == "measure-step":
        step_path = Path(args.step).expanduser().resolve()
        measurement_out = Path(args.measurement_out).expanduser().resolve()
        if not step_path.is_file() or step_path.suffix.casefold() not in {".step", ".stp"}:
            raise ValueError("--step must name an existing .step or .stp file")
        if measurement_out.suffix.casefold() != ".json":
            raise ValueError("--measurement-out must use the .json extension")
        journal = REPO_ROOT / "test_harness" / "nx_journals" / "abc_step_measure.py"
        return execute_nx_journal(
            journal,
            allowed_roots=[journal.parent],
            arguments=[str(step_path), str(measurement_out)],
            explicit_roots=args.nx_root,
            timeout_seconds=args.timeout,
        )
    if args.operation == "boolean-measure":
        target_path = Path(args.target).expanduser().resolve()
        tool_path = Path(args.tool).expanduser().resolve()
        measurement_out = Path(args.measurement_out).expanduser().resolve()
        for label, path in (("target", target_path), ("tool", tool_path)):
            if not path.is_file() or path.suffix.casefold() not in {".step", ".stp"}:
                raise ValueError(f"--{label} must name an existing .step or .stp file")
        if measurement_out.suffix.casefold() != ".json":
            raise ValueError("--measurement-out must use the .json extension")
        journal = REPO_ROOT / "test_harness" / "nx_journals" / "boolean_measure.py"
        arguments = [str(target_path), str(tool_path), args.boolean_operation, str(measurement_out)]
        if args.result_step:
            result_step = Path(args.result_step).expanduser().resolve()
            if result_step.suffix.casefold() not in {".step", ".stp"}:
                raise ValueError("--result-step must use the .step or .stp extension")
            arguments.append(str(result_step))
        return execute_nx_journal(
            journal,
            allowed_roots=[journal.parent],
            arguments=arguments,
            explicit_roots=args.nx_root,
            timeout_seconds=args.timeout,
        )
    return execute_nx_journal(
        args.journal,
        allowed_roots=args.allow_root,
        arguments=args.arg,
        explicit_roots=args.nx_root,
        timeout_seconds=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = execute(args)
    except (NxJournalPolicyError, OSError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "operation": args.operation,
            "ok": False,
            "status": "invalid_request",
            "diagnostics": [
                {
                    "code": "NX_REQUEST_INVALID",
                    "severity": "error",
                    "message": str(exc),
                    "remediation": "Correct the request and retry.",
                }
            ],
        }
    if args.out:
        output = Path(args.out).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
