#!/usr/bin/env python3
"""Validate flat SGGK test-harness recipes supported by sggk_case_runner."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from harness_capabilities import (
    load_capabilities,
    supported_body_builders,
    supported_recipe_apis,
)
from plugin_catalog import plugin_map

CAPABILITIES = load_capabilities()
PLUGIN_RECORDS = plugin_map()
IMPLEMENTED_RECIPE_APIS = {
    "api_boolean",
    "api_boolean_slice",
    "api_boolean_split",
    "api_offset2d",
    "api_offset_body",
    "api_topology_section",
    "check_sgt",
    "step_import",
    "iges_import",
    "step_roundtrip",
    "iges_roundtrip",
} | set(PLUGIN_RECORDS)
SUPPORTED_APIS = set(supported_recipe_apis(CAPABILITIES)) & IMPLEMENTED_RECIPE_APIS
BOOLEAN_TYPES = {"UNION", "INTERSECTION", "SUBTRACTION"}
STEP_APP_PROTOCOLS = {"AP203", "AP214", "AP242"}
OFFSET2D_STATUSES = {
    "Success",
    "EmptyPath",
    "CanNotConnect",
    "CrvReversed",
    "CrvDegenToPoint",
    "UnexpectedFailure",
}
OFFSET2D_CONN_TYPES = {"DoNotConnect", "ByLineSeg", "ByArc"}
OFFSET2D_EXTEND_TYPES = {"TangentExtend", "NatruralExtend", "NaturalExtend"}
PRIMITIVE_KINDS = {"solid_cylinder", "solid_wedge", "solid_sphere", "solid_cone", "solid_torus"}
IMPLEMENTED_BODY_KINDS = PRIMITIVE_KINDS | {
    "plane_sheet",
    "extrude_rect",
    "thicken_rect_sheet",
    "sweep_circle_line",
    "support_sweep_bspline_surface",
    "revolve_line",
    "revolve_rect",
    "pre_boolean_cylinder_wedge",
    "loaded_sgt",
}
BODY_KINDS = set(supported_body_builders(CAPABILITIES)) & IMPLEMENTED_BODY_KINDS
OFFSET_SOURCE_KINDS = {"solid_cylinder", "solid_sphere", "loaded_sgt"}
CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_ASSET_ROOTS = tuple(
    (REPO_ROOT / relative).resolve() for relative in ("artifacts", "test_harness/fixtures", "SGK1.4.10/samples")
)

# Recipe JSON is model-authored, so accepting a misspelled field is more
# dangerous than rejecting an extension that has not yet been wired through
# the fixed harness.  Keep the allowlists next to the validator rather than
# letting the C++ runner silently ignore unknown JSON members.
COMMON_RECIPE_KEYS = {"api", "case_id", "expectations"}
PROVENANCE_RECIPE_KEYS = {
    "dsl_source",
    "dsl_case_id",
    "dsl_variant",
    "family",
    "variant",
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
BODY_COMMON_SUFFIXES = {
    "kind",
    "translate_x",
    "translate_y",
    "translate_z",
    "scale",
    "operation_tol",
    "g1_tol",
    "create_seam_edge",
    "allow_partial_success",
    "boolean_type",
    "operations",
    "source_file",
    "body_index",
}
BODY_KIND_SUFFIXES = {
    "solid_cylinder": {"radius", "height", "angle"},
    "solid_wedge": {"length", "width", "height"},
    "solid_sphere": {"radius"},
    "solid_cone": {"bottom_radius", "top_radius", "height", "angle"},
    "solid_torus": {"long_radius", "short_radius", "angle"},
    "plane_sheet": {"length", "width"},
    "extrude_rect": {"length", "width", "height"},
    "thicken_rect_sheet": {"length", "width", "min_dist", "max_dist"},
    "sweep_circle_line": {"profile_radius", "height"},
    "support_sweep_bspline_surface": {"path_radius", "profile_radius", "height"},
    "revolve_line": {"bottom_radius", "top_radius", "height", "angle"},
    "revolve_rect": {"inner_radius", "outer_radius", "height", "angle"},
    "pre_boolean_cylinder_wedge": {
        "radius",
        "height",
        "length",
        "width",
        "secondary_height",
        "angle",
        "secondary_translate_x",
        "secondary_translate_y",
        "secondary_translate_z",
    },
    "loaded_sgt": set(),
}
GENERIC_EXPECTATION_KEYS = {
    "require_property_calculations",
    "require_finite_properties",
    "require_nonnegative_length_area",
    "require_nonnegative_volume",
    "boolean_volume_relation",
    "boolean_bbox_relation",
    "sample_input_properties",
    "min_result_bodies",
    "max_result_bodies",
    "result_bodies",
    "volume_relation_abs_tol",
    "volume_relation_rel_tol",
    "total_length",
    "total_area",
    "total_volume",
    "total_abs_volume",
    "point_relations",
    "face_point_relations",
    "clash_checks",
    "distance_checks",
    "plane_extreme_checks",
}
for _metric in ("total_length", "total_area", "total_volume", "total_abs_volume"):
    GENERIC_EXPECTATION_KEYS.update(
        {
            f"min_{_metric}",
            f"max_{_metric}",
            f"expected_{_metric}",
            f"{_metric}_abs_tol",
            f"{_metric}_rel_tol",
        }
    )
SPLIT_EXPECTATION_KEYS = {
    "split_outer_body_count",
    "split_inner_body_count",
    "split_wire_body_count",
    "split_total_body_count",
    "split_outer_bodies",
    "split_inner_bodies",
    "split_wire_bodies",
    "split_total_bodies",
}
SLICE_EXPECTATION_KEYS = {
    "slice_result_body_count",
    "slice_wire_body_count",
    "slice_result_bodies",
    "slice_wire_bodies",
}
OFFSET2D_EXPECTATION_KEYS = {"offset2d_status", "offset2d_result_path_count", "offset2d_result_paths"}
TOPOLOGY_SECTION_EXPECTATION_KEYS = {
    "topology_section_edge_count",
    "topology_section_vertex_count",
    "topology_section_total_count",
    "topology_section_edges",
    "topology_section_vertices",
    "topology_section_total",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Recipe JSON file(s) or directories")
    parser.add_argument("--model-diagnostics", default="", help="Optional model-friendly diagnostics JSON path")
    parser.add_argument(
        "--check-assets", action="store_true", help="Fail when source_file references do not exist locally"
    )
    parser.add_argument(
        "--model-asset-policy",
        action="store_true",
        help="Restrict model-authored source_file values to fixed repository asset roots",
    )
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def iter_recipe_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.json")))
        else:
            files.append(path)
    return files


ExpectedType = type | tuple[type, ...]


def type_name(expected_type: ExpectedType) -> str:
    if isinstance(expected_type, tuple):
        return " or ".join(item.__name__ for item in expected_type)
    return expected_type.__name__


def require_key(recipe: dict[str, Any], key: str, expected_type: ExpectedType, errors: list[str]) -> None:
    if key not in recipe:
        errors.append(f"missing required key: {key}")
        return
    if not isinstance(recipe[key], expected_type):
        errors.append(f"{key} must be {type_name(expected_type)}")


def optional_key(recipe: dict[str, Any], key: str, expected_type: ExpectedType, errors: list[str]) -> None:
    if key in recipe and not isinstance(recipe[key], expected_type):
        errors.append(f"{key} must be {type_name(expected_type)}")


def optional_string_list(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    if key not in recipe:
        return
    value = recipe[key]
    if not isinstance(value, list):
        errors.append(f"{key} must be list")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            errors.append(f"{key}[{index}] must be non-empty string")


def reject_unknown_keys(value: Any, allowed: set[str], label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        return
    for key in sorted(value):
        if key not in allowed:
            path = key if label == "recipe" else f"{label}.{key}"
            errors.append(f"unknown field: {path}")


def body_recipe_keys(prefix: str, kind: Any) -> set[str]:
    suffixes = set(BODY_COMMON_SUFFIXES)
    if isinstance(kind, str):
        suffixes.update(BODY_KIND_SUFFIXES.get(kind, set()))
    return {f"{prefix}_{suffix}" for suffix in suffixes}


def expectation_keys_for_api(api: Any) -> set[str]:
    if api == "api_offset2d":
        return set(OFFSET2D_EXPECTATION_KEYS)
    if api == "api_topology_section":
        return set(TOPOLOGY_SECTION_EXPECTATION_KEYS)
    keys = set(GENERIC_EXPECTATION_KEYS)
    if api == "api_boolean_split":
        keys.update(SPLIT_EXPECTATION_KEYS)
    elif api == "api_boolean_slice":
        keys.update(SLICE_EXPECTATION_KEYS)
    return keys


def allowed_recipe_keys(recipe: dict[str, Any]) -> set[str]:
    api = recipe.get("api")
    allowed = set(COMMON_RECIPE_KEYS) | PROVENANCE_RECIPE_KEYS | expectation_keys_for_api(api)
    if api in {"api_boolean", "api_boolean_split", "api_boolean_slice", "api_topology_section"}:
        allowed.update(
            {
                "boolean_type",
                "modeling_tol",
                "max_model_size",
                "check_valid",
                "topo_track",
                "non_destructive",
            }
        )
        allowed.update(body_recipe_keys("target", recipe.get("target_kind")))
        allowed.update(body_recipe_keys("tool", recipe.get("tool_kind")))
    if api == "api_boolean_split":
        allowed.update(
            {
                "split_target_add_face",
                "split_strict_split",
                "split_merge_imprint",
                "split_expectations",
            }
        )
    elif api == "api_boolean_slice":
        allowed.add("slice_expectations")
    elif api == "api_topology_section":
        allowed.add("topology_section_expectations")
    elif api == "api_offset2d":
        allowed.update(
            {
                "offset2d_distance",
                "offset2d_distances",
                "offset2d_dist_tol",
                "offset2d_angle_tol",
                "offset2d_connect_type",
                "offset2d_allow_crv_degenerated",
                "offset2d_allow_crv_reversed",
                "offset2d_allow_self_intersections",
                "offset2d_extend_type",
                "offset2d_path",
                "offset2d_segments",
                "offset2d_expectations",
            }
        )
    elif api == "api_offset_body":
        allowed.update(
            {
                "source_kind",
                "offset_distance",
                "modeling_tol",
                "check_valid",
                "topo_track",
                "source_allow_partial_success",
                "source_g1_tol",
                "source_translate_x",
                "source_translate_y",
                "source_translate_z",
                "source_scale",
                "source_radius",
                "source_height",
                "source_angle",
                "source_create_seam_edge",
                "source_file",
                "source_body_index",
            }
        )
    elif api in {"check_sgt", "step_import", "iges_import", "step_roundtrip", "iges_roundtrip"}:
        allowed.add("source_file")
        if api in {"check_sgt", "step_roundtrip", "iges_roundtrip"}:
            allowed.update({"source_body_index", "body_index"})
        if api == "step_roundtrip":
            allowed.update(
                {
                    "step_app_protocol",
                    "step_surface_to_bspline",
                    "step_curve_to_bspline",
                    "step_spcurve_in_wire_to_bspline",
                    "roundtrip_abs_tol",
                    "roundtrip_rel_tol",
                }
            )
        elif api == "iges_roundtrip":
            allowed.update(
                {
                    "iges_face_only_mode",
                    "iges_write_sgk_specified_data",
                    "roundtrip_abs_tol",
                    "roundtrip_rel_tol",
                }
            )
    return allowed


def require_positive_number(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    require_key(recipe, key, (int, float), errors)
    if key in recipe and isinstance(recipe[key], (int, float)) and recipe[key] <= 0:
        errors.append(f"{key} must be > 0")


def optional_number(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    optional_key(recipe, key, (int, float), errors)


def require_number(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    require_key(recipe, key, (int, float), errors)
    if key in recipe and isinstance(recipe[key], bool):
        errors.append(f"{key} must be number")


def is_number_expr(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)) or isinstance(value, str)


def optional_number_expr(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    if key in recipe and not is_number_expr(recipe[key]):
        errors.append(f"{key} must be number or numeric expression string")


def optional_positive_number(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    optional_number(recipe, key, errors)
    if key in recipe and isinstance(recipe[key], (int, float)) and recipe[key] <= 0:
        errors.append(f"{key} must be > 0")


def validate_metric_expectation(
    value: Any,
    label: str,
    errors: list[str],
    *,
    allowed_extra: set[str] | None = None,
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be object")
        return
    reject_unknown_keys(
        value,
        {"min", "max", "expected", "abs_tol", "rel_tol"} | (allowed_extra or set()),
        label,
        errors,
    )
    for key in ("min", "max", "expected", "abs_tol", "rel_tol"):
        if key in value and not is_number_expr(value[key]):
            errors.append(f"{label}.{key} must be number or numeric expression string")


def validate_expectations_container(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be object")
        return
    for key in (
        "require_property_calculations",
        "require_finite_properties",
        "require_nonnegative_length_area",
        "require_nonnegative_volume",
        "boolean_volume_relation",
        "boolean_bbox_relation",
        "sample_input_properties",
    ):
        if key in value and not isinstance(value[key], bool):
            errors.append(f"{label}.{key} must be bool")
    for key in ("min_result_bodies", "max_result_bodies"):
        if key in value and not (isinstance(value[key], int) and not isinstance(value[key], bool)):
            errors.append(f"{label}.{key} must be int")
    if "result_bodies" in value:
        result_bodies = value["result_bodies"]
        if not isinstance(result_bodies, dict):
            errors.append(f"{label}.result_bodies must be object")
        else:
            reject_unknown_keys(result_bodies, {"min", "max"}, f"{label}.result_bodies", errors)
            for key in ("min", "max"):
                if key in result_bodies and not (
                    isinstance(result_bodies[key], int) and not isinstance(result_bodies[key], bool)
                ):
                    errors.append(f"{label}.result_bodies.{key} must be int")
    for key in ("volume_relation_abs_tol", "volume_relation_rel_tol"):
        if key in value and not is_number_expr(value[key]):
            errors.append(f"{label}.{key} must be number or numeric expression string")
    for key in ("total_length", "total_area", "total_volume", "total_abs_volume"):
        if key in value:
            validate_metric_expectation(value[key], f"{label}.{key}", errors)
        for prefix in ("min", "max", "expected"):
            shorthand = f"{prefix}_{key}"
            if shorthand in value and not is_number_expr(value[shorthand]):
                errors.append(f"{label}.{shorthand} must be number or numeric expression string")
        for suffix in ("abs_tol", "rel_tol"):
            shorthand = f"{key}_{suffix}"
            if shorthand in value and not is_number_expr(value[shorthand]):
                errors.append(f"{label}.{shorthand} must be number or numeric expression string")
    if "point_relations" in value:
        validate_point_relations(value["point_relations"], f"{label}.point_relations", errors)
    if "face_point_relations" in value:
        validate_face_point_relations(value["face_point_relations"], f"{label}.face_point_relations", errors)
    if "clash_checks" in value:
        validate_clash_checks(value["clash_checks"], f"{label}.clash_checks", errors)
    if "distance_checks" in value:
        validate_distance_checks(value["distance_checks"], f"{label}.distance_checks", errors)
    if "plane_extreme_checks" in value:
        validate_plane_extreme_checks(value["plane_extreme_checks"], f"{label}.plane_extreme_checks", errors)
    validate_offset2d_expectations(value, label, errors)
    validate_split_expectations(value, label, errors)
    validate_slice_expectations(value, label, errors)


def validate_offset2d_expectations(value: dict[str, Any], label: str, errors: list[str]) -> None:
    if "offset2d_status" in value:
        status = value["offset2d_status"]
        if not isinstance(status, str) or status not in OFFSET2D_STATUSES:
            errors.append(f"{label}.offset2d_status must be one of {sorted(OFFSET2D_STATUSES)}")
    if "offset2d_result_path_count" in value:
        count = value["offset2d_result_path_count"]
        if not (isinstance(count, int) and not isinstance(count, bool)) or count < 0:
            errors.append(f"{label}.offset2d_result_path_count must be int >= 0")
    if "offset2d_result_paths" in value:
        paths = value["offset2d_result_paths"]
        if not isinstance(paths, dict):
            errors.append(f"{label}.offset2d_result_paths must be object")
        else:
            reject_unknown_keys(paths, {"min", "max"}, f"{label}.offset2d_result_paths", errors)
            bounds: dict[str, int] = {}
            for key in ("min", "max"):
                if key not in paths:
                    continue
                item = paths[key]
                if not (isinstance(item, int) and not isinstance(item, bool)) or item < 0:
                    errors.append(f"{label}.offset2d_result_paths.{key} must be int >= 0")
                else:
                    bounds[key] = item
            if "min" in bounds and "max" in bounds and bounds["max"] < bounds["min"]:
                errors.append(f"{label}.offset2d_result_paths.max must be >= min")


def validate_count_expectation(value: Any, label: str, errors: list[str]) -> None:
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            errors.append(f"{label} must be >= 0")
        return
    if not isinstance(value, dict):
        errors.append(f"{label} must be int or object")
        return
    reject_unknown_keys(value, {"min", "max"}, label, errors)
    bounds: dict[str, int] = {}
    for key in ("min", "max"):
        if key not in value:
            continue
        item = value[key]
        if not (isinstance(item, int) and not isinstance(item, bool)) or item < 0:
            errors.append(f"{label}.{key} must be int >= 0")
        else:
            bounds[key] = item
    if not any(key in value for key in ("min", "max")):
        errors.append(f"{label} must contain min or max")
    if "min" in bounds and "max" in bounds and bounds["max"] < bounds["min"]:
        errors.append(f"{label}.max must be >= min")


def validate_split_expectations(value: dict[str, Any], label: str, errors: list[str]) -> None:
    for key in (
        "split_outer_body_count",
        "split_inner_body_count",
        "split_wire_body_count",
        "split_total_body_count",
        "split_outer_bodies",
        "split_inner_bodies",
        "split_wire_bodies",
        "split_total_bodies",
    ):
        if key in value:
            validate_count_expectation(value[key], f"{label}.{key}", errors)


def validate_slice_expectations(value: dict[str, Any], label: str, errors: list[str]) -> None:
    for key in (
        "slice_result_body_count",
        "slice_wire_body_count",
        "slice_result_bodies",
        "slice_wire_bodies",
    ):
        if key in value:
            validate_count_expectation(value[key], f"{label}.{key}", errors)


def validate_topology_section_expectations(value: dict[str, Any], label: str, errors: list[str]) -> None:
    for key in TOPOLOGY_SECTION_EXPECTATION_KEYS:
        if key in value:
            validate_count_expectation(value[key], f"{label}.{key}", errors)


def validate_point_relations(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{label} must be list")
        return
    allowed_roles = {"result", "target", "tool"}
    allowed_expected = {"Unknown", "OnVertex", "OnEdge", "OnFace", "Inside", "Outside", "OnBoundary", "OnModel"}
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_label} must be object")
            continue
        reject_unknown_keys(
            item,
            {"id", "role", "body_index", "point", "point_ref", "expected", "tolerance", "check_boundary", "required"},
            item_label,
            errors,
        )
        if "id" in item and not isinstance(item["id"], str):
            errors.append(f"{item_label}.id must be string")
        if "point_ref" in item and not isinstance(item["point_ref"], str):
            errors.append(f"{item_label}.point_ref must be string")
        role = item.get("role", "result")
        if role not in allowed_roles:
            errors.append(f"{item_label}.role must be one of {sorted(allowed_roles)}")
        expected = item.get("expected", "Inside")
        if expected not in allowed_expected:
            errors.append(f"{item_label}.expected must be one of {sorted(allowed_expected)}")
        body_index = item.get("body_index", 0)
        if not (isinstance(body_index, int) and not isinstance(body_index, bool)) or body_index < 0:
            errors.append(f"{item_label}.body_index must be int >= 0")
        point = item.get("point")
        if not isinstance(point, list) or len(point) != 3:
            errors.append(f"{item_label}.point must be length-3 list")
        elif not all(is_number_expr(coord) for coord in point):
            errors.append(f"{item_label}.point values must be number or numeric expression string")
        if "tolerance" in item and not is_number_expr(item["tolerance"]):
            errors.append(f"{item_label}.tolerance must be number or numeric expression string")
        for key in ("check_boundary", "required"):
            if key in item and not isinstance(item[key], bool):
                errors.append(f"{item_label}.{key} must be bool")


def validate_face_point_relations(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{label} must be list")
        return
    allowed_roles = {"result", "target", "tool"}
    allowed_expected = {"Unknown", "OnVertex", "OnEdge", "Inside", "Outside", "OnBoundary", "OnFace"}
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_label} must be object")
            continue
        reject_unknown_keys(
            item,
            {
                "id",
                "role",
                "body_index",
                "face_index",
                "face_id",
                "point",
                "point_ref",
                "uv",
                "uv_fraction",
                "expected",
                "tolerance",
                "check_boundary",
                "required",
            },
            item_label,
            errors,
        )
        if "id" in item and not isinstance(item["id"], str):
            errors.append(f"{item_label}.id must be string")
        if "point_ref" in item and not isinstance(item["point_ref"], str):
            errors.append(f"{item_label}.point_ref must be string")
        role = item.get("role", "result")
        if role not in allowed_roles:
            errors.append(f"{item_label}.role must be one of {sorted(allowed_roles)}")
        expected = item.get("expected", "Inside")
        if expected not in allowed_expected:
            errors.append(f"{item_label}.expected must be one of {sorted(allowed_expected)}")
        for key in ("body_index", "face_index", "face_id"):
            if key in item and (not (isinstance(item[key], int) and not isinstance(item[key], bool)) or item[key] < 0):
                errors.append(f"{item_label}.{key} must be int >= 0")
        point = item.get("point")
        if point is not None:
            if not isinstance(point, list) or len(point) != 3:
                errors.append(f"{item_label}.point must be length-3 list")
            elif not all(is_number_expr(coord) for coord in point):
                errors.append(f"{item_label}.point values must be number or numeric expression string")
        uv = item.get("uv")
        if uv is not None:
            if not isinstance(uv, list) or len(uv) != 2:
                errors.append(f"{item_label}.uv must be length-2 list")
            elif not all(is_number_expr(coord) for coord in uv):
                errors.append(f"{item_label}.uv values must be number or numeric expression string")
        uv_fraction = item.get("uv_fraction")
        if uv_fraction is not None:
            if not isinstance(uv_fraction, list) or len(uv_fraction) != 2:
                errors.append(f"{item_label}.uv_fraction must be length-2 list")
            elif not all(is_number_expr(coord) for coord in uv_fraction):
                errors.append(f"{item_label}.uv_fraction values must be number or numeric expression string")
        if "tolerance" in item and not is_number_expr(item["tolerance"]):
            errors.append(f"{item_label}.tolerance must be number or numeric expression string")
        for key in ("check_boundary", "required"):
            if key in item and not isinstance(item[key], bool):
                errors.append(f"{item_label}.{key} must be bool")


def validate_clash_checks(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{label} must be list")
        return
    allowed_roles = {"result", "target", "tool"}
    allowed_modes = {"ClashExistenceOnly", "ClashClassify", "ClashClassifySubEntities"}
    allowed_expected = {
        "Clash_None",
        "Clash_Exists",
        "Clash_AInB",
        "Clash_BInA",
        "Clash_Touch",
        "Clash_Interfere",
        "NoClash",
        "AnyClash",
    }
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_label} must be object")
            continue
        reject_unknown_keys(
            item,
            {
                "id",
                "role_a",
                "role_b",
                "body_index_a",
                "body_index_b",
                "expected",
                "mode",
                "tolerance",
                "required",
            },
            item_label,
            errors,
        )
        if "id" in item and not isinstance(item["id"], str):
            errors.append(f"{item_label}.id must be string")
        role_a = item.get("role_a", "target")
        role_b = item.get("role_b", "tool")
        if role_a not in allowed_roles:
            errors.append(f"{item_label}.role_a must be one of {sorted(allowed_roles)}")
        if role_b not in allowed_roles:
            errors.append(f"{item_label}.role_b must be one of {sorted(allowed_roles)}")
        expected = item.get("expected", "Clash_None")
        if expected not in allowed_expected:
            errors.append(f"{item_label}.expected must be one of {sorted(allowed_expected)}")
        mode = item.get("mode", "ClashClassify")
        if mode not in allowed_modes:
            errors.append(f"{item_label}.mode must be one of {sorted(allowed_modes)}")
        body_index_a = item.get("body_index_a", 0)
        body_index_b = item.get("body_index_b", 0)
        if not (isinstance(body_index_a, int) and not isinstance(body_index_a, bool)) or body_index_a < 0:
            errors.append(f"{item_label}.body_index_a must be int >= 0")
        if not (isinstance(body_index_b, int) and not isinstance(body_index_b, bool)) or body_index_b < 0:
            errors.append(f"{item_label}.body_index_b must be int >= 0")
        if "tolerance" in item and not is_number_expr(item["tolerance"]):
            errors.append(f"{item_label}.tolerance must be number or numeric expression string")
        if "required" in item and not isinstance(item["required"], bool):
            errors.append(f"{item_label}.required must be bool")


def validate_distance_checks(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{label} must be list")
        return
    allowed_roles = {"result", "target", "tool"}
    allowed_kinds = {"minimum", "maximum"}
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_label} must be object")
            continue
        validate_metric_expectation(
            item,
            item_label,
            errors,
            allowed_extra={
                "id",
                "role_a",
                "role_b",
                "body_index_a",
                "body_index_b",
                "kind",
                "threshold",
                "distance",
                "required",
            },
        )
        if "distance" in item:
            validate_metric_expectation(item["distance"], f"{item_label}.distance", errors)
        if "id" in item and not isinstance(item["id"], str):
            errors.append(f"{item_label}.id must be string")
        role_a = item.get("role_a", "target")
        role_b = item.get("role_b", "tool")
        if role_a not in allowed_roles:
            errors.append(f"{item_label}.role_a must be one of {sorted(allowed_roles)}")
        if role_b not in allowed_roles:
            errors.append(f"{item_label}.role_b must be one of {sorted(allowed_roles)}")
        kind = item.get("kind", "minimum")
        if kind not in allowed_kinds:
            errors.append(f"{item_label}.kind must be one of {sorted(allowed_kinds)}")
        body_index_a = item.get("body_index_a", 0)
        body_index_b = item.get("body_index_b", 0)
        if not (isinstance(body_index_a, int) and not isinstance(body_index_a, bool)) or body_index_a < 0:
            errors.append(f"{item_label}.body_index_a must be int >= 0")
        if not (isinstance(body_index_b, int) and not isinstance(body_index_b, bool)) or body_index_b < 0:
            errors.append(f"{item_label}.body_index_b must be int >= 0")
        if "threshold" in item:
            if not is_number_expr(item["threshold"]):
                errors.append(f"{item_label}.threshold must be number or numeric expression string")
            elif (
                isinstance(item["threshold"], (int, float))
                and not isinstance(item["threshold"], bool)
                and item["threshold"] <= 0
            ):
                errors.append(f"{item_label}.threshold must be > 0")
        if "required" in item and not isinstance(item["required"], bool):
            errors.append(f"{item_label}.required must be bool")


def validate_plane_extreme_checks(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{label} must be list")
        return
    allowed_roles = {"result", "target", "tool"}
    allowed_axes = {"x", "y", "z"}
    allowed_sides = {"min", "max"}
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_label} must be object")
            continue
        reject_unknown_keys(
            item,
            {
                "id",
                "role",
                "body_index",
                "axis",
                "side",
                "expected",
                "compare_expected",
                "tolerance",
                "probe_coordinate",
                "plane_span",
                "plane_span_scale",
                "required",
                "export_debug_geometry",
            },
            item_label,
            errors,
        )
        if "id" in item and not isinstance(item["id"], str):
            errors.append(f"{item_label}.id must be string")
        role = item.get("role", "result")
        if role not in allowed_roles:
            errors.append(f"{item_label}.role must be one of {sorted(allowed_roles)}")
        axis = item.get("axis", "x")
        if axis not in allowed_axes:
            errors.append(f"{item_label}.axis must be one of {sorted(allowed_axes)}")
        side = item.get("side", "min")
        if side not in allowed_sides:
            errors.append(f"{item_label}.side must be one of {sorted(allowed_sides)}")
        body_index = item.get("body_index", 0)
        if not (isinstance(body_index, int) and not isinstance(body_index, bool)) or body_index < 0:
            errors.append(f"{item_label}.body_index must be int >= 0")
        compare_expected = item.get("compare_expected", True)
        if "expected" not in item and compare_expected is not False:
            errors.append(f"{item_label}.expected is required when compare_expected is true")
        elif "expected" in item and not is_number_expr(item["expected"]):
            errors.append(f"{item_label}.expected must be number or numeric expression string")
        for key in ("tolerance", "probe_coordinate", "plane_span", "plane_span_scale"):
            if key in item:
                if not is_number_expr(item[key]):
                    errors.append(f"{item_label}.{key} must be number or numeric expression string")
                elif isinstance(item[key], (int, float)) and not isinstance(item[key], bool):
                    if key in {"tolerance", "plane_span_scale"} and item[key] <= 0:
                        errors.append(f"{item_label}.{key} must be > 0")
                    if key == "plane_span" and item[key] < 0:
                        errors.append(f"{item_label}.{key} must be >= 0")
        for key in ("required", "export_debug_geometry", "compare_expected"):
            if key in item and not isinstance(item[key], bool):
                errors.append(f"{item_label}.{key} must be bool")


def validate_expectations(recipe: dict[str, Any], errors: list[str]) -> None:
    if "expectations" in recipe:
        expectations = recipe["expectations"]
        if isinstance(expectations, dict):
            reject_unknown_keys(expectations, expectation_keys_for_api(recipe.get("api")), "expectations", errors)
            if recipe.get("api") == "api_topology_section":
                validate_topology_section_expectations(expectations, "expectations", errors)
        validate_expectations_container(expectations, "expectations", errors)
    validate_expectations_container(recipe, "recipe", errors)


def validate_common_body_fields(recipe: dict[str, Any], prefix: str, errors: list[str]) -> None:
    optional_number(recipe, f"{prefix}_translate_x", errors)
    optional_number(recipe, f"{prefix}_translate_y", errors)
    optional_number(recipe, f"{prefix}_translate_z", errors)
    optional_positive_number(recipe, f"{prefix}_scale", errors)
    optional_positive_number(recipe, f"{prefix}_operation_tol", errors)
    optional_positive_number(recipe, f"{prefix}_g1_tol", errors)
    optional_key(recipe, f"{prefix}_create_seam_edge", bool, errors)
    optional_key(recipe, f"{prefix}_allow_partial_success", bool, errors)
    optional_key(recipe, f"{prefix}_boolean_type", str, errors)
    optional_string_list(recipe, f"{prefix}_operations", errors)
    if recipe.get(f"{prefix}_boolean_type") not in (None, *BOOLEAN_TYPES):
        errors.append(f"{prefix}_boolean_type must be one of {sorted(BOOLEAN_TYPES)}")
    optional_key(recipe, f"{prefix}_source_file", str, errors)
    optional_key(recipe, f"{prefix}_body_index", int, errors)
    if isinstance(recipe.get(f"{prefix}_body_index"), int) and recipe[f"{prefix}_body_index"] < 0:
        errors.append(f"{prefix}_body_index must be >= 0")


def validate_body_spec(
    recipe: dict[str, Any],
    prefix: str,
    errors: list[str],
    *,
    allowed_kinds: set[str] | None = None,
    check_assets: bool = False,
    asset_policy: str = "trusted",
) -> None:
    require_key(recipe, f"{prefix}_kind", str, errors)
    kind = recipe.get(f"{prefix}_kind")
    supported_kinds = BODY_KINDS if allowed_kinds is None else BODY_KINDS & allowed_kinds
    if not isinstance(kind, str) or kind not in supported_kinds:
        errors.append(f"{prefix}_kind must be one of {sorted(supported_kinds)}")
        return

    validate_common_body_fields(recipe, prefix, errors)

    if kind == "solid_cylinder":
        require_positive_number(recipe, f"{prefix}_radius", errors)
        require_positive_number(recipe, f"{prefix}_height", errors)
        optional_positive_number(recipe, f"{prefix}_angle", errors)
    elif kind == "solid_wedge":
        require_positive_number(recipe, f"{prefix}_length", errors)
        require_positive_number(recipe, f"{prefix}_width", errors)
        require_positive_number(recipe, f"{prefix}_height", errors)
    elif kind == "solid_sphere":
        require_positive_number(recipe, f"{prefix}_radius", errors)
    elif kind == "solid_cone":
        require_positive_number(recipe, f"{prefix}_bottom_radius", errors)
        optional_number(recipe, f"{prefix}_top_radius", errors)
        require_positive_number(recipe, f"{prefix}_height", errors)
        optional_positive_number(recipe, f"{prefix}_angle", errors)
    elif kind == "solid_torus":
        require_positive_number(recipe, f"{prefix}_long_radius", errors)
        require_positive_number(recipe, f"{prefix}_short_radius", errors)
        optional_positive_number(recipe, f"{prefix}_angle", errors)
    elif kind == "plane_sheet":
        require_positive_number(recipe, f"{prefix}_length", errors)
        require_positive_number(recipe, f"{prefix}_width", errors)
    elif kind == "extrude_rect":
        require_positive_number(recipe, f"{prefix}_length", errors)
        require_positive_number(recipe, f"{prefix}_width", errors)
        require_positive_number(recipe, f"{prefix}_height", errors)
    elif kind == "thicken_rect_sheet":
        require_positive_number(recipe, f"{prefix}_length", errors)
        require_positive_number(recipe, f"{prefix}_width", errors)
        optional_number(recipe, f"{prefix}_min_dist", errors)
        optional_number(recipe, f"{prefix}_max_dist", errors)
        min_dist = recipe.get(f"{prefix}_min_dist", -10.0)
        max_dist = recipe.get(f"{prefix}_max_dist", 20.0)
        if (
            isinstance(min_dist, (int, float))
            and isinstance(max_dist, (int, float))
            and not isinstance(min_dist, bool)
            and not isinstance(max_dist, bool)
            and max_dist <= min_dist
        ):
            errors.append(f"{prefix}_max_dist must be greater than {prefix}_min_dist")
    elif kind == "sweep_circle_line":
        require_positive_number(recipe, f"{prefix}_profile_radius", errors)
        require_positive_number(recipe, f"{prefix}_height", errors)
    elif kind == "support_sweep_bspline_surface":
        require_positive_number(recipe, f"{prefix}_path_radius", errors)
        require_positive_number(recipe, f"{prefix}_profile_radius", errors)
        require_positive_number(recipe, f"{prefix}_height", errors)
    elif kind == "revolve_line":
        require_positive_number(recipe, f"{prefix}_bottom_radius", errors)
        require_positive_number(recipe, f"{prefix}_top_radius", errors)
        require_positive_number(recipe, f"{prefix}_height", errors)
        optional_positive_number(recipe, f"{prefix}_angle", errors)
    elif kind == "revolve_rect":
        require_positive_number(recipe, f"{prefix}_inner_radius", errors)
        require_positive_number(recipe, f"{prefix}_outer_radius", errors)
        require_positive_number(recipe, f"{prefix}_height", errors)
        optional_positive_number(recipe, f"{prefix}_angle", errors)
        inner = recipe.get(f"{prefix}_inner_radius")
        outer = recipe.get(f"{prefix}_outer_radius")
        if (
            isinstance(inner, (int, float))
            and isinstance(outer, (int, float))
            and not isinstance(inner, bool)
            and not isinstance(outer, bool)
        ):
            if outer <= inner:
                errors.append(f"{prefix}_outer_radius must be greater than {prefix}_inner_radius")
    elif kind == "pre_boolean_cylinder_wedge":
        require_positive_number(recipe, f"{prefix}_radius", errors)
        require_positive_number(recipe, f"{prefix}_height", errors)
        require_positive_number(recipe, f"{prefix}_length", errors)
        require_positive_number(recipe, f"{prefix}_width", errors)
        require_positive_number(recipe, f"{prefix}_secondary_height", errors)
        optional_positive_number(recipe, f"{prefix}_angle", errors)
        optional_number(recipe, f"{prefix}_secondary_translate_x", errors)
        optional_number(recipe, f"{prefix}_secondary_translate_y", errors)
        optional_number(recipe, f"{prefix}_secondary_translate_z", errors)
    elif kind == "loaded_sgt":
        require_key(recipe, f"{prefix}_source_file", str, errors)
        if isinstance(recipe.get(f"{prefix}_source_file"), str):
            validate_source_asset(
                recipe[f"{prefix}_source_file"],
                f"{prefix}_source_file",
                errors,
                check_assets=check_assets,
                asset_policy=asset_policy,
            )


def validate_binary_body_api(
    recipe: dict[str, Any],
    errors: list[str],
    *,
    require_boolean_type: bool,
    check_assets: bool = False,
    asset_policy: str = "trusted",
) -> None:
    require_key(recipe, "case_id", str, errors)
    if require_boolean_type:
        require_key(recipe, "boolean_type", str, errors)
    else:
        optional_key(recipe, "boolean_type", str, errors)
    if recipe.get("boolean_type") not in (None, *BOOLEAN_TYPES):
        errors.append(f"boolean_type must be one of {sorted(BOOLEAN_TYPES)}")
    require_positive_number(recipe, "modeling_tol", errors)
    optional_positive_number(recipe, "max_model_size", errors)
    optional_key(recipe, "check_valid", bool, errors)
    optional_key(recipe, "topo_track", bool, errors)
    optional_key(recipe, "non_destructive", bool, errors)
    api = recipe.get("api")
    api_record = CAPABILITIES.get("apis", {}).get(api, {}) if isinstance(api, str) else {}
    allowed_kinds = set(api_record.get("supported_body_builders", [])) & IMPLEMENTED_BODY_KINDS
    validate_body_spec(
        recipe,
        "target",
        errors,
        allowed_kinds=allowed_kinds,
        check_assets=check_assets,
        asset_policy=asset_policy,
    )
    validate_body_spec(
        recipe,
        "tool",
        errors,
        allowed_kinds=allowed_kinds,
        check_assets=check_assets,
        asset_policy=asset_policy,
    )


def validate_api_boolean(
    recipe: dict[str, Any], errors: list[str], *, check_assets: bool = False, asset_policy: str = "trusted"
) -> None:
    validate_binary_body_api(
        recipe,
        errors,
        require_boolean_type=True,
        check_assets=check_assets,
        asset_policy=asset_policy,
    )


def validate_api_boolean_split(
    recipe: dict[str, Any], errors: list[str], *, check_assets: bool = False, asset_policy: str = "trusted"
) -> None:
    validate_binary_body_api(
        recipe,
        errors,
        require_boolean_type=False,
        check_assets=check_assets,
        asset_policy=asset_policy,
    )
    for key in ("split_target_add_face", "split_strict_split", "split_merge_imprint"):
        optional_key(recipe, key, bool, errors)
    if "split_expectations" in recipe:
        expectations = recipe["split_expectations"]
        if not isinstance(expectations, dict):
            errors.append("split_expectations must be object")
        else:
            reject_unknown_keys(expectations, SPLIT_EXPECTATION_KEYS, "split_expectations", errors)
            validate_split_expectations(expectations, "split_expectations", errors)


def validate_api_boolean_slice(
    recipe: dict[str, Any], errors: list[str], *, check_assets: bool = False, asset_policy: str = "trusted"
) -> None:
    validate_binary_body_api(
        recipe,
        errors,
        require_boolean_type=False,
        check_assets=check_assets,
        asset_policy=asset_policy,
    )
    if "slice_expectations" in recipe:
        expectations = recipe["slice_expectations"]
        if not isinstance(expectations, dict):
            errors.append("slice_expectations must be object")
        else:
            reject_unknown_keys(expectations, SLICE_EXPECTATION_KEYS, "slice_expectations", errors)
            validate_slice_expectations(expectations, "slice_expectations", errors)


def validate_api_topology_section(
    recipe: dict[str, Any], errors: list[str], *, check_assets: bool = False, asset_policy: str = "trusted"
) -> None:
    validate_binary_body_api(
        recipe,
        errors,
        require_boolean_type=False,
        check_assets=check_assets,
        asset_policy=asset_policy,
    )
    if "topology_section_expectations" in recipe:
        expectations = recipe["topology_section_expectations"]
        if not isinstance(expectations, dict):
            errors.append("topology_section_expectations must be object")
        else:
            reject_unknown_keys(
                expectations,
                TOPOLOGY_SECTION_EXPECTATION_KEYS,
                "topology_section_expectations",
                errors,
            )
            validate_topology_section_expectations(
                expectations,
                "topology_section_expectations",
                errors,
            )


def validate_point2(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list) or len(value) != 2:
        errors.append(f"{label} must be length-2 list")
    elif not all(is_number_expr(coord) for coord in value):
        errors.append(f"{label} values must be number or numeric expression string")


def validate_offset2d_segment(item: Any, label: str, errors: list[str]) -> None:
    if not isinstance(item, dict):
        errors.append(f"{label} must be object")
        return
    kind = item.get("kind", "line")
    if not isinstance(kind, str) or kind not in {"line", "arc"}:
        errors.append(f"{label}.kind must be line or arc")
        return
    allowed = (
        {"kind", "sense", "start", "end"}
        if kind == "line"
        else {
            "kind",
            "sense",
            "center",
            "radius",
            "start_angle",
            "end_angle",
            "ccw",
        }
    )
    reject_unknown_keys(item, allowed, label, errors)
    for key in ("sense", "ccw"):
        if key in item and not isinstance(item[key], bool):
            errors.append(f"{label}.{key} must be bool")
    if kind == "line":
        for key in ("start", "end"):
            if key not in item:
                errors.append(f"missing required key: {label}.{key}")
            else:
                validate_point2(item[key], f"{label}.{key}", errors)
        start = item.get("start")
        end = item.get("end")
        if (
            isinstance(start, list)
            and isinstance(end, list)
            and len(start) == 2
            and len(end) == 2
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (*start, *end))
            and start == end
        ):
            errors.append(f"{label} line start and end must differ")
        return

    for key in ("center",):
        if key not in item:
            errors.append(f"missing required key: {label}.{key}")
        else:
            validate_point2(item[key], f"{label}.{key}", errors)
    for key in ("radius", "start_angle", "end_angle"):
        if key not in item:
            errors.append(f"missing required key: {label}.{key}")
        elif not is_number_expr(item[key]):
            errors.append(f"{label}.{key} must be number or numeric expression string")
    radius = item.get("radius")
    if isinstance(radius, (int, float)) and not isinstance(radius, bool) and radius <= 0:
        errors.append(f"{label}.radius must be > 0")


def validate_api_offset2d(recipe: dict[str, Any], errors: list[str]) -> None:
    require_key(recipe, "case_id", str, errors)
    has_uniform_distance = "offset2d_distance" in recipe
    has_segment_distances = "offset2d_distances" in recipe
    if has_uniform_distance == has_segment_distances:
        errors.append("exactly one of offset2d_distance or offset2d_distances is required")
    if has_uniform_distance and not is_number_expr(recipe["offset2d_distance"]):
        errors.append("offset2d_distance must be number or numeric expression string")
    if has_segment_distances:
        distances = recipe["offset2d_distances"]
        if not isinstance(distances, list) or not distances:
            errors.append("offset2d_distances must be a non-empty list")
        elif not all(is_number_expr(item) for item in distances):
            errors.append("offset2d_distances values must be number or numeric expression string")

    for key in ("offset2d_dist_tol", "offset2d_angle_tol"):
        if key not in recipe:
            continue
        value = recipe[key]
        if not is_number_expr(value):
            errors.append(f"{key} must be number or numeric expression string")
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
            errors.append(f"{key} must be > 0")
    connect_type = recipe.get("offset2d_connect_type")
    if connect_type is not None and (not isinstance(connect_type, str) or connect_type not in OFFSET2D_CONN_TYPES):
        errors.append(f"offset2d_connect_type must be one of {sorted(OFFSET2D_CONN_TYPES)}")
    extend_type = recipe.get("offset2d_extend_type")
    if extend_type is not None and (not isinstance(extend_type, str) or extend_type not in OFFSET2D_EXTEND_TYPES):
        errors.append(f"offset2d_extend_type must be one of {sorted(OFFSET2D_EXTEND_TYPES)}")
    for key in (
        "offset2d_allow_crv_degenerated",
        "offset2d_allow_crv_reversed",
        "offset2d_allow_self_intersections",
    ):
        optional_key(recipe, key, bool, errors)

    has_path = "offset2d_path" in recipe
    has_segments = "offset2d_segments" in recipe
    if has_path and has_segments:
        errors.append("use offset2d_path or offset2d_segments, not both")
    path = recipe.get("offset2d_path", recipe.get("offset2d_segments"))
    if not isinstance(path, list) or not path:
        errors.append("offset2d_path must be a non-empty list")
    else:
        for index, item in enumerate(path):
            validate_offset2d_segment(item, f"offset2d_path[{index}]", errors)
        distances = recipe.get("offset2d_distances")
        if isinstance(distances, list) and len(distances) != len(path):
            errors.append("offset2d_distances size must match offset2d_path size")
    if "offset2d_expectations" in recipe:
        expectations = recipe["offset2d_expectations"]
        if not isinstance(expectations, dict):
            errors.append("offset2d_expectations must be object")
        else:
            reject_unknown_keys(expectations, OFFSET2D_EXPECTATION_KEYS, "offset2d_expectations", errors)
            validate_offset2d_expectations(expectations, "offset2d_expectations", errors)


def safe_source_asset_path(value: str) -> tuple[Path | None, str]:
    path = Path(value)
    if path.is_absolute() or path.drive or path.root:
        return None, "must be a repository-relative path, not an absolute or UNC path"
    if ".." in path.parts:
        return None, "must not contain '..' traversal"
    resolved = (REPO_ROOT / path).resolve()
    if not any(resolved == root or root in resolved.parents for root in ALLOWED_ASSET_ROOTS):
        return None, "must stay under artifacts/, test_harness/fixtures/, or SGK1.4.10/samples/"
    return resolved, ""


def validate_source_asset(
    value: str,
    label: str,
    errors: list[str],
    *,
    check_assets: bool,
    asset_policy: str,
) -> None:
    source_file = value.strip()
    if not source_file:
        errors.append(f"{label} must not be empty")
        return
    if asset_policy == "model":
        resolved, unsafe_reason = safe_source_asset_path(source_file)
        if unsafe_reason:
            errors.append(f"unsafe {label}: {unsafe_reason}")
            return
    elif asset_policy == "trusted":
        resolved = Path(source_file).resolve()
    else:
        raise ValueError(f"unknown asset policy: {asset_policy}")
    if check_assets and resolved is not None and not resolved.is_file():
        errors.append(f"{label} not found: {source_file}")


def validate_source_file_recipe(
    recipe: dict[str, Any], errors: list[str], *, check_assets: bool = False, asset_policy: str = "trusted"
) -> None:
    require_key(recipe, "case_id", str, errors)
    require_key(recipe, "source_file", str, errors)
    if isinstance(recipe.get("source_file"), str):
        validate_source_asset(
            recipe["source_file"],
            "source_file",
            errors,
            check_assets=check_assets,
            asset_policy=asset_policy,
        )
    for key in ("source_body_index", "body_index"):
        if key in recipe and (
            not (isinstance(recipe[key], int) and not isinstance(recipe[key], bool)) or recipe[key] < 0
        ):
            errors.append(f"{key} must be int >= 0")
    optional_key(recipe, "step_app_protocol", str, errors)
    if recipe.get("step_app_protocol") not in (None, *STEP_APP_PROTOCOLS):
        errors.append(f"step_app_protocol must be one of {sorted(STEP_APP_PROTOCOLS)}")
    for key in (
        "step_surface_to_bspline",
        "step_curve_to_bspline",
        "step_spcurve_in_wire_to_bspline",
        "iges_face_only_mode",
        "iges_write_sgk_specified_data",
    ):
        optional_key(recipe, key, bool, errors)
    optional_positive_number(recipe, "roundtrip_abs_tol", errors)
    optional_positive_number(recipe, "roundtrip_rel_tol", errors)


def validate_api_offset_body(
    recipe: dict[str, Any], errors: list[str], *, check_assets: bool = False, asset_policy: str = "trusted"
) -> None:
    require_key(recipe, "case_id", str, errors)
    require_key(recipe, "source_kind", str, errors)
    if recipe.get("source_kind") not in OFFSET_SOURCE_KINDS:
        errors.append(f"source_kind must be one of {sorted(OFFSET_SOURCE_KINDS)}")
    require_number(recipe, "offset_distance", errors)
    if isinstance(recipe.get("offset_distance"), (int, float)) and not isinstance(recipe.get("offset_distance"), bool):
        if recipe["offset_distance"] == 0:
            errors.append("offset_distance must be non-zero")
    require_positive_number(recipe, "modeling_tol", errors)
    optional_key(recipe, "check_valid", bool, errors)
    optional_key(recipe, "topo_track", bool, errors)
    optional_key(recipe, "source_allow_partial_success", bool, errors)
    optional_positive_number(recipe, "source_g1_tol", errors)

    source_kind = recipe.get("source_kind")
    if source_kind == "solid_cylinder":
        require_positive_number(recipe, "source_radius", errors)
        require_positive_number(recipe, "source_height", errors)
        optional_positive_number(recipe, "source_angle", errors)
        optional_key(recipe, "source_create_seam_edge", bool, errors)
    elif source_kind == "solid_sphere":
        require_positive_number(recipe, "source_radius", errors)
        optional_key(recipe, "source_create_seam_edge", bool, errors)
    elif source_kind == "loaded_sgt":
        require_key(recipe, "source_file", str, errors)
        if isinstance(recipe.get("source_file"), str):
            validate_source_asset(
                recipe["source_file"],
                "source_file",
                errors,
                check_assets=check_assets,
                asset_policy=asset_policy,
            )
        if "source_body_index" in recipe and (
            not (isinstance(recipe["source_body_index"], int) and not isinstance(recipe["source_body_index"], bool))
            or recipe["source_body_index"] < 0
        ):
            errors.append("source_body_index must be int >= 0")

    for key in ("source_translate_x", "source_translate_y", "source_translate_z"):
        optional_number(recipe, key, errors)
    optional_positive_number(recipe, "source_scale", errors)


def validate_recipe(recipe: dict[str, Any], *, check_assets: bool = False, asset_policy: str = "trusted") -> list[str]:
    errors: list[str] = []
    require_key(recipe, "api", str, errors)
    api = recipe.get("api")
    if api not in SUPPORTED_APIS:
        errors.append(f"api must be one of {sorted(SUPPORTED_APIS)}")
        return errors

    plugin = PLUGIN_RECORDS.get(api) if isinstance(api, str) else None
    if plugin is not None:
        schema = json.loads(plugin.recipe_schema_path.read_text(encoding="utf-8-sig"))
        validator = Draft202012Validator(schema)
        for item in sorted(validator.iter_errors(recipe), key=lambda error: list(error.absolute_path)):
            path = ".".join(str(part) for part in item.absolute_path) or "recipe"
            errors.append(f"{path}: {item.message}")
        case_id = recipe.get("case_id")
        if isinstance(case_id, str) and not CASE_ID_PATTERN.fullmatch(case_id):
            errors.append("case_id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
        return errors

    reject_unknown_keys(recipe, allowed_recipe_keys(recipe), "recipe", errors)
    validate_expectations(recipe, errors)

    if api == "api_boolean":
        validate_api_boolean(recipe, errors, check_assets=check_assets, asset_policy=asset_policy)
    elif api == "api_boolean_slice":
        validate_api_boolean_slice(recipe, errors, check_assets=check_assets, asset_policy=asset_policy)
    elif api == "api_boolean_split":
        validate_api_boolean_split(recipe, errors, check_assets=check_assets, asset_policy=asset_policy)
    elif api == "api_offset2d":
        validate_api_offset2d(recipe, errors)
    elif api == "api_offset_body":
        validate_api_offset_body(recipe, errors, check_assets=check_assets, asset_policy=asset_policy)
    elif api == "api_topology_section":
        validate_api_topology_section(recipe, errors, check_assets=check_assets, asset_policy=asset_policy)
    elif api in {"check_sgt", "step_import", "iges_import", "step_roundtrip", "iges_roundtrip"}:
        validate_source_file_recipe(recipe, errors, check_assets=check_assets, asset_policy=asset_policy)

    case_id = recipe.get("case_id")
    if isinstance(case_id, str) and not CASE_ID_PATTERN.fullmatch(case_id):
        errors.append("case_id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    return errors


def validate_file(path: Path, *, check_assets: bool = False, asset_policy: str = "trusted") -> list[str]:
    if not path.exists():
        return [f"file not found: {path}"]
    try:
        recipe = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]
    if not isinstance(recipe, dict):
        return ["recipe root must be an object"]
    return validate_recipe(recipe, check_assets=check_assets, asset_policy=asset_policy)


def validation_field_path(error: str) -> str:
    if error.startswith("missing required key: "):
        return error.removeprefix("missing required key: ").strip()
    if error.startswith("unknown field: "):
        return error.removeprefix("unknown field: ").strip()
    for marker in (" must be one of ", " must be greater than ", " must be "):
        if marker in error:
            return error.split(marker, 1)[0].strip()
    for marker in (" is required when ", " must not be empty"):
        if marker in error:
            return error.split(marker, 1)[0].strip()
    return "$"


def diagnostic_path(file_path: str, field_path: str) -> str:
    return f"{file_path}:{field_path}" if field_path and field_path != "$" else file_path


def expected_shape_for_field(field: str) -> Any | None:
    if field == "api":
        return {"api": sorted(SUPPORTED_APIS)}
    if field == "case_id":
        return {"case_id": "unique_case_id"}
    if field == "source_file":
        return {"source_file": "path/to/source.step|source.iges|source.sgt"}
    if field == "source_kind":
        return {"source_kind": sorted(OFFSET_SOURCE_KINDS)}
    if field == "offset_distance":
        return {"offset_distance": "non-zero number"}
    if field == "boolean_type" or field.endswith("_boolean_type"):
        return {field: sorted(BOOLEAN_TYPES)}
    if field.endswith("_kind"):
        return {field: sorted(BODY_KINDS)}
    if field in {"target_kind", "tool_kind"}:
        return {field: sorted(BODY_KINDS)}
    return None


def diagnostic(
    code: str,
    file_path: str,
    field_path: str,
    message: str,
    repair_hint: str,
    expected_shape: Any | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": "error",
        "error_code": code,
        "path": diagnostic_path(file_path, field_path),
        "message": message,
        "repair_hint": repair_hint,
    }
    if expected_shape is not None:
        item["expected_shape"] = expected_shape
    return item


def diagnostic_from_validation_error(file_path: str, error: str) -> dict[str, Any]:
    field = validation_field_path(error)
    lower = error.lower()
    expected_shape = expected_shape_for_field(field)

    if error.startswith("file not found: "):
        return diagnostic(
            "FILE_NOT_FOUND",
            file_path,
            "$",
            error,
            "Save the normalized flat recipe JSON to the expected path before running fixed harness gates.",
        )
    if error.startswith("source_file not found: "):
        return diagnostic(
            "SOURCE_FILE_NOT_FOUND",
            file_path,
            "source_file",
            error,
            (
                "Use a source_file that exists in the current artifacts/input root, or return "
                "needs_harness_extension if the corpus asset must be materialized first."
            ),
            expected_shape_for_field("source_file"),
        )
    if error.startswith("unsafe ") and "source_file:" in error:
        return diagnostic(
            "UNSAFE_SOURCE_FILE",
            file_path,
            field,
            error,
            (
                "Use a repository-relative source path under artifacts/, test_harness/fixtures/, "
                "or SGK1.4.10/samples/. External assets must be materialized by fixed code first."
            ),
            expected_shape_for_field("source_file"),
        )
    if error.startswith("invalid JSON: "):
        return diagnostic(
            "INVALID_JSON",
            file_path,
            "$",
            error,
            "Return exactly one valid JSON object with no markdown wrapper or prose outside JSON.",
        )
    if error == "recipe root must be an object":
        return diagnostic(
            "INVALID_RECIPE_ROOT",
            file_path,
            "$",
            error,
            "Flat recipe output must normalize to one JSON object, not an array, string, or scalar.",
            {"api": sorted(SUPPORTED_APIS)},
        )
    if error.startswith("missing required key: "):
        return diagnostic(
            "MISSING_REQUIRED_KEY",
            file_path,
            field,
            error,
            f"Add the required `{field}` field to the flat recipe.",
            expected_shape,
        )
    if error.startswith("unknown field: "):
        return diagnostic(
            "UNKNOWN_RECIPE_FIELD",
            file_path,
            field,
            error,
            (
                "Remove the field or correct its spelling to an allowlisted field for this API; "
                "fixed harness code never guesses unknown model-authored keys."
            ),
        )
    if field == "api" and "must be one of" in lower:
        return diagnostic(
            "UNSUPPORTED_RECIPE_API",
            file_path,
            field,
            error,
            (
                "Use a runnable recipe API listed in interface_capabilities.json, or return "
                "needs_harness_extension for unsupported APIs."
            ),
            expected_shape,
        )
    if field.endswith("_kind") and "must be one of" in lower:
        return diagnostic(
            "UNSUPPORTED_BODY_BUILDER",
            file_path,
            field,
            error,
            (
                "Use a supported body builder from interface_capabilities.json, or return "
                "needs_harness_extension for missing builder support."
            ),
            expected_shape,
        )
    if "must be one of" in lower:
        return diagnostic(
            "INVALID_ENUM_VALUE",
            file_path,
            field,
            error,
            "Use one of the enum values listed in the validation message.",
            expected_shape,
        )
    if "must not be empty" in lower:
        return diagnostic(
            "EMPTY_REQUIRED_FIELD",
            file_path,
            field,
            error,
            f"Provide a non-empty value for `{field}`.",
            expected_shape,
        )
    if (
        "must be > 0" in lower
        or "must be >= 0" in lower
        or "must be greater than" in lower
        or "must be number or numeric expression string" in lower
    ):
        return diagnostic(
            "INVALID_NUMERIC_FIELD",
            file_path,
            field,
            error,
            (
                "Use a numeric value in the allowed range, or a supported numeric expression "
                "string where expressions are accepted."
            ),
            expected_shape,
        )
    if re.search(r"must be (object|list|bool|int|string|str|non-empty string)", lower):
        return diagnostic(
            "INVALID_FIELD_TYPE",
            file_path,
            field,
            error,
            "Use the exact JSON type required by the runner recipe schema.",
            expected_shape,
        )
    if "is required when" in lower:
        return diagnostic(
            "CONDITIONALLY_REQUIRED_FIELD",
            file_path,
            field,
            error,
            "Add this field or change the controlling option so the field is no longer required.",
            expected_shape,
        )
    return diagnostic(
        "RECIPE_VALIDATION_ERROR",
        file_path,
        field,
        error,
        "Adjust the flat recipe to match the supported runner recipe schema.",
        expected_shape,
    )


def validate_file_record(path: Path, *, check_assets: bool = False, asset_policy: str = "trusted") -> dict[str, Any]:
    errors = validate_file(path, check_assets=check_assets, asset_policy=asset_policy)
    return {
        "path": str(path),
        "ok": not errors,
        "errors": errors,
    }


def build_model_diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    for record in records:
        file_path = str(record.get("path") or "")
        for error in record.get("errors", []):
            diagnostics.append(diagnostic_from_validation_error(file_path, str(error)))
    return {
        "generated_at": now_iso_like(),
        "ok": not diagnostics,
        "file_count": len(records),
        "diagnostic_count": len(diagnostics),
        "diagnostics": diagnostics,
    }


def main() -> int:
    args = parse_args()
    files = iter_recipe_files(args.paths)
    failed = 0
    records: list[dict[str, Any]] = []
    for path in files:
        record = validate_file_record(
            path,
            check_assets=args.check_assets,
            asset_policy="model" if args.model_asset_policy else "trusted",
        )
        records.append(record)
        errors = record["errors"]
        if errors:
            failed += 1
            print(f"FAIL {path}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK {path}")
    if args.model_diagnostics:
        write_json(Path(args.model_diagnostics), build_model_diagnostics(records))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
