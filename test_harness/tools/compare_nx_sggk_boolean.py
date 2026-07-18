#!/usr/bin/env python3
"""Classify one boolean case into the five-way cross-kernel verdict.

Both sides are judged by Parasolid measurements so neither kernel is trusted
as the authority:

- the NX boolean journal measures the Parasolid result (volume, area, body
  count, and a free-edge closedness probe);
- the NX STEP measurement journal measures SGGK's exported result STEP with
  the same Parasolid yardstick.

SGGK's self-reported ``properties.json`` is recorded as secondary evidence
only.  Verdicts: ``both_correct``, ``sggk_correct``, ``parasolid_correct``,
``both_wrong``, ``inconclusive``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RESULT_KIND = "nx_sggk_boolean_comparison"
VERDICTS = ("both_correct", "sggk_correct", "parasolid_correct", "both_wrong", "inconclusive")
TOLERANCE_FORMULA = "abs(a-b) <= abs_tol + rel_tol * max(abs(a), abs(b))"


class ComparisonInputError(ValueError):
    """Raised when an input artifact is malformed or unavailable."""


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ComparisonInputError(f"{label} is unavailable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ComparisonInputError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ComparisonInputError(f"{label} JSON root must be an object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _number(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComparisonInputError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        qualifier = "finite nonnegative" if nonnegative else "finite"
        raise ComparisonInputError(f"{label} must be {qualifier}")
    return result


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ComparisonInputError(f"{label} must be a boolean")
    return value


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ComparisonInputError(f"{label} must be a nonnegative integer")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ComparisonInputError(f"{label} must be an object")
    return value


def _numeric_agree(a: float, b: float, abs_tol: float, rel_tol: float) -> dict[str, Any]:
    delta = a - b
    tolerance = abs_tol + rel_tol * max(abs(a), abs(b))
    return {
        "ok": abs(delta) <= tolerance,
        "a": a,
        "b": b,
        "abs_delta": abs(delta),
        "tolerance": tolerance,
    }


def _load_nx_boolean(path: Path) -> dict[str, Any]:
    payload = _load_object(path.resolve(), "NX boolean measurement")
    status = str(payload.get("status") or "")
    import_record = _object(payload.get("import"), "NX boolean.import")
    target_import = _object(import_record.get("target"), "NX boolean.import.target")
    tool_import = _object(import_record.get("tool"), "NX boolean.import.tool")
    boolean = _object(payload.get("boolean"), "NX boolean.boolean")
    measurement = _object(payload.get("measurement"), "NX boolean.measurement")
    bodies = measurement.get("bodies")
    if not isinstance(bodies, list):
        raise ComparisonInputError("NX boolean measurement.bodies must be an array")
    return {
        "status": status,
        "import_ok": _bool(target_import.get("ok"), "NX boolean.import.target.ok")
        and _bool(tool_import.get("ok"), "NX boolean.import.tool.ok"),
        "boolean_ok": _bool(boolean.get("ok"), "NX boolean.boolean.ok"),
        "operation": str(boolean.get("operation") or ""),
        "error_message": str(boolean.get("error_message") or ""),
        "measurement_ok": _bool(measurement.get("ok"), "NX boolean.measurement.ok"),
        "body_count": _int(measurement.get("body_count"), "NX boolean.measurement.body_count"),
        "total_area": _number(measurement.get("total_area"), "NX boolean total_area", nonnegative=True),
        "total_abs_volume": _number(
            measurement.get("total_abs_volume"), "NX boolean total_abs_volume", nonnegative=True
        ),
        "all_solid_closed": _bool(measurement.get("all_solid_closed"), "NX boolean.all_solid_closed"),
        "bodies": bodies,
    }


def _load_nx_sggk_results(paths: list[Path]) -> dict[str, Any]:
    """Aggregate one or more NX STEP measurements of SGGK's exported result bodies."""

    if not paths:
        return {
            "available": False,
            "import_ok": False,
            "measurement_ok": False,
            "body_count": 0,
            "total_area": 0.0,
            "total_abs_volume": 0.0,
            "all_solid_closed": False,
            "bodies": [],
        }
    bodies: list[dict[str, Any]] = []
    measurement_ok = True
    import_ok = True
    for path in paths:
        payload = _load_object(path.resolve(), "NX SGGK-result measurement")
        import_record = _object(payload.get("import"), "NX SGGK-result.import")
        if not _bool(import_record.get("ok"), "NX SGGK-result.import.ok"):
            import_ok = False
        measurement = _object(payload.get("measurement"), "NX SGGK-result.measurement")
        if not _bool(measurement.get("ok"), "NX SGGK-result.measurement.ok"):
            measurement_ok = False
        for raw in measurement.get("bodies") or []:
            body = _object(raw, "NX SGGK-result body")
            bodies.append(
                {
                    "is_solid": _bool(body.get("is_solid"), "NX SGGK-result body.is_solid"),
                    "closed": _bool(body.get("closed"), "NX SGGK-result body.closed"),
                    "free_edge_count": _int(body.get("free_edge_count"), "NX SGGK-result body.free_edge_count"),
                    "measurement_ok": _bool(body.get("measurement_ok"), "NX SGGK-result body.measurement_ok"),
                    "area": _number(body.get("area"), "NX SGGK-result body.area", nonnegative=True),
                    "abs_volume": _number(body.get("abs_volume"), "NX SGGK-result body.abs_volume", nonnegative=True),
                }
            )
    return {
        "available": True,
        "import_ok": import_ok,
        "measurement_ok": measurement_ok and all(item["measurement_ok"] for item in bodies),
        "body_count": len(bodies),
        "total_area": sum(item["area"] for item in bodies),
        "total_abs_volume": sum(item["abs_volume"] for item in bodies),
        "all_solid_closed": bool(bodies) and all(item["closed"] for item in bodies),
        "bodies": bodies,
    }


def _load_sggk_case(case_dir: Path) -> dict[str, Any]:
    case_dir = case_dir.expanduser().resolve()
    status = _load_object(case_dir / "report" / "status.json", "SGGK status")
    properties = _load_object(case_dir / "report" / "properties.json", "SGGK properties")
    bodies = properties.get("bodies")
    if not isinstance(bodies, list):
        raise ComparisonInputError("SGGK properties.bodies must be an array")
    property_ok: list[bool] = []
    total_area = 0.0
    total_abs_volume = 0.0
    for index, raw in enumerate(bodies):
        body = _object(raw, f"SGGK body {index}")
        ok = _bool(body.get("property_ok"), f"SGGK body {index}.property_ok")
        property_ok.append(ok)
        if ok:
            total_area += _number(body.get("area"), f"SGGK body {index}.area", nonnegative=True)
            total_abs_volume += abs(_number(body.get("volume"), f"SGGK body {index}.volume"))
    validation_ok = None
    validation_path = case_dir / "report" / "validation.json"
    if validation_path.is_file():
        validation_ok = _bool(_load_object(validation_path, "SGGK validation").get("ok"), "SGGK validation.ok")
    return {
        "api_ok": _bool(status.get("succeeded"), "SGGK status.succeeded"),
        "result_body_count": _int(status.get("result_body_count"), "SGGK status.result_body_count"),
        "self_measurement_ok": bool(property_ok) and all(property_ok),
        "self_total_area": total_area,
        "self_total_abs_volume": total_abs_volume,
        "validation_ok": validation_ok,
    }


def _valid_closed(measurement: dict[str, Any]) -> bool:
    return bool(
        measurement.get("measurement_ok")
        and measurement.get("body_count", 0) > 0
        and measurement.get("all_solid_closed")
        and measurement.get("total_abs_volume", 0.0) > 0.0
    )


def classify(
    sggk_case: Path,
    nx_boolean: Path,
    nx_sggk_results: list[Path],
    *,
    abs_tol: float = 0.01,
    rel_tol: float = 1e-5,
) -> dict[str, Any]:
    abs_tol = _number(abs_tol, "abs_tol", nonnegative=True)
    rel_tol = _number(rel_tol, "rel_tol", nonnegative=True)
    sggk = _load_sggk_case(sggk_case)
    parasolid = _load_nx_boolean(nx_boolean)
    sggk_result = _load_nx_sggk_results(nx_sggk_results)

    parasolid_available = parasolid["import_ok"]
    parasolid_valid = parasolid["boolean_ok"] and _valid_closed(parasolid)
    sggk_result_measurable = sggk_result["available"] and sggk_result["import_ok"]
    sggk_valid = sggk["api_ok"] and sggk_result_measurable and _valid_closed(sggk_result)

    checks: dict[str, Any] = {}
    if sggk_result_measurable and parasolid["measurement_ok"]:
        checks["volume_agree"] = _numeric_agree(
            sggk_result["total_abs_volume"], parasolid["total_abs_volume"], abs_tol, rel_tol
        )
        checks["area_agree"] = _numeric_agree(
            sggk_result["total_area"], parasolid["total_area"], abs_tol, rel_tol
        )
        checks["body_count_agree"] = {
            "ok": sggk_result["body_count"] == parasolid["body_count"],
            "sggk": sggk_result["body_count"],
            "parasolid": parasolid["body_count"],
        }
    measurements_agree = bool(checks) and all(check["ok"] for check in checks.values())

    reasons: list[str] = []
    if not parasolid_available:
        verdict = "inconclusive"
        reasons.append(
            "Parasolid 无法导入输入 STEP（建模范围或格式限制，例如超大坐标），无法提供布尔参照结果"
        )
    elif not sggk_result["available"]:
        verdict = "inconclusive"
        reasons.append("缺少 SGGK 结果的 Parasolid 测量（result STEP 导出或 NX 测量不可用）")
    elif not sggk_result_measurable:
        if not sggk["api_ok"] and parasolid_valid:
            verdict = "parasolid_correct"
            reasons.append("SGGK API 失败；Parasolid 结果为封闭有效实体")
        else:
            verdict = "inconclusive"
            reasons.append("Parasolid 无法导入 SGGK 的结果 STEP（范围/格式限制），无法独立验证 SGGK 结果")
    elif sggk_valid and parasolid_valid and measurements_agree:
        verdict = "both_correct"
        reasons.append("两内核结果均为封闭实体且 Parasolid 测量互相一致")
    elif sggk_valid and parasolid_valid and not measurements_agree:
        verdict = "inconclusive"
        reasons.append("两内核各自产出封闭实体，但 Parasolid 测量体积/面积/数量不一致，无法判定谁对谁错")
    elif sggk_valid and not parasolid_valid:
        verdict = "sggk_correct"
        reasons.append("SGGK 结果经 Parasolid 测量为封闭有效实体；Parasolid 布尔失败或结果非封闭")
    elif parasolid_valid and not sggk_valid:
        verdict = "parasolid_correct"
        reasons.append("Parasolid 结果为封闭有效实体；SGGK API 失败或结果经 Parasolid 测量非封闭/无效")
    elif not sggk_valid and not parasolid_valid:
        verdict = "both_wrong"
        reasons.append("SGGK 与 Parasolid 均失败或结果均非封闭/无效")
    else:
        verdict = "inconclusive"
        reasons.append("证据不足，无法归类")

    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "verdict": verdict,
        "verdicts": list(VERDICTS),
        "reasons": reasons,
        "tolerances": {"abs_tol": abs_tol, "rel_tol": rel_tol, "formula": TOLERANCE_FORMULA},
        "sggk": sggk,
        "parasolid": parasolid,
        "sggk_result_nx": sggk_result,
        "signals": {
            "sggk_valid": sggk_valid,
            "parasolid_available": parasolid_available,
            "parasolid_valid": parasolid_valid,
            "sggk_result_measurable": sggk_result_measurable,
            "measurements_agree": measurements_agree,
        },
        "checks": checks,
    }
    return result


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


_VERDICT_LABELS = {
    "both_correct": "两者都对（结果一致且封闭）",
    "sggk_correct": "SGGK 更对",
    "parasolid_correct": "Parasolid 更对",
    "both_wrong": "两者都不对",
    "inconclusive": "无法判定",
}


def render_markdown(result: dict[str, Any]) -> str:
    verdict = str(result.get("verdict"))
    parasolid = result["parasolid"]
    sggk_result = result["sggk_result_nx"]
    sggk = result["sggk"]
    lines = [
        "# NX / SGGK 布尔对比报告",
        "",
        f"**结论：{_VERDICT_LABELS.get(verdict, verdict)}（`{verdict}`）**",
        "",
        "## 判定理由",
        "",
    ]
    lines.extend(f"- {reason}" for reason in result.get("reasons", []))
    lines.extend(
        [
            "",
            "## Parasolid 测量对比（同一裁判）",
            "",
            "| 指标 | SGGK 结果（Parasolid 测） | Parasolid 结果 |",
            "|---|---:|---:|",
            f"| Body 数 | {sggk_result['body_count']} | {parasolid['body_count']} |",
            f"| 总面积 mm² | {_fmt(sggk_result['total_area'])} | {_fmt(parasolid['total_area'])} |",
            f"| 总体积 mm³ | {_fmt(sggk_result['total_abs_volume'])} | {_fmt(parasolid['total_abs_volume'])} |",
            f"| 全部封闭 | {_fmt(sggk_result['all_solid_closed'])} | {_fmt(parasolid['all_solid_closed'])} |",
            "",
            "## SGGK 自报（次要证据）",
            "",
            f"- API 成功：{_fmt(sggk['api_ok'])}；结果 Body 数：{sggk['result_body_count']}",
            f"- 自报体积：{_fmt(sggk['self_total_abs_volume'])}；自报面积：{_fmt(sggk['self_total_area'])}",
            f"- SGGK 校验通过：{_fmt(sggk['validation_ok'])}",
            "",
            f"数值判定公式：`{result['tolerances']['formula']}`",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(out_dir: Path, result: dict[str, Any]) -> tuple[Path, Path]:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "comparison.json"
    markdown_path = out_dir / "comparison.zh-CN.md"
    _write_json(json_path, result)
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sggk-case", required=True, type=Path)
    parser.add_argument("--nx-boolean", required=True, type=Path, help="NX boolean measurement JSON")
    parser.add_argument(
        "--nx-sggk-result",
        action="append",
        default=[],
        type=Path,
        help="NX STEP measurement of one SGGK result body; repeat for multiple",
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--abs-tol", type=float, default=0.01)
    parser.add_argument("--rel-tol", type=float, default=1e-5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = classify(
            args.sggk_case,
            args.nx_boolean,
            list(args.nx_sggk_result),
            abs_tol=args.abs_tol,
            rel_tol=args.rel_tol,
        )
        json_path, markdown_path = write_outputs(args.out, result)
    except ComparisonInputError as exc:
        print(f"comparison input error: {exc}", file=sys.stderr)
        return 1
    print(f"verdict={result['verdict']}")
    print(f"comparison_json={json_path}")
    print(f"comparison_markdown={markdown_path}")
    return 0 if result["verdict"] != "inconclusive" else 2


if __name__ == "__main__":
    raise SystemExit(main())
