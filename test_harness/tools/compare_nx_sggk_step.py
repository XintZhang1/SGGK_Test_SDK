#!/usr/bin/env python3
"""Compare one NX STEP measurement with one SGGK ``step_import`` case."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


SCHEMA_VERSION = 1
RESULT_KIND = "nx_sggk_step_comparison"
TOLERANCE_FORMULA = "abs(nx-sggk) <= abs_tol + rel_tol * max(abs(nx), abs(sggk))"
HARNESS_ROOT = Path(__file__).resolve().parents[1]
NX_MEASUREMENT_SCHEMA = HARNESS_ROOT / "schemas" / "nx_step_measurement.schema.json"
COMPARISON_SCHEMA = HARNESS_ROOT / "schemas" / "nx_sggk_step_comparison.schema.json"


class ComparisonInputError(ValueError):
    """Raised when an input artifact is malformed or internally inconsistent."""


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


def _validator(path: Path) -> Draft202012Validator:
    schema = _load_object(path, path.name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate_json(value: Mapping[str, Any], validator: Draft202012Validator, label: str) -> None:
    errors = sorted(validator.iter_errors(dict(value)), key=lambda item: list(item.absolute_path))
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(item) for item in first.absolute_path) or "<root>"
    raise ComparisonInputError(f"{label} schema violation at {location}: {first.message}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ComparisonInputError(f"SGGK copied STEP input is unavailable: {exc}") from exc
    return digest.hexdigest()


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ComparisonInputError(f"{label} must be a boolean")
    return value


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ComparisonInputError(f"{label} must be a nonnegative integer")
    return value


def _number(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComparisonInputError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        qualifier = "finite nonnegative" if nonnegative else "finite"
        raise ComparisonInputError(f"{label} must be {qualifier}")
    return result


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ComparisonInputError(f"{label} must be an object")
    return value


def _load_nx_measurement(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _load_object(path.resolve(), "NX measurement")
    _validate_json(payload, _validator(NX_MEASUREMENT_SCHEMA), "NX measurement")
    input_record = _object(payload["input"], "NX measurement.input")
    digest = str(input_record.get("sha256") or "")
    if len(digest) != 64:
        raise ComparisonInputError("NX measurement must bind a 64-character input SHA-256")
    import_record = _object(payload["import"], "NX measurement.import")
    measurement = _object(payload["measurement"], "NX measurement.measurement")
    bodies = measurement.get("bodies")
    if not isinstance(bodies, list):
        raise ComparisonInputError("NX measurement.measurement.bodies must be an array")
    import_count = _int(import_record.get("body_count"), "NX measurement.import.body_count")
    measurement_count = _int(measurement.get("body_count"), "NX measurement.measurement.body_count")
    if import_count != measurement_count or measurement_count != len(bodies):
        raise ComparisonInputError("NX measurement body counts are internally inconsistent")
    measured_count = sum(
        1
        for index, item in enumerate(bodies)
        if _bool(_object(item, f"NX measurement body {index}").get("measurement_ok"), f"NX body {index}.measurement_ok")
    )
    if measured_count != _int(measurement.get("measured_body_count"), "NX measured_body_count"):
        raise ComparisonInputError("NX measured_body_count is internally inconsistent")
    body_area = sum(
        _number(_object(item, f"NX body {index}").get("area"), f"NX body {index}.area", nonnegative=True)
        for index, item in enumerate(bodies)
    )
    body_volume = sum(
        _number(
            _object(item, f"NX body {index}").get("abs_volume"),
            f"NX body {index}.abs_volume",
            nonnegative=True,
        )
        for index, item in enumerate(bodies)
    )
    total_area = _number(measurement.get("total_area"), "NX total_area", nonnegative=True)
    total_volume = _number(measurement.get("total_abs_volume"), "NX total_abs_volume", nonnegative=True)
    if not math.isclose(body_area, total_area, rel_tol=1e-12, abs_tol=1e-12):
        raise ComparisonInputError("NX total_area does not equal the per-body sum")
    if not math.isclose(body_volume, total_volume, rel_tol=1e-12, abs_tol=1e-12):
        raise ComparisonInputError("NX total_abs_volume does not equal the per-body sum")
    nx_version = _object(payload["nx"], "NX measurement.nx")
    side = {
        "version": str(nx_version.get("full_version") or nx_version.get("version") or ""),
        "import_ok": _bool(import_record.get("ok"), "NX measurement.import.ok"),
        "measurement_ok": _bool(measurement.get("ok"), "NX measurement.measurement.ok"),
        "body_count": measurement_count,
        "total_area": total_area,
        "total_abs_volume": total_volume,
    }
    return {"sha256": digest}, side


def _copied_step(case_dir: Path) -> Path:
    input_dir = case_dir / "input"
    try:
        candidates = [
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.stem.casefold() == "source" and path.suffix.casefold() in {".step", ".stp"}
        ]
    except OSError as exc:
        raise ComparisonInputError(f"SGGK case input directory is unavailable: {exc}") from exc
    if len(candidates) != 1:
        raise ComparisonInputError("SGGK case must contain exactly one input/source.step or input/source.stp")
    return candidates[0]


def _load_sggk_case(case_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    case_dir = case_dir.expanduser().resolve()
    manifest = _load_object(case_dir / "manifest.json", "SGGK manifest")
    if manifest.get("api") != "step_import":
        raise ComparisonInputError("SGGK manifest.api must be step_import")
    status = _load_object(case_dir / "report" / "status.json", "SGGK status")
    exchange = _load_object(case_dir / "report" / "data_exchange.json", "SGGK data exchange report")
    properties = _load_object(case_dir / "report" / "properties.json", "SGGK properties")
    bodies = properties.get("bodies")
    if not isinstance(bodies, list):
        raise ComparisonInputError("SGGK properties.bodies must be an array")
    body_count = _int(status.get("result_body_count"), "SGGK status.result_body_count")
    if len(bodies) != body_count:
        raise ComparisonInputError("SGGK result_body_count does not equal properties.bodies length")
    property_ok: list[bool] = []
    total_area = 0.0
    total_abs_volume = 0.0
    total_shell_count = 0
    shell_count_available = True
    for index, raw in enumerate(bodies):
        body = _object(raw, f"SGGK body {index}")
        summary = _object(body.get("summary"), f"SGGK body {index}.summary")
        if "shells" in summary:
            total_shell_count += _int(summary.get("shells"), f"SGGK body {index}.summary.shells")
        else:
            shell_count_available = False
        ok = _bool(body.get("property_ok"), f"SGGK body {index}.property_ok")
        property_ok.append(ok)
        if ok:
            total_area += _number(body.get("area"), f"SGGK body {index}.area", nonnegative=True)
            volume = _number(body.get("volume"), f"SGGK body {index}.volume")
            total_abs_volume += abs(volume)
    copied_input = _copied_step(case_dir)
    failed_items = _int(exchange.get("failed_item_count"), "SGGK data_exchange.failed_item_count")
    invalid_topology = _int(exchange.get("invalid_topology_count"), "SGGK data_exchange.invalid_topology_count")
    side = {
        "version": str(manifest.get("sggk_version") or ""),
        "import_ok": _bool(status.get("succeeded"), "SGGK status.succeeded")
        and failed_items == 0
        and invalid_topology == 0,
        "measurement_ok": len(property_ok) == body_count and all(property_ok),
        "body_count": body_count,
        "shell_count": total_shell_count if shell_count_available else None,
        "total_area": total_area,
        "total_abs_volume": total_abs_volume,
    }
    return {"sha256": _sha256_file(copied_input)}, side


def _representation_diagnostics(nx: Mapping[str, Any], sggk: Mapping[str, Any]) -> list[dict[str, Any]]:
    nx_body_count = _int(nx.get("body_count"), "NX body_count")
    sggk_body_count = _int(sggk.get("body_count"), "SGGK body_count")
    if nx_body_count == sggk_body_count:
        return []

    raw_shell_count = sggk.get("shell_count")
    shell_count = (
        _int(raw_shell_count, "SGGK shell_count")
        if raw_shell_count is not None
        else None
    )
    if shell_count == nx_body_count:
        return [
            {
                "code": "NX_SGGK_COMPOUND_BODY_SHELL_AGGREGATION",
                "severity": "info",
                "classification": "cross_kernel_representation_difference",
                "nx_body_count": nx_body_count,
                "sggk_body_count": sggk_body_count,
                "sggk_shell_count": shell_count,
                "geometry_bug_confirmed": False,
                "message": (
                    f"NX 将装配表示为 {nx_body_count} 个实体；SGGK 将同一结果聚合为 "
                    f"{sggk_body_count} 个复合 Body，其中共有 {shell_count} 个 shell。"
                    "NX 实体数与 SGGK shell 数一致，严格 Body 数差异属于跨内核表示差异，"
                    "不能据此自动确认几何 bug。"
                ),
            }
        ]
    return [
        {
            "code": "NX_SGGK_BODY_COUNT_REPRESENTATION_UNEXPLAINED",
            "severity": "warning",
            "classification": "unexplained_representation_difference",
            "nx_body_count": nx_body_count,
            "sggk_body_count": sggk_body_count,
            "sggk_shell_count": shell_count,
            "geometry_bug_confirmed": False,
            "message": (
                "NX 与 SGGK 的 Body 数不同，且当前 SGGK shell 汇总不能解释该差异。"
                "严格计数检查仍未通过，但该表示层差异本身不能自动确认几何 bug。"
            ),
        }
    ]


def _numeric_check(nx: float, sggk: float, abs_tol: float, rel_tol: float, *, ready: bool) -> dict[str, Any]:
    delta = nx - sggk
    tolerance = abs_tol + rel_tol * max(abs(nx), abs(sggk))
    return {
        "kind": "abs_rel_numeric",
        "ok": ready and abs(delta) <= tolerance,
        "nx": nx,
        "sggk": sggk,
        "delta": delta,
        "abs_delta": abs(delta),
        "tolerance": tolerance,
        "abs_tol": abs_tol,
        "rel_tol": rel_tol,
    }


def compare(
    nx_measurement: Path,
    sggk_case: Path,
    *,
    abs_tol: float = 0.01,
    rel_tol: float = 1e-5,
) -> dict[str, Any]:
    abs_tol = _number(abs_tol, "abs_tol", nonnegative=True)
    rel_tol = _number(rel_tol, "rel_tol", nonnegative=True)
    nx_input, nx = _load_nx_measurement(nx_measurement)
    sggk_input, sggk = _load_sggk_case(sggk_case)
    same_input = nx_input["sha256"] == sggk_input["sha256"]
    measurements_ready = bool(nx["measurement_ok"] and sggk["measurement_ok"])
    checks: dict[str, dict[str, Any]] = {
        "input_sha256": {
            "kind": "sha256_identity",
            "ok": same_input,
            "nx": nx_input["sha256"],
            "sggk": sggk_input["sha256"],
        },
        "import": {
            "kind": "boolean_required_true",
            "ok": bool(nx["import_ok"] and sggk["import_ok"]),
            "expected": True,
            "nx": nx["import_ok"],
            "sggk": sggk["import_ok"],
        },
        "body_count": {
            "kind": "exact_integer",
            "ok": nx["body_count"] == sggk["body_count"],
            "nx": nx["body_count"],
            "sggk": sggk["body_count"],
            "delta": nx["body_count"] - sggk["body_count"],
        },
        "total_area": _numeric_check(
            nx["total_area"],
            sggk["total_area"],
            abs_tol,
            rel_tol,
            ready=measurements_ready,
        ),
        "total_abs_volume": _numeric_check(
            nx["total_abs_volume"],
            sggk["total_abs_volume"],
            abs_tol,
            rel_tol,
            ready=measurements_ready,
        ),
    }
    failures = [name + "_failed" for name, check in checks.items() if not check["ok"]]
    diagnostics = _representation_diagnostics(nx, sggk)
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "ok": not failures,
        "input": {
            "same_input": same_input,
            "sha256": nx_input["sha256"] if same_input else "",
            "nx_sha256": nx_input["sha256"],
            "sggk_sha256": sggk_input["sha256"],
        },
        "tolerances": {
            "abs_tol": abs_tol,
            "rel_tol": rel_tol,
            "formula": TOLERANCE_FORMULA,
        },
        "nx": nx,
        "sggk": sggk,
        "checks": checks,
        "failures": failures,
        "diagnostics": diagnostics,
    }
    _validate_json(result, _validator(COMPARISON_SCHEMA), "comparison output")
    return result


def _display_number(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


def render_markdown(result: Mapping[str, Any]) -> str:
    checks = _object(result.get("checks"), "comparison.checks")
    rows = [
        ("输入 SHA-256", checks["input_sha256"], "必须完全一致"),
        ("STEP 导入", checks["import"], "NX 与 SGGK 均须成功"),
        ("Body 数量", checks["body_count"], "精确相等"),
        ("总面积 (mm²)", checks["total_area"], f"≤ {_display_number(checks['total_area']['tolerance'])}"),
        (
            "总绝对体积 (mm³)",
            checks["total_abs_volume"],
            f"≤ {_display_number(checks['total_abs_volume']['tolerance'])}",
        ),
    ]
    lines = [
        "# NX / SGGK STEP 对比报告",
        "",
        f"**结论：{'通过' if result.get('ok') else '未通过'}**",
        "",
        f"- 输入 SHA-256：`{result['input']['sha256'] or '不一致'}`",
        f"- NX 版本：`{result['nx']['version'] or '未知'}`",
        f"- SGGK 版本：`{result['sggk']['version'] or '未知'}`",
        f"- 绝对容差：`{_display_number(result['tolerances']['abs_tol'])}`",
        f"- 相对容差：`{_display_number(result['tolerances']['rel_tol'])}`",
        "",
        "| 检查 | NX | SGGK | 判定规则 | 结果 |",
        "|---|---:|---:|---|---|",
    ]
    for label, check, rule in rows:
        lines.append(
            f"| {label} | {_display_number(check['nx'])} | {_display_number(check['sggk'])} "
            f"| {rule} | {'通过' if check['ok'] else '失败'} |"
        )
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        lines.extend(["", "## 表示层解释", ""])
        for raw in diagnostics:
            diagnostic = _object(raw, "comparison diagnostic")
            lines.append(f"- {diagnostic.get('message', '')}")
    if result.get("failures"):
        lines.extend(["", "## 未通过项", ""])
        lines.extend(f"- `{failure}`" for failure in result["failures"])
    lines.extend(
        [
            "",
            "数值判定公式：",
            "",
            f"`{result['tolerances']['formula']}`",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(out_dir: Path, result: Mapping[str, Any]) -> tuple[Path, Path]:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "comparison.json"
    markdown_path = out_dir / "comparison.zh-CN.md"
    json_path.write_text(json.dumps(dict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nx-measurement", required=True, type=Path)
    parser.add_argument("--sggk-case", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="Output directory for JSON and Chinese Markdown")
    parser.add_argument("--abs-tol", type=float, default=0.01)
    parser.add_argument("--rel-tol", type=float, default=1e-5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = compare(
            args.nx_measurement,
            args.sggk_case,
            abs_tol=args.abs_tol,
            rel_tol=args.rel_tol,
        )
        json_path, markdown_path = write_outputs(args.out, result)
    except ComparisonInputError as exc:
        print(f"comparison input error: {exc}", file=sys.stderr)
        return 1
    print(f"comparison_json={json_path}")
    print(f"comparison_markdown={markdown_path}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
