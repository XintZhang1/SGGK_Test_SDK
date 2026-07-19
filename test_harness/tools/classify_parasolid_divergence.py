#!/usr/bin/env python3
"""Deterministic cause classification for NX/Parasolid boolean divergence.

Reads one or more ``comparison.json`` documents produced by
``compare_nx_sggk_boolean.py`` and maps each case to a fixed cause class.
The classification is diagnostic evidence only: it never confirms an SDK
bug, never calls a model, and never trusts a single kernel as the
authority.  Missing or corrupt inputs degrade to ``measurement_unavailable``
instead of raising.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RESULT_KIND = "parasolid_divergence_analysis"
CAUSE_CONSISTENT = "consistent"
CAUSE_CLASSES = (
    CAUSE_CONSISTENT,
    "parasolid_import_limited",
    "transport_export_suspect",
    "non_closed_result",
    "body_count_mismatch",
    "volume_drift",
    "area_drift",
    "divergent_closed_geometry",
    "measurement_unavailable",
    "unclassified_divergence",
)
KNOWN_VERDICTS = {"both_correct", "sggk_correct", "parasolid_correct", "both_wrong", "inconclusive"}
MAX_BODIES_SCANNED = 512
MAX_COMPARISON_BYTES = 8 * 1024 * 1024
MAX_REASONS = 4
MAX_REASON_CHARS = 120


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _load_comparison(path: Path) -> dict[str, Any]:
    """Read one comparison.json with a hard size bound; corrupt data becomes {}."""

    try:
        if path.stat().st_size > MAX_COMPARISON_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def classify_comparison(case_id: str, comparison: Mapping[str, Any]) -> dict[str, Any]:
    """Map one case's comparison.json to a deterministic divergence cause class.

    Rule table (first match wins).  Everything here is additive diagnostic
    metadata; it must never be treated as proof of an SDK defect.
    """

    doc = comparison if isinstance(comparison, Mapping) else {}
    verdict = str(doc.get("verdict") or "")
    signals = _dict(doc.get("signals"))
    sggk = _dict(doc.get("sggk"))
    parasolid = _dict(doc.get("parasolid"))
    sggk_result = _dict(doc.get("sggk_result_nx"))
    checks = _dict(doc.get("checks"))
    tolerances = _dict(doc.get("tolerances"))

    free_edges = 0
    for body in _list(sggk_result.get("bodies"))[:MAX_BODIES_SCANNED]:
        count = _int(_dict(body).get("free_edge_count"))
        if count is not None and count > 0:
            free_edges += count

    volume_check = _dict(checks.get("volume_agree"))
    area_check = _dict(checks.get("area_agree"))
    body_check = _dict(checks.get("body_count_agree"))

    evidence = {
        "sggk_api_ok": _bool(sggk.get("api_ok")),
        "sggk_result_body_count": _int(sggk.get("result_body_count")),
        "parasolid_import_ok": _bool(parasolid.get("import_ok")),
        "parasolid_boolean_ok": _bool(parasolid.get("boolean_ok")),
        "parasolid_measurement_ok": _bool(parasolid.get("measurement_ok")),
        "parasolid_body_count": _int(parasolid.get("body_count")),
        "parasolid_all_solid_closed": _bool(parasolid.get("all_solid_closed")),
        "sggk_result_measurable": _bool(signals.get("sggk_result_measurable")),
        "sggk_result_nx_body_count": _int(sggk_result.get("body_count")),
        "sggk_result_nx_all_solid_closed": _bool(sggk_result.get("all_solid_closed")),
        "sggk_result_nx_free_edges": free_edges,
        "abs_tol": _number(tolerances.get("abs_tol")),
        "rel_tol": _number(tolerances.get("rel_tol")),
        "volume_abs_delta": _number(volume_check.get("abs_delta")),
        "area_abs_delta": _number(area_check.get("abs_delta")),
        "body_count_sggk": _int(body_check.get("sggk")),
        "body_count_parasolid": _int(body_check.get("parasolid")),
    }

    parasolid_available = _bool(signals.get("parasolid_available"))
    if parasolid_available is None:
        parasolid_available = parasolid.get("import_ok") is True
    sggk_api_ok = sggk.get("api_ok") is True
    measurable = _bool(signals.get("sggk_result_measurable"))
    if measurable is None:
        measurable = sggk_result.get("available") is True and sggk_result.get("import_ok") is True
    sggk_valid = signals.get("sggk_valid") is True
    parasolid_valid = signals.get("parasolid_valid") is True

    reasons: list[str] = []
    if verdict not in KNOWN_VERDICTS:
        cause_class = "measurement_unavailable"
        reasons.append("对比记录缺失或不可解析，无法确定判定结论")
    elif verdict == "both_correct":
        cause_class = CAUSE_CONSISTENT
        reasons.append("两内核结果均为封闭实体且 Parasolid 测量一致")
    elif not parasolid_available:
        cause_class = "parasolid_import_limited"
        reasons.append("Parasolid 无法导入输入 STEP（如多体或建模范围限制），缺少参照结果")
        reasons.append("差异不能据此归因于 SGGK")
    elif sggk_api_ok and not measurable:
        cause_class = "transport_export_suspect"
        reasons.append("SGGK 报告成功，但其结果 STEP 无法被 NX 导入测量")
        reasons.append("疑似 result STEP 导出/传输环节问题，而非布尔计算本身")
    elif (
        free_edges > 0
        or ((_int(sggk_result.get("body_count")) or 0) > 0 and sggk_result.get("all_solid_closed") is False)
        or ((_int(parasolid.get("body_count")) or 0) > 0 and parasolid.get("all_solid_closed") is False)
    ):
        cause_class = "non_closed_result"
        reasons.append("测得结果包含自由边或非封闭体，几何未闭合")
    elif body_check and body_check.get("ok") is False:
        cause_class = "body_count_mismatch"
        reasons.append(
            f"Body 数量不一致：SGGK 结果 {body_check.get('sggk')} vs Parasolid {body_check.get('parasolid')}"
        )
    elif volume_check and volume_check.get("ok") is False:
        cause_class = "volume_drift"
        reasons.append(
            f"体积差异超差：abs_delta={volume_check.get('abs_delta')} tolerance={volume_check.get('tolerance')}"
        )
    elif area_check and area_check.get("ok") is False:
        cause_class = "area_drift"
        reasons.append(
            f"面积差异超差：abs_delta={area_check.get('abs_delta')} tolerance={area_check.get('tolerance')}"
        )
    elif verdict == "inconclusive" and sggk_valid and parasolid_valid:
        cause_class = "divergent_closed_geometry"
        reasons.append("两内核各自产出封闭实体，但测量结果不一致，需人工复核")
    elif parasolid.get("measurement_ok") is False or (
        sggk_result.get("available") is True and sggk_result.get("measurement_ok") is False
    ):
        cause_class = "measurement_unavailable"
        reasons.append("测量数据缺失或不可用，无法完成分类")
    else:
        cause_class = "unclassified_divergence"
        reasons.append("已记录差异但无法归入已知类别，需人工复核")

    return {
        "case_id": str(case_id),
        "verdict": verdict,
        "cause_class": cause_class,
        "reasons": [reason[:MAX_REASON_CHARS] for reason in reasons[:MAX_REASONS]],
        "evidence": evidence,
    }


def _iter_case_comparison_dirs(compare_root: Path) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    try:
        children = sorted(compare_root.iterdir(), key=lambda item: item.name)
    except OSError:
        return entries
    for child in children:
        if not child.is_dir():
            continue
        comparison_dir = child / "comparison"
        if comparison_dir.is_dir():
            entries.append((child.name, comparison_dir))
    return entries


def _classify_cases(compare_root: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for case_id, comparison_dir in _iter_case_comparison_dirs(compare_root):
        comparison = _load_comparison(comparison_dir / "comparison.json")
        cases.append(classify_comparison(case_id, comparison))
    return cases


def _attention(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in cases if entry.get("cause_class") != CAUSE_CONSISTENT]


def attention_case_entries(compare_root: Path) -> list[dict[str, Any]]:
    """Return per-case classification entries for every case that is not consistent."""

    return _attention(_classify_cases(Path(compare_root)))


def analyze_compare_root(compare_root: Path) -> dict[str, Any]:
    """Build the full parasolid_analysis.json document for one compare root."""

    compare_root = Path(compare_root)
    cases = _classify_cases(compare_root)
    verdict_counts: dict[str, int] = {}
    cause_class_counts: dict[str, int] = {}
    for entry in cases:
        verdict = str(entry.get("verdict") or "") or "unknown"
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        cause = str(entry.get("cause_class") or "")
        cause_class_counts[cause] = cause_class_counts.get(cause, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "compare_root": str(compare_root),
        "case_count": len(cases),
        "cases": cases,
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "cause_class_counts": dict(sorted(cause_class_counts.items())),
        "attention_cases": _attention(cases),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare-root", required=True, help="Batch compare root containing <case_id>/comparison/")
    parser.add_argument("--out", required=True, help="Output directory for parasolid_analysis.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    compare_root = Path(args.compare_root).expanduser().resolve()
    out_root = Path(args.out).expanduser().resolve()
    analysis = analyze_compare_root(compare_root)
    analysis["attention_cases"] = attention_case_entries(compare_root)
    out_root.mkdir(parents=True, exist_ok=True)
    analysis_path = out_root / "parasolid_analysis.json"
    analysis_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"cases={analysis['case_count']} attention={len(analysis['attention_cases'])} "
        f"causes={json.dumps(analysis['cause_class_counts'], ensure_ascii=False, sort_keys=True)}"
    )
    print(f"analysis={analysis_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
