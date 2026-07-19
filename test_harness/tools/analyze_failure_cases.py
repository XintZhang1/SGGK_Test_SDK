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
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import oracle_text_zh
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
FAULT_MODULES = (
    "distance_oracle",
    "point_relation_oracle",
    "clash_oracle",
    "plane_extreme_oracle",
    "step_import",
    "step_export",
    "api_under_test",
    "test_authoring",
    "unclassified",
)
MAX_EVIDENCE = 8
MAX_EVIDENCE_CHARS = 240
DEFAULT_MAX_CASES = 64
VISUAL_MAX_CASES = 4
DEFAULT_PROFILE = "siliconflow_vision"
RECHECK_MAX_CHECKS = 16
RECHECK_TIMEOUT_SECONDS = 300.0
RECHECK_AGREE_ABS_TOL = 1e-6
RECHECK_AGREE_REL_TOL = 1e-6

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
# 归因模块标签直接复用 oracle_text_zh 的统一映射，保证 UI/数据库/analysis.md 口径一致。
MODULE_LABEL_ZH = oracle_text_zh.FAULT_MODULE_LABEL_ZH
MODULE_NOTES_ZH = {
    "distance_oracle": "SGGK 距离测量与 Parasolid 复核对不上，疑点落在距离测量工具本身",
    "point_relation_oracle": "SGGK 点关系判定与 Parasolid 复核对不上，疑点落在点关系判定工具本身",
    "clash_oracle": "SGGK 干涉判定与 Parasolid 复核对不上，疑点落在干涉判定工具本身",
    "plane_extreme_oracle": "SGGK 平面极值测量与 Parasolid 复核对不上，疑点落在平面极值测量工具本身",
    "step_import": "NX 侧导入输入 STEP 受限（例如超大坐标），疑点落在 STEP 导入环节",
    "step_export": "SGGK 结果 STEP 导入 NX 失败或导入后几何损坏，疑点落在 STEP 导出环节",
    "api_under_test": "几何结果违反不变量，且各测量环节互相印证，疑点落在被测接口本身",
    "test_authoring": "SGGK 测量与 Parasolid 复核一致，但与模型写的预期不符，疑点落在测试预期编写",
    "unclassified": "现有证据无法把疑点收敛到单个模块（含两内核结果分歧但各自自洽的情形），不做强行归因",
}
FAMILY_TO_MODULE = {
    "point_relations": "point_relation_oracle",
    "face_point_relations": "point_relation_oracle",
    "distance_checks": "distance_oracle",
    "clash_checks": "clash_oracle",
    "plane_extreme_checks": "plane_extreme_oracle",
}
# 输入坐标量级超过该阈值时，NX 侧导入输入 STEP 视为受限（大坐标导入限制）。
NX_IMPORT_COORD_LIMIT = 1.0e5
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


# ---------------------------------------------------------------------------
# Parasolid 复核（模块级归因）
# ---------------------------------------------------------------------------

_NX_AVAILABLE: bool | None = None


def _nx_recheck_available() -> bool:
    """Static NX discovery, cached per process; never launches anything."""

    global _NX_AVAILABLE
    if _NX_AVAILABLE is None:
        try:
            from test_harness.nx import inspect_nx_environment

            selected = inspect_nx_environment().selected
            _NX_AVAILABLE = bool(selected is not None and selected.run_journal_path is not None)
        except Exception:  # noqa: BLE001 - detection must never break pre-analysis
            _NX_AVAILABLE = False
    return _NX_AVAILABLE


def _locate_case_steps(case_dir: Path, case_id: str) -> dict[str, Any]:
    """Find this case's parasolid_compare STEP exports via its export manifest.

    Walks ancestors of the case capsule looking for
    ``parasolid_compare/<case_id>/export/export_manifest.json`` whose recorded
    case_dir matches, never above the repository root.  Returns role paths
    (target/tool single files, result directory) or empty strings.
    """

    located = {"target": "", "tool": "", "result_dir": ""}
    try:
        resolved_case = case_dir.resolve()
        root = REPO_ROOT.resolve()
    except OSError:
        return located
    for parent in resolved_case.parents:
        if parent == root.parent:
            break
        export_dir = parent / "parasolid_compare" / case_id / "export"
        manifest_path = export_dir / "export_manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = _dict(_load(manifest_path))
        recorded = _str(manifest.get("case_dir"))
        try:
            if recorded and Path(recorded).resolve() != resolved_case:
                continue
        except OSError:
            continue
        exports = _dict(manifest.get("exports"))
        for role in ("target", "tool"):
            step = _str(_dict(exports.get(role)).get("step"))
            if step and Path(step).is_file():
                located[role] = str(Path(step))
        result_steps = [
            _str(_dict(exports.get(key)).get("step"))
            for key in sorted(exports)
            if str(key).startswith("result") and _str(_dict(exports.get(key)).get("step"))
        ]
        if result_steps and all(Path(step).is_file() for step in result_steps):
            located["result_dir"] = str(Path(result_steps[0]).parent)
        if located["target"] or located["tool"] or located["result_dir"]:
            return located
    return located


def _int_field(record: dict[str, Any], key: str) -> int:
    value = record.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _build_recheck_checks(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Map failed validation records to recheck spec checks (bounded)."""

    validation = data["validation"]
    checks: list[dict[str, Any]] = []
    for family in ("point_relations", "face_point_relations"):
        for record in _failed_records(validation, family):
            point = _point(record.get("point"))
            if point is None:
                expectations = _expectation_records(data, family)
                point = _point(_dict(expectations.get(_str(record.get("id")))).get("point"))
            if point is None:
                continue
            checks.append(
                {
                    "id": _str(record.get("id")),
                    "family": family,
                    "kind": "point_relation",
                    "role": _str(record.get("role")),
                    "body_index": _int_field(record, "body_index"),
                    "point": list(point),
                    "tolerance": _num(record.get("tolerance")) or 1e-3,
                    "oracle_actual": _str(record.get("actual")),
                    "expected": _str(record.get("expected")),
                }
            )
    for record in _failed_records(validation, "distance_checks"):
        expectation = _dict(record.get("expectation"))
        checks.append(
            {
                "id": _str(record.get("id")),
                "family": "distance_checks",
                "kind": "distance",
                "role_a": _str(record.get("role_a")),
                "role_b": _str(record.get("role_b")),
                "body_index_a": _int_field(record, "body_index_a"),
                "body_index_b": _int_field(record, "body_index_b"),
                "oracle_actual": _num(record.get("actual")),
                "expectation": {
                    "min": _num(expectation.get("min")) if expectation.get("min_set") else None,
                    "max": _num(expectation.get("max")) if expectation.get("max_set") else None,
                    "abs_tol": _num(expectation.get("abs_tol")),
                    "rel_tol": _num(expectation.get("rel_tol")),
                },
            }
        )
    for record in _failed_records(validation, "clash_checks"):
        checks.append(
            {
                "id": _str(record.get("id")),
                "family": "clash_checks",
                "kind": "clash",
                "role_a": _str(record.get("role_a")),
                "role_b": _str(record.get("role_b")),
                "body_index_a": _int_field(record, "body_index_a"),
                "body_index_b": _int_field(record, "body_index_b"),
                "tolerance": _num(record.get("tolerance")) or 1e-3,
                "oracle_actual": _str(record.get("actual")),
                "expected": _str(record.get("expected")),
            }
        )
    for record in _failed_records(validation, "plane_extreme_checks"):
        checks.append(
            {
                "id": _str(record.get("id")),
                "family": "plane_extreme_checks",
                "kind": "plane_extreme",
                "role": _str(record.get("role")),
                "body_index": _int_field(record, "body_index"),
                "axis": _str(record.get("axis")),
                "side": _str(record.get("side")),
                "oracle_actual": _num(record.get("actual_extreme")),
                "expected": _num(record.get("expected")),
                "tolerance": _num(record.get("tolerance")),
            }
        )
    return checks[:RECHECK_MAX_CHECKS]


def _values_agree(oracle_actual: float | None, nx_actual: float | None, abs_tol: float | None) -> bool:
    if oracle_actual is None or nx_actual is None:
        return False
    allowed = max(abs_tol or 0.0, RECHECK_AGREE_ABS_TOL)
    allowed = max(allowed, RECHECK_AGREE_REL_TOL * max(abs(oracle_actual), abs(nx_actual)))
    return abs(oracle_actual - nx_actual) <= allowed


def _nx_satisfies_distance(expectation: dict[str, Any], nx_actual: float) -> bool:
    abs_tol = _num(expectation.get("abs_tol")) or 0.0
    lower = _num(expectation.get("min"))
    upper = _num(expectation.get("max"))
    if lower is not None and nx_actual < lower - abs_tol:
        return False
    if upper is not None and nx_actual > upper + abs_tol:
        return False
    return True


def _classify_recheck(check: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Combine one spec check with its journal record into attribution evidence."""

    family = str(check.get("family") or "")
    entry: dict[str, Any] = {
        "id": str(check.get("id") or ""),
        "family": family,
        "kind": str(check.get("kind") or ""),
        "status": str(record.get("status") or "error"),
        "oracle_actual": check.get("oracle_actual"),
        "nx_actual": record.get("actual"),
        "relation": "unmeasured",
    }
    if entry["status"] != "measured":
        return entry
    kind = entry["kind"]
    if kind in {"point_relation", "clash"}:
        agree = str(check.get("oracle_actual") or "") == str(record.get("actual") or "")
        entry["relation"] = "agree" if agree else "disagree"
        entry["nx_satisfies_expectation"] = str(record.get("actual") or "") == str(check.get("expected") or "")
    elif kind == "distance":
        nx_actual = _num(record.get("actual"))
        oracle_actual = _num(check.get("oracle_actual"))
        expectation = _dict(check.get("expectation"))
        abs_tol = _num(expectation.get("abs_tol"))
        entry["relation"] = "agree" if _values_agree(oracle_actual, nx_actual, abs_tol) else "disagree"
        entry["nx_satisfies_expectation"] = (
            _nx_satisfies_distance(expectation, nx_actual) if nx_actual is not None else None
        )
    elif kind == "plane_extreme":
        nx_actual = _num(record.get("actual"))
        oracle_actual = _num(check.get("oracle_actual"))
        tolerance = _num(check.get("tolerance"))
        entry["relation"] = "agree" if _values_agree(oracle_actual, nx_actual, tolerance) else "disagree"
        expected = _num(check.get("expected"))
        entry["nx_satisfies_expectation"] = (
            abs(nx_actual - expected) <= max(tolerance or 0.0, RECHECK_AGREE_ABS_TOL)
            if nx_actual is not None and expected is not None
            else None
        )
    return entry


def _default_recheck_runner(
    case_dir: Path,
    case_id: str,
    checks: list[dict[str, Any]],
    steps: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """Run the oracle_recheck journal through the fixed NX journal channel."""

    if not _nx_recheck_available():
        return {"ran": False, "note": "NX 不可用，跳过 Parasolid 复核", "checks": []}
    journal = REPO_ROOT / "test_harness" / "nx_journals" / "oracle_recheck.py"
    work_dir = Path(tempfile.mkdtemp(prefix=f"sggk_recheck_{case_id[:24]}_"))
    try:
        keep_keys = {"family", "oracle_actual", "expected", "expectation"}
        spec_checks = [{key: value for key, value in check.items() if key not in keep_keys} for check in checks]
        spec_path = work_dir / "recheck_spec.json"
        out_path = work_dir / "recheck_out.json"
        spec_path.write_text(
            json.dumps(
                {"schema_version": 1, "kind": "sggk_nx_oracle_recheck_request", "checks": spec_checks},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        from test_harness.nx import execute_nx_journal

        report = execute_nx_journal(
            journal,
            allowed_roots=[journal.parent],
            arguments=[
                steps.get("target") or "-",
                steps.get("tool") or "-",
                steps.get("result_dir") or "-",
                str(spec_path),
                str(out_path),
            ],
            timeout_seconds=timeout,
        )
        if not isinstance(report, dict) or report.get("ok") is not True:
            status = _str(report.get("status")) if isinstance(report, dict) else ""
            return {"ran": False, "note": f"Parasolid 复核执行失败（{status or '未知原因'}）", "checks": []}
        payload = _dict(_load(out_path))
        records = {str(item.get("id") or ""): item for item in _list(payload.get("checks")) if isinstance(item, dict)}
        results = []
        for check in checks:
            record = records.get(str(check.get("id") or ""))
            if record is None:
                continue
            results.append(_classify_recheck(check, record))
        return {"ran": True, "note": "", "checks": results}
    except Exception as exc:  # noqa: BLE001 - the recheck is best-effort evidence
        return {"ran": False, "note": f"Parasolid 复核执行失败（{exc}）"[:200], "checks": []}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_oracle_recheck(
    case_dir: Path,
    case_id: str,
    checks: list[dict[str, Any]],
    *,
    runner: Any = None,
    timeout: float = RECHECK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Best-effort Parasolid recheck for failed oracle checks. Never raises."""

    try:
        if not checks:
            return {"ran": False, "note": "无可复核的失败校验项", "checks": []}
        steps = _locate_case_steps(Path(case_dir), case_id)
        needed_roles = set()
        for check in checks:
            for key in ("role", "role_a", "role_b"):
                role = _str(check.get(key))
                if role in {"target", "tool", "result"}:
                    needed_roles.add(role)
        missing = sorted(
            role
            for role in needed_roles
            if not (steps.get("result_dir") if role == "result" else steps.get(role))
        )
        if missing:
            return {
                "ran": False,
                "note": f"缺少 {('/'.join(missing))} 的 STEP 传输文件，跳过 Parasolid 复核",
                "checks": [],
            }
        executor = runner if runner is not None else _default_recheck_runner
        outcome = executor(Path(case_dir), case_id, checks, steps, timeout)
        if isinstance(outcome, dict) and "ran" in outcome:
            return outcome
        return {"ran": False, "note": "Parasolid 复核返回无效结果", "checks": []}
    except Exception as exc:  # noqa: BLE001 - the recheck is best-effort evidence
        return {"ran": False, "note": f"Parasolid 复核执行失败（{exc}）"[:200], "checks": []}


def _has_huge_input_coordinates(data: dict[str, Any]) -> bool:
    """True when any input bbox coordinate exceeds the NX STEP import comfort range."""

    boxes = _role_boxes(data)
    for bbox in boxes["target"] + boxes["tool"]:
        for key in ("min", "max"):
            point = bbox.get(key)
            if not isinstance(point, list):
                continue
            for coord in point:
                number = _num(coord)
                if number is not None and abs(number) > NX_IMPORT_COORD_LIMIT:
                    return True
    return False


def _attribute_fault_module(
    data: dict[str, Any],
    domain: str,
    domain_evidence: list[str],
    recheck: dict[str, Any],
) -> tuple[str, list[str]]:
    """Three-way deterministic attribution to a concrete harness/SDK module."""

    evidence: list[str] = []
    comparison = data["comparison"]
    cause_class = ""
    if comparison:
        cause_class = _str(classify_comparison(str(data["case_id"]), comparison).get("cause_class"))

    # 模型预期在几何上不可能/自相矛盾 → 测试编写问题。
    if domain == "test_expectation_suspect":
        _evidence(evidence, "module_rule=expectation_geometrically_impossible")
        return "test_authoring", evidence

    # 传输环节证据：NX 侧导入输入 STEP 受限 → step_import；其余传输漂移 → step_export。
    if domain == "transport_suspect":
        if cause_class == "parasolid_import_limited":
            _evidence(evidence, f"module_rule=nx_input_step_import_limited cause_class={cause_class}")
            return "step_import", evidence
        if _has_huge_input_coordinates(data):
            _evidence(evidence, f"module_rule=input_step_huge_coordinates limit={NX_IMPORT_COORD_LIMIT:g}")
            return "step_import", evidence
        _evidence(evidence, f"module_rule=result_step_transport_drift cause_class={cause_class or 'n/a'}")
        return "step_export", evidence

    # 两内核几何结果分歧但 Parasolid 内部证据自洽：不强行归因到任何模块。
    if cause_class == "divergent_closed_geometry":
        _evidence(evidence, "module_rule=cross_kernel_divergence_self_consistent")
        return "unclassified", evidence

    # Parasolid 复核三方对照：oracle 实测 vs NX 复核 vs 模型预期。
    oracle_votes: dict[str, int] = {}
    authoring_votes = 0
    if recheck.get("ran"):
        for item in _list(recheck.get("checks")):
            if not isinstance(item, dict) or item.get("relation") not in {"agree", "disagree"}:
                continue
            module = FAMILY_TO_MODULE.get(_str(item.get("family")))
            relation = _str(item.get("relation"))
            check_id = _str(item.get("id")) or "?"
            if relation == "disagree" and module:
                oracle_votes[module] = oracle_votes.get(module, 0) + 1
                _evidence(
                    evidence,
                    f"module_rule=oracle_vs_nx_recheck check={check_id} "
                    f"sggk={item.get('oracle_actual')} nx={item.get('nx_actual')}",
                )
            elif relation == "agree":
                satisfies = item.get("nx_satisfies_expectation")
                if satisfies is False:
                    authoring_votes += 1
                    _evidence(
                        evidence,
                        f"module_rule=oracle_and_nx_agree_vs_expectation check={check_id}",
                    )
                elif satisfies is True and module:
                    # 两侧测量一致且满足预期，SGGK oracle 却判失败：判分逻辑可疑。
                    oracle_votes[module] = oracle_votes.get(module, 0) + 1
                    _evidence(
                        evidence,
                        f"module_rule=oracle_verdict_contradicts_recheck check={check_id}",
                    )
    if oracle_votes:
        ranked = sorted(
            oracle_votes.items(),
            key=lambda item: (-item[1], FAULT_MODULES.index(item[0])),
        )
        return ranked[0][0], evidence
    if authoring_votes:
        return "test_authoring", evidence

    # oracle 记录自相矛盾（无 NX 复核也可判定）：按涉及的校验族归因。
    if domain == "oracle_tooling_suspect":
        for family in ORACLE_FAMILIES:
            module = FAMILY_TO_MODULE.get(family)
            if module and any(f"family={family}" in line for line in domain_evidence):
                _evidence(evidence, f"module_rule=oracle_internal_inconsistency family={family}")
                return module, evidence
        _evidence(evidence, "module_rule=oracle_internal_inconsistency family=unknown")
        return "unclassified", evidence

    # 几何不变量被违反，且没有任何工具/传输/预期侧证据 → 被测接口。
    if domain == "geometry_result_suspect":
        _evidence(evidence, "module_rule=geometry_invariant_violated_others_cleared")
        return "api_under_test", evidence

    _evidence(evidence, "module_rule=insufficient_module_evidence")
    return "unclassified", evidence


def analyze_case(case_dir: Path, *, recheck_runner: Any = None, skip_recheck: bool = False) -> dict[str, Any]:
    """Deterministic fault-domain pre-analysis for one case capsule. Never raises."""

    case_dir = Path(case_dir)
    try:
        return _analyze(case_dir, recheck_runner=recheck_runner, skip_recheck=skip_recheck)
    except Exception:  # noqa: BLE001 - pre-analysis must never break its caller
        result = _result(case_dir.name, "inconclusive", 0.1, ["analysis_internal_error"])
        result["fault_module"] = "unclassified"
        return result


def _attach_fault_module(
    result: dict[str, Any],
    data: dict[str, Any],
    domain: str,
    domain_evidence: list[str],
    recheck: dict[str, Any],
) -> dict[str, Any]:
    module, module_evidence = _attribute_fault_module(data, domain, domain_evidence, recheck)
    result["fault_module"] = module
    merged = list(result["evidence"])
    for line in module_evidence:
        if len(merged) < MAX_EVIDENCE and line not in merged:
            merged.append(line)
    result["evidence"] = merged[:MAX_EVIDENCE]
    module_label = MODULE_LABEL_ZH.get(module, module)
    result["notes"] = (
        f"归因模块：{module_label}（{module}）——{MODULE_NOTES_ZH[module]}；"
        f"{DOMAIN_NOTES_ZH[domain]}{ADVISORY_SUFFIX}"
    )
    recheck_note = _str(recheck.get("note"))
    if recheck.get("ran") or recheck_note:
        result["recheck"] = {
            "ran": bool(recheck.get("ran")),
            "note": recheck_note,
            "checks": [
                {
                    "id": _str(item.get("id")),
                    "kind": _str(item.get("kind")),
                    "status": _str(item.get("status")),
                    "oracle_actual": item.get("oracle_actual"),
                    "nx_actual": item.get("nx_actual"),
                    "relation": _str(item.get("relation")),
                    "nx_satisfies_expectation": item.get("nx_satisfies_expectation"),
                }
                for item in _list(recheck.get("checks"))[:RECHECK_MAX_CHECKS]
                if isinstance(item, dict)
            ],
        }
    priority, priority_reason = compute_priority(result)
    result["priority"] = priority
    result["priority_reason_zh"] = priority_reason
    return result


def _analyze(case_dir: Path, *, recheck_runner: Any = None, skip_recheck: bool = False) -> dict[str, Any]:
    data = _collect(case_dir)
    case_id = str(data["case_id"])
    evidence: list[str] = []
    domain = "inconclusive"
    confidence = 0.2
    if _rule_point_relation_outside_bbox(data, evidence):
        domain, confidence = "test_expectation_suspect", 0.9
    elif _rule_distance_vs_bbox_gap(data, evidence):
        domain, confidence = "test_expectation_suspect", 0.85
    elif _rule_disjoint_union(data, evidence):
        domain, confidence = "test_expectation_suspect", 0.95
    elif _rule_transport(data, evidence):
        domain, confidence = "transport_suspect", 0.8
    elif _rule_oracle_inconsistent(data, evidence):
        domain, confidence = "oracle_tooling_suspect", 0.65
    elif _rule_geometry_invariants(data, evidence):
        domain, confidence = "geometry_result_suspect", 0.55
    else:
        validation = data["validation"]
        failures = [_str(item) for item in _list(validation.get("failures")) if _str(item)]
        if validation.get("ok") is False or failures:
            for failure in failures[:4]:
                _evidence(evidence, f"oracle_failure={failure}")
            if not evidence:
                _evidence(evidence, "validation_ok=false")
            domain, confidence = "geometry_result_suspect", 0.4
        else:
            _evidence(evidence, "no_deterministic_rule_matched")
    result = _result(case_id, domain, confidence, evidence)
    if skip_recheck:
        recheck: dict[str, Any] = {"ran": False, "note": "", "checks": []}
    else:
        recheck = run_oracle_recheck(case_dir, case_id, _build_recheck_checks(data), runner=recheck_runner)
    return _attach_fault_module(result, data, domain, evidence, recheck)


def compute_priority(analysis: dict[str, Any]) -> tuple[str, str]:
    """Rate how urgently a failed case needs kernel attention.

    Per the review requirement: when the SGGK result agrees with Parasolid at
    topology/geometry level AND the vision review does not question the
    geometry, the likelihood of a kernel defect is low and the case is
    explicitly de-prioritized so the list is not misread as "half broken".
    """

    module = str(analysis.get("fault_module") or "unclassified")
    parasolid = analysis.get("parasolid") if isinstance(analysis.get("parasolid"), dict) else {}
    verdict = str(parasolid.get("verdict") or "")
    parasolid_consistent = verdict in {"both_correct", "sggk_correct"}
    hint = str(analysis.get("visual_fault_hint") or "")
    visual_consistent = hint not in {"geometry"}
    if parasolid_consistent and visual_consistent and module in {"test_authoring", "unclassified"}:
        return (
            "low",
            "SGGK 结果与 Parasolid 在拓扑几何层面比对一致，视觉复核未质疑几何结果；"
            "按低优先级处理（疑似非内核缺陷，仅供复核）。",
        )
    return "high", "存在未排除的分歧证据，需要进一步分析。"


def merge_visual_hint(analysis: dict[str, Any], hint: str, notes: str) -> dict[str, Any]:
    """Attach an advisory vision fault hint; the deterministic domain never changes."""

    merged = dict(analysis)
    merged["visual_fault_hint"] = hint
    merged["visual_notes"] = " ".join(str(notes).split())[:500]
    expected = HINT_TO_DOMAIN.get(hint)
    merged["visual_disagrees"] = bool(expected and expected != analysis.get("fault_domain"))
    priority, priority_reason = compute_priority(merged)
    merged["priority"] = priority
    merged["priority_reason_zh"] = priority_reason
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
    skip_recheck: bool = False,
) -> dict[str, Any]:
    """Analyze every failed case under a root; write per-case pre_analysis.json files."""

    cases_root = Path(cases_root)
    out_dir = Path(out_dir)
    analyses: list[dict[str, Any]] = []
    overlays: list[tuple[str, Path]] = []
    for case_dir in scan_failed_case_dirs(cases_root, max_cases):
        analysis = analyze_case(case_dir, skip_recheck=skip_recheck)
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
    module_counts: dict[str, int] = {}
    priority_counts: dict[str, int] = {}
    for analysis in analyses:
        domain = str(analysis.get("fault_domain") or "inconclusive")
        counts[domain] = counts.get(domain, 0) + 1
        module = str(analysis.get("fault_module") or "unclassified")
        module_counts[module] = module_counts.get(module, 0) + 1
        priority = str(analysis.get("priority") or "high")
        priority_counts[priority] = priority_counts.get(priority, 0) + 1
    summary = {
        "schema_version": SCHEMA_VERSION,
        "kind": "failure_pre_analysis_summary",
        "cases_root": str(cases_root),
        "out_dir": str(out_dir),
        "total_cases": len(analyses),
        "fault_domain_counts": counts,
        "fault_module_counts": module_counts,
        "priority_counts": priority_counts,
        "visual": {"requested": with_visual, "note": visual_note},
        "cases": [
            {
                "case_id": str(item.get("case_id") or ""),
                "fault_domain": str(item.get("fault_domain") or ""),
                "fault_module": str(item.get("fault_module") or ""),
                "confidence": item.get("confidence"),
                "priority": str(item.get("priority") or "high"),
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
    parser.add_argument(
        "--skip-recheck",
        action="store_true",
        help="Do not run the Parasolid oracle recheck (fault_module falls back to deterministic evidence only)",
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
        skip_recheck=bool(args.skip_recheck),
    )
    counts = summary["fault_domain_counts"]
    module_counts = summary["fault_module_counts"]
    parts = "、".join(
        f"{DOMAIN_LABEL_ZH.get(domain, domain)} {count}" for domain, count in sorted(counts.items())
    )
    module_parts = "、".join(
        f"{MODULE_LABEL_ZH.get(module, module)} {count}" for module, count in sorted(module_counts.items())
    )
    print(
        f"预分析完成：{summary['total_cases']} 例失败用例；{parts or '无'}；归因模块：{module_parts or '无'}"
        f"（诊断性证据，不构成 SDK 缺陷定论）→ {out_dir / 'pre_analysis_summary.json'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
