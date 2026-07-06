#!/usr/bin/env python3
"""Validate flat SGGK test-harness recipes supported by sggk_case_runner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


SUPPORTED_APIS = {"api_boolean", "check_sgt", "step_import", "iges_import", "step_roundtrip", "iges_roundtrip"}
BOOLEAN_TYPES = {"UNION", "INTERSECTION", "SUBTRACTION"}
STEP_APP_PROTOCOLS = {"AP203", "AP214", "AP242"}
PRIMITIVE_KINDS = {"solid_cylinder", "solid_wedge", "solid_sphere", "solid_cone", "solid_torus"}
BODY_KINDS = PRIMITIVE_KINDS | {
    "extrude_rect",
    "thicken_rect_sheet",
    "sweep_circle_line",
    "support_sweep_bspline_surface",
    "revolve_line",
    "revolve_rect",
    "pre_boolean_cylinder_wedge",
    "loaded_sgt",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Recipe JSON file(s) or directories")
    return parser.parse_args()


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


def require_positive_number(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    require_key(recipe, key, (int, float), errors)
    if key in recipe and isinstance(recipe[key], (int, float)) and recipe[key] <= 0:
        errors.append(f"{key} must be > 0")


def optional_number(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    optional_key(recipe, key, (int, float), errors)


def is_number_expr(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)) or isinstance(value, str)


def optional_number_expr(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    if key in recipe and not is_number_expr(recipe[key]):
        errors.append(f"{key} must be number or numeric expression string")


def optional_positive_number(recipe: dict[str, Any], key: str, errors: list[str]) -> None:
    optional_number(recipe, key, errors)
    if key in recipe and isinstance(recipe[key], (int, float)) and recipe[key] <= 0:
        errors.append(f"{key} must be > 0")


def validate_metric_expectation(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be object")
        return
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
            for key in ("min", "max"):
                if key in result_bodies and not (isinstance(result_bodies[key], int) and not isinstance(result_bodies[key], bool)):
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
        if "id" in item and not isinstance(item["id"], str):
            errors.append(f"{item_label}.id must be string")
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
        if "id" in item and not isinstance(item["id"], str):
            errors.append(f"{item_label}.id must be string")
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
        validate_metric_expectation(item, item_label, errors)
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
            elif isinstance(item["threshold"], (int, float)) and not isinstance(item["threshold"], bool) and item["threshold"] <= 0:
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
        validate_expectations_container(recipe["expectations"], "expectations", errors)
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


def validate_body_spec(recipe: dict[str, Any], prefix: str, errors: list[str]) -> None:
    require_key(recipe, f"{prefix}_kind", str, errors)
    kind = recipe.get(f"{prefix}_kind")
    if kind not in BODY_KINDS:
        errors.append(f"{prefix}_kind must be one of {sorted(BODY_KINDS)}")
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
        if isinstance(inner, (int, float)) and isinstance(outer, (int, float)) and not isinstance(inner, bool) and not isinstance(outer, bool):
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


def validate_api_boolean(recipe: dict[str, Any], errors: list[str]) -> None:
    require_key(recipe, "case_id", str, errors)
    require_key(recipe, "boolean_type", str, errors)
    if recipe.get("boolean_type") not in BOOLEAN_TYPES:
        errors.append(f"boolean_type must be one of {sorted(BOOLEAN_TYPES)}")
    require_positive_number(recipe, "modeling_tol", errors)
    optional_positive_number(recipe, "max_model_size", errors)
    optional_key(recipe, "check_valid", bool, errors)
    optional_key(recipe, "topo_track", bool, errors)
    optional_key(recipe, "non_destructive", bool, errors)
    validate_body_spec(recipe, "target", errors)
    validate_body_spec(recipe, "tool", errors)


def validate_source_file_recipe(recipe: dict[str, Any], errors: list[str]) -> None:
    require_key(recipe, "case_id", str, errors)
    require_key(recipe, "source_file", str, errors)
    if isinstance(recipe.get("source_file"), str) and not recipe["source_file"].strip():
        errors.append("source_file must not be empty")
    for key in ("source_body_index", "body_index"):
        if key in recipe and (not (isinstance(recipe[key], int) and not isinstance(recipe[key], bool)) or recipe[key] < 0):
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


def validate_recipe(recipe: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    require_key(recipe, "api", str, errors)
    validate_expectations(recipe, errors)
    api = recipe.get("api")
    if api not in SUPPORTED_APIS:
        errors.append(f"api must be one of {sorted(SUPPORTED_APIS)}")
        return errors

    if api == "api_boolean":
        validate_api_boolean(recipe, errors)
    elif api in {"check_sgt", "step_import", "iges_import", "step_roundtrip", "iges_roundtrip"}:
        validate_source_file_recipe(recipe, errors)
    return errors


def validate_file(path: Path) -> list[str]:
    if not path.exists():
        return [f"file not found: {path}"]
    try:
        recipe = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]
    if not isinstance(recipe, dict):
        return ["recipe root must be an object"]
    return validate_recipe(recipe)


def main() -> int:
    args = parse_args()
    files = iter_recipe_files(args.paths)
    failed = 0
    for path in files:
        errors = validate_file(path)
        if errors:
            failed += 1
            print(f"FAIL {path}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK {path}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
