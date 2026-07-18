from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_harness.tools.compile_attack_dsl import DslError, compile_dsl_file


def _write_dsl(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "case.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _base_dsl(case: dict) -> dict:
    return {
        "dsl_version": 1,
        "constants": {"topo_tol": 0.01},
        "defaults": {"api": "api_boolean", "boolean_type": "SUBTRACTION", "modeling_tol": "topo_tol"},
        "cases": [case],
    }


def _primitive_case(**extra: object) -> dict:
    case: dict[str, object] = {
        "case_id": "demo",
        "target": {"kind": "solid_cylinder", "radius": 10.0, "height": 20.0},
        "tool": {"kind": "solid_wedge", "length": 5.0, "width": 6.0, "height": 7.0},
    }
    case.update(extra)
    return case


def test_case_level_chain_fails_closed(tmp_path: Path) -> None:
    path = _write_dsl(
        tmp_path,
        _base_dsl(
            _primitive_case(
                chain=[
                    {"id": "step_1", "op": "solid_wedge", "length": 3.0, "width": 4.0, "height": 5.0},
                    {"id": "step_2", "op": "boolean", "base": "target", "tool": "step_1"},
                ]
            )
        ),
    )

    with pytest.raises(DslError, match="unsupported case fields"):
        compile_dsl_file(path)


def test_unknown_option_field_fails_closed(tmp_path: Path) -> None:
    path = _write_dsl(
        tmp_path,
        _base_dsl(_primitive_case(options={"boolean_type": "SUBTRACTION", "repeat": 2})),
    )

    with pytest.raises(DslError, match="unsupported fields"):
        compile_dsl_file(path)


def test_known_case_and_option_fields_still_compile(tmp_path: Path) -> None:
    path = _write_dsl(
        tmp_path,
        _base_dsl(
            _primitive_case(
                hypothesis="baseline",
                options={"boolean_type": "SUBTRACTION", "check_valid": True},
                expectations={"result_bodies": {"min": 1, "max": 1}},
            )
        ),
    )

    recipes = compile_dsl_file(path)

    assert len(recipes) == 1
    assert recipes[0]["case_id"] == "demo"
    assert recipes[0]["api"] == "api_boolean"


# ---------------------------------------------------------------------------
# Parameter clusters
# ---------------------------------------------------------------------------


def _cluster_dsl(clusters: list[dict], bases: dict | None = None) -> dict:
    return {
        "dsl_version": 1,
        "constants": {"topo_tol": 0.01, "geom_tol": 0.00001, "max_model_size": 500000.0},
        "defaults": {"api": "api_boolean", "boolean_type": "SUBTRACTION", "modeling_tol": "topo_tol"},
        "cluster_bases": bases
        if bases is not None
        else {
            "base_a": {
                "target": {"kind": "solid_cylinder", "radius": 10.0, "height": 20.0},
                "tool": {"kind": "solid_wedge", "length": 5.0, "width": 6.0, "height": 7.0, "translate": [0.0, 0.0, 0.0]},
                "expectations": {"result_bodies": {"min": 1}},
            }
        },
        "parameter_clusters": clusters,
    }


def test_translate_axis_cluster_expands_deterministically(tmp_path: Path) -> None:
    payload = _cluster_dsl(
        [
            {
                "cluster_id": "tx",
                "type": "translate_axis",
                "bases": ["base_a"],
                "vary": {"path": "tool.translate_x"},
                "grids": [{"kind": "linspace", "min": 1.0, "max": 5.0, "count": 5}],
            }
        ]
    )
    path = _write_dsl(tmp_path, payload)

    first = compile_dsl_file(path)
    second = compile_dsl_file(path)

    assert first == second
    assert len(first) == 5
    assert [recipe["tool_translate_x"] for recipe in first] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert first[0]["case_id"] == "base_a__tx_i01"
    assert first[0]["dsl_cluster_id"] == "tx"
    assert first[0]["dsl_cluster_type"] == "translate_axis"
    assert first[0]["dsl_cluster_base"] == "base_a"


def test_contact_band_cluster_hits_exact_and_tolerance_sides(tmp_path: Path) -> None:
    payload = _cluster_dsl(
        [
            {
                "cluster_id": "band",
                "type": "contact_band",
                "bases": ["base_a"],
                "vary": {
                    "path": "tool.translate_x",
                    "center": 100.0,
                    "bands": ["exact", "topo_tol"],
                    "steps_per_band": 2,
                },
            }
        ]
    )
    path = _write_dsl(tmp_path, payload)

    recipes = compile_dsl_file(path)

    values = [recipe["tool_translate_x"] for recipe in recipes]
    assert values == [100.0, 99.995, 100.005, 99.99, 100.01]
    assert [recipe["case_id"] for recipe in recipes] == [
        "base_a__band_exact",
        "base_a__band_topo_dn01",
        "base_a__band_topo_up01",
        "base_a__band_topo_dn02",
        "base_a__band_topo_up02",
    ]


def test_cluster_cap_of_50_cases_is_enforced(tmp_path: Path) -> None:
    payload = _cluster_dsl(
        [
            {
                "cluster_id": "too_wide",
                "type": "translate_axis",
                "bases": ["base_a"],
                "vary": {"path": "tool.translate_x"},
                "grids": [{"kind": "linspace", "min": 0.0, "max": 100.0, "count": 51}],
            }
        ]
    )
    path = _write_dsl(tmp_path, payload)

    with pytest.raises(DslError, match="per-cluster cap is 50"):
        compile_dsl_file(path)


def test_cluster_requires_known_base_and_valid_path(tmp_path: Path) -> None:
    unknown_base = _write_dsl(
        tmp_path,
        _cluster_dsl(
            [
                {
                    "cluster_id": "c1",
                    "type": "translate_axis",
                    "bases": ["missing_base"],
                    "vary": {"path": "tool.translate_x"},
                    "grids": [{"kind": "linspace", "min": 0.0, "max": 1.0, "count": 2}],
                }
            ]
        ),
    )
    with pytest.raises(DslError, match="unknown cluster base"):
        compile_dsl_file(unknown_base)

    bad_path = _write_dsl(
        tmp_path,
        _cluster_dsl(
            [
                {
                    "cluster_id": "c2",
                    "type": "translate_axis",
                    "bases": ["base_a"],
                    "vary": {"path": "tool.chain.9.translate_x"},
                    "grids": [{"kind": "linspace", "min": 0.0, "max": 1.0, "count": 2}],
                }
            ]
        ),
    )
    with pytest.raises(DslError, match="does not resolve in the cluster base"):
        compile_dsl_file(bad_path)


def test_cluster_base_must_not_carry_its_own_sweeps(tmp_path: Path) -> None:
    payload = _cluster_dsl(
        [
            {
                "cluster_id": "c1",
                "type": "translate_axis",
                "bases": ["base_a"],
                "vary": {"path": "tool.translate_x"},
                "grids": [{"kind": "linspace", "min": 0.0, "max": 1.0, "count": 2}],
            }
        ]
    )
    payload["cluster_bases"]["base_a"]["sweeps"] = [{"path": "tool.translate_x", "values": [1.0]}]
    path = _write_dsl(tmp_path, payload)

    with pytest.raises(DslError, match="only variation source"):
        compile_dsl_file(path)


def test_all_cluster_types_expand(tmp_path: Path) -> None:
    clusters = [
        {"cluster_id": "c_translate", "type": "translate_axis", "bases": ["base_a"], "vary": {"path": "tool.translate_x"}, "grids": [{"kind": "linspace", "min": 0.0, "max": 4.0, "count": 5}]},
        {"cluster_id": "c_line", "type": "translate_line", "bases": ["base_a"], "vary": {"path": "tool.translate", "direction": [1.0, 1.0, 0.0], "center": [0.0, 0.0, 0.0]}, "grids": [{"kind": "linspace", "min": 0.0, "max": 2.0, "count": 3}]},
        {"cluster_id": "c_scale", "type": "scale_uniform", "bases": ["base_a"], "vary": {"path": "tool.scale"}, "grids": [{"kind": "linspace", "min": 0.5, "max": 2.0, "count": 4}]},
        {"cluster_id": "c_size", "type": "size_dimension", "bases": ["base_a"], "vary": {"path": "tool.radius"}, "grids": [{"kind": "geomspace", "min": 1.0, "max": 8.0, "count": 4}]},
        {"cluster_id": "c_band", "type": "contact_band", "bases": ["base_a"], "vary": {"path": "tool.translate_x", "center": 10.0, "bands": ["exact", "geom_tol"], "steps_per_band": 2}},
        {"cluster_id": "c_tol", "type": "tolerance_sweep", "bases": ["base_a"], "vary": {"path": "options.modeling_tol"}, "grids": [{"kind": "geomspace", "min": 0.00001, "max": 0.01, "count": 4}]},
        {"cluster_id": "c_angle", "type": "angle_sweep", "bases": ["base_a"], "vary": {"path": "tool.angle"}, "grids": [{"kind": "linspace", "min": 1.0, "max": 6.0, "count": 6}]},
        {"cluster_id": "c_large", "type": "large_coordinate_shift", "bases": ["base_a"], "vary": {"path": "tool.translate_x", "fractions": [0.5, 0.9], "sign": "both"}},
        {"cluster_id": "c_bool", "type": "boolean_type_cycle", "bases": ["base_a"], "vary": {"path": "options.boolean_type", "values": [{"value": "SUBTRACTION", "suffix": "sub"}, {"value": "UNION", "suffix": "uni", "set": {"expectations.result_bodies.max": 1}}]}},
        {"cluster_id": "c_toggle", "type": "option_toggle", "bases": ["base_a"], "vary": {"path": "options.check_valid"}},
        {"cluster_id": "c_mirror", "type": "mirror_sign", "bases": ["base_a"], "vary": {"path": "tool.translate_y", "magnitudes": [5.0, 10.0]}},
        {"cluster_id": "c_jitter", "type": "seeded_jitter", "bases": ["base_a"], "vary": {"path": "tool.translate_z", "min": -1.0, "max": 1.0, "count": 8, "seed": 7}},
        {"cluster_id": "c_uv", "type": "uv_domain", "bases": ["base_a"], "vary": {"paths": ["tool.u_fraction", "tool.v_fraction"]}, "grids": [{"kind": "linspace", "min": 0.0, "max": 1.0, "count": 5}]},
        {"cluster_id": "c_enum", "type": "enum_cycle", "bases": ["base_a"], "vary": {"path": "options.boolean_type", "values": ["SUBTRACTION", "INTERSECTION", "UNION"]}},
    ]
    path = _write_dsl(tmp_path, _cluster_dsl(clusters))

    recipes = compile_dsl_file(path)

    by_type: dict[str, int] = {}
    for recipe in recipes:
        by_type[recipe["dsl_cluster_type"]] = by_type.get(recipe["dsl_cluster_type"], 0) + 1
    assert by_type == {
        "translate_axis": 5,
        "translate_line": 3,
        "scale_uniform": 4,
        "size_dimension": 4,
        "contact_band": 5,
        "tolerance_sweep": 4,
        "angle_sweep": 6,
        "large_coordinate_shift": 4,
        "boolean_type_cycle": 2,
        "option_toggle": 2,
        "mirror_sign": 4,
        "seeded_jitter": 8,
        "uv_domain": 5,
        "enum_cycle": 3,
    }
    toggle = [recipe for recipe in recipes if recipe["dsl_cluster_id"] == "c_toggle"]
    assert [recipe["check_valid"] for recipe in toggle] == [False, True]
    union = [recipe for recipe in recipes if recipe["case_id"] == "base_a__c_bool_uni"][0]
    assert union["boolean_type"] == "UNION"
    assert union["expectations"]["result_bodies"]["max"] == 1
    mirror = [recipe["tool_translate_y"] for recipe in recipes if recipe["dsl_cluster_id"] == "c_mirror"]
    assert mirror == [5.0, -5.0, 10.0, -10.0]
    jitter_first = [recipe["tool_translate_z"] for recipe in recipes if recipe["dsl_cluster_id"] == "c_jitter"]
    assert len(set(jitter_first)) == 8


def test_cluster_check_mode_samples_and_reports_theoretical_count(tmp_path: Path) -> None:
    from test_harness.tools.compile_attack_dsl import check_dsl_file

    payload = _cluster_dsl(
        [
            {
                "cluster_id": "wide",
                "type": "translate_axis",
                "bases": ["base_a"],
                "vary": {"path": "tool.translate_x"},
                "grids": [{"kind": "linspace", "min": 0.0, "max": 49.0, "count": 50}],
            }
        ]
    )
    path = _write_dsl(tmp_path, payload)

    record, valid_recipes = check_dsl_file(path, True, cluster_sample=True)

    assert record["ok"] is True
    assert record["cluster_recipe_count"] == 50
    assert len(valid_recipes) == 4  # first two + middle + last
    sampled_ids = [recipe["case_id"] for recipe in valid_recipes]
    assert sampled_ids[0] == "base_a__wide_i01"
    assert sampled_ids[-1] == "base_a__wide_i50"


def test_cluster_only_dsl_needs_no_cases_array(tmp_path: Path) -> None:
    payload = _cluster_dsl(
        [
            {
                "cluster_id": "c1",
                "type": "option_toggle",
                "bases": ["base_a"],
                "vary": {"path": "options.topo_track"},
            }
        ]
    )
    path = _write_dsl(tmp_path, payload)

    recipes = compile_dsl_file(path)

    assert len(recipes) == 2
    assert {recipe["topo_track"] for recipe in recipes} == {False, True}
