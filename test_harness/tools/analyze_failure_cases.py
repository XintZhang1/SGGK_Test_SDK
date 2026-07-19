#!/usr/bin/env python3
"""Deterministic failure pre-analysis for SGGK case capsules.

Assigns each failed case a diagnostic ``fault_domain`` from fixed host-side
rules (first match wins) and optionally merges advisory vision-model fault
hints.  Everything produced here is diagnostic evidence only: it never
confirms an SDK defect, never gates, approves, or alters any verdict, and the
advisory vision hint never overrides the deterministic classification.
Corrupt or missing inputs degrade to ``inconclusive`` instead of raising.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from classify_parasolid_divergence import classify_comparison

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1
FAULT_DOMAINS = (
    "test_expectation_suspect",
    "oracle_tooling_suspect",
    "transport_suspect",
    "geometry_result_suspect",
    "inconclusive",
)
MAX_EVIDENCE = 8
MAX_EVIDENCE_CHARS = 240
DEFAULT_MAX_CASES = 64
VISUAL_MAX_CASES = 4
DEFAULT_PROFILE = "siliconflow_vision"

DOMAIN_LABEL_ZH = {
    "test_expectation_suspect": "疑似测试预期问题",
    "oracle_tooling_suspect": "疑似 Oracle 工具链问题",
    "transport_suspect": "疑似传输/导出环节问题",
    "geometry_result_suspect": "疑似 SDK 几何结果问题",
    "inconclusive": "证据不足无法归因",
}
DOMAIN_NOTES_ZH = {
    "test_expectation_suspect": "确定性证据指向测试预期本身可疑（几何上不可能或自相矛盾的预期）",
    "oracle_tooling_suspect": "确定性证据指向 oracle/测量工具记录内部不一致",
    "transport_suspect": "确定性证据指向结果导出/传输环节（自报属性与 NX 测量漂移或结果不可导入）",
    "geometry_result_suspect": "排除预期与工具问题后，SDK 几何结果本身违反几何不变量，需内核侧进一步排查",
    "inconclusive": "证据不足，无法给出确定性的责任域判断",
}
ADVISORY_SUFFIX = "（诊断性证据，不构成 SDK 缺陷定论）"

HINT_TO_DOMAIN = {
    "test_expectation": "test_expectation_suspect",
    "geometry": "geometry_result_suspect",
    "transport": "transport_suspect",
    "tooling": "oracle_tooling_suspect",
    "unclear": "inconclusive",
}
POINT_RELATION_VALUES = {"Unknown", "OnVertex", "OnEdge", "OnFace", "Inside", "Outside", "OnBoundary", "OnModel"}
ORACLE_FAMILIES = (
    "point_relations",
    "face_point_relations",
    "clash_checks",
    "distance_checks",
    "plane_extreme_checks",
)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"_json_error": f"{exc.msg} at line {exc.lineno}"}
    except OSError:
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) and "_json_error" not in value else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _point(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, list | tuple) or len(value) != 3:
        return None
    coords = [_num(item) for item in value]
    if any(coord is None for coord in coords):
        return None
    return (float(coords[0]), float(coords[1]), float(coords[2]))


def _bbox(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("empty"):
        return None
    mins = _point(raw.get("min"))
    maxs = _point(raw.get("max"))
    if mins is None or maxs is None:
        return None
    return {"min": list(mins), "max": list(maxs)}


def _bbox_gap(a: dict[str, Any], b: dict[str, Any]) -> float:
    gaps = [
        max(0.0, b["min"][axis] - a["max"][axis], a["min"][axis] - b["max"][axis])
        for axis in range(3)
    ]
    return sum(gap * gap for gap in gaps) ** 0.5


def _bbox_contains(bbox: dict[str, Any], point: tuple[float, float, float], tol: float) -> bool:
    return all(bbox["min"][axis] - tol <= point[axis] <= bbox["max"][axis] + tol for axis in range(3))


def _evidence(evidence: list[str], text: str) -> None:
    if len(evidence) < MAX_EVIDENCE:
        evidence.append(" ".join(str(text).split())[:MAX_EVIDENCE_CHARS])


def _collect(case_dir: Path) -> dict[str, Any]:
    report = case_dir / "report"
    manifest = _dict(_load(case_dir / "manifest.json"))
    run_state = _dict(_load(case_dir / "run_state.json"))
    recipe = _dict(_load(case_dir / "input" / "recipe.json"))
    if not recipe:
        recipe_path = _str(manifest.get("recipe_path")) or _str(run_state.get("recipe_path"))
        if recipe_path:
            recipe = _dict(_load(Path(recipe_path)))
    case_id = (
        _str(manifest.get("case_id"))
        or _str(run_state.get("case_id"))
        or _str(recipe.get("case_id"))
        or case_dir.name
    )
    return {
        "case_id": case_id,
        "manifest": manifest,
        "run_state": run_state,
        "recipe": recipe,
        "status": _dict(_load(report / "status.json")),
        "validation": _dict(_load(report / "validation.json")),
        "validation_raw": _load(report / "validation.json"),
        "topo_check": _dict(_load(report / "topo_check.json")),
        "input_properties": _dict(_load(report / "input_properties.json")),
        "properties": _dict(_load(report / "properties.json")),
        "comparison": _dict(_load(case_dir / "comparison" / "comparison.json")),
    }


def _modeling_tol(data: dict[str, Any]) -> float:
    options = _dict(data["manifest"].get("options"))
    for value in (options.get("modeling_tol"), data["recipe"].get("modeling_tol")):
        number = _num(value)
        if number is not None and number > 0:
            return number
    return 0.0


def _role_boxes(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    boxes: dict[str, list[dict[str, Any]]] = {"target": [], "tool": [], "result": []}
    input_properties = data["input_properties"]
    for role in ("target", "tool"):
        for entry in _list(input_properties.get(role)):
            bbox = _bbox(_dict(entry).get("bbox"))
            if bbox is not None:
                boxes[role].append(bbox)
    for body in _list(data["properties"].get("bodies")):
        bbox = _bbox(_dict(body).get("bbox"))
        if bbox is not None:
            boxes["result"].append(bbox)
    return boxes


def _role_box(
    boxes: dict[str, list[dict[str, Any]]],
    role: Any,
    body_index: Any,
) -> dict[str, Any] | None:
    entries = boxes.get(_str(role), [])
    index = body_index if isinstance(body_index, int) and not isinstance(body_index, bool) else 0
    if 0 <= index < len(entries):
        return entries[index]
    return entries[0] if entries else None


def _failed_records(validation: dict[str, Any], family: str) -> list[dict[str, Any]]:
    return [
        record
        for record in _list(validation.get(family))
        if isinstance(record, dict) and record.get("ok") is False
    ]


def _expectation_records(data: dict[str, Any], family: str) -> dict[str, dict[str, Any]]:
    """Index expectation echoes by check id (validation echo first, then recipe)."""

    result: dict[str, dict[str, Any]] = {}
    expectations = _dict(data["validation"].get("expectations"))
    for record in _list(expectations.get(family)):
        if isinstance(record, dict) and _str(record.get("id")):
            result[_str(record["id"])] = record
    recipe_expectations = _dict(data["recipe"].get("expectations"))
    for record in _list(recipe_expectations.get(family)):
        if isinstance(record, dict) and _str(record.get("id")):
            result.setdefault(_str(record["id"]), record)
    return result


def _rule_point_relation_outside_bbox(data: dict[str, Any], evidence: list[str]) -> bool:
    validation = data["validation"]
    failed = _failed_records(validation, "point_relations") + _failed_records(validation, "face_point_relations")
    if not failed:
        return False
    boxes = _role_boxes(data)
    all_boxes = boxes["target"] + boxes["tool"] + boxes["result"]
    if not all_boxes:
        return False
    expectations = _expectation_records(data, "point_relations")
    tol = max(_modeling_tol(data), 1e-6)
    for record in failed:
        point = _point(record.get("point"))
        if point is None:
            point = _point(_dict(expectations.get(_str(record.get("id")))).get("point"))
        if point is None:
            continue
        if not any(_bbox_contains(bbox, point, tol) for bbox in all_boxes):
            _evidence(
                evidence,
                f"rule=point_relation_outside_bbox check={record.get('id')} point={list(point)} "
                f"boxes={len(all_boxes)} tol={tol:g}",
            )
            return True
    return False


def _rule_distance_vs_bbox_gap(data: dict[str, Any], evidence: list[str]) -> bool:
    validation = data["validation"]
    failed = _failed_records(validation, "distance_checks")
    if not failed:
        return False
    boxes = _role_boxes(data)
    tol = max(_modeling_tol(data), 1e-6)
    for record in failed:
        box_a = _role_box(boxes, record.get("role_a"), record.get("body_index_a"))
        box_b = _role_box(boxes, record.get("role_b"), record.get("body_index_b"))
        if box_a is None or box_b is None:
            continue
        gap = _bbox_gap(box_a, box_b)
        distance = _dict(record.get("distance"))
        expected_min = _num(distance.get("min")) if distance.get("min_set") else None
        if expected_min is None:
            continue
        if expected_min <= tol and gap > tol:
            _evidence(
                evidence,
                f"rule=distance_contact_vs_bbox_gap check={record.get('id')} expected_min={expected_min:g} "
                f"bbox_gap={gap:g} tol={tol:g}",
            )
            return True
        if expected_min > tol and gap == 0.0:
            _evidence(
                evidence,
                f"rule=distance_separation_vs_bbox_overlap check={record.get('id')} expected_min={expected_min:g} "
                "bboxes_overlap",
            )
            return True
    return False


def _rule_disjoint_union(data: dict[str, Any], evidence: list[str]) -> bool:
    recipe = data["recipe"]
    manifest = data["manifest"]
    api = _str(recipe.get("api")) or _str(manifest.get("api"))
    options = _dict(manifest.get("options"))
    boolean_type = _str(recipe.get("boolean_type")) or _str(options.get("boolean_type"))
    is_union = api == "api_combine_bodies" or (api == "api_boolean" and boolean_type.upper() in {"UNITE", "UNION"})
    if not is_union:
        return False
    expectations = _dict(recipe.get("expectations"))
    result_bodies = _dict(expectations.get("result_bodies"))
    expected_max = result_bodies.get("max")
    if not (isinstance(expected_max, int) and not isinstance(expected_max, bool)):
        candidate = expectations.get("max_result_bodies")
        expected_max = candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else None
    boxes = _role_boxes(data)
    input_boxes = boxes["target"] + boxes["tool"]
    if expected_max is None or len(input_boxes) < 2 or expected_max >= len(input_boxes):
        return False
    tol = max(_modeling_tol(data), 1e-6)
    for index, first in enumerate(input_boxes):
        for second in input_boxes[index + 1 :]:
            if _bbox_gap(first, second) <= tol:
                return False
    _evidence(
        evidence,
        f"rule=disjoint_union_body_count api={api} expected_max={expected_max} "
        f"input_bodies={len(input_boxes)} tol={tol:g}",
    )
    return True


def _rule_transport(data: dict[str, Any], evidence: list[str]) -> bool:
    comparison = data["comparison"]
    if not comparison:
        return False
    classification = classify_comparison(str(data["case_id"]), comparison)
    if classification.get("cause_class") == "transport_export_suspect":
        for reason in _list(classification.get("reasons"))[:2]:
            _evidence(evidence, f"rule=transport_export_suspect {reason}")
        return True
    sggk = _dict(comparison.get("sggk"))
    sggk_result_nx = _dict(comparison.get("sggk_result_nx"))
    tolerances = _dict(comparison.get("tolerances"))
    if not (sggk.get("self_measurement_ok") is True and sggk_result_nx.get("measurement_ok") is True):
        return False
    abs_tol = _num(tolerances.get("abs_tol")) or 0.0
    rel_tol = _num(tolerances.get("rel_tol")) or 0.0
    pairs = (
        ("area", sggk.get("self_total_area"), sggk_result_nx.get("total_area")),
        ("abs_volume", sggk.get("self_total_abs_volume"), sggk_result_nx.get("total_abs_volume")),
    )
    for name, self_value, nx_value in pairs:
        self_num = _num(self_value)
        nx_num = _num(nx_value)
        if self_num is None or nx_num is None:
            continue
        allowed = abs_tol + rel_tol * max(abs(self_num), abs(nx_num))
        if abs(self_num - nx_num) > allowed:
            _evidence(
                evidence,
                f"rule=self_vs_nx_measurement_drift metric={name} sggk_self={self_num:g} "
                f"nx_measured={nx_num:g} allowed={allowed:g}",
            )
            return True
    return False


def _rule_oracle_inconsistent(data: dict[str, Any], evidence: list[str]) -> bool:
    validation = data["validation"]
    for family in ORACLE_FAMILIES:
        by_id: dict[str, set[str]] = {}
        for record in _list(validation.get(family)):
            if not isinstance(record, dict):
                continue
            check_id = _str(record.get("id"))
            actual = record.get("actual")
            if check_id and actual is not None:
                normalized = json.dumps(actual, sort_keys=True, default=str)
                bucket = by_id.setdefault(check_id, set())
                bucket.add(normalized)
                if len(bucket) > 1:
                    _evidence(
                        evidence,
                        f"rule=oracle_contradictory_actuals family={family} check={check_id}",
                    )
                    return True
            for field in ("actual", "actual_extreme", "probe_coordinate"):
                raw = record.get(field)
                if isinstance(raw, float) and not math.isfinite(raw):
                    _evidence(
                        evidence,
                        f"rule=oracle_non_finite_actual family={family} check={check_id or '?'} field={field}",
                    )
                    return True
            if family == "distance_checks":
                actual_num = _num(actual)
                if actual_num is not None and actual_num < 0:
                    _evidence(
                        evidence,
                        f"rule=oracle_negative_distance check={check_id or '?'} actual={actual_num:g}",
                    )
                    return True
            if family in {"point_relations", "face_point_relations"}:
                if isinstance(actual, str) and actual and actual not in POINT_RELATION_VALUES:
                    _evidence(
                        evidence,
                        f"rule=oracle_unknown_relation_value family={family} check={check_id or '?'} "
                        f"actual={actual}",
                    )
                    return True
    return False


def _rule_geometry_invariants(data: dict[str, Any], evidence: list[str]) -> bool:
    topo_check = data["topo_check"]
    for key in ("bodies", "topologies"):
        for entry in _list(topo_check.get(key)):
            if not isinstance(entry, dict) or entry.get("ok") is not False:
                continue
            error = _str(entry.get("error_string")) or f"error_code={entry.get('error_code')}"
            _evidence(evidence, f"rule=topo_check_failed {error}")
            return True
    return False


def _result(case_id: str, domain: str, confidence: float, evidence: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "fault_domain": domain,
        "confidence": confidence,
        "evidence": evidence[:MAX_EVIDENCE],
        "notes": DOMAIN_NOTES_ZH[domain] + ADVISORY_SUFFIX,
    }


def analyze_case(case_dir: Path) -> dict[str, Any]:
    """Deterministic fault-domain pre-analysis for one case capsule. Never raises."""

    case_dir = Path(case_dir)
    try:
        return _analyze(case_dir)
    except Exception:  # noqa: BLE001 - pre-analysis must never break its caller
        return _result(case_dir.name, "inconclusive", 0.1, ["analysis_internal_error"])


def _analyze(case_dir: Path) -> dict[str, Any]:
    data = _collect(case_dir)
    case_id = str(data["case_id"])
    evidence: list[str] = []
    if _rule_point_relation_outside_bbox(data, evidence):
        return _result(case_id, "test_expectation_suspect", 0.9, evidence)
    if _rule_distance_vs_bbox_gap(data, evidence):
        return _result(case_id, "test_expectation_suspect", 0.85, evidence)
    if _rule_disjoint_union(data, evidence):
        return _result(case_id, "test_expectation_suspect", 0.95, evidence)
    if _rule_transport(data, evidence):
        return _result(case_id, "transport_suspect", 0.8, evidence)
    if _rule_oracle_inconsistent(data, evidence):
        return _result(case_id, "oracle_tooling_suspect", 0.65, evidence)
    if _rule_geometry_invariants(data, evidence):
        return _result(case_id, "geometry_result_suspect", 0.55, evidence)
    validation = data["validation"]
    failures = [_str(item) for item in _list(validation.get("failures")) if _str(item)]
    if validation.get("ok") is False or failures:
        for failure in failures[:4]:
            _evidence(evidence, f"oracle_failure={failure}")
        if not evidence:
            _evidence(evidence, "validation_ok=false")
        return _result(case_id, "geometry_result_suspect", 0.4, evidence)
    _evidence(evidence, "no_deterministic_rule_matched")
    return _result(case_id, "inconclusive", 0.2, evidence)


def merge_visual_hint(analysis: dict[str, Any], hint: str, notes: str) -> dict[str, Any]:
    """Attach an advisory vision fault hint; the deterministic domain never changes."""

    merged = dict(analysis)
    merged["visual_fault_hint"] = hint
    merged["visual_notes"] = " ".join(str(notes).split())[:500]
    expected = HINT_TO_DOMAIN.get(hint)
    merged["visual_disagrees"] = bool(expected and expected != analysis.get("fault_domain"))
    return merged


def render_overlay(case_dir: Path, out_path: Path) -> str:
    """Render the annotated analysis overlay; returns the PNG path or '' on failure."""

    try:
        import render_case_preview

        result = render_case_preview.case_analysis_overlay(Path(case_dir), Path(out_path))
        return str(result.get("preview") or "")
    except Exception:  # noqa: BLE001 - the overlay is best-effort diagnostic evidence
        return ""


def _is_case_dir(path: Path) -> bool:
    return (path / "manifest.json").is_file() and (path / "report").is_dir()


def _is_failed_case(case_dir: Path) -> bool:
    validation = _dict(_load(case_dir / "report" / "validation.json"))
    if validation.get("ok") is False or _list(validation.get("failures")):
        return True
    status = _dict(_load(case_dir / "report" / "status.json"))
    if status.get("succeeded") is False:
        return True
    run_state = _dict(_load(case_dir / "run_state.json"))
    if run_state.get("timed_out") is True:
        return True
    returncode = run_state.get("returncode")
    return isinstance(returncode, int) and not isinstance(returncode, bool) and returncode != 0


def scan_failed_case_dirs(cases_root: Path, limit: int) -> list[Path]:
    cases: set[Path] = set()
    if _is_case_dir(cases_root):
        cases.add(cases_root.resolve())
    for manifest in sorted(cases_root.rglob("manifest.json")):
        case_dir = manifest.parent
        if "_recipes" not in case_dir.parts and _is_case_dir(case_dir):
            cases.add(case_dir.resolve())
    failed = [case_dir for case_dir in sorted(cases, key=lambda item: str(item).lower()) if _is_failed_case(case_dir)]
    return failed[: max(1, limit)]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _safe_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "_.-" else "_" for char in value)
    return safe.strip("._-") or "case"


def analyze_cases_root(
    cases_root: Path,
    out_dir: Path,
    *,
    max_cases: int = DEFAULT_MAX_CASES,
    with_visual: bool = False,
    profile: str = DEFAULT_PROFILE,
    gateway: Any = None,
) -> dict[str, Any]:
    """Analyze every failed case under a root; write per-case pre_analysis.json files."""

    cases_root = Path(cases_root)
    out_dir = Path(out_dir)
    analyses: list[dict[str, Any]] = []
    overlays: list[tuple[str, Path]] = []
    for case_dir in scan_failed_case_dirs(cases_root, max_cases):
        analysis = analyze_case(case_dir)
        case_name = _safe_name(str(analysis.get("case_id") or case_dir.name))
        case_out = out_dir / case_name
        overlay = render_overlay(case_dir, case_out / f"{case_name}_analysis.png")
        if overlay:
            analysis["analysis_png"] = overlay
            overlays.append((str(analysis["case_id"]), Path(overlay)))
        _write_json(case_out / "pre_analysis.json", analysis)
        analyses.append(analysis)

    visual_note = ""
    if with_visual and overlays:
        import run_visual_review

        visual = run_visual_review.run_fault_hint_review(
            overlays[:VISUAL_MAX_CASES],
            out_dir / "visual",
            profile=profile,
            gateway=gateway,
        )
        visual_note = str(visual.get("note") or "")
        hints = visual.get("hints") if isinstance(visual.get("hints"), dict) else {}
        if hints:
            rewritten: list[dict[str, Any]] = []
            for analysis in analyses:
                hint = hints.get(str(analysis.get("case_id")))
                if hint:
                    analysis = merge_visual_hint(
                        analysis,
                        str(hint.get("fault_hint") or ""),
                        str(hint.get("notes") or ""),
                    )
                    _write_json(out_dir / _safe_name(str(analysis["case_id"])) / "pre_analysis.json", analysis)
                rewritten.append(analysis)
            analyses = rewritten

    counts: dict[str, int] = {}
    for analysis in analyses:
        domain = str(analysis.get("fault_domain") or "inconclusive")
        counts[domain] = counts.get(domain, 0) + 1
    summary = {
        "schema_version": SCHEMA_VERSION,
        "kind": "failure_pre_analysis_summary",
        "cases_root": str(cases_root),
        "out_dir": str(out_dir),
        "total_cases": len(analyses),
        "fault_domain_counts": counts,
        "visual": {"requested": with_visual, "note": visual_note},
        "cases": [
            {
                "case_id": str(item.get("case_id") or ""),
                "fault_domain": str(item.get("fault_domain") or ""),
                "confidence": item.get("confidence"),
                "visual_fault_hint": str(item.get("visual_fault_hint") or ""),
                "visual_disagrees": bool(item.get("visual_disagrees")),
            }
            for item in analyses
        ],
    }
    _write_json(out_dir / "pre_analysis_summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", required=True, help="Case capsule root to scan for failed cases")
    parser.add_argument("--out", required=True, help="Output directory for pre_analysis.json + summary")
    parser.add_argument(
        "--with-visual",
        action="store_true",
        help="Also request advisory vision fault hints for the analysis overlays",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="Vision provider profile for --with-visual")
    parser.add_argument(
        "--max-cases",
        type=int,
        default=DEFAULT_MAX_CASES,
        help=f"Maximum failed cases to analyze (default {DEFAULT_MAX_CASES})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, gateway: Any = None) -> int:
    args = parse_args(argv)
    cases_root = Path(args.cases_root).expanduser()
    if not cases_root.is_absolute():
        cases_root = (REPO_ROOT / cases_root).resolve()
    out_dir = Path(args.out).expanduser()
    if not out_dir.is_absolute():
        out_dir = (REPO_ROOT / out_dir).resolve()
    if not cases_root.is_dir():
        print(f"cases root 不存在：{cases_root}")
        return 2
    summary = analyze_cases_root(
        cases_root,
        out_dir,
        max_cases=max(1, int(args.max_cases)),
        with_visual=bool(args.with_visual),
        profile=str(args.profile),
        gateway=gateway,
    )
    counts = summary["fault_domain_counts"]
    parts = "、".join(
        f"{DOMAIN_LABEL_ZH.get(domain, domain)} {count}" for domain, count in sorted(counts.items())
    )
    print(
        f"预分析完成：{summary['total_cases']} 例失败用例；{parts or '无'}"
        f"（诊断性证据，不构成 SDK 缺陷定论）→ {out_dir / 'pre_analysis_summary.json'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
