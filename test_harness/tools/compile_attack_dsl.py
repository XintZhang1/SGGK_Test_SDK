#!/usr/bin/env python3
"""Compile parameterized SGGK attack DSL files into flat runner recipes."""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import math
import re
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_capabilities import load_capabilities, supported_body_builders
from validate_recipe import allowed_recipe_keys, diagnostic_from_validation_error, validate_recipe

CAPABILITIES = load_capabilities()
SUPPORTED_BODY_BUILDERS = set(supported_body_builders(CAPABILITIES))

NUMERIC_BODY_FIELDS = {
    "radius",
    "height",
    "angle",
    "length",
    "width",
    "bottom_radius",
    "top_radius",
    "inner_radius",
    "outer_radius",
    "long_radius",
    "short_radius",
    "profile_radius",
    "path_radius",
    "secondary_height",
    "min_dist",
    "max_dist",
    "operation_tol",
    "g1_tol",
    "scale",
    "translate_x",
    "translate_y",
    "translate_z",
    "secondary_translate_x",
    "secondary_translate_y",
    "secondary_translate_z",
    "body_index",
}
STRING_BODY_FIELDS = {"kind", "boolean_type", "source_file"}
BOOL_BODY_FIELDS = {"create_seam_edge", "allow_partial_success"}
OPTION_FIELDS = {
    "api",
    "boolean_type",
    "modeling_tol",
    "max_model_size",
    "check_valid",
    "topo_track",
    "non_destructive",
    "source_body_index",
    "body_index",
    "step_app_protocol",
    "step_surface_to_bspline",
    "step_curve_to_bspline",
    "step_spcurve_in_wire_to_bspline",
    "iges_face_only_mode",
    "iges_write_sgk_specified_data",
    "roundtrip_abs_tol",
    "roundtrip_rel_tol",
}
STRING_OPTION_FIELDS = {"api", "boolean_type", "step_app_protocol"}
NUMERIC_OPTION_FIELDS = {
    "modeling_tol",
    "max_model_size",
    "source_body_index",
    "body_index",
    "roundtrip_abs_tol",
    "roundtrip_rel_tol",
}
BOOL_OPTION_FIELDS = {
    "check_valid",
    "topo_track",
    "non_destructive",
    "step_surface_to_bspline",
    "step_curve_to_bspline",
    "step_spcurve_in_wire_to_bspline",
    "iges_face_only_mode",
    "iges_write_sgk_specified_data",
}
CHAIN_META_FIELDS = {"op", "tool", "profile"}
EXPECTATION_BOOL_FIELDS = {
    "require_property_calculations",
    "require_finite_properties",
    "require_nonnegative_length_area",
    "require_nonnegative_volume",
    "boolean_volume_relation",
    "boolean_bbox_relation",
    "sample_input_properties",
}
EXPECTATION_INT_FIELDS = {"min_result_bodies", "max_result_bodies"}
EXPECTATION_NUMERIC_FIELDS = {"volume_relation_abs_tol", "volume_relation_rel_tol"}
EXPECTATION_METRICS = {"total_length", "total_area", "total_volume", "total_abs_volume"}
POINT_RELATION_NUMERIC_FIELDS = {"tolerance"}
POINT_RELATION_INT_FIELDS = {"body_index"}
FACE_POINT_RELATION_NUMERIC_FIELDS = {"tolerance"}
FACE_POINT_RELATION_INT_FIELDS = {"body_index", "face_index", "face_id"}
CLASH_CHECK_NUMERIC_FIELDS = {"tolerance"}
CLASH_CHECK_INT_FIELDS = {"body_index_a", "body_index_b"}
DISTANCE_CHECK_NUMERIC_FIELDS = {"threshold", "min", "max", "expected", "abs_tol", "rel_tol"}
DISTANCE_CHECK_INT_FIELDS = {"body_index_a", "body_index_b"}
PLANE_EXTREME_NUMERIC_FIELDS = {"expected", "tolerance", "probe_coordinate", "plane_span", "plane_span_scale"}
PLANE_EXTREME_INT_FIELDS = {"body_index"}
PROVENANCE_FIELDS = {
    "hypothesis",
    "source",
    "source_ref",
    "notes",
    "source_task_id",
    "source_task_path",
    "source_risk_id",
    "source_risk_family",
    "source_risk_categories",
}


class DslError(ValueError):
    pass


def ensure_supported_body_builder(kind: Any, path: str) -> None:
    if not isinstance(kind, str) or not kind:
        return
    if kind not in SUPPORTED_BODY_BUILDERS:
        raise DslError(
            f"{path}: unsupported body builder {kind!r}; "
            "add it to interface_capabilities.json and fixed compiler/runner support, or return needs_harness_extension"
        )


@dataclass(frozen=True)
class Expansion:
    suffix: str
    patch: dict[str, Any]


VECTOR_PATCH_LEAVES = {
    "axis",
    "center",
    "control_points",
    "direction",
    "point",
    "points",
    "secondary_translate",
    "translate",
    "uv",
    "uv_fraction",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="DSL JSON file(s) or directories")
    parser.add_argument("--out", help="Directory for compiled recipe JSON files")
    parser.add_argument("--check", action="store_true", help="Validate DSL expansion without writing compiled recipes")
    parser.add_argument("--no-validate", action="store_true", help="Skip validating compiled recipes")
    parser.add_argument(
        "--model-asset-policy",
        action="store_true",
        help="Restrict compiled source_file values to fixed repository asset roots",
    )
    parser.add_argument("--report", default="", help="Optional JSON report path for check/compile results")
    parser.add_argument("--model-diagnostics", default="", help="Optional model-friendly diagnostics JSON path")
    args = parser.parse_args()
    if args.check and args.out:
        parser.error("--out cannot be used with --check")
    if not args.check and not args.out:
        parser.error("--out is required unless --check is passed")
    return args


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def iter_dsl_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.json")))
        else:
            raise DslError(f"DSL path not found: {path}")
    return sorted(set(files), key=lambda p: str(p).lower())


def safe_eval_numeric(expr: str, scope: dict[str, float]) -> float:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise DslError(f"invalid numeric expression {expr!r}: {exc}") from exc

    def eval_node(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in scope:
                raise DslError(f"unknown numeric symbol {node.id!r} in expression {expr!r}")
            return float(scope[node.id])
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = eval_node(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            left = eval_node(node.left)
            right = eval_node(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            return left**right
        raise DslError(f"unsupported numeric expression {expr!r}")

    return eval_node(tree)


def resolve_number(value: Any, scope: dict[str, float], field: str) -> float:
    if isinstance(value, bool):
        raise DslError(f"{field} must be numeric, not bool")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return safe_eval_numeric(value, scope)
    raise DslError(f"{field} must be numeric")


def resolve_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise DslError(f"{field} must be bool")


def resolve_string(value: Any, field: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise DslError(f"{field} must be a non-empty string")


def sanitize_id(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    if not result:
        raise DslError("case_id must not be empty after sanitization")
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise DslError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise DslError(f"{path}: DSL root must be an object")
    return loaded


def resolve_constants(raw: dict[str, Any]) -> dict[str, float]:
    scope: dict[str, float] = {"pi": math.pi, "tau": math.tau}
    pending = dict(raw)
    while pending:
        progressed = False
        for key, value in list(pending.items()):
            if not isinstance(key, str) or not key:
                raise DslError("constant names must be non-empty strings")
            try:
                scope[key] = resolve_number(value, scope, f"constants.{key}")
            except DslError:
                continue
            del pending[key]
            progressed = True
        if not progressed:
            names = ", ".join(sorted(pending))
            raise DslError(f"could not resolve constants: {names}")
    return scope


def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def apply_patch_path(case: dict[str, Any], path: str, value: Any) -> None:
    if not path:
        raise DslError("patch path must not be empty")
    parts = path.split(".")
    target: Any = case
    for part in parts[:-1]:
        if isinstance(target, list):
            try:
                target = target[int(part)]
            except (ValueError, IndexError) as exc:
                raise DslError(f"invalid list patch segment {part!r} in {path!r}") from exc
        elif isinstance(target, dict):
            if part not in target:
                target[part] = {}
            target = target[part]
        else:
            raise DslError(f"cannot patch through non-container at {part!r} in {path!r}")

    leaf = parts[-1]
    if isinstance(target, list):
        try:
            target[int(leaf)] = value
        except (ValueError, IndexError) as exc:
            raise DslError(f"invalid list patch leaf {leaf!r} in {path!r}") from exc
    elif isinstance(target, dict):
        target[leaf] = value
    else:
        raise DslError(f"cannot patch non-container leaf in {path!r}")


def is_scalar_patch_sweep(path: str, value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    leaf = path.rsplit(".", 1)[-1]
    if leaf in VECTOR_PATCH_LEAVES:
        return False
    return all(not isinstance(item, (dict, list)) for item in value)


def expansions_from_variant(item: dict[str, Any], index: int) -> list[Expansion]:
    if not isinstance(item, dict):
        raise DslError("variants entries must be objects")
    suffix = sanitize_id(str(item.get("suffix", f"v{index}")))
    patch = item.get("set", {})
    if not isinstance(patch, dict):
        raise DslError("variant.set must be an object")

    sweep_paths: list[str] = []
    sweep_values: list[list[Any]] = []
    base_patch: dict[str, Any] = {}
    for path, value in patch.items():
        if is_scalar_patch_sweep(path, value):
            sweep_paths.append(path)
            sweep_values.append(value)
        else:
            base_patch[path] = value

    if not sweep_paths:
        return [Expansion(suffix=suffix, patch=patch)]

    expansions: list[Expansion] = []
    for value_index, combo in enumerate(itertools.product(*sweep_values), start=1):
        expanded_patch = dict(base_patch)
        expanded_patch.update(dict(zip(sweep_paths, combo)))
        expansions.append(Expansion(suffix=f"{suffix}_s{value_index}", patch=expanded_patch))
    return expansions


def expansions_from_sweep(item: dict[str, Any], index: int) -> list[Expansion]:
    if not isinstance(item, dict):
        raise DslError("sweeps entries must be objects")
    path = resolve_string(item.get("path"), "sweep.path")
    values = item.get("values")
    if not isinstance(values, list) or not values:
        raise DslError("sweep.values must be a non-empty array")

    expansions: list[Expansion] = []
    for value_index, raw in enumerate(values, start=1):
        if isinstance(raw, dict):
            suffix = sanitize_id(str(raw.get("suffix", f"s{index}_{value_index}")))
            if "value" not in raw:
                raise DslError("sweep value object must contain value")
            value = raw["value"]
        else:
            suffix = sanitize_id(f"s{index}_{value_index}")
            value = raw
        expansions.append(Expansion(suffix=suffix, patch={path: value}))
    return expansions


def expansions_from_paired_sweep(item: dict[str, Any], index: int) -> list[Expansion]:
    if not isinstance(item, dict):
        raise DslError("paired_sweeps entries must be objects")
    paths = item.get("paths")
    if not isinstance(paths, list) or not paths:
        raise DslError("paired_sweep.paths must be a non-empty array")
    resolved_paths = [resolve_string(path, f"paired_sweep.paths.{path_index}") for path_index, path in enumerate(paths)]
    values = item.get("values")
    if not isinstance(values, list) or not values:
        raise DslError("paired_sweep.values must be a non-empty array")

    expansions: list[Expansion] = []
    for value_index, raw in enumerate(values, start=1):
        if not isinstance(raw, dict):
            raise DslError("paired_sweep value entries must be objects")
        suffix = sanitize_id(str(raw.get("suffix", f"p{index}_{value_index}")))
        raw_values = raw.get("values")
        if not isinstance(raw_values, list):
            raise DslError("paired_sweep value object must contain a values array")
        if len(raw_values) != len(resolved_paths):
            raise DslError(
                f"paired_sweep value {suffix!r} has {len(raw_values)} values for {len(resolved_paths)} paths"
            )
        expansions.append(Expansion(suffix=suffix, patch=dict(zip(resolved_paths, raw_values))))
    return expansions


def case_expansions(case: dict[str, Any]) -> list[Expansion]:
    variants = case.get("variants", [])
    sweeps = case.get("sweeps", [])
    paired_sweeps = case.get("paired_sweeps", [])
    if variants is None:
        variants = []
    if sweeps is None:
        sweeps = []
    if paired_sweeps is None:
        paired_sweeps = []
    if not isinstance(variants, list):
        raise DslError("case.variants must be an array")
    if not isinstance(sweeps, list):
        raise DslError("case.sweeps must be an array")
    if not isinstance(paired_sweeps, list):
        raise DslError("case.paired_sweeps must be an array")

    variant_expansions = [
        expansion for i, item in enumerate(variants, start=1) for expansion in expansions_from_variant(item, i)
    ]
    if not variant_expansions:
        variant_expansions = [Expansion(suffix="", patch={})]

    sweep_groups = [expansions_from_sweep(item, i) for i, item in enumerate(sweeps, start=1)]
    sweep_groups.extend(expansions_from_paired_sweep(item, i) for i, item in enumerate(paired_sweeps, start=1))
    if not sweep_groups:
        sweep_groups = [[Expansion(suffix="", patch={})]]

    results: list[Expansion] = []
    for variant in variant_expansions:
        for sweep_combo in itertools.product(*sweep_groups):
            suffixes = [value for value in [variant.suffix, *(item.suffix for item in sweep_combo)] if value]
            patch: dict[str, Any] = {}
            patch.update(variant.patch)
            for sweep in sweep_combo:
                patch.update(sweep.patch)
            results.append(Expansion(suffix="_".join(suffixes), patch=patch))
    return results


def normalize_case(raw_case: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw_case, dict):
        raise DslError("cases entries must be objects")
    merged = merge_dicts({"options": defaults}, raw_case)
    for field in OPTION_FIELDS:
        if field in merged:
            merged.setdefault("options", {})[field] = merged.pop(field)
    return merged


def flatten_options(options: dict[str, Any], scope: dict[str, float]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in options.items():
        if key not in OPTION_FIELDS:
            continue
        if key in STRING_OPTION_FIELDS:
            result[key] = resolve_string(value, f"options.{key}")
        elif key in NUMERIC_OPTION_FIELDS:
            resolved = resolve_number(value, scope, f"options.{key}")
            result[key] = int(resolved) if key in {"source_body_index", "body_index"} else resolved
        elif key in BOOL_OPTION_FIELDS:
            result[key] = resolve_bool(value, f"options.{key}")
    result.setdefault("api", "api_boolean")
    return result


def resolve_expectation_metric(value: Any, scope: dict[str, float], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DslError(f"{field} must be an object")
    result: dict[str, Any] = {}
    for key, raw in value.items():
        if key in {"min", "max", "expected", "abs_tol", "rel_tol"}:
            result[key] = resolve_number(raw, scope, f"{field}.{key}")
        else:
            result[key] = deepcopy(raw)
    return result


def resolve_point_vector(value: Any, scope: dict[str, float], field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise DslError(f"{field} must be a 3-number array")
    return [resolve_number(coord, scope, f"{field}.{coord_index}") for coord_index, coord in enumerate(value)]


def resolve_key_points(value: Any, scope: dict[str, float], field: str) -> dict[str, list[float]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DslError(f"{field} must be an object")
    points: dict[str, list[float]] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not name:
            raise DslError(f"{field} names must be non-empty strings")
        point_value = raw.get("point") if isinstance(raw, dict) and "point" in raw else raw
        points[name] = resolve_point_vector(point_value, scope, f"{field}.{name}")
    return points


def resolve_point_ref(
    item: dict[str, Any],
    relation: dict[str, Any],
    scope: dict[str, float],
    key_points: dict[str, list[float]],
    item_field: str,
) -> None:
    if "point_ref" in item:
        if "point" in item:
            raise DslError(f"{item_field} cannot set both point and point_ref")
        name = resolve_string(item["point_ref"], f"{item_field}.point_ref")
        if name not in key_points:
            raise DslError(f"{item_field}.point_ref {name!r} is not defined in key_points")
        relation["point_ref"] = name
        relation["point"] = deepcopy(key_points[name])
    elif "point" in item:
        relation["point"] = resolve_point_vector(item["point"], scope, f"{item_field}.point")


def resolve_point_relations(
    value: Any,
    scope: dict[str, float],
    field: str,
    key_points: dict[str, list[float]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DslError(f"{field} must be an array")
    relations: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_field = f"{field}.{index}"
        if not isinstance(item, dict):
            raise DslError(f"{item_field} must be an object")
        relation: dict[str, Any] = {}
        resolve_point_ref(item, relation, scope, key_points, item_field)
        for key, raw in item.items():
            if key in {"point", "point_ref"}:
                continue
            elif key in POINT_RELATION_NUMERIC_FIELDS:
                relation[key] = resolve_number(raw, scope, f"{item_field}.{key}")
            elif key in POINT_RELATION_INT_FIELDS:
                relation[key] = int(resolve_number(raw, scope, f"{item_field}.{key}"))
            elif key in {"check_boundary", "required"}:
                relation[key] = resolve_bool(raw, f"{item_field}.{key}")
            else:
                relation[key] = deepcopy(raw)
        relations.append(relation)
    return relations


def resolve_face_point_relations(
    value: Any,
    scope: dict[str, float],
    field: str,
    key_points: dict[str, list[float]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DslError(f"{field} must be an array")
    relations: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_field = f"{field}.{index}"
        if not isinstance(item, dict):
            raise DslError(f"{item_field} must be an object")
        relation: dict[str, Any] = {}
        resolve_point_ref(item, relation, scope, key_points, item_field)
        for key, raw in item.items():
            if key in {"point", "point_ref"}:
                continue
            elif key in {"uv", "uv_fraction"}:
                if not isinstance(raw, list) or len(raw) != 2:
                    raise DslError(f"{item_field}.{key} must be a 2-number array")
                relation[key] = [
                    resolve_number(coord, scope, f"{item_field}.{key}.{coord_index}")
                    for coord_index, coord in enumerate(raw)
                ]
            elif key in FACE_POINT_RELATION_NUMERIC_FIELDS:
                relation[key] = resolve_number(raw, scope, f"{item_field}.{key}")
            elif key in FACE_POINT_RELATION_INT_FIELDS:
                relation[key] = int(resolve_number(raw, scope, f"{item_field}.{key}"))
            elif key in {"check_boundary", "required"}:
                relation[key] = resolve_bool(raw, f"{item_field}.{key}")
            else:
                relation[key] = deepcopy(raw)
        relations.append(relation)
    return relations


def resolve_clash_checks(value: Any, scope: dict[str, float], field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DslError(f"{field} must be an array")
    checks: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_field = f"{field}.{index}"
        if not isinstance(item, dict):
            raise DslError(f"{item_field} must be an object")
        check: dict[str, Any] = {}
        for key, raw in item.items():
            if key in CLASH_CHECK_NUMERIC_FIELDS:
                check[key] = resolve_number(raw, scope, f"{item_field}.{key}")
            elif key in CLASH_CHECK_INT_FIELDS:
                check[key] = int(resolve_number(raw, scope, f"{item_field}.{key}"))
            elif key == "required":
                check[key] = resolve_bool(raw, f"{item_field}.{key}")
            else:
                check[key] = deepcopy(raw)
        checks.append(check)
    return checks


def resolve_distance_checks(value: Any, scope: dict[str, float], field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DslError(f"{field} must be an array")
    checks: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_field = f"{field}.{index}"
        if not isinstance(item, dict):
            raise DslError(f"{item_field} must be an object")
        check: dict[str, Any] = {}
        for key, raw in item.items():
            if key == "distance":
                check[key] = resolve_expectation_metric(raw, scope, f"{item_field}.distance")
            elif key == "threshold":
                resolved = resolve_number(raw, scope, f"{item_field}.threshold")
                if resolved > 0:
                    check[key] = resolved
            elif key in DISTANCE_CHECK_NUMERIC_FIELDS:
                check[key] = resolve_number(raw, scope, f"{item_field}.{key}")
            elif key in DISTANCE_CHECK_INT_FIELDS:
                check[key] = int(resolve_number(raw, scope, f"{item_field}.{key}"))
            elif key == "required":
                check[key] = resolve_bool(raw, f"{item_field}.{key}")
            else:
                check[key] = deepcopy(raw)
        checks.append(check)
    return checks


def resolve_plane_extreme_checks(value: Any, scope: dict[str, float], field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DslError(f"{field} must be an array")
    checks: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_field = f"{field}.{index}"
        if not isinstance(item, dict):
            raise DslError(f"{item_field} must be an object")
        check: dict[str, Any] = {}
        for key, raw in item.items():
            if key in PLANE_EXTREME_NUMERIC_FIELDS:
                check[key] = resolve_number(raw, scope, f"{item_field}.{key}")
            elif key in PLANE_EXTREME_INT_FIELDS:
                check[key] = int(resolve_number(raw, scope, f"{item_field}.{key}"))
            elif key in {"required", "export_debug_geometry", "compare_expected"}:
                check[key] = resolve_bool(raw, f"{item_field}.{key}")
            else:
                check[key] = deepcopy(raw)
        checks.append(check)
    return checks


def resolve_expectations(
    value: Any,
    scope: dict[str, float],
    field: str = "expectations",
    key_points: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DslError(f"{field} must be an object")
    resolved_key_points = key_points or {}
    result: dict[str, Any] = {}
    for key, raw in value.items():
        if key in EXPECTATION_BOOL_FIELDS:
            result[key] = resolve_bool(raw, f"{field}.{key}")
        elif key in EXPECTATION_INT_FIELDS:
            result[key] = int(resolve_number(raw, scope, f"{field}.{key}"))
        elif key == "result_bodies":
            if not isinstance(raw, dict):
                raise DslError(f"{field}.result_bodies must be an object")
            result[key] = {
                child_key: int(resolve_number(child_value, scope, f"{field}.result_bodies.{child_key}"))
                for child_key, child_value in raw.items()
                if child_key in {"min", "max"}
            }
        elif key in EXPECTATION_NUMERIC_FIELDS:
            result[key] = resolve_number(raw, scope, f"{field}.{key}")
        elif key in EXPECTATION_METRICS:
            if isinstance(raw, dict):
                result[key] = resolve_expectation_metric(raw, scope, f"{field}.{key}")
            else:
                result[key] = {"expected": resolve_number(raw, scope, f"{field}.{key}")}
        elif key == "point_relations":
            result[key] = resolve_point_relations(raw, scope, f"{field}.point_relations", resolved_key_points)
        elif key == "face_point_relations":
            result[key] = resolve_face_point_relations(raw, scope, f"{field}.face_point_relations", resolved_key_points)
        elif key == "clash_checks":
            result[key] = resolve_clash_checks(raw, scope, f"{field}.clash_checks")
        elif key == "distance_checks":
            result[key] = resolve_distance_checks(raw, scope, f"{field}.distance_checks")
        elif key == "plane_extreme_checks":
            result[key] = resolve_plane_extreme_checks(raw, scope, f"{field}.plane_extreme_checks")
        elif any(
            key == f"{prefix}_{metric}" for prefix in ("min", "max", "expected") for metric in EXPECTATION_METRICS
        ):
            result[key] = resolve_number(raw, scope, f"{field}.{key}")
        elif any(key == f"{metric}_{suffix}" for metric in EXPECTATION_METRICS for suffix in ("abs_tol", "rel_tol")):
            result[key] = resolve_number(raw, scope, f"{field}.{key}")
        else:
            raise DslError(
                f"{field}.{key}: unsupported expectation/oracle key; "
                "use a supported expectation field from interface_capabilities.json or return needs_harness_extension"
            )
    return result


def vector_to_axes(body: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in body:
        return {}
    value = body[key]
    if not isinstance(value, list) or len(value) != 3:
        raise DslError(f"body.{key} must be a 3-number array")
    return {
        f"{key}_x": value[0],
        f"{key}_y": value[1],
        f"{key}_z": value[2],
    }


def body_without_meta(body: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in body.items() if key not in CHAIN_META_FIELDS}


def body_vector_components(body: dict[str, Any], key: str) -> tuple[Any, Any, Any]:
    if key in body:
        value = body[key]
        if not isinstance(value, list) or len(value) != 3:
            raise DslError(f"body.{key} must be a 3-number array")
        return value[0], value[1], value[2]
    return (
        body.get(f"{key}_x", 0.0),
        body.get(f"{key}_y", 0.0),
        body.get(f"{key}_z", 0.0),
    )


def add_numeric_expression(left: Any, right: Any) -> Any:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left + right
    if left in (0, 0.0):
        return right
    if right in (0, 0.0):
        return left
    return f"({left}) + ({right})"


def multiply_numeric_expression(left: Any, right: Any) -> Any:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left * right
    if left in (1, 1.0):
        return right
    if right in (1, 1.0):
        return left
    return f"({left}) * ({right})"


def apply_transform_spec(body: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(body)
    if "scale" in step:
        result["scale"] = multiply_numeric_expression(result.get("scale", 1.0), step["scale"])
    if "translate" in step:
        dx, dy, dz = body_vector_components(step, "translate")
    else:
        dx = step.get("translate_x", 0.0)
        dy = step.get("translate_y", 0.0)
        dz = step.get("translate_z", 0.0)
    result["translate_x"] = add_numeric_expression(result.get("translate_x", 0.0), dx)
    result["translate_y"] = add_numeric_expression(result.get("translate_y", 0.0), dy)
    result["translate_z"] = add_numeric_expression(result.get("translate_z", 0.0), dz)
    return result


def compile_boolean_chain_body(base: dict[str, Any], step: dict[str, Any], role: str) -> dict[str, Any]:
    if "tool" not in step:
        raise DslError(f"{role}: boolean chain op requires tool")
    tool = compile_body_spec(step["tool"], f"{role}.boolean.tool")
    boolean_type = step.get("boolean_type", step.get("type", "SUBTRACTION"))
    if base.get("kind") == "solid_cylinder" and tool.get("kind") == "solid_wedge":
        unsupported_base_fields = {"scale", "translate", "translate_x", "translate_y", "translate_z"} & set(base)
        if unsupported_base_fields:
            fields = ", ".join(sorted(unsupported_base_fields))
            raise DslError(f"{role}: pre_boolean_cylinder_wedge cannot compile base transform fields yet: {fields}")
        if tool.get("scale", 1.0) not in (1, 1.0):
            raise DslError(f"{role}: pre_boolean_cylinder_wedge cannot compile scaled wedge tool yet")
        result = {
            "kind": "pre_boolean_cylinder_wedge",
            "boolean_type": boolean_type,
            "radius": base.get("radius"),
            "height": base.get("height"),
            "angle": base.get("angle", "tau"),
            "create_seam_edge": base.get("create_seam_edge", True),
            "length": tool.get("length"),
            "width": tool.get("width"),
            "secondary_height": tool.get("height"),
            "operation_tol": step.get(
                "operation_tol", step.get("modeling_tol", base.get("operation_tol", tool.get("operation_tol", 0.01)))
            ),
        }
        tx, ty, tz = body_vector_components(tool, "translate")
        result["secondary_translate_x"] = tx
        result["secondary_translate_y"] = ty
        result["secondary_translate_z"] = tz
        result["operations"] = [
            *base.get("operations", []),
            resolve_string(step.get("id", "boolean"), f"{role}.boolean.id"),
            *tool.get("operations", []),
        ]
        ensure_supported_body_builder(result.get("kind"), f"{role}.boolean.result.kind")
        return result
    raise DslError(
        f"{role}: unsupported boolean chain pattern "
        f"{base.get('kind')!r} {boolean_type} {tool.get('kind')!r}; "
        "add a runner body builder or emit needs_harness_extension"
    )


def compile_body_chain(chain: list[Any], role: str) -> dict[str, Any]:
    current: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    previous_body: dict[str, Any] | None = None

    for index, raw_step in enumerate(chain):
        if not isinstance(raw_step, dict):
            raise DslError(f"{role}.chain[{index}] must be an object")
        step = deepcopy(raw_step)
        op = resolve_string(step.get("op"), f"{role}.chain[{index}].op")
        op_id = resolve_string(step.get("id", f"op_{index + 1}_{op}"), f"{role}.chain[{index}].id")

        if op == "primitive":
            if "kind" not in step:
                raise DslError(f"{role}.chain[{index}]: primitive op requires kind")
            if current is not None:
                previous_body = deepcopy(current)
            current = body_without_meta(step)
            ensure_supported_body_builder(current.get("kind"), f"{role}.chain[{index}].kind")
            current["operations"] = [op_id]
            profile = None
        elif op == "body":
            if current is not None:
                previous_body = deepcopy(current)
            current = compile_body_spec(step.get("body"), f"{role}.chain[{index}].body")
            current["operations"] = [*current.get("operations", []), op_id]
            profile = None
        elif op == "load_sgt":
            if "source_file" not in step:
                raise DslError(f"{role}.chain[{index}]: load_sgt requires source_file")
            if current is not None:
                previous_body = deepcopy(current)
            current = body_without_meta(step)
            current["kind"] = "loaded_sgt"
            ensure_supported_body_builder(current.get("kind"), f"{role}.chain[{index}].kind")
            current["operations"] = [op_id]
            profile = None
        elif op == "rect_profile":
            profile = {"kind": "rect_profile", **body_without_meta(step)}
            profile["operations"] = [op_id]
            current = None
        elif op == "circle_profile":
            profile = {"kind": "circle_profile", **body_without_meta(step)}
            profile["operations"] = [op_id]
            current = None
        elif op == "line_profile":
            profile = {"kind": "line_profile", **body_without_meta(step)}
            profile["operations"] = [op_id]
            current = None
        elif op == "radial_rect_profile":
            profile = {"kind": "radial_rect_profile", **body_without_meta(step)}
            profile["operations"] = [op_id]
            current = None
        elif op == "extrude":
            if profile is None or profile.get("kind") not in {"rect_profile", "circle_profile"}:
                raise DslError(
                    f"{role}.chain[{index}]: extrude currently requires preceding rect_profile or circle_profile"
                )
            if "height" not in step:
                raise DslError(f"{role}.chain[{index}]: extrude requires height")
            if profile.get("kind") == "rect_profile":
                current = {
                    "kind": "extrude_rect",
                    "length": profile.get("length"),
                    "width": profile.get("width"),
                    "height": step.get("height"),
                    "operations": [*profile.get("operations", []), op_id],
                }
            else:
                current = {
                    "kind": "solid_cylinder",
                    "radius": profile.get("radius", profile.get("profile_radius")),
                    "height": step.get("height"),
                    "operations": [*profile.get("operations", []), op_id],
                }
            for key in ("operation_tol", "g1_tol"):
                if key in step:
                    current[key] = step[key]
                elif key in profile:
                    current[key] = profile[key]
            ensure_supported_body_builder(current.get("kind"), f"{role}.chain[{index}].kind")
            profile = None
        elif op in {"thicken", "thicken_rect_sheet"}:
            if profile is None or profile.get("kind") != "rect_profile":
                raise DslError(f"{role}.chain[{index}]: {op} currently requires preceding rect_profile")
            current = {
                "kind": "thicken_rect_sheet",
                "length": profile.get("length"),
                "width": profile.get("width"),
                "operations": [*profile.get("operations", []), op_id],
            }
            if "thickness" in step and "min_dist" not in step and "max_dist" not in step:
                current["min_dist"] = 0.0
                current["max_dist"] = step["thickness"]
            else:
                current["min_dist"] = step.get("min_dist", -10.0)
                current["max_dist"] = step.get("max_dist", 20.0)
            for key in ("operation_tol", "g1_tol", "allow_partial_success"):
                if key in step:
                    current[key] = step[key]
                elif key in profile:
                    current[key] = profile[key]
            ensure_supported_body_builder(current.get("kind"), f"{role}.chain[{index}].kind")
            profile = None
        elif op == "sweep_line":
            if profile is None or profile.get("kind") != "circle_profile":
                raise DslError(f"{role}.chain[{index}]: sweep_line currently requires preceding circle_profile")
            if "height" not in step:
                raise DslError(f"{role}.chain[{index}]: sweep_line requires height")
            current = {
                "kind": "sweep_circle_line",
                "profile_radius": profile.get("radius", profile.get("profile_radius")),
                "height": step.get("height"),
                "operations": [*profile.get("operations", []), op_id],
            }
            for key in ("operation_tol", "g1_tol"):
                if key in step:
                    current[key] = step[key]
                elif key in profile:
                    current[key] = profile[key]
            ensure_supported_body_builder(current.get("kind"), f"{role}.chain[{index}].kind")
            profile = None
        elif op in {"support_sweep", "support_sweep_bspline_surface"}:
            if current is not None:
                previous_body = deepcopy(current)
            profile_spec = step.get("profile") if isinstance(step.get("profile"), dict) else {}
            path_spec = step.get("path") if isinstance(step.get("path"), dict) else {}
            current = {
                "kind": "support_sweep_bspline_surface",
                "path_radius": step.get("path_radius", path_spec.get("path_radius", path_spec.get("radius"))),
                "profile_radius": step.get(
                    "profile_radius",
                    step.get("radius", profile_spec.get("profile_radius", profile_spec.get("radius"))),
                ),
                "height": step.get("height", path_spec.get("height")),
                "operations": [op_id],
            }
            for key in ("operation_tol", "g1_tol"):
                if key in step:
                    current[key] = step[key]
            ensure_supported_body_builder(current.get("kind"), f"{role}.chain[{index}].kind")
            profile = None
        elif op == "revolve":
            if profile is None or profile.get("kind") not in {"line_profile", "radial_rect_profile"}:
                raise DslError(
                    f"{role}.chain[{index}]: revolve currently requires preceding line_profile or radial_rect_profile"
                )
            if profile.get("kind") == "line_profile":
                current = {
                    "kind": "revolve_line",
                    "bottom_radius": profile.get("bottom_radius"),
                    "top_radius": profile.get("top_radius"),
                    "height": profile.get("height"),
                    "angle": step.get("angle", "tau"),
                    "operations": [*profile.get("operations", []), op_id],
                }
            else:
                current = {
                    "kind": "revolve_rect",
                    "inner_radius": profile.get("inner_radius"),
                    "outer_radius": profile.get("outer_radius"),
                    "height": profile.get("height"),
                    "angle": step.get("angle", "tau"),
                    "operations": [*profile.get("operations", []), op_id],
                }
            if "operation_tol" in step:
                current["operation_tol"] = step["operation_tol"]
            elif "operation_tol" in profile:
                current["operation_tol"] = profile["operation_tol"]
            ensure_supported_body_builder(current.get("kind"), f"{role}.chain[{index}].kind")
            profile = None
        elif op == "boolean":
            if current is None:
                raise DslError(f"{role}.chain[{index}]: boolean requires an existing body")
            if "tool" not in step and previous_body is not None:
                step = {**step, "tool": current}
                current = compile_boolean_chain_body(previous_body, step, role)
            else:
                current = compile_boolean_chain_body(current, step, role)
            previous_body = None
            profile = None
        elif op == "transform":
            if current is None:
                raise DslError(f"{role}.chain[{index}]: transform requires an existing body")
            current = apply_transform_spec(current, step)
            current["operations"] = [*current.get("operations", []), op_id]
        elif op in SUPPORTED_BODY_BUILDERS:
            if current is not None:
                previous_body = deepcopy(current)
            current = body_without_meta(step)
            current["kind"] = op
            ensure_supported_body_builder(current.get("kind"), f"{role}.chain[{index}].kind")
            current["operations"] = [op_id]
            profile = None
        else:
            raise DslError(f"{role}.chain[{index}]: unsupported op {op!r}")

    if current is None:
        raise DslError(f"{role}: chain did not produce a body")
    ensure_supported_body_builder(current.get("kind"), f"{role}.kind")
    return current


def compile_body_spec(body: Any, role: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise DslError(f"{role} must be an object")
    if "chain" not in body:
        result = deepcopy(body)
        ensure_supported_body_builder(result.get("kind"), f"{role}.kind")
        return result
    chain = body["chain"]
    if not isinstance(chain, list) or not chain:
        raise DslError(f"{role}.chain must be a non-empty array")
    compiled = compile_body_chain(chain, role)
    extras = {key: value for key, value in body.items() if key != "chain"}
    return merge_dicts(compiled, extras)


def flatten_body(prefix: str, body: dict[str, Any], scope: dict[str, float]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise DslError(f"{prefix} must be an object")
    expanded: dict[str, Any] = {}
    expanded.update(vector_to_axes(body, "translate"))
    expanded.update(vector_to_axes(body, "secondary_translate"))
    expanded.update(body)

    result: dict[str, Any] = {}
    for key, value in expanded.items():
        if key in STRING_BODY_FIELDS:
            result[f"{prefix}_{key}"] = resolve_string(value, f"{prefix}.{key}")
        elif key in BOOL_BODY_FIELDS:
            result[f"{prefix}_{key}"] = resolve_bool(value, f"{prefix}.{key}")
        elif key in NUMERIC_BODY_FIELDS:
            resolved = resolve_number(value, scope, f"{prefix}.{key}")
            result[f"{prefix}_{key}"] = int(resolved) if key == "body_index" else resolved
        elif key in {"translate", "secondary_translate"}:
            continue
        elif key == "operations":
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise DslError(f"{prefix}.operations must be an array of non-empty strings")
            result[f"{prefix}_operations"] = deepcopy(value)
    return result


def compile_one_case(
    raw_case: dict[str, Any],
    defaults: dict[str, Any],
    scope: dict[str, float],
    source_path: Path,
    global_key_points: dict[str, list[float]],
) -> list[dict[str, Any]]:
    base_case = normalize_case(raw_case, defaults)
    base_id = sanitize_id(resolve_string(base_case.get("case_id", base_case.get("id")), "case.case_id"))
    expansions = case_expansions(base_case)
    recipes: list[dict[str, Any]] = []

    for expansion in expansions:
        concrete = deepcopy(base_case)
        for path, value in expansion.patch.items():
            apply_patch_path(concrete, path, value)

        options = concrete.get("options", {})
        if not isinstance(options, dict):
            raise DslError("case.options must be an object")
        recipe = flatten_options(options, scope)
        if "max_model_size" in scope and "max_model_size" not in recipe:
            recipe["max_model_size"] = scope["max_model_size"]
        expectations: dict[str, Any] = {}
        if isinstance(options.get("expectations"), dict):
            expectations = merge_dicts(expectations, options["expectations"])
        if isinstance(concrete.get("expectations"), dict):
            expectations = merge_dicts(expectations, concrete["expectations"])
        if expectations:
            key_points = dict(global_key_points)
            key_points.update(resolve_key_points(concrete.get("key_points", {}), scope, f"{base_id}.key_points"))
            recipe["expectations"] = resolve_expectations(expectations, scope, key_points=key_points)
        recipe["case_id"] = base_id if not expansion.suffix else f"{base_id}_{expansion.suffix}"
        if recipe.get("api") == "api_boolean":
            if "target" not in concrete or "tool" not in concrete:
                raise DslError(f"{base_id}: api_boolean cases require target and tool")
            target_body = compile_body_spec(concrete["target"], f"{base_id}.target")
            tool_body = compile_body_spec(concrete["tool"], f"{base_id}.tool")
            recipe.update(flatten_body("target", target_body, scope))
            recipe.update(flatten_body("tool", tool_body, scope))
        elif recipe.get("api") in {"check_sgt", "step_import", "iges_import", "step_roundtrip", "iges_roundtrip"}:
            recipe["source_file"] = resolve_string(concrete.get("source_file"), f"{base_id}.source_file")

        metadata = concrete.get("metadata", {})
        if metadata is not None and not isinstance(metadata, dict):
            raise DslError(f"{base_id}: metadata must be an object")
        recipe["dsl_source"] = str(source_path)
        recipe["dsl_case_id"] = base_id
        if expansion.suffix:
            recipe["dsl_variant"] = expansion.suffix
        for key in PROVENANCE_FIELDS:
            if key in concrete:
                recipe[key] = concrete[key]
            elif isinstance(metadata, dict) and key in metadata:
                recipe[key] = metadata[key]
        # Defaults are shared across DSL APIs, but flat recipes are strict and
        # must not carry options that the selected runner adapter ignores.
        allowed = allowed_recipe_keys(recipe)
        for key in list(recipe):
            if key in OPTION_FIELDS and key not in allowed:
                del recipe[key]
        recipes.append(recipe)
    return recipes


def compile_dsl_file(path: Path) -> list[dict[str, Any]]:
    dsl = load_json(path)
    if dsl.get("dsl_version") not in (1, "1", None):
        raise DslError(f"{path}: unsupported dsl_version {dsl.get('dsl_version')!r}")
    scope = resolve_constants(dsl.get("constants", {}))
    global_key_points = resolve_key_points(dsl.get("key_points", {}), scope, "key_points")
    defaults = dsl.get("defaults", {})
    if not isinstance(defaults, dict):
        raise DslError(f"{path}: defaults must be an object")
    cases = dsl.get("cases")
    if not isinstance(cases, list) or not cases:
        raise DslError(f"{path}: cases must be a non-empty array")

    recipes: list[dict[str, Any]] = []
    for raw_case in cases:
        recipes.extend(compile_one_case(raw_case, defaults, scope, path, global_key_points))
    return recipes


def is_cluster_seed_file(path: Path) -> bool:
    try:
        loaded = load_json(path)
    except DslError:
        return False
    return loaded.get("kind") == "cluster_seed"


def write_recipe(recipe: dict[str, Any], out_dir: Path) -> Path:
    case_id = resolve_string(recipe.get("case_id"), "recipe.case_id")
    out_path = out_dir / f"{case_id}.json"
    out_path.write_text(json.dumps(recipe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out_path


def check_dsl_file(
    path: Path,
    validate: bool,
    *,
    asset_policy: str = "trusted",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    record: dict[str, Any] = {
        "path": str(path),
        "ok": False,
        "skipped": False,
        "skip_reason": "",
        "recipe_count": 0,
        "validation_failure_count": 0,
        "compile_error": "",
        "recipes": [],
    }
    if is_cluster_seed_file(path):
        record["ok"] = True
        record["skipped"] = True
        record["skip_reason"] = "cluster_seed; expand with build_source_guided_cluster.py before DSL compilation"
        return record, []

    try:
        recipes = compile_dsl_file(path)
    except DslError as exc:
        record["compile_error"] = str(exc)
        return record, []

    valid_recipes: list[dict[str, Any]] = []
    for recipe in recipes:
        case_id = recipe.get("case_id", "<unknown>")
        item = {"case_id": case_id, "ok": True, "errors": []}
        if validate:
            errors = validate_recipe(recipe, asset_policy=asset_policy)
            if errors:
                item["ok"] = False
                item["errors"] = errors
                record["validation_failure_count"] = int(record["validation_failure_count"]) + 1
        if item["ok"]:
            valid_recipes.append(recipe)
        record["recipes"].append(item)

    record["recipe_count"] = len(recipes)
    record["ok"] = not record["compile_error"] and int(record["validation_failure_count"]) == 0
    return record, valid_recipes


def diagnostic_from_compile_error(path: str, message: str) -> dict[str, Any]:
    lower = message.lower()
    if "api_boolean cases require target and tool" in lower:
        return {
            "severity": "error",
            "error_code": "MISSING_TARGET_TOOL",
            "path": path,
            "message": message,
            "expected_shape": {
                "case_id": "example_case",
                "target": {"chain": [{"id": "target_profile", "op": "rect_profile"}]},
                "tool": {"chain": [{"id": "tool_profile", "op": "circle_profile"}]},
            },
            "repair_hint": (
                "api_boolean cases require direct target and tool objects. "
                "Do not use top-level chains plus case inputs."
            ),
        }
    if "cases must be a non-empty array" in lower:
        return {
            "severity": "error",
            "error_code": "MISSING_CASES",
            "path": path,
            "message": message,
            "expected_shape": {"cases": [{"case_id": "example_case", "target": {}, "tool": {}}]},
            "repair_hint": "Return attack_dsl.dsl with a non-empty cases array.",
        }
    if "unsupported dsl_version" in lower:
        return {
            "severity": "error",
            "error_code": "UNSUPPORTED_DSL_VERSION",
            "path": path,
            "message": message,
            "repair_hint": "Use dsl_version 1 or omit dsl_version.",
        }
    if "unsupported boolean chain pattern" in lower:
        return {
            "severity": "error",
            "error_code": "UNSUPPORTED_BOOLEAN_CHAIN_PATTERN",
            "path": path,
            "message": message,
            "repair_hint": (
                "Use supported direct body builders or supported chain steps; "
                "otherwise return needs_harness_extension."
            ),
        }
    if "unsupported body builder" in lower:
        return {
            "severity": "error",
            "error_code": "UNSUPPORTED_BODY_BUILDER",
            "path": path,
            "message": message,
            "repair_hint": (
                "Use a body builder listed in interface_capabilities.json, or return "
                "needs_harness_extension for the missing builder."
            ),
        }
    if "unsupported expectation/oracle key" in lower:
        return {
            "severity": "error",
            "error_code": "UNSUPPORTED_EXPECTATION_ORACLE",
            "path": path,
            "message": message,
            "repair_hint": (
                "Use supported expectation fields such as result_bodies, require_finite_properties, "
                "total_volume, point_relations, face_point_relations, clash_checks, distance_checks, "
                "or plane_extreme_checks. Do not emit an expectations.properties array."
            ),
        }
    if "unsupported op" in lower:
        return {
            "severity": "error",
            "error_code": "UNSUPPORTED_CHAIN_OP",
            "path": path,
            "message": message,
            "repair_hint": (
                "Use supported chain ops such as primitive, body, load_sgt, direct body-builder ops, "
                "rect_profile, circle_profile, extrude, thicken, sweep_line, support_sweep, revolve, "
                "boolean, or transform."
            ),
        }
    if "currently requires preceding" in lower or "requires an existing body" in lower:
        return {
            "severity": "error",
            "error_code": "INVALID_CHAIN_ORDER",
            "path": path,
            "message": message,
            "repair_hint": (
                "Order chain steps so profile builders precede generated-body ops and "
                "transforms/booleans follow an existing body."
            ),
        }
    if "boolean chain op requires tool" in lower:
        return {
            "severity": "error",
            "error_code": "MISSING_BOOLEAN_CHAIN_TOOL",
            "path": path,
            "message": message,
            "repair_hint": (
                "For nested boolean chains, include a tool object in the boolean step or place the "
                "base body followed by the tool body/transform immediately before op=boolean."
            ),
        }
    if "must be bool" in lower:
        return {
            "severity": "error",
            "error_code": "INVALID_BOOLEAN_FIELD",
            "path": path,
            "message": message,
            "repair_hint": (
                "Use literal true/false for boolean fields. For boolean_volume_relation, set "
                "sample_input_properties=true and boolean_volume_relation=true instead of an "
                "object, formula, or relation string."
            ),
        }
    if "unknown numeric symbol" in lower:
        return {
            "severity": "error",
            "error_code": "UNKNOWN_NUMERIC_SYMBOL",
            "path": path,
            "message": message,
            "repair_hint": "Use constants declared in dsl.constants or numeric literals.",
        }
    if "point_ref" in lower and "is not defined in key_points" in lower:
        return {
            "severity": "error",
            "error_code": "UNDEFINED_POINT_REF",
            "path": path,
            "message": message,
            "repair_hint": (
                "Declare every referenced point_ref under root key_points or case.key_points, "
                "or replace the reference with an explicit point array."
            ),
        }
    if "must be numeric" in lower or "invalid numeric expression" in lower:
        return {
            "severity": "error",
            "error_code": "INVALID_NUMERIC_FIELD",
            "path": path,
            "message": message,
            "repair_hint": "Use numbers or expressions over declared constants for numeric fields.",
        }
    return {
        "severity": "error",
        "error_code": "DSL_COMPILE_ERROR",
        "path": path,
        "message": message,
        "repair_hint": "Follow a known-good DSL example and use only supported DSL fields.",
    }


def build_model_diagnostics(summary: dict[str, Any]) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    for file_record in summary.get("files", []):
        if not isinstance(file_record, dict):
            continue
        path = str(file_record.get("path", ""))
        compile_error = file_record.get("compile_error")
        if isinstance(compile_error, str) and compile_error:
            diagnostics.append(diagnostic_from_compile_error(path, compile_error))
        for recipe in file_record.get("recipes", []):
            if not isinstance(recipe, dict) or recipe.get("ok"):
                continue
            for error in recipe.get("errors", []):
                diagnostics.append(
                    diagnostic_from_validation_error(f"{path}:{recipe.get('case_id', '<unknown>')}", str(error))
                )
    return {
        "generated_at": now_iso_like(),
        "ok": bool(summary.get("ok")),
        "diagnostic_count": len(diagnostics),
        "diagnostics": diagnostics,
    }


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out) if args.out else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    try:
        files = iter_dsl_files(args.paths)
        all_paths: list[str] = []
        file_records: list[dict[str, Any]] = []
        compile_failures = 0
        validation_failures = 0
        for dsl_path in files:
            record, valid_recipes = check_dsl_file(
                dsl_path,
                not args.no_validate,
                asset_policy="model" if args.model_asset_policy else "trusted",
            )
            file_records.append(record)
            if record.get("skipped"):
                if args.check:
                    print(f"SKIP {dsl_path}: {record['skip_reason']}")
                continue
            if record["compile_error"]:
                compile_failures += 1
                print(f"FAIL {dsl_path}", file=sys.stderr)
                print(f"  - {record['compile_error']}", file=sys.stderr)
                continue
            validation_failures += int(record["validation_failure_count"])
            for recipe_item in record["recipes"]:
                if recipe_item["ok"]:
                    if args.check:
                        print(f"OK {recipe_item['case_id']}")
                else:
                    print(f"FAIL {recipe_item['case_id']}", file=sys.stderr)
                    for error in recipe_item["errors"]:
                        print(f"  - {error}", file=sys.stderr)
            if out_dir is not None:
                for recipe in valid_recipes:
                    recipe_path = write_recipe(recipe, out_dir)
                    all_paths.append(str(recipe_path))
                    print(f"OK {recipe_path}")

        summary = {
            "generated_at": now_iso_like(),
            "mode": "check" if args.check else "compile",
            "validated": not args.no_validate,
            "ok": compile_failures == 0 and validation_failures == 0,
            "file_count": len(file_records),
            "recipe_count": sum(int(record.get("recipe_count") or 0) for record in file_records),
            "compiled_count": len(all_paths),
            "compile_failure_count": compile_failures,
            "validation_failure_count": validation_failures,
            "out": str(out_dir) if out_dir is not None else "",
            "compiled_paths": all_paths,
            "files": file_records,
        }
        if args.report:
            write_json(Path(args.report), summary)
        if args.model_diagnostics:
            write_json(Path(args.model_diagnostics), build_model_diagnostics(summary))
        if args.check:
            print(
                f"checked={summary['file_count']} recipes={summary['recipe_count']} "
                f"compile_failures={compile_failures} validation_failures={validation_failures}"
            )
        else:
            print(f"compiled={len(all_paths)} out={out_dir}")
        if compile_failures:
            return 1
        return 0 if validation_failures == 0 else 2
    except DslError as exc:
        print(f"compile_attack_dsl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
