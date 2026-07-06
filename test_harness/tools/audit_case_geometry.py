#!/usr/bin/env python3
"""Audit rendered or runnable SGGK case artifacts for duplicate geometry.

The preview screenshots are still the human-facing check. This tool adds a
machine-readable pass over the same artifact reports: bbox-only geometry hashes,
nearest target/tool bbox contact, and tolerance-band checks inferred from common
variant names such as overlap_geom, exact, and gap_topo.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any


DEFAULT_TOPO_TOL = 1e-2
DEFAULT_GEOM_TOL = 1e-5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Case artifact dirs, artifact roots, or preview_index.json files")
    parser.add_argument("--out", required=True, help="Output directory for geometry_audit.json/md")
    parser.add_argument("--round-digits", type=int, default=9, help="Digits used when hashing bbox coordinates")
    parser.add_argument("--topo-tol", type=float, default=DEFAULT_TOPO_TOL)
    parser.add_argument("--geom-tol", type=float, default=DEFAULT_GEOM_TOL)
    parser.add_argument("--expectation-slack", type=float, default=0.25, help="Relative slack for inferred tolerance-band checks")
    parser.add_argument("--exact-bbox-runner", default="", help="Optional sggk_case_runner.exe for exact input target/tool bbox probes")
    parser.add_argument("--exact-bbox-timeout", type=float, default=60.0, help="Seconds allowed for each exact input bbox probe")
    parser.add_argument("--fail-on-duplicates", action="store_true", help="Return 2 when duplicate same-boolean input geometry is found")
    parser.add_argument("--fail-on-tolerance-mismatch", action="store_true", help="Return 2 when inferred tolerance offsets mismatch")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"_json_error": f"{exc.msg} at line {exc.lineno}, column {exc.colno}"}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_float_list(value: Any, count: int) -> list[float] | None:
    if not isinstance(value, list) or len(value) != count:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def is_case_dir(path: Path) -> bool:
    return (path / "manifest.json").is_file() and (path / "report").is_dir()


def iter_case_dirs(paths: list[str]) -> list[Path]:
    cases: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if path.is_file() and path.name == "preview_index.json":
            index = load_json(path)
            if isinstance(index, list):
                for item in index:
                    if isinstance(item, dict) and isinstance(item.get("case_dir"), str):
                        case_dir = Path(item["case_dir"])
                        if is_case_dir(case_dir):
                            cases.add(case_dir.resolve())
            continue
        if is_case_dir(path):
            cases.add(path.resolve())
            continue
        if not path.is_dir():
            continue
        for manifest in path.rglob("manifest.json"):
            case_dir = manifest.parent
            if "_recipes" not in case_dir.parts and is_case_dir(case_dir):
                cases.add(case_dir.resolve())
    return sorted(cases, key=lambda item: str(item).lower())


def bbox_from_report(value: Any) -> dict[str, list[float]] | None:
    if not isinstance(value, dict) or value.get("empty"):
        return None
    mins = as_float_list(value.get("min"), 3)
    maxs = as_float_list(value.get("max"), 3)
    if mins is None or maxs is None:
        return None
    return {"min": mins, "max": maxs}


def bbox_from_locator(locator: Any) -> dict[str, list[float]] | None:
    if not isinstance(locator, dict):
        return None
    bbox = bbox_from_report(locator.get("bbox"))
    if bbox is not None:
        return bbox
    point = as_float_list(locator.get("point"), 3)
    if point is not None:
        return {"min": point, "max": point}
    return None


def input_body_bboxes(input_index: Any, role: str) -> list[dict[str, list[float]]]:
    if not isinstance(input_index, dict):
        return []
    result: list[dict[str, list[float]]] = []
    for item in input_index.get("inputs", []):
        if not isinstance(item, dict) or as_str(item.get("role")) != role:
            continue
        for topo in item.get("topologies", []):
            if not isinstance(topo, dict) or topo.get("type") != "Body":
                continue
            bbox = bbox_from_locator(topo.get("locator"))
            if bbox is not None:
                result.append(bbox)
    return result


def result_bboxes(properties: Any) -> list[dict[str, list[float]]]:
    if not isinstance(properties, dict):
        return []
    result: list[dict[str, list[float]]] = []
    for body in properties.get("bodies", []):
        if not isinstance(body, dict):
            continue
        bbox = bbox_from_report(body.get("bbox"))
        if bbox is not None:
            result.append(bbox)
    return result


def safe_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_").lower()
    return text or "case"


def exact_bbox_probe_recipe(case_id: str, source_file: Path, topo_tol: float, geom_tol: float) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for axis in ("x", "y", "z"):
        checks.append(
            {
                "id": f"{axis}_min",
                "role": "result",
                "axis": axis,
                "side": "min",
                "expected": 0.0,
                "tolerance": max(topo_tol, geom_tol),
                "required": False,
            }
        )
        checks.append(
            {
                "id": f"{axis}_max",
                "role": "result",
                "axis": axis,
                "side": "max",
                "expected": 0.0,
                "tolerance": max(topo_tol, geom_tol),
                "required": False,
            }
        )
    return {
        "case_id": case_id,
        "api": "check_sgt",
        "source_file": str(source_file),
        "modeling_tol": topo_tol,
        "expectations": {
            "result_bodies": {"min": 1},
            "require_property_calculations": False,
            "require_finite_properties": False,
            "require_nonnegative_length_area": False,
            "boolean_volume_relation": False,
            "boolean_bbox_relation": False,
            "plane_extreme_checks": checks,
        },
    }


def bbox_from_plane_extreme_records(records: Any) -> dict[str, Any] | None:
    values: dict[tuple[str, str], float] = {}
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, dict):
            continue
        axis = record.get("axis")
        side = record.get("side")
        actual = record.get("actual_extreme")
        if axis not in {"x", "y", "z"} or side not in {"min", "max"}:
            continue
        if not isinstance(actual, (int, float)) or isinstance(actual, bool) or not math.isfinite(float(actual)):
            continue
        probe = record.get("probe")
        if not isinstance(probe, dict) or probe.get("success") is not True:
            continue
        values[(str(axis), str(side))] = float(actual)
    if any((axis, side) not in values for axis in ("x", "y", "z") for side in ("min", "max")):
        return None
    mins = [values[(axis, "min")] for axis in ("x", "y", "z")]
    maxs = [values[(axis, "max")] for axis in ("x", "y", "z")]
    if any(maxs[index] < mins[index] for index in range(3)):
        return None
    return {"min": mins, "max": maxs, "source": "plane_distance_extrema"}


def exact_input_bboxes(
    case_dir: Path,
    role: str,
    runner: Path | None,
    probe_root: Path | None,
    topo_tol: float,
    geom_tol: float,
    timeout: float,
) -> list[dict[str, Any]]:
    if runner is None or probe_root is None:
        return []
    source_file = case_dir / "input" / f"{role}.sgt"
    if not source_file.is_file():
        return []
    case_id = safe_id(f"xb_{stable_hash({'case': str(case_dir), 'role': role, 'source': str(source_file.resolve())})}_{role}")
    recipe = exact_bbox_probe_recipe(case_id, source_file, topo_tol, geom_tol)
    recipe_path = probe_root / "recipes" / f"{case_id}.json"
    write_json(recipe_path, recipe)
    try:
        completed = subprocess.run(
            [str(runner), "--recipe", str(recipe_path), "--out", str(probe_root / "runs")],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    validation_path = probe_root / "runs" / case_id / "report" / "validation.json"
    validation = load_json(validation_path)
    if not isinstance(validation, dict):
        return []
    bbox = bbox_from_plane_extreme_records(validation.get("plane_extreme_checks"))
    if bbox is None:
        return []
    bbox["probe_case_id"] = case_id
    bbox["probe_returncode"] = completed.returncode
    return [bbox]


def rounded_bbox(bbox: dict[str, list[float]], digits: int) -> list[float]:
    return [round(value, digits) for value in bbox["min"] + bbox["max"]]


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def bbox_axis_clearances(a: dict[str, list[float]], b: dict[str, list[float]]) -> list[dict[str, float | int | str]]:
    clearances: list[dict[str, float | int | str]] = []
    for axis in range(3):
        a_min, a_max = a["min"][axis], a["max"][axis]
        b_min, b_max = b["min"][axis], b["max"][axis]
        if a_max < b_min:
            clearances.append({"axis": axis, "kind": "gap", "value": b_min - a_max})
        elif b_max < a_min:
            clearances.append({"axis": axis, "kind": "gap", "value": a_min - b_max})
        else:
            overlap = min(a_max, b_max) - max(a_min, b_min)
            clearances.append({"axis": axis, "kind": "overlap", "value": -overlap})
    return clearances


def nearest_body_contact(
    targets: list[dict[str, list[float]]],
    tools: list[dict[str, list[float]]],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for target_index, target in enumerate(targets):
        for tool_index, tool in enumerate(tools):
            clearances = bbox_axis_clearances(target, tool)
            signed = min(clearances, key=lambda item: abs(float(item["value"])))
            gaps = [max(float(item["value"]), 0.0) for item in clearances]
            distance = sum(gap * gap for gap in gaps) ** 0.5
            item = {
                "target_index": target_index,
                "tool_index": tool_index,
                "distance": distance,
                "signed_clearance": float(signed["value"]),
                "signed_axis": int(signed["axis"]),
                "signed_kind": as_str(signed["kind"]),
                "axis_clearances": clearances,
            }
            if best is None or abs(item["signed_clearance"]) < abs(float(best["signed_clearance"])):
                best = item
    return best


def variant_name(manifest: Any, case_id: str) -> str:
    if isinstance(manifest, dict):
        dsl = manifest.get("dsl")
        if isinstance(dsl, dict) and as_str(dsl.get("variant")):
            return as_str(dsl.get("variant"))
    return case_id


def expected_signed_clearance(name: str, topo_tol: float, geom_tol: float) -> dict[str, Any] | None:
    lowered = name.lower()
    patterns = [
        ("overlap_topo", -topo_tol),
        ("overlap_geom", -geom_tol),
        ("exact", 0.0),
        ("flush", 0.0),
        ("gap_geom", geom_tol),
        ("gap_topo", topo_tol),
    ]
    for token, expected in patterns:
        if token in lowered:
            return {"token": token, "expected": expected}
    return None


def tolerance_ok(actual: float, expected: float, topo_tol: float, geom_tol: float, slack: float) -> tuple[bool, float]:
    if expected == 0.0:
        allowed = geom_tol * max(2.0, slack)
    else:
        allowed = max(abs(expected) * slack, geom_tol * 2.0, topo_tol * 1e-6)
    return abs(actual - expected) <= allowed, allowed


def read_case(
    case_dir: Path,
    round_digits: int,
    topo_tol: float,
    geom_tol: float,
    slack: float,
    exact_runner: Path | None,
    exact_probe_root: Path | None,
    exact_timeout: float,
) -> dict[str, Any]:
    manifest = load_json(case_dir / "manifest.json") or {}
    input_index = load_json(case_dir / "report" / "input_topology_index.json") or {}
    properties = load_json(case_dir / "report" / "properties.json") or {}
    status = load_json(case_dir / "report" / "status.json") or {}
    validation = load_json(case_dir / "report" / "validation.json") or {}

    case_id = as_str(manifest.get("case_id")) or case_dir.name
    dsl = manifest.get("dsl") if isinstance(manifest.get("dsl"), dict) else {}
    options = manifest.get("options") if isinstance(manifest.get("options"), dict) else {}
    boolean_type = as_str(options.get("boolean_type"))

    report_target = input_body_bboxes(input_index, "target")
    report_tool = input_body_bboxes(input_index, "tool")
    exact_target = exact_input_bboxes(case_dir, "target", exact_runner, exact_probe_root, topo_tol, geom_tol, exact_timeout)
    exact_tool = exact_input_bboxes(case_dir, "tool", exact_runner, exact_probe_root, topo_tol, geom_tol, exact_timeout)
    target = exact_target or report_target
    tool = exact_tool or report_tool
    result = result_bboxes(properties)
    input_bbox_sources = {
        "target": "plane_distance_extrema" if exact_target else "report_bbox",
        "tool": "plane_distance_extrema" if exact_tool else "report_bbox",
    }
    signature_payload = {
        "api": as_str(manifest.get("api")),
        "boolean_type": boolean_type,
        "target": [rounded_bbox(item, round_digits) for item in target],
        "tool": [rounded_bbox(item, round_digits) for item in tool],
        "result": [rounded_bbox(item, round_digits) for item in result],
    }
    input_payload = {
        "api": as_str(manifest.get("api")),
        "boolean_type": boolean_type,
        "target": signature_payload["target"],
        "tool": signature_payload["tool"],
    }
    contact = nearest_body_contact(target, tool)
    name = variant_name(manifest, case_id)
    inferred = expected_signed_clearance(name, topo_tol, geom_tol)
    tolerance_check = None
    if inferred is not None and contact is not None:
        ok, allowed = tolerance_ok(float(contact["signed_clearance"]), float(inferred["expected"]), topo_tol, geom_tol, slack)
        tolerance_check = {
            "token": inferred["token"],
            "expected_signed_clearance": inferred["expected"],
            "actual_signed_clearance": contact["signed_clearance"],
            "allowed_abs_error": allowed,
            "ok": ok,
        }

    return {
        "case_id": case_id,
        "case_dir": str(case_dir),
        "api": as_str(manifest.get("api")),
        "boolean_type": boolean_type,
        "dsl_case": as_str(dsl.get("case_id")) if isinstance(dsl, dict) else "",
        "dsl_variant": as_str(dsl.get("variant")) if isinstance(dsl, dict) else "",
        "variant_name": name,
        "succeeded": status.get("succeeded") if isinstance(status, dict) else None,
        "validation_ok": validation.get("ok") if isinstance(validation, dict) else None,
        "geometry_hash": stable_hash(signature_payload),
        "input_hash": stable_hash(input_payload),
        "signature": signature_payload,
        "input_bbox_sources": input_bbox_sources,
        "input_contact": contact,
        "tolerance_check": tolerance_check,
    }


def duplicate_groups(cases: list[dict[str, Any]], key: str, same_boolean_only: bool) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        group_key: tuple[Any, ...]
        if same_boolean_only:
            group_key = (
                key,
                case.get(key),
                case.get("api"),
                case.get("boolean_type"),
                case.get("dsl_case"),
            )
        else:
            group_key = (key, case.get(key))
        buckets[group_key].append(case)
    groups: list[dict[str, Any]] = []
    for group_key, items in buckets.items():
        if len(items) < 2:
            continue
        groups.append(
            {
                "hash": group_key[1],
                "api": items[0].get("api"),
                "boolean_type": items[0].get("boolean_type"),
                "dsl_case": items[0].get("dsl_case"),
                "cases": [
                    {
                        "case_id": item.get("case_id"),
                        "dsl_variant": item.get("dsl_variant"),
                        "case_dir": item.get("case_dir"),
                    }
                    for item in items
                ],
            }
        )
    return sorted(groups, key=lambda item: (as_str(item.get("dsl_case")), as_str(item.get("boolean_type")), as_str(item.get("hash"))))


def markdown_report(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Geometry Audit")
    lines.append("")
    lines.append(f"- Cases: {summary['total_cases']}")
    lines.append(f"- Same-boolean duplicate input groups: {len(summary['same_boolean_duplicate_input_groups'])}")
    lines.append(f"- Full geometry duplicate groups: {len(summary['duplicate_geometry_groups'])}")
    lines.append(f"- Tolerance mismatches: {len(summary['tolerance_mismatches'])}")
    exact = summary.get("exact_input_bbox")
    if isinstance(exact, dict):
        lines.append(f"- Exact input bbox: `{exact.get('enabled')}`")
    lines.append("")

    if summary["tolerance_mismatches"]:
        lines.append("## Tolerance Mismatches")
        lines.append("")
        for item in summary["tolerance_mismatches"][:50]:
            check = item.get("tolerance_check") if isinstance(item.get("tolerance_check"), dict) else {}
            lines.append(
                "- `{case_id}` variant `{variant}` expected {expected} got {actual} on axis {axis}".format(
                    case_id=item.get("case_id"),
                    variant=item.get("variant_name"),
                    expected=check.get("expected_signed_clearance"),
                    actual=check.get("actual_signed_clearance"),
                    axis=(item.get("input_contact") or {}).get("signed_axis"),
                )
            )
        lines.append("")

    if summary["same_boolean_duplicate_input_groups"]:
        lines.append("## Duplicate Inputs")
        lines.append("")
        for group in summary["same_boolean_duplicate_input_groups"][:30]:
            lines.append(
                f"- `{group['hash']}` api={group.get('api')} boolean={group.get('boolean_type')} dsl={group.get('dsl_case')}"
            )
            for case in group["cases"][:12]:
                lines.append(f"  - `{case['case_id']}` variant `{case.get('dsl_variant')}`")
        lines.append("")

    lines.append("## Case Contact Summary")
    lines.append("")
    lines.append("| case | variant | boolean | signed_clearance | axis | expected | ok | hash |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | --- | --- |")
    for item in summary["cases"]:
        contact = item.get("input_contact") if isinstance(item.get("input_contact"), dict) else {}
        check = item.get("tolerance_check") if isinstance(item.get("tolerance_check"), dict) else {}
        ok_value = check.get("ok") if check else ""
        expected = check.get("expected_signed_clearance") if check else ""
        lines.append(
            "| `{case}` | `{variant}` | `{boolean}` | {actual} | {axis} | {expected} | {ok} | `{hash}` |".format(
                case=item.get("case_id"),
                variant=item.get("dsl_variant") or item.get("variant_name"),
                boolean=item.get("boolean_type"),
                actual=contact.get("signed_clearance", ""),
                axis=contact.get("signed_axis", ""),
                expected=expected,
                ok=ok_value,
                hash=item.get("geometry_hash"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    cases_dirs = iter_case_dirs(args.paths)
    if not cases_dirs:
        print("no case artifacts found")
        return 1
    if args.exact_bbox_timeout <= 0.0:
        print("--exact-bbox-timeout must be > 0")
        return 1
    exact_runner: Path | None = None
    if args.exact_bbox_runner:
        exact_runner = Path(args.exact_bbox_runner)
        if not exact_runner.is_file():
            print(f"--exact-bbox-runner not found: {exact_runner}")
            return 1

    out_dir = Path(args.out).resolve()
    exact_probe_root = out_dir / "exact_bbox_probes" if exact_runner is not None else None
    cases = [
        read_case(
            case_dir,
            args.round_digits,
            args.topo_tol,
            args.geom_tol,
            args.expectation_slack,
            exact_runner,
            exact_probe_root,
            args.exact_bbox_timeout,
        )
        for case_dir in cases_dirs
    ]
    tolerance_mismatches = [
        case for case in cases
        if isinstance(case.get("tolerance_check"), dict) and case["tolerance_check"].get("ok") is False
    ]
    duplicate_geometry = duplicate_groups(cases, "geometry_hash", same_boolean_only=False)
    same_boolean_duplicate_inputs = duplicate_groups(cases, "input_hash", same_boolean_only=True)

    summary = {
        "topo_tol": args.topo_tol,
        "geom_tol": args.geom_tol,
        "round_digits": args.round_digits,
        "exact_input_bbox": {
            "enabled": exact_runner is not None,
            "runner": str(exact_runner) if exact_runner is not None else "",
            "probe_out": str(exact_probe_root) if exact_probe_root is not None else "",
            "timeout_seconds": args.exact_bbox_timeout,
        },
        "total_cases": len(cases),
        "cases": cases,
        "duplicate_geometry_groups": duplicate_geometry,
        "same_boolean_duplicate_input_groups": same_boolean_duplicate_inputs,
        "tolerance_mismatches": tolerance_mismatches,
    }

    write_json(out_dir / "geometry_audit.json", summary)
    (out_dir / "geometry_audit.md").write_text(markdown_report(summary), encoding="utf-8")

    print(f"audit={out_dir / 'geometry_audit.json'}")
    print(f"report={out_dir / 'geometry_audit.md'}")
    print(f"cases={len(cases)} duplicate_inputs={len(same_boolean_duplicate_inputs)} tolerance_mismatches={len(tolerance_mismatches)}")

    if args.fail_on_duplicates and same_boolean_duplicate_inputs:
        return 2
    if args.fail_on_tolerance_mismatch and tolerance_mismatches:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
