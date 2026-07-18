#!/usr/bin/env python3
"""Run one SGGK boolean case through STEP export, Parasolid boolean, and the comparator.

For each case the orchestrator:

1. exports ``input/target.sgt`` / ``input/tool.sgt`` / ``output/result_*.sgt``
   to STEP with the fixed ``export_case_step.py`` helper;
2. runs the Parasolid boolean journal on target+tool;
3. measures every exported SGGK result STEP with the NX STEP journal;
4. classifies the case with ``compare_nx_sggk_boolean.py`` into the five-way
   verdict (``both_correct`` / ``sggk_correct`` / ``parasolid_correct`` /
   ``both_wrong`` / ``inconclusive``).

Batch mode walks a case list or a cases root with stable sharding and resume.
All commands run with ``shell=False`` and bounded timeouts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RESULT_KIND = "nx_sggk_boolean_compare_run"
REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = Path(__file__).resolve().parent
EXPORT_HELPER = TOOLS_ROOT / "export_case_step.py"
NX_RUNTIME = TOOLS_ROOT / "nx_runtime.py"
COMPARATOR = TOOLS_ROOT / "compare_nx_sggk_boolean.py"

OPERATION_MAP = {
    "UNION": "unite",
    "SUBTRACTION": "subtract",
    "INTERSECTION": "intersect",
}
# APIs whose operation is fixed by their own semantics; the runner still
# records its *default* boolean_type in manifest options, which is meaningless
# for these APIs and must not be trusted.
API_FIXED_OPERATION = {
    "api_combine_bodies": "unite",
}
VERDICTS = ("both_correct", "sggk_correct", "parasolid_correct", "both_wrong", "inconclusive")


class OrchestratorError(RuntimeError):
    pass


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise OrchestratorError(f"{label} is unavailable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OrchestratorError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise OrchestratorError(f"{label} JSON root must be an object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _python() -> str:
    return sys.executable


def _run(command: list[str], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "timed_out": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "returncode": None,
            "timed_out": True,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout_tail": "",
            "stderr_tail": f"timed out after {timeout:g}s",
        }


def _case_boolean_operation(case_dir: Path) -> str:
    recipe_path = case_dir / "input" / "recipe.json"
    recipe: dict[str, Any] = {}
    if recipe_path.is_file():
        recipe = _load_object(recipe_path, "case recipe")
        boolean_type = str(recipe.get("boolean_type") or "").strip().upper()
        if boolean_type in OPERATION_MAP:
            return OPERATION_MAP[boolean_type]
    manifest = _load_object(case_dir / "manifest.json", "case manifest")
    api = str(manifest.get("api") or recipe.get("api") or "").strip()
    if api in API_FIXED_OPERATION:
        return API_FIXED_OPERATION[api]
    # The runner writes its *default* boolean_type into manifest options for
    # every API, so the fallback is only meaningful for boolean-family cases.
    if api.startswith("api_boolean"):
        options = manifest.get("options")
        if isinstance(options, dict):
            boolean_type = str(options.get("boolean_type") or "").strip().upper()
            if boolean_type in OPERATION_MAP:
                return OPERATION_MAP[boolean_type]
    raise OrchestratorError(f"cannot determine boolean operation for case: {case_dir}")


def _validate_case(case_dir: Path) -> None:
    if not (case_dir / "input" / "target.sgt").is_file():
        raise OrchestratorError(f"case is missing input/target.sgt: {case_dir}")
    if not (case_dir / "input" / "tool.sgt").is_file():
        raise OrchestratorError(f"case is missing input/tool.sgt: {case_dir}")
    if not (case_dir / "report" / "status.json").is_file():
        raise OrchestratorError(f"case is missing report/status.json: {case_dir}")
    if not (case_dir / "report" / "properties.json").is_file():
        raise OrchestratorError(f"case is missing report/properties.json: {case_dir}")


def run_one_case(
    case_dir: Path,
    out_dir: Path,
    *,
    runner: Path,
    nx_root: Path,
    sggk_timeout: float,
    nx_timeout: float,
    abs_tol: float,
    rel_tol: float,
) -> dict[str, Any]:
    case_dir = case_dir.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    _validate_case(case_dir)
    operation = _case_boolean_operation(case_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "case_dir": str(case_dir),
        "case_id": case_dir.name,
        "operation": operation,
        "verdict": "inconclusive",
        "ok": False,
        "stages": {},
        "errors": [],
    }

    export_dir = out_dir / "export"
    export_cmd = [
        _python(),
        str(EXPORT_HELPER),
        "--case",
        str(case_dir),
        "--runner",
        str(runner),
        "--out",
        str(export_dir),
        "--timeout",
        f"{sggk_timeout:g}",
    ]
    export_run = _run(export_cmd, sggk_timeout * 4 + 60)
    record["stages"]["export"] = export_run
    export_manifest_path = export_dir / "export_manifest.json"
    if not export_manifest_path.is_file():
        record["errors"].append(f"STEP export produced no manifest: {export_run['stderr_tail']}")
        _write_json(out_dir / "run_summary.json", record)
        return record
    export_manifest = _load_object(export_manifest_path, "export manifest")
    exports = export_manifest.get("exports") or {}
    target_step = (exports.get("target") or {}).get("step")
    tool_step = (exports.get("tool") or {}).get("step")
    if not target_step or not tool_step:
        record["errors"].append("STEP export did not produce target.step/tool.step")
        _write_json(out_dir / "run_summary.json", record)
        return record

    nx_dir = out_dir / "nx"
    nx_dir.mkdir(parents=True, exist_ok=True)
    nx_boolean_path = nx_dir / "boolean_measurement.json"
    boolean_cmd = [
        _python(),
        str(NX_RUNTIME),
        "boolean-measure",
        "--nx-root",
        str(nx_root),
        "--target",
        str(target_step),
        "--tool",
        str(tool_step),
        "--operation",
        operation,
        "--measurement-out",
        str(nx_boolean_path),
        "--result-step",
        str(nx_dir / "parasolid_result.step"),
        "--timeout",
        f"{nx_timeout:g}",
    ]
    boolean_run = _run(boolean_cmd, nx_timeout + 120)
    record["stages"]["nx_boolean"] = boolean_run

    sggk_result_measures: list[str] = []
    result_steps = sorted(
        (Path((exports.get(role) or {}).get("step") or "") for role in exports if role.startswith("result_")),
        key=lambda path: path.name,
    )
    for result_step in result_steps:
        if not result_step.is_file():
            continue
        measure_out = nx_dir / f"sggk_result_{result_step.stem}_measure.json"
        measure_cmd = [
            _python(),
            str(NX_RUNTIME),
            "measure-step",
            "--nx-root",
            str(nx_root),
            "--step",
            str(result_step),
            "--measurement-out",
            str(measure_out),
            "--timeout",
            f"{nx_timeout:g}",
        ]
        measure_run = _run(measure_cmd, nx_timeout + 120)
        record["stages"][f"nx_measure_{result_step.stem}"] = measure_run
        if measure_out.is_file():
            sggk_result_measures.append(str(measure_out))

    comparison_dir = out_dir / "comparison"
    compare_cmd = [
        _python(),
        str(COMPARATOR),
        "--sggk-case",
        str(case_dir),
        "--nx-boolean",
        str(nx_boolean_path),
        "--out",
        str(comparison_dir),
        "--abs-tol",
        f"{abs_tol:g}",
        "--rel-tol",
        f"{rel_tol:g}",
    ]
    for measure in sggk_result_measures:
        compare_cmd.extend(["--nx-sggk-result", measure])
    compare_run = _run(compare_cmd, 120)
    record["stages"]["compare"] = compare_run

    comparison_path = comparison_dir / "comparison.json"
    if comparison_path.is_file():
        comparison = _load_object(comparison_path, "comparison")
        record["verdict"] = str(comparison.get("verdict") or "inconclusive")
        record["ok"] = True
        record["comparison_json"] = str(comparison_path)
        record["comparison_markdown"] = str(comparison_dir / "comparison.zh-CN.md")
    else:
        record["errors"].append(f"comparator produced no output: {compare_run['stderr_tail']}")
    _write_json(out_dir / "run_summary.json", record)
    return record


def _iter_case_dirs(cases_root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in cases_root.iterdir()
            if path.is_dir() and (path / "input" / "target.sgt").is_file() and (path / "input" / "tool.sgt").is_file()
        ),
        key=lambda path: path.name,
    )


def _load_case_list(path: Path) -> list[Path]:
    cases: list[Path] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            cases.append(Path(stripped).expanduser().resolve())
    return cases


def _case_verdict_and_reasons(comparison_path: Path) -> tuple[str, list[str]]:
    comparison = _load_object(comparison_path, "comparison")
    verdict = str(comparison.get("verdict") or "inconclusive")
    reasons = [str(item) for item in comparison.get("reasons") or []]
    return verdict, reasons


def _write_batch_report(out_root: Path, summary: dict[str, Any]) -> Path:
    """Write the mandatory Parasolid-comparison report.

    Cases that agree with Parasolid (``both_correct``) need no action; every
    other verdict is surfaced for the kernel developers with its reason.
    """

    consistent = [case for case in summary["cases"] if case["verdict"] == "both_correct"]
    attention = [case for case in summary["cases"] if case["verdict"] != "both_correct"]
    lines = [
        "# Parasolid 强制对比报告",
        "",
        f"- 用例总数：{summary['total_cases']}（本次执行 {summary['processed']}，续跑跳过 {summary['skipped']}）",
        f"- **与 Parasolid 一致（不用管）：{len(consistent)}**",
        f"- **需关注：{len(attention)}**",
        "",
        "## 判定计数",
        "",
    ]
    labels = {
        "both_correct": "两者都对（一致）",
        "sggk_correct": "SGGK 更对",
        "parasolid_correct": "Parasolid 更对",
        "both_wrong": "两者都不对",
        "inconclusive": "无法判定",
    }
    for verdict, count in summary["verdict_counts"].items():
        if count:
            lines.append(f"- {labels.get(verdict, verdict)}（`{verdict}`）：{count}")
    lines.extend(["", "## 需关注用例", ""])
    if not attention:
        lines.append("无。全部用例与 Parasolid 一致。")
    for case in attention:
        lines.append(f"### `{case['case_id']}` — {labels.get(case['verdict'], case['verdict'])}")
        for reason in case.get("reasons") or []:
            lines.append(f"- {reason}")
        if case.get("errors"):
            for error in case["errors"][:3]:
                lines.append(f"- 执行错误：{error}")
        lines.append("")
    lines.extend(
        [
            "## 与 Parasolid 一致（不用管）",
            "",
        ]
    )
    if not consistent:
        lines.append("无。")
    else:
        lines.append("以下用例 SGGK 与 Parasolid 结果一致，无需处理：")
        lines.append("")
        for case in consistent:
            lines.append(f"- `{case['case_id']}`")
    lines.append("")
    report_path = out_root / "parasolid_comparison.zh-CN.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--case", type=Path, help="Single boolean case artifact directory")
    source.add_argument("--cases-root", type=Path, help="Directory of boolean case directories")
    source.add_argument("--case-list", type=Path, help="Text file with one case directory per line")
    parser.add_argument("--out", required=True, type=Path, help="Output root")
    parser.add_argument("--runner", required=True, type=Path, help="sggk_case_runner.exe path")
    parser.add_argument("--nx-root", required=True, type=Path, help="Siemens NX installation root")
    parser.add_argument("--sggk-timeout", type=float, default=120.0)
    parser.add_argument("--nx-timeout", type=float, default=300.0)
    parser.add_argument("--abs-tol", type=float, default=0.01)
    parser.add_argument("--rel-tol", type=float, default=1e-5)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="Process at most N cases in this shard")
    parser.add_argument("--resume", action="store_true", help="Skip cases that already have a comparison.json")
    parser.add_argument(
        "--fail-on-attention",
        action="store_true",
        help="Return exit code 2 when any case needs attention (verdict other than both_correct)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = args.runner.expanduser().resolve()
    nx_root = args.nx_root.expanduser().resolve()
    out_root = args.out.expanduser().resolve()
    if not runner.is_file():
        print(f"--runner does not exist: {runner}", file=sys.stderr)
        return 1
    if not nx_root.is_dir():
        print(f"--nx-root does not exist: {nx_root}", file=sys.stderr)
        return 1
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        print("--shard-index must satisfy 0 <= index < shard-count", file=sys.stderr)
        return 1

    if args.case is not None:
        cases = [args.case.expanduser().resolve()]
    elif args.cases_root is not None:
        cases = _iter_case_dirs(args.cases_root.expanduser().resolve())
    else:
        cases = _load_case_list(args.case_list.expanduser().resolve())
    if args.shard_count > 1:
        cases = [case for index, case in enumerate(cases) if index % args.shard_count == args.shard_index]
    if args.limit > 0:
        cases = cases[: args.limit]

    out_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "total_cases": len(cases),
        "processed": 0,
        "skipped": 0,
        "verdict_counts": {verdict: 0 for verdict in VERDICTS},
        "cases": [],
    }
    for case_dir in cases:
        case_out = out_root / case_dir.name
        comparison_path = case_out / "comparison" / "comparison.json"
        if args.resume and comparison_path.is_file():
            verdict, reasons = _case_verdict_and_reasons(comparison_path)
            summary["skipped"] += 1
            summary["verdict_counts"][verdict] = summary["verdict_counts"].get(verdict, 0) + 1
            summary["cases"].append(
                {"case_id": case_dir.name, "verdict": verdict, "skipped": True, "reasons": reasons}
            )
            continue
        record = run_one_case(
            case_dir,
            case_out,
            runner=runner,
            nx_root=nx_root,
            sggk_timeout=args.sggk_timeout,
            nx_timeout=args.nx_timeout,
            abs_tol=args.abs_tol,
            rel_tol=args.rel_tol,
        )
        verdict = str(record.get("verdict") or "inconclusive")
        reasons: list[str] = []
        if comparison_path.is_file():
            _v, reasons = _case_verdict_and_reasons(comparison_path)
        summary["processed"] += 1
        summary["verdict_counts"][verdict] = summary["verdict_counts"].get(verdict, 0) + 1
        summary["cases"].append(
            {
                "case_id": case_dir.name,
                "verdict": verdict,
                "skipped": False,
                "ok": bool(record.get("ok")),
                "errors": record.get("errors", []),
                "reasons": reasons,
            }
        )
        _write_json(out_root / "batch_summary.json", summary)
    _write_json(out_root / "batch_summary.json", summary)
    report_path = _write_batch_report(out_root, summary)
    print(f"total={summary['total_cases']} processed={summary['processed']} skipped={summary['skipped']}")
    consistent = sum(1 for case in summary["cases"] if case["verdict"] == "both_correct")
    attention = len(summary["cases"]) - consistent
    print(f"  consistent_with_parasolid(不用管)={consistent}")
    print(f"  needs_attention(需关注)={attention}")
    for verdict, count in summary["verdict_counts"].items():
        if count:
            print(f"  {verdict}={count}")
    print(f"batch_summary={out_root / 'batch_summary.json'}")
    print(f"parasolid_report={report_path}")
    if args.fail_on_attention and attention > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
