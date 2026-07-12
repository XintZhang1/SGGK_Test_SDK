#!/usr/bin/env python3
"""Expand a reviewed source-guided seed into compact SGGK attack DSL."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from typing import Any


class ClusterError(ValueError):
    pass


DEFAULT_CONSTANTS: dict[str, Any] = {
    "topo_tol": 0.01,
    "geom_tol": 0.00001,
    "max_model_size": 500000.0,
    "tau": "2 * pi",
}

DEFAULT_DEFAULTS: dict[str, Any] = {
    "api": "api_boolean",
    "modeling_tol": "topo_tol",
    "max_model_size": "max_model_size",
    "check_valid": True,
    "topo_track": False,
    "non_destructive": True,
}

DEFAULT_EXPECTATIONS: dict[str, Any] = {
    "result_bodies": {"min": 1},
    "require_property_calculations": True,
    "require_finite_properties": True,
    "require_nonnegative_length_area": True,
    "require_nonnegative_volume": False,
    "boolean_volume_relation": True,
    "volume_relation_abs_tol": "topo_tol",
    "volume_relation_rel_tol": 0.00000001,
    "sample_input_properties": True,
}

BANDS = [
    ("overlap_topo", "-", "topo_tol", {"SUBTRACTION": 1, "INTERSECTION": 1}),
    ("overlap_geom", "-", "geom_tol", {"SUBTRACTION": 1, "INTERSECTION": 0}),
    ("exact", "", "", {"SUBTRACTION": 1, "INTERSECTION": 0}),
    ("gap_geom", "+", "geom_tol", {"SUBTRACTION": 1, "INTERSECTION": 0}),
    ("gap_topo", "+", "topo_tol", {"SUBTRACTION": 1, "INTERSECTION": 0}),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed", help="Reviewed cluster_seed JSON")
    parser.add_argument("--out", required=True, help="Output attack DSL JSON")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ClusterError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ClusterError(f"{path}: root must be an object")
    return loaded


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ClusterError(f"{label} must be an object")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ClusterError(f"{label} must be a non-empty string")
    return value


def sanitize_id(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    if not result:
        raise ClusterError("id became empty after sanitization")
    return result


def bool_prefix(boolean_type: str) -> str:
    return {
        "SUBTRACTION": "sub",
        "INTERSECTION": "int",
        "UNION": "union",
    }.get(boolean_type, sanitize_id(boolean_type))


def contact_text(value: Any) -> str:
    if isinstance(value, bool):
        raise ClusterError("contact_value must not be bool")
    if isinstance(value, (int, float)):
        return repr(float(value))
    if isinstance(value, str) and value:
        return value
    raise ClusterError("contact_value must be a number or numeric expression string")


def contact_expression(contact_value: Any, sign: str, offset: Any) -> Any:
    if sign == "":
        return deepcopy(contact_value)
    if sign not in {"+", "-"}:
        raise ClusterError(f"literal direction must be '+' or '-', got {sign!r}")
    if isinstance(offset, bool) or offset in ("", None):
        raise ClusterError("offset must be a number or numeric expression string")
    return f"{contact_text(contact_value)} {sign} {offset}"


def normalize_boolean_types(item: dict[str, Any] | None = None) -> list[str]:
    if item is None:
        return ["SUBTRACTION", "INTERSECTION"]
    if "boolean_types" in item:
        values = item["boolean_types"]
        if not isinstance(values, list) or not values:
            raise ClusterError("source_literal_offsets[].boolean_types must be a non-empty list")
        result = [require_string(value, "boolean_type") for value in values]
    elif "boolean_type" in item:
        result = [require_string(item["boolean_type"], "boolean_type")]
    else:
        result = ["SUBTRACTION", "INTERSECTION"]
    unsupported = [value for value in result if value not in {"SUBTRACTION", "INTERSECTION", "UNION"}]
    if unsupported:
        raise ClusterError(f"unsupported boolean types in cluster seed: {unsupported}")
    return result


def min_result_for(item: dict[str, Any] | None, boolean_type: str, defaults: dict[str, int]) -> int:
    if item is None:
        return defaults[boolean_type]
    raw = item.get("min_result_bodies", item.get("result_bodies_min"))
    if raw is None:
        return defaults.get(boolean_type, 0)
    if isinstance(raw, dict):
        raw = raw.get(boolean_type, defaults.get(boolean_type, 0))
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ClusterError("min_result_bodies must be int >= 0 or a boolean-type map")
    return raw


def build_band_values(contact_value: Any, literal_offsets: list[Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for suffix, sign, offset, defaults in BANDS:
        for boolean_type in normalize_boolean_types():
            values.append(
                {
                    "suffix": f"{bool_prefix(boolean_type)}_{suffix}",
                    "values": [
                        boolean_type,
                        contact_expression(contact_value, sign, offset),
                        min_result_for(None, boolean_type, defaults),
                    ],
                }
            )

    for index, raw in enumerate(literal_offsets, start=1):
        item = require_object(raw, f"source_literal_offsets[{index}]")
        suffix = sanitize_id(str(item.get("suffix") or item.get("name") or f"literal_{index}"))
        if "value" in item:
            value = item["value"]
        else:
            if "offset" not in item:
                raise ClusterError("source_literal_offsets[] must contain offset or value")
            value = contact_expression(contact_value, require_string(item.get("direction", "+"), "direction"), item["offset"])
        for boolean_type in normalize_boolean_types(item):
            defaults = {"SUBTRACTION": 1, "INTERSECTION": 0, "UNION": 1}
            values.append(
                {
                    "suffix": f"{bool_prefix(boolean_type)}_source_{suffix}",
                    "values": [boolean_type, value, min_result_for(item, boolean_type, defaults)],
                }
            )
    return values


def collect_literal_offsets(seed: dict[str, Any], override: dict[str, Any]) -> list[Any]:
    if "source_literal_offsets" in override:
        raw = override["source_literal_offsets"]
    else:
        raw = seed.get("source_literal_offsets", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ClusterError("source_literal_offsets must be a list")
    return raw


def make_case(seed: dict[str, Any], override: dict[str, Any], suffix: str) -> dict[str, Any]:
    cluster_id = sanitize_id(require_string(seed.get("cluster_id"), "cluster_id"))
    case_id = sanitize_id(str(override.get("case_id") or (cluster_id if not suffix else f"{cluster_id}_{suffix}")))
    contact_path = require_string(override.get("contact_path", seed.get("contact_path")), "contact_path")
    if "contact_value" not in override and "contact_value" not in seed:
        raise ClusterError("contact_value is required")
    contact_value = override.get("contact_value", seed.get("contact_value"))
    literal_offsets = collect_literal_offsets(seed, override)

    if "target" not in override and "target" not in seed:
        raise ClusterError(f"{case_id}: target is required")
    if "tool" not in override and "tool" not in seed:
        raise ClusterError(f"{case_id}: tool is required")

    expectations = merge_dicts(DEFAULT_EXPECTATIONS, require_object(seed.get("expectations", {}), "expectations"))
    expectations = merge_dicts(expectations, require_object(override.get("expectations", {}), "sibling.expectations"))

    case: dict[str, Any] = {
        "case_id": case_id,
        "source_ref": require_string(override.get("source_ref", seed.get("source_ref")), "source_ref"),
        "hypothesis": require_string(override.get("hypothesis", seed.get("hypothesis")), "hypothesis"),
        "source_review": deepcopy(override.get("source_review", seed.get("source_review", {}))),
        "target": deepcopy(override.get("target", seed.get("target"))),
        "tool": deepcopy(override.get("tool", seed.get("tool"))),
        "expectations": expectations,
        "paired_sweeps": [
            {
                "paths": [
                    "options.boolean_type",
                    contact_path,
                    "expectations.result_bodies.min",
                ],
                "values": build_band_values(contact_value, literal_offsets),
            }
        ],
    }

    options = merge_dicts(require_object(seed.get("options", {}), "options"), require_object(override.get("options", {}), "sibling.options"))
    if options:
        case["options"] = options
    metadata = merge_dicts(require_object(seed.get("metadata", {}), "metadata"), require_object(override.get("metadata", {}), "sibling.metadata"))
    if metadata:
        case["metadata"] = metadata
    return case


def iter_sibling_overrides(seed: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    siblings: list[tuple[str, dict[str, Any]]] = []
    generated = seed.get("generated_sibling")
    if generated is not None:
        siblings.append(("generated", require_object(generated, "generated_sibling")))
    large = seed.get("large_coordinate_sibling")
    if large is not None:
        siblings.append(("large_coordinate", require_object(large, "large_coordinate_sibling")))
    extra = seed.get("siblings", [])
    if extra:
        if not isinstance(extra, list):
            raise ClusterError("siblings must be a list")
        for index, raw in enumerate(extra, start=1):
            item = require_object(raw, f"siblings[{index}]")
            suffix = sanitize_id(str(item.get("case_suffix") or item.get("suffix") or f"sibling_{index}"))
            siblings.append((suffix, item))
    return siblings


def build_dsl(seed: dict[str, Any]) -> dict[str, Any]:
    if seed.get("kind") not in (None, "cluster_seed"):
        raise ClusterError("seed.kind must be cluster_seed when present")
    constants = merge_dicts(DEFAULT_CONSTANTS, require_object(seed.get("constants", {}), "constants"))
    defaults = merge_dicts(DEFAULT_DEFAULTS, require_object(seed.get("defaults", {}), "defaults"))

    cases = [make_case(seed, {}, "")]
    for default_suffix, override in iter_sibling_overrides(seed):
        suffix = sanitize_id(str(override.get("case_suffix") or override.get("suffix") or default_suffix))
        cases.append(make_case(seed, override, suffix))

    return {
        "dsl_version": 1,
        "source_review": deepcopy(seed.get("source_review", {})),
        "metadata": {
            "kind": "source_guided_cluster",
            "cluster_id": sanitize_id(require_string(seed.get("cluster_id"), "cluster_id")),
            "source_ref": seed.get("source_ref"),
            "review_required": seed.get("review_required", True),
            "generator": "build_source_guided_cluster.py",
        },
        "constants": constants,
        "defaults": defaults,
        "cases": cases,
    }


def main() -> int:
    args = parse_args()
    try:
        seed = read_json(Path(args.seed))
        dsl = build_dsl(seed)
        write_json(Path(args.out), dsl)
        print(f"OK {args.out} cases={len(dsl['cases'])}")
        return 0
    except ClusterError as exc:
        print(f"build_source_guided_cluster: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
