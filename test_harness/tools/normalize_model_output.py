#!/usr/bin/env python3
"""Normalize small-model harness outputs and emit model-friendly diagnostics.

The normalizer is intentionally conservative. It accepts low-risk syntactic
variants such as wrapper/raw DSL files and common alias fields, but it does not
silently translate invented runner schemas into runnable tests.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from campaign_profiles import FORBIDDEN_CANDIDATE_FIELDS, validate_campaign_request
from validate_harness_extension import normalize_extension_request
from validate_recipe import safe_source_asset_path

SUPPORTED_KINDS = {
    "api_plugin_candidate",
    "attack_dsl",
    "flat_recipe",
    "cluster_seed",
    "needs_harness_extension",
    "campaign_request",
}
SUPPORTED_BOOLEAN_TYPES = {
    "subtract": "SUBTRACTION",
    "subtraction": "SUBTRACTION",
    "difference": "SUBTRACTION",
    "intersect": "INTERSECTION",
    "intersection": "INTERSECTION",
    "common": "INTERSECTION",
    "union": "UNION",
    "fuse": "UNION",
}
ORACLE_TO_EXPECTATION_ARRAY = {
    "distance": "distance_checks",
    "clash": "clash_checks",
    "plane_extreme": "plane_extreme_checks",
    "point_relation": "point_relations",
    "face_point_relation": "face_point_relations",
}


def validate_model_source_assets(value: Any, path: str, diagnostics: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "source_file" or key.endswith("_source_file"):
                if not isinstance(child, str) or not child.strip():
                    diagnostics.append(
                        diagnostic(
                            "error",
                            "UNSAFE_SOURCE_FILE",
                            child_path,
                            "Model-authored source_file must be a non-empty repository-relative path.",
                            "Use a materialized path listed in the task's input asset availability.",
                        )
                    )
                    continue
                _, unsafe_reason = safe_source_asset_path(child.strip())
                if unsafe_reason:
                    diagnostics.append(
                        diagnostic(
                            "error",
                            "UNSAFE_SOURCE_FILE",
                            child_path,
                            f"Unsafe model-authored asset reference: {unsafe_reason}.",
                            (
                                "Use a repository-relative path under artifacts/, test_harness/fixtures/, "
                                "or SGK1.4.10/samples/ after fixed code materializes the asset."
                            ),
                        )
                    )
            else:
                validate_model_source_assets(child, child_path, diagnostics)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_model_source_assets(child, f"{path}[{index}]", diagnostics)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Model output JSON, raw DSL JSON, or raw flat recipe JSON")
    parser.add_argument("--request-id", default="", help="Request id used to name normalized output files")
    parser.add_argument("--model-output-root", default="artifacts/model_outputs")
    parser.add_argument("--out", default="", help="Explicit normalized output path")
    parser.add_argument("--diagnostics", default="", help="Optional diagnostics JSON path")
    parser.add_argument("--print-json", action="store_true", help="Print normalization report JSON")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def safe_id(value: str) -> str:
    result = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in value)
    result = result.strip("._-")
    return result or "model_output"


def diagnostic(
    severity: str,
    code: str,
    path: str,
    message: str,
    repair_hint: str,
    expected_shape: Any | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": severity,
        "error_code": code,
        "path": path,
        "message": message,
        "repair_hint": repair_hint,
    }
    if expected_shape is not None:
        item["expected_shape"] = expected_shape
    return item


def canonical_boolean_type(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return SUPPORTED_BOOLEAN_TYPES.get(value.strip().lower(), value)


def normalize_aliases(value: Any, path: str, diagnostics: list[dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [normalize_aliases(item, f"{path}[{index}]", diagnostics) for index, item in enumerate(value)]
    if not isinstance(value, dict):
        return value

    result: dict[str, Any] = {}
    for key, raw in value.items():
        next_key = key
        next_value = normalize_aliases(raw, f"{path}.{key}" if path else key, diagnostics)
        if key == "operation":
            next_key = "boolean_type"
            next_value = canonical_boolean_type(next_value)
            diagnostics.append(
                diagnostic(
                    "info",
                    "NORMALIZED_OPERATION_ALIAS",
                    path or "$",
                    "Converted operation to boolean_type.",
                    "Prefer boolean_type with SUBTRACTION, INTERSECTION, or UNION in attack DSL.",
                )
            )
        elif key == "type":
            if "chain" in path or re.search(r"\.chain\[\d+\]$", path):
                next_key = "op"
                diagnostics.append(
                    diagnostic(
                        "info",
                        "NORMALIZED_TYPE_TO_OP",
                        path or "$",
                        "Converted chain step type to op.",
                        "Use op for DSL chain steps.",
                    )
                )
            elif "kind" not in value:
                next_key = "kind"
                diagnostics.append(
                    diagnostic(
                        "info",
                        "NORMALIZED_TYPE_TO_KIND",
                        path or "$",
                        "Converted body type to kind.",
                        "Use kind for direct body builder specs.",
                    )
                )
        elif key == "boolean_type":
            next_value = canonical_boolean_type(next_value)
        elif key == "distance" and value.get("api") == "api_offset_body" and "offset_distance" not in value:
            next_key = "offset_distance"
            diagnostics.append(
                diagnostic(
                    "info",
                    "NORMALIZED_OFFSET_DISTANCE_ALIAS",
                    path or "$",
                    "Converted api_offset_body distance to offset_distance.",
                    "Prefer offset_distance in api_offset_body flat recipes.",
                )
            )
        result[next_key] = next_value
    return result


def normalize_oracles(case: dict[str, Any], case_path: str, diagnostics: list[dict[str, Any]]) -> None:
    raw_oracles = case.pop("oracles", None)
    if raw_oracles is None:
        return
    if not isinstance(raw_oracles, list):
        diagnostics.append(
            diagnostic(
                "error",
                "INVALID_ORACLES",
                f"{case_path}.oracles",
                "oracles must be an array when present.",
                "Move supported oracle checks into case.expectations.",
            )
        )
        return
    expectations = case.setdefault("expectations", {})
    if not isinstance(expectations, dict):
        diagnostics.append(
            diagnostic(
                "error",
                "INVALID_EXPECTATIONS",
                f"{case_path}.expectations",
                "expectations must be an object.",
                "Use an object with result_bodies, distance_checks, clash_checks, or other supported fields.",
            )
        )
        return
    for index, raw_oracle in enumerate(raw_oracles):
        oracle_path = f"{case_path}.oracles[{index}]"
        if not isinstance(raw_oracle, dict):
            diagnostics.append(
                diagnostic(
                    "error",
                    "INVALID_ORACLE",
                    oracle_path,
                    "oracle entries must be objects.",
                    "Remove the invalid oracle.",
                )
            )
            continue
        oracle_type = raw_oracle.get("type") or raw_oracle.get("kind")
        params = raw_oracle.get("params", raw_oracle)
        if not isinstance(oracle_type, str):
            diagnostics.append(
                diagnostic(
                    "error",
                    "ORACLE_TYPE_MISSING",
                    oracle_path,
                    "oracle type is missing.",
                    "Use a supported expectation field.",
                )
            )
            continue
        key = oracle_type.strip().lower()
        if key == "result_bodies":
            if isinstance(params, dict):
                result_bodies: dict[str, Any] = {}
                if params.get("expect_non_empty") is True:
                    result_bodies["min"] = 1
                for child_key in ("min", "max"):
                    if child_key in params:
                        result_bodies[child_key] = params[child_key]
                if result_bodies:
                    expectations["result_bodies"] = result_bodies
            diagnostics.append(
                diagnostic(
                    "info",
                    "NORMALIZED_RESULT_BODIES_ORACLE",
                    oracle_path,
                    "Converted result_bodies oracle to expectations.result_bodies.",
                    "Prefer expectations.result_bodies directly.",
                )
            )
        elif key == "properties":
            expectations.setdefault("require_property_calculations", True)
            expectations.setdefault("require_finite_properties", True)
            diagnostics.append(
                diagnostic(
                    "info",
                    "NORMALIZED_PROPERTIES_ORACLE",
                    oracle_path,
                    "Converted properties oracle to property-calculation expectations.",
                    "Use explicit total_volume or total_abs_volume bounds when they are analytically known.",
                )
            )
        elif key == "topocheck":
            diagnostics.append(
                diagnostic(
                    "warning",
                    "TOPOCHECK_ORACLE_IS_IMPLICIT",
                    oracle_path,
                    (
                        "topocheck is not a standalone DSL expectation; runner validity checks "
                        "cover supported body outputs."
                    ),
                    "Use check_valid/topo_track options or concrete topology-related expectations.",
                )
            )
        elif key in ORACLE_TO_EXPECTATION_ARRAY and isinstance(params, dict):
            expectation_key = ORACLE_TO_EXPECTATION_ARRAY[key]
            expectations.setdefault(expectation_key, []).append(deepcopy(params))
            diagnostics.append(
                diagnostic(
                    "info",
                    "NORMALIZED_ORACLE_ARRAY",
                    oracle_path,
                    f"Converted {key} oracle to expectations.{expectation_key}.",
                    f"Prefer expectations.{expectation_key} directly.",
                )
            )
        else:
            diagnostics.append(
                diagnostic(
                    "error",
                    "UNSUPPORTED_ORACLE",
                    oracle_path,
                    f"Unsupported oracle type: {oracle_type!r}.",
                    "Use supported expectation fields or return needs_harness_extension.",
                )
            )


def normalize_expectation_shorthands(container: dict[str, Any], path: str, diagnostics: list[dict[str, Any]]) -> None:
    expectations = container.get("expectations")
    if not isinstance(expectations, dict):
        return
    result_bodies = expectations.get("result_bodies")
    if isinstance(result_bodies, int) and not isinstance(result_bodies, bool):
        expectations["result_bodies"] = {"min": result_bodies}
        diagnostics.append(
            diagnostic(
                "info",
                "NORMALIZED_RESULT_BODIES_SHORTHAND",
                f"{path}.expectations.result_bodies",
                "Converted scalar result_bodies to an object with min.",
                'Prefer expectations.result_bodies as {"min": 1} or {"min": 1, "max": 1}.',
            )
        )


def normalize_dsl(dsl: dict[str, Any], diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = normalize_aliases(deepcopy(dsl), "$.dsl", diagnostics)
    if not isinstance(normalized, dict):
        return normalized

    if "dsl_version" not in normalized:
        normalized["dsl_version"] = 1
        diagnostics.append(
            diagnostic(
                "info",
                "ADDED_DSL_VERSION",
                "$.dsl",
                "Added default dsl_version=1.",
                "Emit dsl_version explicitly.",
            )
        )

    cases = normalized.get("cases")
    if not isinstance(cases, list) or not cases:
        diagnostics.append(
            diagnostic(
                "error",
                "MISSING_CASES",
                "$.dsl.cases",
                "DSL must contain a non-empty cases array.",
                "Return attack_dsl with dsl.cases containing runnable case objects.",
                {"cases": [{"case_id": "...", "target": {}, "tool": {}}]},
            )
        )
        return normalized

    has_global_chains = isinstance(normalized.get("chains"), list)
    for index, raw_case in enumerate(cases):
        case_path = f"$.dsl.cases[{index}]"
        if not isinstance(raw_case, dict):
            diagnostics.append(
                diagnostic(
                    "error",
                    "INVALID_CASE",
                    case_path,
                    "Each DSL case must be an object.",
                    "Replace non-object cases with objects containing case_id, target, and tool.",
                )
            )
            continue
        normalize_oracles(raw_case, case_path, diagnostics)
        normalize_expectation_shorthands(raw_case, case_path, diagnostics)
        if "target" not in raw_case or "tool" not in raw_case:
            code = "UNSUPPORTED_GLOBAL_CHAINS_SCHEMA" if has_global_chains else "MISSING_TARGET_TOOL"
            hint = (
                "Do not use top-level chains plus case inputs. Move valid chain specs into each "
                "case.target and case.tool."
                if has_global_chains
                else "api_boolean cases require direct target and tool objects."
            )
            diagnostics.append(
                diagnostic(
                    "error",
                    code,
                    case_path,
                    "api_boolean cases require target and tool.",
                    hint,
                    {
                        "case_id": "example_case",
                        "target": {"chain": [{"id": "target_profile", "op": "rect_profile"}]},
                        "tool": {"chain": [{"id": "tool_profile", "op": "circle_profile"}]},
                    },
                )
            )
    return normalized


def infer_kind(loaded: Any) -> str:
    if not isinstance(loaded, dict):
        return "invalid"
    kind = loaded.get("kind")
    if isinstance(kind, str):
        return kind
    if "dsl_version" in loaded or "cases" in loaded:
        return "attack_dsl"
    if "api" in loaded and ("case_id" in loaded or "source_file" in loaded):
        return "flat_recipe"
    return "invalid"


def output_path_for(kind: str, request_id: str, root: Path, explicit: str) -> Path:
    if explicit:
        return Path(explicit)
    base = safe_id(request_id)
    if kind == "attack_dsl":
        return root / f"{base}_dsl.json"
    if kind == "flat_recipe":
        return root / f"{base}_recipe.json"
    if kind == "cluster_seed":
        return root / f"{base}_cluster_seed.json"
    return root / f"{base}.json"


def normalize_loaded(loaded: Any, request_id: str, root: Path, explicit_out: str) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "generated_at": now_iso_like(),
        "request_id": request_id,
        "kind": "invalid",
        "ok": False,
        "normalized_path": "",
        "diagnostics": diagnostics,
    }
    if not isinstance(loaded, dict):
        diagnostics.append(
            diagnostic(
                "error",
                "MODEL_OUTPUT_NOT_OBJECT",
                "$",
                "Model output must be one JSON object.",
                "Return exactly one JSON object matching a supported contract.",
            )
        )
        return report

    kind = infer_kind(loaded)
    report["kind"] = kind
    forbidden_fields = sorted(
        field
        for field in FORBIDDEN_CANDIDATE_FIELDS
        if field in loaded and not (kind == "cluster_seed" and field == "tool")
    )
    if forbidden_fields:
        diagnostics.append(
            diagnostic(
                "error",
                "MODEL_OUTPUT_EXECUTABLE_FIELDS_FORBIDDEN",
                "$",
                f"Model output contains forbidden executable or fixed-binding fields: {forbidden_fields}.",
                (
                    "Return only the selected output contract. Campaigns use campaign_request "
                    "profile_id plus bounded args."
                ),
            )
        )
        return report
    if kind not in SUPPORTED_KINDS:
        diagnostics.append(
            diagnostic(
                "error",
                "UNSUPPORTED_MODEL_KIND",
                "$.kind",
                f"Unsupported model output kind: {kind!r}.",
                "Use api_plugin_candidate, attack_dsl, flat_recipe, cluster_seed, needs_harness_extension, or campaign_request.",
            )
        )
        return report

    if kind == "attack_dsl":
        dsl = loaded.get("dsl") if loaded.get("kind") == "attack_dsl" else loaded
        if not isinstance(dsl, dict):
            diagnostics.append(
                diagnostic(
                    "error",
                    "ATTACK_DSL_MISSING_DSL",
                    "$.dsl",
                    "attack_dsl output must contain object field dsl.",
                    'Wrap the DSL as {"kind":"attack_dsl","dsl":{...}}.',
                )
            )
            return report
        normalized_payload = normalize_dsl(dsl, diagnostics)
    elif kind == "flat_recipe":
        recipe = loaded.get("recipe") if loaded.get("kind") == "flat_recipe" else loaded
        if not isinstance(recipe, dict):
            diagnostics.append(
                diagnostic(
                    "error",
                    "FLAT_RECIPE_MISSING_RECIPE",
                    "$.recipe",
                    "flat_recipe output must contain object field recipe.",
                    'Wrap the recipe as {"kind":"flat_recipe","recipe":{...}}.',
                )
            )
            return report
        normalized_payload = normalize_aliases(deepcopy(recipe), "$.recipe", diagnostics)
        if isinstance(normalized_payload, dict):
            normalize_expectation_shorthands(normalized_payload, "$.recipe", diagnostics)
    elif kind == "needs_harness_extension":
        normalized_payload, extension_notes = normalize_extension_request(deepcopy(loaded))
        diagnostics.extend(extension_notes)
    elif kind == "campaign_request":
        normalized_payload, campaign_errors = validate_campaign_request(deepcopy(loaded))
        for message in campaign_errors:
            diagnostics.append(
                diagnostic(
                    "error",
                    "CAMPAIGN_REQUEST_INVALID",
                    "$",
                    message,
                    "Select a registered profile_id and provide only bounded args allowed by its args_schema.",
                )
            )
        if normalized_payload is None:
            return report
    else:
        normalized_payload = deepcopy(loaded)

    if kind in {"attack_dsl", "flat_recipe", "cluster_seed"}:
        validate_model_source_assets(normalized_payload, "$", diagnostics)

    out_path = output_path_for(kind, request_id, root, explicit_out)
    write_json(out_path, normalized_payload)
    report["normalized_path"] = str(out_path)
    report["ok"] = not any(item.get("severity") == "error" for item in diagnostics)
    return report


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    request_id = args.request_id or input_path.stem
    try:
        report = normalize_loaded(read_json(input_path), request_id, Path(args.model_output_root), args.out)
    except (OSError, json.JSONDecodeError) as exc:
        report = {
            "generated_at": now_iso_like(),
            "request_id": request_id,
            "kind": "invalid",
            "ok": False,
            "normalized_path": "",
            "diagnostics": [
                diagnostic(
                    "error",
                    "MODEL_OUTPUT_READ_FAILED",
                    str(input_path),
                    str(exc),
                    "Ensure the model output is valid JSON.",
                )
            ],
        }
    if args.diagnostics:
        write_json(Path(args.diagnostics), report)
    if args.print_json or not args.diagnostics:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
