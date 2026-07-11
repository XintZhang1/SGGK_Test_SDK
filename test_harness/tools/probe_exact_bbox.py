#!/usr/bin/env python3
"""Probe exact SGT body bounding boxes with coordinate-plane distances."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from validate_recipe import validate_recipe


TOPO_TOL = 1e-2
MAX_MODEL_SIZE = 5e5
SUPPORTED_EXTENSIONS = {".sgt"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[], help="SGT file or directory. Can be repeated.")
    parser.add_argument("--source-list", action="append", default=[], help="Text path list or discover_corpus.py JSON index.")
    parser.add_argument("--runner", default="", help="sggk_case_runner executable. Auto-detected when omitted.")
    parser.add_argument("--out", required=True, help="Output directory for exact_bbox.json/md and probe artifacts.")
    parser.add_argument("--body-index", type=int, default=0, help="Result body index inside each SGT.")
    parser.add_argument("--topo-tol", type=float, default=TOPO_TOL)
    parser.add_argument("--max-model-size", type=float, default=MAX_MODEL_SIZE)
    parser.add_argument("--plane-span", type=float, default=0.0, help="Explicit finite probe-face half span. 0 means runner default.")
    parser.add_argument("--plane-span-scale", type=float, default=4.0, help="Runner scale for probe-face span when plane-span is 0.")
    parser.add_argument("--timeout", type=float, default=60.0, help="Seconds per SGT probe run.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum SGT files to probe. 0 means all.")
    parser.add_argument("--fail-on-error", action="store_true", help="Return 2 when any source cannot be probed.")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def sanitize_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_").lower()
    return text or "case"


def stable_hash(value: Any, length: int = 10) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:length]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def default_runner_path() -> Path | None:
    for candidate in (
        Path("build/test_harness/Release/sggk_case_runner.exe"),
        Path("build/test_harness/Debug/sggk_case_runner.exe"),
        Path("build/test_harness/sggk_case_runner.exe"),
    ):
        if candidate.is_file():
            return candidate.resolve()
    return None


def resolve_runner(raw: str) -> Path:
    if raw:
        runner = Path(raw)
        if not runner.is_file():
            raise ValueError(f"--runner not found: {runner}")
        return runner.resolve()
    detected = default_runner_path()
    if detected is None:
        raise ValueError("--runner is required; no default sggk_case_runner was found")
    return detected


def load_source_list(path: Path) -> list[str]:
    if not path.exists():
        raise ValueError(f"source list not found: {path}")
    if path.suffix.lower() == ".json":
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError(f"source list JSON root must be object: {path}")
        files = data.get("files")
        if not isinstance(files, list):
            raise ValueError(f"source list JSON must contain files array: {path}")
        result: list[str] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            raw = item.get("path") or item.get("source_file")
            if isinstance(raw, str) and raw:
                result.append(raw)
        return result

    result: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            result.append(line)
    return result


def iter_sources(raw_paths: list[str]) -> list[Path]:
    found: set[Path] = set()
    for raw_path in raw_paths:
        path = Path(raw_path)
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            found.add(path.resolve())
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS:
                    found.add(child.resolve())
    return sorted(found, key=lambda item: str(item).lower())


def collect_sources(args: argparse.Namespace) -> list[Path]:
    raw_paths = list(args.source)
    for raw_list in args.source_list:
        raw_paths.extend(load_source_list(Path(raw_list)))
    sources = iter_sources(raw_paths)
    if args.limit > 0:
        sources = sources[: args.limit]
    return sources


def source_label(path: Path) -> str:
    return f"{sanitize_id(path.stem)}_{stable_hash(str(path.resolve()))}"


def plane_extreme_checks(args: argparse.Namespace) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for axis in ("x", "y", "z"):
        for side in ("min", "max"):
            check: dict[str, Any] = {
                "id": f"{axis}_{side}",
                "role": "result",
                "body_index": args.body_index,
                "axis": axis,
                "side": side,
                "compare_expected": False,
                "required": True,
                "tolerance": args.topo_tol,
                "plane_span_scale": args.plane_span_scale,
                "export_debug_geometry": True,
            }
            if args.plane_span > 0.0:
                check["plane_span"] = args.plane_span
            checks.append(check)
    return checks


def probe_recipe(source: Path, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "case_id": sanitize_id(f"exact_bbox_{source_label(source)}"),
        "api": "check_sgt",
        "source_file": str(source),
        "source_body_index": args.body_index,
        "modeling_tol": args.topo_tol,
        "max_model_size": args.max_model_size,
        "expectations": {
            "result_bodies": {"min": 1},
            "require_property_calculations": False,
            "require_finite_properties": False,
            "require_nonnegative_length_area": False,
            "boolean_volume_relation": False,
            "boolean_bbox_relation": False,
            "plane_extreme_checks": plane_extreme_checks(args),
        },
    }


def finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def bbox_from_records(records: Any) -> tuple[dict[str, Any] | None, list[str], dict[str, dict[str, Any]]]:
    values: dict[tuple[str, str], float] = {}
    extrema: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if not isinstance(records, list):
        return None, ["plane_extreme_checks missing"], extrema
    for record in records:
        if not isinstance(record, dict):
            continue
        axis = record.get("axis")
        side = record.get("side")
        key = f"{axis}_{side}"
        if axis not in {"x", "y", "z"} or side not in {"min", "max"}:
            continue
        probe = record.get("probe") if isinstance(record.get("probe"), dict) else {}
        actual = finite_number(record.get("actual_extreme"))
        extrema[key] = {
            "axis": axis,
            "side": side,
            "actual_extreme": actual,
            "probe_coordinate": record.get("probe_coordinate"),
            "probe_coordinate_source": record.get("probe_coordinate_source"),
            "plane_span": record.get("plane_span"),
            "probe_success": probe.get("success"),
            "point_on_plane": probe.get("point_on_plane"),
            "point_on_body": probe.get("point_on_body"),
            "topology_body": probe.get("topology_body"),
            "debug_geometry": record.get("debug_geometry"),
        }
        if probe.get("success") is not True:
            errors.append(f"{key}: distance probe failed")
            continue
        if actual is None:
            errors.append(f"{key}: actual_extreme missing or non-finite")
            continue
        values[(str(axis), str(side))] = actual

    missing = [f"{axis}_{side}" for axis in ("x", "y", "z") for side in ("min", "max") if (axis, side) not in values]
    errors.extend(f"{item}: missing" for item in missing)
    if errors:
        return None, errors, extrema

    mins = [values[(axis, "min")] for axis in ("x", "y", "z")]
    maxs = [values[(axis, "max")] for axis in ("x", "y", "z")]
    dims = [maxs[index] - mins[index] for index in range(3)]
    if any(dim < 0.0 for dim in dims):
        return None, ["exact bbox min exceeds max"], extrema
    bbox = {
        "source": "plane_distance_extrema",
        "min": mins,
        "max": maxs,
        "dims": dims,
        "center": [(mins[index] + maxs[index]) * 0.5 for index in range(3)],
    }
    return bbox, [], extrema


def run_probe(runner: Path, source: Path, args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    recipe = probe_recipe(source, args)
    errors = validate_recipe(recipe)
    if errors:
        return {
            "source_file": str(source),
            "case_id": recipe.get("case_id"),
            "ok": False,
            "errors": [f"generated recipe invalid: {error}" for error in errors],
        }

    recipe_path = out_dir / "recipes" / f"{recipe['case_id']}.json"
    write_json(recipe_path, recipe)
    command = [str(runner), "--recipe", str(recipe_path), "--out", str(out_dir / "runs")]
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
        )
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        return {
            "source_file": str(source),
            "case_id": recipe.get("case_id"),
            "recipe_path": str(recipe_path),
            "command": command,
            "ok": False,
            "timed_out": True,
            "elapsed_seconds": time.time() - started,
            "errors": [f"runner timeout after {args.timeout:g}s"],
            "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
        }

    case_dir = out_dir / "runs" / str(recipe["case_id"])
    validation_path = case_dir / "report" / "validation.json"
    if not validation_path.is_file():
        return {
            "source_file": str(source),
            "case_id": recipe.get("case_id"),
            "recipe_path": str(recipe_path),
            "command": command,
            "ok": False,
            "timed_out": timed_out,
            "returncode": completed.returncode,
            "elapsed_seconds": time.time() - started,
            "errors": ["probe validation missing"],
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }

    validation = read_json(validation_path)
    bbox, bbox_errors, extrema = bbox_from_records(validation.get("plane_extreme_checks") if isinstance(validation, dict) else None)
    ok = bbox is not None and completed.returncode == 0
    errors_out = list(bbox_errors)
    if completed.returncode != 0:
        errors_out.append(f"runner returncode {completed.returncode}")
    return {
        "source_file": str(source),
        "case_id": recipe.get("case_id"),
        "recipe_path": str(recipe_path),
        "probe_artifact_dir": str(case_dir),
        "validation_path": str(validation_path),
        "command": command,
        "ok": ok,
        "timed_out": timed_out,
        "returncode": completed.returncode,
        "elapsed_seconds": time.time() - started,
        "bbox": bbox or {},
        "extrema": extrema,
        "errors": errors_out,
        "stdout_tail": completed.stdout[-2000:] if not ok else "",
        "stderr_tail": completed.stderr[-2000:] if not ok else "",
    }


def markdown_report(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Exact BBox Probe")
    lines.append("")
    lines.append(f"- Generated: `{summary.get('generated_at')}`")
    lines.append(f"- Runner: `{summary.get('runner')}`")
    lines.append(f"- Sources: `{summary.get('total_sources')}`")
    lines.append(f"- OK: `{summary.get('ok_count')}`")
    lines.append(f"- Errors: `{summary.get('error_count')}`")
    lines.append(f"- Body index: `{summary.get('body_index')}`")
    lines.append("- Method: `coordinate-plane api_topo_minimum_distance`")
    lines.append("")
    lines.append("| source | ok | min | max | dims | probe case |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for case in summary.get("cases", []):
        if not isinstance(case, dict):
            continue
        bbox = case.get("bbox") if isinstance(case.get("bbox"), dict) else {}
        lines.append(
            "| `{source}` | `{ok}` | `{mins}` | `{maxs}` | `{dims}` | `{probe}` |".format(
                source=case.get("source_file", ""),
                ok=case.get("ok"),
                mins=bbox.get("min", ""),
                maxs=bbox.get("max", ""),
                dims=bbox.get("dims", ""),
                probe=case.get("probe_artifact_dir", ""),
            )
        )
    errors = [case for case in summary.get("cases", []) if isinstance(case, dict) and case.get("errors")]
    if errors:
        lines.extend(["", "## Errors", ""])
        for case in errors:
            lines.append(f"- `{case.get('source_file')}`: `{case.get('errors')}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.body_index < 0:
        print("--body-index must be >= 0")
        return 1
    if args.topo_tol <= 0.0:
        print("--topo-tol must be > 0")
        return 1
    if args.max_model_size <= 0.0:
        print("--max-model-size must be > 0")
        return 1
    if args.plane_span < 0.0:
        print("--plane-span must be >= 0")
        return 1
    if args.plane_span_scale <= 0.0:
        print("--plane-span-scale must be > 0")
        return 1
    if args.timeout <= 0.0:
        print("--timeout must be > 0")
        return 1

    try:
        runner = resolve_runner(args.runner)
        sources = collect_sources(args)
    except ValueError as exc:
        print(str(exc))
        return 1
    if not sources:
        print("no SGT sources found")
        return 1

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = [run_probe(runner, source, args, out_dir) for source in sources]
    summary = {
        "generated_at": now_iso_like(),
        "runner": str(runner),
        "body_index": args.body_index,
        "topo_tol": args.topo_tol,
        "max_model_size": args.max_model_size,
        "plane_span": args.plane_span,
        "plane_span_scale": args.plane_span_scale,
        "timeout_seconds": args.timeout,
        "total_sources": len(sources),
        "ok_count": sum(1 for case in cases if case.get("ok") is True),
        "error_count": sum(1 for case in cases if case.get("ok") is not True),
        "cases": cases,
    }
    summary["ok"] = summary["error_count"] == 0
    write_json(out_dir / "exact_bbox.json", summary)
    write_text(out_dir / "exact_bbox.md", markdown_report(summary))
    print(f"summary={out_dir / 'exact_bbox.json'}")
    print(f"report={out_dir / 'exact_bbox.md'}")
    print(f"sources={summary['total_sources']} ok={summary['ok_count']} errors={summary['error_count']}")
    if args.fail_on_error and not summary["ok"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
