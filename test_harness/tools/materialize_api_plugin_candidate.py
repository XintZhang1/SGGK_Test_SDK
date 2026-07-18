#!/usr/bin/env python3
"""Validate a model-authored adapter spec and materialize a fixed-template plugin."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from api_adaptation_contract import (
    BODY_LIST_TO_BODY_REQUIRED_ORACLES,
    UNARY_BODY_TO_BODIES_REQUIRED_ORACLES,
    validate_adaptation_contract,
    validate_candidate_identity,
)
from api_archetype_mapping import map_signature
from campaign_profiles import FORBIDDEN_CANDIDATE_FIELDS
from jsonschema import Draft202012Validator
from plugin_catalog import (
    ALLOWED_SDK_MODULES,
    API_ID_RE,
    HEADER_RE,
    PluginCatalogError,
    discover_plugins,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_SCHEMA = json.loads(
    (REPO_ROOT / "test_harness/schemas/api_plugin_candidate.schema.json").read_text(
        encoding="utf-8"
    )
)
# Fixed runner body-builder vocabulary (sggk_case_runner MakeBodyFromSpec);
# loaded_sgt is deliberately excluded because model output may not carry files.
UNARY_TARGET_KINDS = frozenset(
    {
        "solid_sphere",
        "solid_cylinder",
        "solid_cone",
        "solid_torus",
        "solid_wedge",
        "plane_sheet",
        "extrude_rect",
        "thicken_rect_sheet",
        "sweep_circle_line",
        "support_sweep_bspline_surface",
        "revolve_line",
        "revolve_rect",
        "pre_boolean_cylinder_wedge",
    }
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _forbidden(value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_forbidden(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in FORBIDDEN_CANDIDATE_FIELDS or normalized in {
                "argv",
                "commands",
                "executable",
                "shell",
            }:
                errors.append(f"{path}.{key}: model-authored execution field is forbidden")
            errors.extend(_forbidden(item, f"{path}.{key}"))
    return errors


def _schema_errors(schema: dict[str, Any], value: Any) -> list[str]:
    errors: list[str] = []
    for item in sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    ):
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in item.absolute_path
        )
        errors.append(f"{path}: {item.message}")
    return errors


def _validate_local_refs(value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_validate_local_refs(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and (not isinstance(item, str) or not item.startswith("#")):
                errors.append(f"{path}.$ref: only local fragment references are allowed")
            errors.extend(_validate_local_refs(item, f"{path}.{key}"))
    return errors


def _validate_body_list_to_body_oracles(candidate: dict[str, Any]) -> list[str]:
    """Require semantic smoke assertions that model metadata cannot weaken."""

    errors: list[str] = []
    smoke = candidate.get("smoke_recipe")
    smoke = smoke if isinstance(smoke, dict) else {}
    expectations = smoke.get("expectations")
    expectations = expectations if isinstance(expectations, dict) else {}
    result_bodies = expectations.get("result_bodies")
    result_bodies = result_bodies if isinstance(result_bodies, dict) else {}
    if result_bodies.get("min") != 1 or result_bodies.get("max") != 1:
        errors.append(
            "$.smoke_recipe.expectations.result_bodies must require exactly one body "
            "with min=1 and max=1"
        )
    for field in (
        "require_property_calculations",
        "require_finite_properties",
        "require_nonnegative_volume",
    ):
        if expectations.get(field) is not True:
            errors.append(f"$.smoke_recipe.expectations.{field} must be true")

    capability = candidate.get("capability")
    capability = capability if isinstance(capability, dict) else {}
    supported = capability.get("supported_oracles")
    missing_oracles = (
        BODY_LIST_TO_BODY_REQUIRED_ORACLES - set(supported)
        if isinstance(supported, list)
        and all(isinstance(item, str) for item in supported)
        else BODY_LIST_TO_BODY_REQUIRED_ORACLES
    )
    if missing_oracles:
        errors.append(
            "$.capability.supported_oracles is missing fixed body_list_to_body host "
            f"oracles {sorted(missing_oracles)!r}"
        )

    recipe_schema = candidate.get("recipe_schema")
    recipe_schema = recipe_schema if isinstance(recipe_schema, dict) else {}
    top_required = recipe_schema.get("required")
    if not isinstance(top_required, list) or "expectations" not in top_required:
        errors.append("$.recipe_schema.required must contain expectations")
    schema_properties = recipe_schema.get("properties")
    schema_properties = schema_properties if isinstance(schema_properties, dict) else {}
    expectations_schema = schema_properties.get("expectations")
    expectations_schema = expectations_schema if isinstance(expectations_schema, dict) else {}
    if (
        expectations_schema.get("type") != "object"
        or expectations_schema.get("additionalProperties") is not False
    ):
        errors.append(
            "$.recipe_schema.properties.expectations must be an inline strict object"
        )
    required_expectations = expectations_schema.get("required")
    required_fields = {
        "result_bodies",
        "require_property_calculations",
        "require_finite_properties",
        "require_nonnegative_volume",
    }
    required_expectations_are_strings = isinstance(required_expectations, list) and all(
        isinstance(item, str) for item in required_expectations
    )
    if not required_expectations_are_strings or not required_fields.issubset(
        set(required_expectations)
    ):
        errors.append(
            "$.recipe_schema.properties.expectations.required must contain all fixed "
            "body_list_to_body oracle fields"
        )
    expectation_properties = expectations_schema.get("properties")
    expectation_properties = (
        expectation_properties if isinstance(expectation_properties, dict) else {}
    )
    result_schema = expectation_properties.get("result_bodies")
    result_schema = result_schema if isinstance(result_schema, dict) else {}
    result_required = result_schema.get("required")
    result_properties = result_schema.get("properties")
    result_properties = result_properties if isinstance(result_properties, dict) else {}
    result_required_are_strings = isinstance(result_required, list) and all(
        isinstance(item, str) for item in result_required
    )
    if (
        result_schema.get("type") != "object"
        or result_schema.get("additionalProperties") is not False
        or not result_required_are_strings
        or not {"min", "max"}.issubset(set(result_required))
        or not isinstance(result_properties.get("min"), dict)
        or result_properties["min"].get("const") != 1
        or not isinstance(result_properties.get("max"), dict)
        or result_properties["max"].get("const") != 1
    ):
        errors.append(
            "$.recipe_schema.properties.expectations.properties.result_bodies must "
            "strictly require min=1 and max=1"
        )
    for field in (
        "require_property_calculations",
        "require_finite_properties",
        "require_nonnegative_volume",
    ):
        field_schema = expectation_properties.get(field)
        if not isinstance(field_schema, dict) or field_schema.get("const") is not True:
            errors.append(
                "$.recipe_schema.properties.expectations.properties."
                f"{field} must use const=true"
            )
    return errors


_UNARY_STRING_DEFAULT_RE = re.compile(r"^[A-Za-z0-9_.+\-]{0,64}$")
_UNARY_RESERVED_RECIPE_FIELDS = {"api", "case_id", "expectations"}


def _validate_unary_body_to_bodies_oracles(candidate: dict[str, Any]) -> list[str]:
    """Require semantic smoke assertions that model metadata cannot weaken."""

    errors: list[str] = []
    smoke = candidate.get("smoke_recipe")
    smoke = smoke if isinstance(smoke, dict) else {}
    expectations = smoke.get("expectations")
    expectations = expectations if isinstance(expectations, dict) else {}
    result_bodies = expectations.get("result_bodies")
    result_bodies = result_bodies if isinstance(result_bodies, dict) else {}
    minimum = result_bodies.get("min")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        errors.append(
            "$.smoke_recipe.expectations.result_bodies.min must be an integer >= 1"
        )
    for field in (
        "require_property_calculations",
        "require_finite_properties",
        "require_nonnegative_volume",
    ):
        if expectations.get(field) is not True:
            errors.append(f"$.smoke_recipe.expectations.{field} must be true")

    capability = candidate.get("capability")
    capability = capability if isinstance(capability, dict) else {}
    supported = capability.get("supported_oracles")
    missing_oracles = (
        UNARY_BODY_TO_BODIES_REQUIRED_ORACLES - set(supported)
        if isinstance(supported, list)
        and all(isinstance(item, str) for item in supported)
        else UNARY_BODY_TO_BODIES_REQUIRED_ORACLES
    )
    if missing_oracles:
        errors.append(
            "$.capability.supported_oracles is missing fixed unary_body_to_bodies host "
            f"oracles {sorted(missing_oracles)!r}"
        )

    recipe_schema = candidate.get("recipe_schema")
    recipe_schema = recipe_schema if isinstance(recipe_schema, dict) else {}
    top_required = recipe_schema.get("required")
    if not isinstance(top_required, list) or "expectations" not in top_required:
        errors.append("$.recipe_schema.required must contain expectations")
    schema_properties = recipe_schema.get("properties")
    schema_properties = schema_properties if isinstance(schema_properties, dict) else {}
    expectations_schema = schema_properties.get("expectations")
    expectations_schema = expectations_schema if isinstance(expectations_schema, dict) else {}
    if (
        expectations_schema.get("type") != "object"
        or expectations_schema.get("additionalProperties") is not False
    ):
        errors.append(
            "$.recipe_schema.properties.expectations must be an inline strict object"
        )
    required_expectations = expectations_schema.get("required")
    required_fields = {
        "result_bodies",
        "require_property_calculations",
        "require_finite_properties",
        "require_nonnegative_volume",
    }
    required_expectations_are_strings = isinstance(required_expectations, list) and all(
        isinstance(item, str) for item in required_expectations
    )
    if not required_expectations_are_strings or not required_fields.issubset(
        set(required_expectations)
    ):
        errors.append(
            "$.recipe_schema.properties.expectations.required must contain all fixed "
            "unary_body_to_bodies oracle fields"
        )
    expectation_properties = expectations_schema.get("properties")
    expectation_properties = (
        expectation_properties if isinstance(expectation_properties, dict) else {}
    )
    result_schema = expectation_properties.get("result_bodies")
    result_schema = result_schema if isinstance(result_schema, dict) else {}
    result_required = result_schema.get("required")
    result_properties = result_schema.get("properties")
    result_properties = result_properties if isinstance(result_properties, dict) else {}
    min_schema = result_properties.get("min")
    max_schema = result_properties.get("max")
    if (
        result_schema.get("type") != "object"
        or result_schema.get("additionalProperties") is not False
        or not isinstance(result_required, list)
        or "min" not in result_required
        or not isinstance(min_schema, dict)
        or min_schema.get("type") != "integer"
        or min_schema.get("minimum") != 1
    ):
        errors.append(
            "$.recipe_schema.properties.expectations.properties.result_bodies must "
            "strictly require an integer min with minimum=1"
        )
    if max_schema is not None and (
        not isinstance(max_schema, dict)
        or max_schema.get("type") != "integer"
        or not isinstance(max_schema.get("minimum"), int)
        or max_schema["minimum"] < 1
    ):
        errors.append(
            "$.recipe_schema.properties.expectations.properties.result_bodies.max must "
            "be an integer schema with minimum >= 1 when present"
        )
    for field in (
        "require_property_calculations",
        "require_finite_properties",
        "require_nonnegative_volume",
    ):
        field_schema = expectation_properties.get(field)
        if not isinstance(field_schema, dict) or field_schema.get("const") is not True:
            errors.append(
                "$.recipe_schema.properties.expectations.properties."
                f"{field} must use const=true"
            )
    return errors


def _default_matches_cpp_type(cpp_type: str, default: Any) -> bool:
    if cpp_type == "bool":
        return isinstance(default, bool)
    if cpp_type == "int":
        return isinstance(default, int) and not isinstance(default, bool)
    if cpp_type in {"double", "float"}:
        return isinstance(default, int | float) and not isinstance(default, bool)
    return isinstance(default, str)


def _validate_unary_smoke_target(candidate: dict[str, Any]) -> list[str]:
    """Require the flat runner body-builder convention in smoke/schema.

    The fixed unary adapter constructs its input from ``recipe.boolean.target``,
    which the runner parses from flat ``target_kind``/``target_*`` fields.  A
    nested or invented shape (``body``, ``target``, ``boolean.target``) silently
    builds the wrong input and only fails inside the SDK.
    """

    errors: list[str] = []
    smoke = candidate.get("smoke_recipe")
    smoke = smoke if isinstance(smoke, dict) else {}
    kind = smoke.get("target_kind")
    if not isinstance(kind, str) or kind not in UNARY_TARGET_KINDS:
        errors.append(
            "$.smoke_recipe.target_kind must be one of the fixed runner body builders "
            f"{sorted(UNARY_TARGET_KINDS)!r} (flat target_kind/target_* convention; "
            "nested shapes like body/target/boolean.target are not supported)"
        )
    recipe_schema = candidate.get("recipe_schema")
    recipe_schema = recipe_schema if isinstance(recipe_schema, dict) else {}
    required = recipe_schema.get("required")
    if not isinstance(required, list) or "target_kind" not in required:
        errors.append("$.recipe_schema.required must contain target_kind")
    schema_properties = recipe_schema.get("properties")
    schema_properties = schema_properties if isinstance(schema_properties, dict) else {}
    missing_translate = sorted(
        f"target_translate_{axis}"
        for axis in ("x", "y", "z")
        if f"target_translate_{axis}" not in schema_properties
    )
    if missing_translate:
        errors.append(
            "$.recipe_schema.properties must expose numeric placement levers "
            f"{missing_translate!r} so candidates can reach the complexity floor"
        )
    return errors


def _validate_unary_scalar_params(
    candidate: dict[str, Any],
    expected_contract: dict[str, Any] | None,
) -> list[str]:
    """Bind the unary adapter scalar parameters to the trusted contract signature."""

    errors: list[str] = []
    spec = candidate.get("adapter_spec") if isinstance(candidate.get("adapter_spec"), dict) else {}
    params = spec.get("scalar_params")
    if not isinstance(params, list):
        return ["$.adapter_spec.scalar_params must be an array for unary_body_to_bodies"]
    recipe_fields: set[str] = set()
    for index, param in enumerate(params):
        if not isinstance(param, dict):
            errors.append(f"$.adapter_spec.scalar_params[{index}] must be an object")
            continue
        name = param.get("name")
        cpp_type = param.get("cpp_type")
        recipe_field = param.get("recipe_field")
        default = param.get("default")
        if not isinstance(name, str) or not name:
            errors.append(f"$.adapter_spec.scalar_params[{index}].name must be a non-empty string")
        if cpp_type not in {"double", "float", "int", "bool", "std::string"}:
            errors.append(
                f"$.adapter_spec.scalar_params[{index}].cpp_type is unsupported: {cpp_type!r}"
            )
            continue
        if not _default_matches_cpp_type(str(cpp_type), default):
            errors.append(
                f"$.adapter_spec.scalar_params[{index}].default does not match {cpp_type}"
            )
        if cpp_type == "std::string" and not _UNARY_STRING_DEFAULT_RE.fullmatch(str(default)):
            errors.append(
                f"$.adapter_spec.scalar_params[{index}].default must be a bounded plain string"
            )
        if not isinstance(recipe_field, str) or not recipe_field:
            errors.append(
                f"$.adapter_spec.scalar_params[{index}].recipe_field must be a non-empty string"
            )
            continue
        if recipe_field in _UNARY_RESERVED_RECIPE_FIELDS or recipe_field.startswith(
            ("target_", "tool_")
        ):
            errors.append(
                f"$.adapter_spec.scalar_params[{index}].recipe_field collides with runner "
                f"fields: {recipe_field!r}"
            )
        if recipe_field in recipe_fields:
            errors.append(
                f"$.adapter_spec.scalar_params[{index}].recipe_field is duplicated: "
                f"{recipe_field!r}"
            )
        recipe_fields.add(recipe_field)
    if expected_contract is None:
        return errors
    mapped = map_signature(
        str(expected_contract.get("function_name") or ""),
        str(expected_contract.get("function_signature") or ""),
    )
    if mapped is None or mapped.get("adapter_archetype") != "unary_body_to_bodies":
        return errors + [
            "trusted contract function_signature is not a unary_body_to_bodies signature"
        ]
    expected = mapped.get("scalar_params") if isinstance(mapped.get("scalar_params"), list) else []
    actual_shape = [
        (param.get("name"), param.get("cpp_type")) for param in params if isinstance(param, dict)
    ]
    expected_shape = [(item.get("name"), item.get("cpp_type")) for item in expected]
    if actual_shape != expected_shape:
        errors.append(
            "$.adapter_spec.scalar_params must match the trusted signature parameters in "
            f"name, order, and type; expected {expected_shape!r}"
        )
    return errors


def _single_added_field_diff(
    reference: Any,
    mutated: Any,
    path: tuple[str | int, ...] = (),
) -> tuple[list[tuple[tuple[str | int, ...], str]], list[str]]:
    """Return added object fields and every non-addition difference."""

    additions: list[tuple[tuple[str | int, ...], str]] = []
    other_differences: list[str] = []
    if type(reference) is not type(mutated):
        return additions, [f"{path!r}: value type changed"]
    if isinstance(reference, dict):
        removed = sorted(set(reference) - set(mutated))
        for key in removed:
            other_differences.append(f"{path + (key,)!r}: field was removed")
        for key in sorted(set(mutated) - set(reference)):
            additions.append((path, key))
        for key in sorted(set(reference) & set(mutated)):
            child_additions, child_differences = _single_added_field_diff(
                reference[key],
                mutated[key],
                path + (key,),
            )
            additions.extend(child_additions)
            other_differences.extend(child_differences)
        return additions, other_differences
    if isinstance(reference, list):
        if len(reference) != len(mutated):
            return additions, [f"{path!r}: array length changed"]
        for index, (reference_item, mutated_item) in enumerate(zip(reference, mutated)):
            child_additions, child_differences = _single_added_field_diff(
                reference_item,
                mutated_item,
                path + (index,),
            )
            additions.extend(child_additions)
            other_differences.extend(child_differences)
        return additions, other_differences
    if reference != mutated:
        other_differences.append(f"{path!r}: value changed")
    return additions, other_differences


def _validate_single_unknown_field_negative(
    recipe_schema: dict[str, Any],
    smoke_recipe: dict[str, Any],
    negative_recipe: dict[str, Any],
) -> list[str]:
    additions, other_differences = _single_added_field_diff(
        smoke_recipe,
        negative_recipe,
    )
    errors: list[str] = []
    if len(additions) != 1 or other_differences:
        errors.append(
            "$.negative_recipe must equal $.smoke_recipe plus exactly one added "
            "unknown field; removed, changed, or multiple fields are forbidden"
        )
        return errors
    validation_errors = list(Draft202012Validator(recipe_schema).iter_errors(negative_recipe))
    parent_path, _added_key = additions[0]
    if (
        len(validation_errors) != 1
        or validation_errors[0].validator != "additionalProperties"
        or tuple(validation_errors[0].absolute_path) != parent_path
    ):
        errors.append(
            "$.negative_recipe must fail only the matching additionalProperties=false "
            "check for its single added unknown field"
        )
    return errors


def _symbol(api: str) -> str:
    return "RunPlugin" + "".join(part.capitalize() for part in api.split("_"))


def _adapter_source(api: str, clone_field: str) -> str:
    symbol = _symbol(api)
    return f'''/*
 * AUTO-GENERATED BY THE FIXED SGGK HARNESS HOST TEMPLATE.
 *
 * Review this file together with recipe.schema.json, smoke.recipe.json,
 * negative.recipe.json, the plugin build report, and review_report.zh-CN.md.
 * The model selected only a bounded adapter specification; it did not author
 * this C++, command lines, SDK paths, build flags, or acceptance state.
 *
 * Adapter invariants:
 *   1. Create a fresh artifact capsule for every isolated case.
 *   2. Serialize both inputs before calling the SDK API.
 *   3. Record API status, TopoCheck, properties, semantic validation, and the
 *      explicit reason why this archetype has no ModelingRet TopoTrack channel.
 *   4. Return success only when the API, topology check, and fixed validation
 *      all succeed; a non-null return value alone is never sufficient.
 */
int {symbol}(const CliOptions& cli, const CaseRecipe& recipe)
{{
    // Artifact layout is deterministic so reports can hash and locate every input/output.
    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);

    // Only the contract-bound boolean field may alter the SDK clone behavior.
    bool clone = true;
    if (!cli.recipePath.empty())
    {{
        const std::string json = ReadTextFile(cli.recipePath);
        FindBool(json, "{clone_field}", clone);
    }}
    // Construct and persist inputs before the API call for reproducible failure handoff.
    auto target = MakeBodyFromSpec(recipe.boolean.target, "target");
    auto tool = MakeBodyFromSpec(recipe.boolean.tool, "tool");
    SerializeTopology(target, caseDir / "input" / "target.sgt");
    SerializeTopology(tool, caseDir / "input" / "tool.sgt");
    WriteInputProvenance(recipe, target, tool, caseDir);
    const auto targetProperties = ComputeBodyProperties(
        std::vector<sggk::BodyPtr>{{target}}, recipe.expectations.sampleInputProperties);
    const auto toolProperties = ComputeBodyProperties(
        std::vector<sggk::BodyPtr>{{tool}}, recipe.expectations.sampleInputProperties);
    WriteInputProperties(targetProperties, toolProperties, caseDir);

    sggk::BodyList inputs;
    inputs.push_back(target);
    inputs.push_back(tool);
    // This is the only SDK operation selected by the trusted adaptation contract.
    auto result = sggk::{api}(inputs, clone);
    const bool apiSucceeded = static_cast<bool>(result);
    std::vector<sggk::BodyPtr> resultBodies;
    if (result)
    {{
        resultBodies.push_back(result);
    }}
    WriteStatusGeneric(
        apiSucceeded, apiSucceeded ? 0U : 1U, apiSucceeded ? "" : "null result",
        0, resultBodies.size(), caseDir);
    SerializeResultBodies(resultBodies, caseDir);
    // Independent semantic oracles prevent a successful return from masking bad geometry.
    const bool topoOk = apiSucceeded && WriteTopoCheck(resultBodies, caseDir);
    const auto resultProperties = ComputeBodyProperties(resultBodies);
    WriteProperties(resultProperties, caseDir);
    const bool validationOk = WriteValidation(
        recipe, resultBodies, resultProperties,
        std::vector<sggk::BodyPtr>{{target}}, targetProperties,
        std::vector<sggk::BodyPtr>{{tool}}, toolProperties, caseDir);
    const std::string topoTrackReason = "fixed body_list_to_body adapter has no ModelingRet TopoTrack channel";
    WriteEmptyTopoTrack(caseDir, topoTrackReason);
    WriteSkippedTopoTrackSummary(recipe, caseDir, topoTrackReason);
    std::cout << "case_id=" << recipe.caseId << "\\n"
              << "succeeded=" << (apiSucceeded ? "true" : "false") << "\\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\\n";
    return (apiSucceeded && topoOk && validationOk) ? 0 : 2;
}}
'''


def _cpp_float_literal(value: Any) -> str:
    number = float(value)
    text = repr(number)
    if not any(marker in text for marker in (".", "e", "E", "inf", "nan")):
        text += ".0"
    return text


def _cpp_string_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _unary_param_block(scalar_params: list[dict[str, Any]]) -> tuple[str, str, str]:
    """Render declaration, recipe-lookup, and call-argument fragments for scalars."""

    declarations: list[str] = []
    lookups: list[str] = []
    call_args: list[str] = []
    for param in scalar_params:
        name = str(param["name"])
        cpp_type = str(param["cpp_type"])
        recipe_field = str(param["recipe_field"])
        default = param["default"]
        if cpp_type in {"double", "float"}:
            declarations.append(f"    double {name} = {_cpp_float_literal(default)};")
            lookups.append(f'        FindDouble(json, "{recipe_field}", {name});')
            call_args.append(name if cpp_type == "double" else f"static_cast<float>({name})")
        elif cpp_type == "int":
            declarations.append(f"    int {name} = {int(default)};")
            lookups.append(f'        FindInt(json, "{recipe_field}", {name});')
            call_args.append(name)
        elif cpp_type == "bool":
            declarations.append(f"    bool {name} = {'true' if default else 'false'};")
            lookups.append(f'        FindBool(json, "{recipe_field}", {name});')
            call_args.append(name)
        else:
            declarations.append(f"    std::string {name} = {_cpp_string_literal(str(default))};")
            lookups.append(f'        FindString(json, "{recipe_field}", {name});')
            call_args.append(name)
    declaration_text = "\n".join(declarations) if declarations else "    // The API takes no scalar parameters."
    lookup_text = "\n".join(lookups) if lookups else "        // No scalar parameters to read."
    return declaration_text, lookup_text, "".join(f", {arg}" for arg in call_args)


def _adapter_source_unary(api: str, scalar_params: list[dict[str, Any]]) -> str:
    symbol = _symbol(api)
    declarations, lookups, call_suffix = _unary_param_block(scalar_params)
    return f'''/*
 * AUTO-GENERATED BY THE FIXED SGGK HARNESS HOST TEMPLATE.
 *
 * Review this file together with recipe.schema.json, smoke.recipe.json,
 * negative.recipe.json, the plugin build report, and review_report.zh-CN.md.
 * The model selected only a bounded adapter specification; it did not author
 * this C++, command lines, SDK paths, build flags, or acceptance state.
 *
 * Adapter invariants:
 *   1. Create a fresh artifact capsule for every isolated case.
 *   2. Serialize the input body before calling the SDK API.
 *   3. Record ModelingRet status, error entities, TopoCheck, properties, and
 *      semantic validation; the flat recipe lane captures no TopoTrack channel.
 *   4. Return success only when the API, topology check, and fixed validation
 *      all succeed; a non-null ModelingRet alone is never sufficient.
 */
int {symbol}(const CliOptions& cli, const CaseRecipe& recipe)
{{
    // Artifact layout is deterministic so reports can hash and locate every input/output.
    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);

    // Only contract-bound scalar fields may alter the SDK call; each has a
    // model-specified default that the recipe may override by field name.
{declarations}
    if (!cli.recipePath.empty())
    {{
        const std::string json = ReadTextFile(cli.recipePath);
{lookups}
    }}
    // Construct and persist the input before the API call for reproducible failure handoff.
    auto target = MakeBodyFromSpec(recipe.boolean.target, "target");
    SerializeTopology(target, caseDir / "input" / "target.sgt");
    const auto targetProperties = ComputeBodyProperties(
        std::vector<sggk::BodyPtr>{{target}}, recipe.expectations.sampleInputProperties);
    WriteInputProperties(targetProperties, {{}}, caseDir);

    // This is the only SDK operation selected by the trusted adaptation contract.
    auto ret = sggk::{api}(target{call_suffix});
    if (!ret)
    {{
        throw std::runtime_error("{api} returned null modeling return");
    }}
    WriteStatus(ret, caseDir);
    CaptureErrorEntities(ret->Status(), caseDir);
    const auto resultBodies = ToBodyVector(ret->ResultBodies());
    SerializeResultBodies(resultBodies, caseDir);
    // Independent semantic oracles prevent a successful return from masking bad geometry.
    const bool topoOk = ret->Succeeded() && WriteTopoCheck(resultBodies, caseDir);
    const auto resultProperties = ComputeBodyProperties(resultBodies);
    WriteProperties(resultProperties, caseDir);
    const bool validationOk = WriteValidation(
        recipe, resultBodies, resultProperties,
        std::vector<sggk::BodyPtr>{{target}}, targetProperties,
        {{}}, {{}}, caseDir);
    const std::string topoTrackReason =
        "fixed unary_body_to_bodies adapter records ModelingRet status; "
        "topology tracking is captured as status/topocheck artifacts only";
    WriteEmptyTopoTrack(caseDir, topoTrackReason);
    WriteSkippedTopoTrackSummary(recipe, caseDir, topoTrackReason);
    std::cout << "case_id=" << recipe.caseId << "\\n"
              << "succeeded=" << (ret->Succeeded() ? "true" : "false") << "\\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\\n"
              << "error_code=" << ret->Status().ErrorCode() << "\\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\\n";
    return (ret->Succeeded() && topoOk && validationOk) ? 0 : 2;
}}
'''


def validate_candidate(
    candidate: Any,
    *,
    expected_contract: dict[str, Any] | None = None,
    expected_contract_sha256: str = "",
) -> list[str]:
    errors = _schema_errors(CANDIDATE_SCHEMA, candidate)
    errors.extend(_forbidden(candidate))
    if not isinstance(candidate, dict):
        return errors
    api = candidate.get("api")
    spec = candidate.get("adapter_spec") if isinstance(candidate.get("adapter_spec"), dict) else {}
    if isinstance(api, str) and spec.get("function_name") != api:
        errors.append("$.adapter_spec.function_name must equal $.api for fixed archetypes")
    header = spec.get("sdk_header")
    if isinstance(header, str) and not HEADER_RE.fullmatch(header):
        errors.append("$.adapter_spec.sdk_header is not a safe SDK include")
    modules = spec.get("sdk_modules") if isinstance(spec.get("sdk_modules"), list) else []
    unknown_modules = sorted(set(modules) - ALLOWED_SDK_MODULES)
    if unknown_modules:
        errors.append(f"$.adapter_spec.sdk_modules contains unsupported modules: {unknown_modules}")
    if spec.get("archetype") == "body_list_to_body":
        errors.extend(_validate_body_list_to_body_oracles(candidate))
    elif spec.get("archetype") == "unary_body_to_bodies":
        errors.extend(_validate_unary_body_to_bodies_oracles(candidate))
        errors.extend(_validate_unary_scalar_params(candidate, expected_contract))
        errors.extend(_validate_unary_smoke_target(candidate))
    recipe_schema = candidate.get("recipe_schema")
    if isinstance(recipe_schema, dict):
        if recipe_schema.get("type") != "object" or recipe_schema.get("additionalProperties") is not False:
            errors.append("$.recipe_schema must be a strict object with additionalProperties=false")
        api_schema = (
            recipe_schema.get("properties", {}).get("api")
            if isinstance(recipe_schema.get("properties"), dict)
            else None
        )
        if not isinstance(api_schema, dict) or api_schema.get("const") != api:
            errors.append("$.recipe_schema.properties.api.const must equal $.api")
        ref_errors = _validate_local_refs(recipe_schema, "$.recipe_schema")
        errors.extend(ref_errors)
        smoke_errors: list[str] = []
        if not ref_errors and isinstance(candidate.get("smoke_recipe"), dict):
            smoke_errors = [
                f"$.smoke_recipe{message.removeprefix('$')}"
                for message in _schema_errors(recipe_schema, candidate["smoke_recipe"])
            ]
            errors.extend(smoke_errors)
        if (
            not ref_errors
            and not smoke_errors
            and isinstance(candidate.get("smoke_recipe"), dict)
            and isinstance(candidate.get("negative_recipe"), dict)
        ):
            errors.extend(
                _validate_single_unknown_field_negative(
                    recipe_schema,
                    candidate["smoke_recipe"],
                    candidate["negative_recipe"],
                )
            )
    if isinstance(api, str) and not API_ID_RE.fullmatch(api):
        errors.append("$.api is not a valid API id")
    if expected_contract is not None:
        contract_errors = validate_adaptation_contract(
            expected_contract,
            expected_contract_sha256,
        )
        errors.extend(f"trusted contract: {message}" for message in contract_errors)
        if not contract_errors:
            errors.extend(validate_candidate_identity(candidate, expected_contract))
    return errors


def materialize(
    candidate: dict[str, Any],
    out: Path,
    *,
    expected_contract: dict[str, Any] | None = None,
    expected_contract_sha256: str = "",
) -> dict[str, Any]:
    errors = validate_candidate(
        candidate,
        expected_contract=expected_contract,
        expected_contract_sha256=expected_contract_sha256,
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "ok": not errors,
        "kind": "api_plugin_candidate",
        "api": candidate.get("api"),
        "adaptation_contract_sha256": expected_contract_sha256,
        "intake_sha256": (
            expected_contract.get("intake_sha256", "")
            if isinstance(expected_contract, dict)
            else ""
        ),
        "identity_bound": expected_contract is not None and not errors,
        "host_oracle_contract": {
            "archetype": (
                candidate.get("adapter_spec", {}).get("archetype")
                if isinstance(candidate.get("adapter_spec"), dict)
                else ""
            ),
            "required_oracles": sorted(BODY_LIST_TO_BODY_REQUIRED_ORACLES),
            "topocheck_enforced_by_fixed_adapter": True,
        },
        "errors": errors,
        "materialized_plugin": "",
    }
    if errors:
        return report
    api = str(candidate["api"])
    spec = candidate["adapter_spec"]
    archetype = str(spec["archetype"])
    input_roles = ["target", "tool"] if archetype == "body_list_to_body" else ["target"]
    report["host_oracle_contract"]["required_oracles"] = sorted(
        BODY_LIST_TO_BODY_REQUIRED_ORACLES
        if archetype == "body_list_to_body"
        else UNARY_BODY_TO_BODIES_REQUIRED_ORACLES
    )
    plugin_root = out / "plugins" / api
    manifest = {
        "contract_version": 1,
        "api": api,
        "version": 1,
        "description": candidate["description"],
        "archetype": archetype,
        "adapter_symbol": _symbol(api),
        "adapter_file": "adapter.inc",
        "recipe_schema": "recipe.schema.json",
        "sdk_headers": [spec["sdk_header"]],
        "sdk_modules": spec["sdk_modules"],
        "input_roles": input_roles,
        "result_roles": ["result"],
        "topotrack": candidate["topotrack"],
        "capability": candidate["capability"],
        "examples": {
            "positive": ["examples/smoke.json"],
            "negative": ["examples/negative.invalid.json"],
        },
    }
    _write_json(plugin_root / "plugin.json", manifest)
    _write_json(plugin_root / "recipe.schema.json", candidate["recipe_schema"])
    _write_json(plugin_root / "examples/smoke.json", candidate["smoke_recipe"])
    _write_json(plugin_root / "examples/negative.invalid.json", candidate["negative_recipe"])
    if archetype == "unary_body_to_bodies":
        adapter_text = _adapter_source_unary(api, list(spec["scalar_params"]))
    else:
        adapter_text = _adapter_source(api, str(spec["clone_field"]))
    (plugin_root / "adapter.inc").write_text(adapter_text, encoding="utf-8")
    try:
        records = discover_plugins(plugin_root.parent)
    except (PluginCatalogError, OSError, json.JSONDecodeError) as exc:
        report["errors"] = [str(exc)]
        return report
    if len(records) != 1 or records[0].api != api:
        report["errors"] = ["materialized catalog did not contain exactly the requested API"]
        return report
    report["ok"] = True
    report["materialized_plugin"] = str(plugin_root)
    report["plugin"] = records[0].as_dict()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate")
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", default="")
    parser.add_argument("--model-diagnostics", default="")
    parser.add_argument("--expected-contract", default="")
    parser.add_argument("--expected-contract-sha256", default="")
    args = parser.parse_args()
    try:
        value = json.loads(Path(args.candidate).read_text(encoding="utf-8-sig"))
        expected_contract = (
            json.loads(Path(args.expected_contract).read_text(encoding="utf-8-sig"))
            if args.expected_contract
            else None
        )
        report = materialize(
            value,
            Path(args.out).resolve(),
            expected_contract=expected_contract,
            expected_contract_sha256=args.expected_contract_sha256,
        ) if isinstance(value, dict) else {
            "schema_version": 1,
            "ok": False,
            "kind": "api_plugin_candidate",
            "errors": ["candidate root must be an object"],
        }
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        report = {"schema_version": 1, "ok": False, "errors": [str(exc)]}
    if args.report:
        _write_json(Path(args.report), report)
    if args.model_diagnostics:
        _write_json(
            Path(args.model_diagnostics),
            {
                "ok": report.get("ok") is True,
                "diagnostics": [
                    {
                        "severity": "error",
                        "error_code": "API_PLUGIN_CANDIDATE_INVALID",
                        "path": "$",
                        "message": error,
                        "repair_hint": (
                            "Return a strict fixed-archetype adapter spec, recipe schema, "
                            "and positive/negative examples."
                        ),
                    }
                    for error in report.get("errors", [])
                ],
            },
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
