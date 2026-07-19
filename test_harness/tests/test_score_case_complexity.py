from __future__ import annotations

import json
from pathlib import Path

from test_harness.tools.score_case_complexity import (
    evaluate_candidate_file,
    evaluate_dsl_candidate,
    evaluate_flat_recipe_candidate,
    score_case,
)


def _complex_case() -> dict:
    return {
        "case_id": "complex",
        "target": {
            "chain": [
                {"id": "p", "op": "rect_profile", "length": 100.0, "width": 80.0},
                {"id": "e", "op": "extrude", "height": 60.0},
                {"id": "t", "op": "transform", "translate": [10.0, 0.0, 0.0]},
            ]
        },
        "tool": {"kind": "solid_cylinder", "radius": 10.0, "height": 30.0, "translate_x": "50.0 - topo_tol"},
        "expectations": {
            "result_bodies": {"min": 1},
            "require_finite_properties": True,
            "boolean_volume_relation": True,
        },
    }


def _simple_case() -> dict:
    return {
        "case_id": "simple",
        "target": {"kind": "solid_cylinder", "radius": 10.0, "height": 20.0},
        "tool": {"kind": "solid_wedge", "length": 5.0, "width": 6.0, "height": 7.0},
    }


def test_score_case_detects_complexity_dimensions() -> None:
    scored = score_case(_complex_case(), "complex")
    assert scored["dimensions"]["chain_depth"] == 1
    assert scored["dimensions"]["generated_topology"] == 1
    assert scored["dimensions"]["tolerance_band"] == 1
    assert scored["dimensions"]["oracle_strength"] == 1
    assert scored["dimensions"]["transform_usage"] == 1
    assert scored["score"] >= 5


def test_score_case_flags_simple_pair_as_weak() -> None:
    scored = score_case(_simple_case(), "simple")
    assert scored["score"] == 0
    assert not scored["oracle_families"]


def test_dsl_candidate_floor_rejects_simple_only_candidate() -> None:
    dsl = {
        "defaults": {"api": "api_boolean"},
        "cases": [_simple_case(), {**_simple_case(), "case_id": "simple_2"}],
    }
    result = evaluate_dsl_candidate(dsl, "candidate.json")
    assert result["ok"] is False
    codes = {item["error_code"] for item in result["diagnostics"] if item["severity"] == "error"}
    assert "COMPLEXITY_DIMENSIONS_MISSING" in codes
    assert "COMPLEXITY_STRONG_CASES_TOO_FEW" in codes
    assert "COMPLEXITY_GENERATED_TOPOLOGY_MISSING" in codes


def test_dsl_candidate_floor_accepts_diverse_candidate() -> None:
    dsl = {
        "defaults": {
            "api": "api_boolean",
            "expectations": {"result_bodies": {"min": 1}, "require_finite_properties": True},
        },
        "cases": [
            _complex_case(),
            {
                "case_id": "large",
                "target": {"kind": "solid_sphere", "radius": 50.0, "translate_x": 399800.0},
                "tool": {"kind": "solid_cone", "bottom_radius": 40.0, "top_radius": 10.0, "height": 100.0, "translate_x": "399800.0 + 60.0 - geom_tol"},
                "expectations": {"result_bodies": {"min": 1}, "require_nonnegative_volume": True},
            },
            {
                "case_id": "empty",
                "target": {"kind": "solid_torus", "long_radius": 100.0, "short_radius": 20.0},
                "tool": {"kind": "solid_sphere", "radius": 10.0, "translate_x": 500.0},
                "options": {"boolean_type": "INTERSECTION"},
                "expectations": {"result_bodies": {"min": 0, "max": 0}},
            },
        ],
    }
    result = evaluate_dsl_candidate(dsl, "candidate.json")
    assert result["ok"] is True
    assert set(result["dimensions_covered"]) >= {
        "chain_depth",
        "generated_topology",
        "tolerance_band",
        "oracle_strength",
        "large_coordinate",
        "degenerate_or_negative",
        "transform_usage",
    }


def test_cluster_bases_are_scored_as_cases() -> None:
    dsl = {
        "defaults": {"api": "api_boolean", "expectations": {"result_bodies": {"min": 1}, "require_finite_properties": True}},
        "cluster_bases": {"base_complex": _complex_case()},
        "parameter_clusters": [
            {
                "cluster_id": "c1",
                "type": "translate_axis",
                "bases": ["base_complex"],
                "vary": {"path": "tool.translate_x"},
                "grids": [{"kind": "linspace", "min": 0.0, "max": 10.0, "count": 10}],
            }
        ],
    }
    result = evaluate_dsl_candidate(dsl, "candidate.json")
    assert result["case_scores"][0]["case_id"] == "cluster_base:base_complex"
    assert result["case_scores"][0]["score"] >= 5


def test_flat_recipe_floor() -> None:
    simple = {
        "api": "api_boolean",
        "case_id": "flat_simple",
        "modeling_tol": 0.01,
        "target_kind": "solid_cylinder",
        "target_radius": 10.0,
        "target_height": 20.0,
        "tool_kind": "solid_wedge",
        "tool_length": 5.0,
        "tool_width": 6.0,
        "tool_height": 7.0,
    }
    result = evaluate_flat_recipe_candidate(simple, "recipe.json")
    assert result["ok"] is False

    complex_recipe = {
        **simple,
        "case_id": "flat_complex",
        "target_kind": "revolve_line",
        "target_bottom_radius": 100.0,
        "target_top_radius": 80.0,
        "target_translate_x": 399800.0,
        "tool_translate_x": 399850.0,
        "expectations": {"result_bodies": {"min": 1}, "require_finite_properties": True},
    }
    result = evaluate_flat_recipe_candidate(complex_recipe, "recipe.json")
    assert result["ok"] is True
    assert "generated_topology" in result["dimensions_covered"]
    assert "large_coordinate" in result["dimensions_covered"]


def test_evaluate_candidate_file_detects_kinds(tmp_path: Path) -> None:
    dsl_path = tmp_path / "dsl.json"
    dsl_path.write_text(
        json.dumps({"kind": "attack_dsl", "dsl": {"cases": [_complex_case()]}}),
        encoding="utf-8",
    )
    result = evaluate_candidate_file(dsl_path)
    assert result["kind"] == "attack_dsl"

    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps({"kind": "cluster_seed", "cluster_id": "x"}), encoding="utf-8")
    result = evaluate_candidate_file(seed_path)
    assert result["ok"] is True
    assert result["kind"] == "cluster_seed"
