#!/usr/bin/env python3
"""Deterministic complexity scoring for model-authored SGGK test candidates.

The fixed pipeline gate runs this scorer on attack_dsl and flat_recipe
candidates so "simple cases only" output cannot pass review.  The scorer never
calls a provider or the SDK: it reads the candidate JSON, scores each case on
fixed complexity dimensions, and emits model-friendly diagnostics when the
candidate falls below the host complexity floor.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

GENERATED_BODY_BUILDERS = {
    "extrude_rect",
    "thicken_rect_sheet",
    "sweep_circle_line",
    "support_sweep_bspline_surface",
    "revolve_line",
    "revolve_rect",
    "pre_boolean_cylinder_wedge",
}
GENERATED_CHAIN_OPS = {
    "extrude",
    "thicken",
    "sweep_line",
    "support_sweep",
    "revolve",
    "boolean",
}
MEASURABLE_ORACLE_KEYS = {
    "result_bodies",
    "point_relations",
    "face_point_relations",
    "clash_checks",
    "distance_checks",
    "plane_extreme_checks",
    "total_length",
    "total_area",
    "total_volume",
    "total_abs_volume",
    "boolean_volume_relation",
    "boolean_bbox_relation",
}
# API-specific count/status oracles from interface_capabilities.json; these are
# measurable result oracles, not API-status checks.
COUNT_ORACLE_KEYS = {
    "split_outer_bodies",
    "split_inner_bodies",
    "split_wire_bodies",
    "split_total_bodies",
    "slice_result_bodies",
    "slice_wire_bodies",
    "topology_section_edges",
    "topology_section_vertices",
    "topology_section_total",
    "offset2d_result_path_count",
    "offset2d_result_paths",
    "offset2d_status",
    "roundtrip_comparison",
}
TOLERANCE_SYMBOLS = ("topo_tol", "geom_tol")
LARGE_COORDINATE_THRESHOLD = 10_000.0

DIMENSIONS = (
    "chain_depth",
    "generated_topology",
    "tolerance_band",
    "oracle_strength",
    "large_coordinate",
    "degenerate_or_negative",
    "transform_usage",
)

# Host complexity floor for attack_dsl candidates.
MIN_DIMENSIONS_COVERED = 4
MIN_STRONG_CASE_FRACTION = 0.5
STRONG_CASE_SCORE = 3
# Host complexity floor for flat_recipe candidates.
MIN_FLAT_SCORE = 3


class ComplexityError(ValueError):
    """Raised when a candidate cannot be scored at all."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _iter_body_specs(case: dict[str, Any]) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []
    for role in ("target", "tool"):
        body = case.get(role)
        if isinstance(body, dict):
            bodies.append(body)
    return bodies


def _body_chain(body: dict[str, Any]) -> list[Any]:
    chain = body.get("chain")
    return chain if isinstance(chain, list) else []


def _body_kind(body: dict[str, Any]) -> str:
    kind = body.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    for step in _body_chain(body):
        if isinstance(step, dict) and step.get("op") == "primitive" and isinstance(step.get("kind"), str):
            return str(step["kind"])
    return ""


def _case_uses_generated_topology(case: dict[str, Any]) -> bool:
    for body in _iter_body_specs(case):
        if _body_kind(body) in GENERATED_BODY_BUILDERS:
            return True
        for step in _body_chain(body):
            if isinstance(step, dict) and step.get("op") in GENERATED_CHAIN_OPS:
                return True
    return False


def _case_chain_depth(case: dict[str, Any]) -> int:
    depth = 0
    for body in _iter_body_specs(case):
        depth = max(depth, len(_body_chain(body)))
    return depth


def _text_mentions_tolerance(value: Any) -> bool:
    if isinstance(value, str):
        return any(symbol in value for symbol in TOLERANCE_SYMBOLS)
    if isinstance(value, list):
        return any(_text_mentions_tolerance(item) for item in value)
    if isinstance(value, dict):
        return any(_text_mentions_tolerance(item) for item in value.values())
    return False


def _case_has_tolerance_band(case: dict[str, Any]) -> bool:
    if case.get("sweeps") or case.get("paired_sweeps"):
        return True
    for key in ("target", "tool", "options", "expectations", "variants"):
        if _text_mentions_tolerance(case.get(key)):
            return True
    return False


def _oracle_families(expectations: dict[str, Any]) -> set[str]:
    families: set[str] = set()
    for key in MEASURABLE_ORACLE_KEYS | COUNT_ORACLE_KEYS:
        if key not in expectations:
            continue
        value = expectations[key]
        if isinstance(value, bool):
            if value:
                families.add(key)
            continue
        if value:
            families.add(key)
    for key in ("require_property_calculations", "require_finite_properties", "require_nonnegative_length_area", "require_nonnegative_volume"):
        if expectations.get(key) is True:
            families.add("properties")
    return families


def _case_large_coordinate(case: dict[str, Any]) -> bool:
    def scan(value: Any) -> bool:
        if _is_number(value):
            return abs(float(value)) >= LARGE_COORDINATE_THRESHOLD
        if isinstance(value, list):
            return any(scan(item) for item in value)
        if isinstance(value, dict):
            return any(scan(item) for item in value.values())
        return False

    for role in ("target", "tool"):
        body = case.get(role)
        if not isinstance(body, dict):
            continue
        for key, value in body.items():
            if key.startswith("translate") and scan(value):
                return True
        for step in _body_chain(body):
            if isinstance(step, dict) and step.get("op") == "transform":
                for key, value in step.items():
                    if key.startswith("translate") and scan(value):
                        return True
    return False


def _case_degenerate_or_negative(case: dict[str, Any]) -> bool:
    expectations = case.get("expectations")
    if isinstance(expectations, dict):
        result_bodies = expectations.get("result_bodies")
        if isinstance(result_bodies, dict) and result_bodies.get("max") == 0:
            return True
        status = expectations.get("offset2d_status")
        if isinstance(status, str) and status and status.lower() not in {"success", "ok"}:
            return True
    for body in _iter_body_specs(case):
        for step in _body_chain(body):
            if not isinstance(step, dict):
                continue
            angle = step.get("angle")
            if _is_number(angle) and 0 < float(angle) < 6.283185307179586:
                return True
    return False


def _case_transform_usage(case: dict[str, Any]) -> bool:
    for body in _iter_body_specs(case):
        for step in _body_chain(body):
            if not isinstance(step, dict) or step.get("op") != "transform":
                continue
            for key, value in step.items():
                if key.startswith("translate"):
                    if _is_number(value) and float(value) != 0.0:
                        return True
                    if isinstance(value, list) and any(_is_number(item) and float(item) != 0.0 for item in value):
                        return True
                if key == "scale" and _is_number(value) and float(value) != 1.0:
                    return True
        for key, value in body.items():
            if key.startswith("translate_") and _is_number(value) and float(value) != 0.0:
                return True
            if key == "scale" and _is_number(value) and float(value) != 1.0:
                return True
    return False


def score_case(case: dict[str, Any], case_id: str) -> dict[str, Any]:
    """Score one DSL case or cluster base on the fixed dimensions."""

    dimensions: dict[str, int] = {}
    depth = _case_chain_depth(case)
    dimensions["chain_depth"] = 1 if depth >= 2 else 0
    dimensions["generated_topology"] = 1 if _case_uses_generated_topology(case) else 0
    dimensions["tolerance_band"] = 1 if _case_has_tolerance_band(case) else 0
    expectations = case.get("expectations")
    families = _oracle_families(expectations) if isinstance(expectations, dict) else set()
    dimensions["oracle_strength"] = 1 if len(families) >= 2 else (1 if families else 0)
    dimensions["large_coordinate"] = 1 if _case_large_coordinate(case) else 0
    dimensions["degenerate_or_negative"] = 1 if _case_degenerate_or_negative(case) else 0
    dimensions["transform_usage"] = 1 if _case_transform_usage(case) else 0
    total = sum(dimensions.values())
    return {
        "case_id": case_id,
        "score": total,
        "dimensions": dimensions,
        "oracle_families": sorted(families),
    }


def score_flat_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    """Score one flat runner recipe on the fixed dimensions."""

    dimensions: dict[str, int] = {"chain_depth": 0}
    kinds = {str(recipe.get("target_kind") or ""), str(recipe.get("tool_kind") or ""), str(recipe.get("source_kind") or "")}
    dimensions["generated_topology"] = 1 if kinds & GENERATED_BODY_BUILDERS else 0
    modeling_tol = recipe.get("modeling_tol")
    tolerance_focus = (_is_number(modeling_tol) and float(modeling_tol) != 0.01) or any(
        _is_number(recipe.get(key)) for key in ("operation_tol", "g1_tol")
    )
    dimensions["tolerance_band"] = 1 if tolerance_focus else 0
    expectations = recipe.get("expectations")
    families = _oracle_families(expectations) if isinstance(expectations, dict) else set()
    dimensions["oracle_strength"] = 1 if families else 0
    large = False
    transform = False
    for key, value in recipe.items():
        if not _is_number(value):
            continue
        if key.startswith(("target_translate_", "tool_translate_")):
            if abs(float(value)) >= LARGE_COORDINATE_THRESHOLD:
                large = True
            if float(value) != 0.0:
                transform = True
        if key in {"target_scale", "tool_scale"} and float(value) != 1.0:
            transform = True
    dimensions["large_coordinate"] = 1 if large else 0
    dimensions["degenerate_or_negative"] = 0
    expectations = recipe.get("expectations")
    if isinstance(expectations, dict):
        result_bodies = expectations.get("result_bodies")
        if isinstance(result_bodies, dict) and result_bodies.get("max") == 0:
            dimensions["degenerate_or_negative"] = 1
        status = expectations.get("offset2d_status")
        if isinstance(status, str) and status and status.lower() not in {"success", "ok"}:
            dimensions["degenerate_or_negative"] = 1
    dimensions["transform_usage"] = 1 if transform else 0
    total = sum(dimensions.values())
    return {
        "case_id": str(recipe.get("case_id") or "<unknown>"),
        "score": total,
        "dimensions": dimensions,
        "oracle_families": sorted(families),
    }


def _diagnostic(severity: str, code: str, path: str, message: str, repair_hint: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "error_code": code,
        "path": path,
        "message": message,
        "repair_hint": repair_hint,
    }


def evaluate_dsl_candidate(dsl: dict[str, Any], path: str) -> dict[str, Any]:
    """Score all cases and cluster bases of an attack_dsl candidate."""

    defaults = dsl.get("defaults") if isinstance(dsl.get("defaults"), dict) else {}
    default_expectations: dict[str, Any] = {}
    if isinstance(defaults.get("expectations"), dict):
        default_expectations = defaults["expectations"]

    def with_defaults(case: dict[str, Any]) -> dict[str, Any]:
        if not default_expectations:
            return case
        merged = dict(case)
        case_expectations = merged.get("expectations") if isinstance(merged.get("expectations"), dict) else {}
        merged["expectations"] = {**default_expectations, **case_expectations}
        return merged

    cases = dsl.get("cases")
    bases = dsl.get("cluster_bases")
    scored: list[dict[str, Any]] = []
    if isinstance(cases, list):
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                continue
            case_id = str(case.get("case_id") or case.get("id") or f"case_{index}")
            scored.append(score_case(with_defaults(case), case_id))
    if isinstance(bases, dict):
        for base_id, base in bases.items():
            if isinstance(base, dict):
                scored.append(score_case(with_defaults(base), f"cluster_base:{base_id}"))
    if not scored:
        raise ComplexityError(f"{path}: no scorable cases or cluster bases found")

    covered = sorted(
        {dimension for item in scored for dimension, value in item["dimensions"].items() if value}
    )
    strong = [item for item in scored if item["score"] >= STRONG_CASE_SCORE]
    strong_fraction = len(strong) / len(scored)
    has_deep_chain = any(item["dimensions"]["chain_depth"] for item in scored)
    has_generated = any(item["dimensions"]["generated_topology"] for item in scored)

    diagnostics: list[dict[str, Any]] = []
    missing = [dimension for dimension in DIMENSIONS if dimension not in covered]
    if len(covered) < MIN_DIMENSIONS_COVERED:
        diagnostics.append(
            _diagnostic(
                "error",
                "COMPLEXITY_DIMENSIONS_MISSING",
                path,
                f"candidate covers {len(covered)}/{len(DIMENSIONS)} complexity dimensions; "
                f"missing: {', '.join(missing)}.",
                "Add cases that combine more complexity dimensions: multi-op chains, generated "
                "topology, tolerance-band sweeps around exact contact/geom_tol/topo_tol, "
                "large coordinates, degenerate or negative inputs, and non-trivial transforms.",
            )
        )
    if strong_fraction < MIN_STRONG_CASE_FRACTION:
        diagnostics.append(
            _diagnostic(
                "error",
                "COMPLEXITY_STRONG_CASES_TOO_FEW",
                path,
                f"only {len(strong)}/{len(scored)} cases reach the strong-case score "
                f"{STRONG_CASE_SCORE}; required fraction is {MIN_STRONG_CASE_FRACTION}.",
                "Strengthen weak cases: each case should combine at least three dimensions, "
                "for example a generated-topology chain plus a tolerance band plus two "
                "measurable oracle families.",
            )
        )
    if not (has_deep_chain or has_generated):
        diagnostics.append(
            _diagnostic(
                "error",
                "COMPLEXITY_GENERATED_TOPOLOGY_MISSING",
                path,
                "no case uses a multi-op chain or generated topology builder.",
                "Use chain steps such as rect_profile -> extrude/thicken, circle_profile -> "
                "sweep_line, line_profile -> revolve, support_sweep, or a pre_boolean body for "
                "at least one target or tool.",
            )
        )
    weak = [item["case_id"] for item in scored if item["score"] <= 1]
    if weak:
        diagnostics.append(
            _diagnostic(
                "warning",
                "COMPLEXITY_WEAK_CASES",
                path,
                f"{len(weak)} cases score <= 1: {', '.join(weak[:8])}.",
                "Keep at most a few simple smoke anchors; make the remaining cases combine "
                "multiple complexity dimensions.",
            )
        )
    return {
        "ok": not any(item["severity"] == "error" for item in diagnostics),
        "kind": "attack_dsl",
        "case_count": len(scored),
        "dimensions_covered": covered,
        "strong_case_fraction": round(strong_fraction, 4),
        "case_scores": scored,
        "diagnostics": diagnostics,
    }


def evaluate_flat_recipe_candidate(recipe: dict[str, Any], path: str) -> dict[str, Any]:
    scored = score_flat_recipe(recipe)
    diagnostics: list[dict[str, Any]] = []
    if scored["score"] < MIN_FLAT_SCORE:
        diagnostics.append(
            _diagnostic(
                "error",
                "COMPLEXITY_FLAT_RECIPE_TOO_SIMPLE",
                path,
                f"flat recipe scores {scored['score']}; the host floor is {MIN_FLAT_SCORE}.",
                "Combine a generated-topology body, a non-default tolerance focus, a "
                "non-trivial transform or large coordinate, and at least one measurable "
                "oracle family instead of a default-placed primitive.",
            )
        )
    if not scored["oracle_families"]:
        diagnostics.append(
            _diagnostic(
                "error",
                "COMPLEXITY_ORACLE_MISSING",
                path,
                "flat recipe has no measurable oracle family.",
                "Add real oracles such as result_bodies bounds, properties, point/face "
                "relations, clash, distance, or plane extrema; API status alone is not a pass.",
            )
        )
    return {
        "ok": not any(item["severity"] == "error" for item in diagnostics),
        "kind": "flat_recipe",
        "case_count": 1,
        "dimensions_covered": sorted(
            dimension for dimension, value in scored["dimensions"].items() if value
        ),
        "strong_case_fraction": 1.0 if scored["score"] >= STRONG_CASE_SCORE else 0.0,
        "case_scores": [scored],
        "diagnostics": diagnostics,
    }


def evaluate_candidate_file(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ComplexityError(f"{path}: candidate root must be an object")
    label = str(path)
    if loaded.get("kind") == "cluster_seed":
        # The fixed gate expands cluster seeds into attack DSL first and scores
        # the expanded file; a raw seed carries no directly scorable cases.
        return {
            "ok": True,
            "kind": "cluster_seed",
            "case_count": 0,
            "dimensions_covered": [],
            "strong_case_fraction": 0.0,
            "case_scores": [],
            "diagnostics": [],
            "skipped": "cluster_seed; the expanded attack DSL is scored instead",
        }
    if loaded.get("kind") == "attack_dsl" and isinstance(loaded.get("dsl"), dict):
        return evaluate_dsl_candidate(loaded["dsl"], label)
    if loaded.get("kind") == "flat_recipe" and isinstance(loaded.get("recipe"), dict):
        return evaluate_flat_recipe_candidate(loaded["recipe"], label)
    if "parameter_clusters" in loaded or "cases" in loaded or "dsl_version" in loaded:
        return evaluate_dsl_candidate(loaded, label)
    if "api" in loaded and "case_id" in loaded:
        return evaluate_flat_recipe_candidate(loaded, label)
    raise ComplexityError(f"{path}: cannot infer candidate kind for complexity scoring")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", help="attack_dsl or flat_recipe candidate JSON path")
    parser.add_argument("--report", default="", help="Optional JSON report path")
    parser.add_argument("--model-diagnostics", default="", help="Optional model-friendly diagnostics JSON path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = evaluate_candidate_file(Path(args.candidate))
    except (ComplexityError, json.JSONDecodeError) as exc:
        result = {
            "ok": False,
            "kind": "",
            "case_count": 0,
            "dimensions_covered": [],
            "strong_case_fraction": 0.0,
            "case_scores": [],
            "diagnostics": [
                _diagnostic("error", "COMPLEXITY_SCORING_FAILED", args.candidate, str(exc), "Return a scorable attack_dsl or flat_recipe candidate.")
            ],
        }
    result["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.model_diagnostics:
        diagnostics_path = Path(args.model_diagnostics)
        diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostics_path.write_text(
            json.dumps(
                {
                    "generated_at": result["generated_at"],
                    "ok": result["ok"],
                    "diagnostic_count": len(result["diagnostics"]),
                    "diagnostics": result["diagnostics"],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    if not result["ok"]:
        for item in result["diagnostics"]:
            if item["severity"] == "error":
                print(f"  - {item['error_code']}: {item['message']}", file=sys.stderr)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
