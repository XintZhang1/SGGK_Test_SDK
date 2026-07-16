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
