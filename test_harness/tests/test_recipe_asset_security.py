from __future__ import annotations

import json

from compile_attack_dsl import check_dsl_file
from normalize_model_output import normalize_loaded
from validate_recipe import validate_recipe


def source_recipe(source_file: str) -> dict[str, object]:
    return {
        "case_id": "asset_security",
        "api": "step_import",
        "source_file": source_file,
    }


def test_model_source_assets_reject_absolute_unc_and_traversal_paths() -> None:
    unsafe = (
        r"C:\Windows\win.ini",
        r"\\server\share\probe.step",
        "/etc/passwd",
        "artifacts/../README.md",
    )

    for value in unsafe:
        errors = validate_recipe(source_recipe(value), asset_policy="model")
        assert any(error.startswith("unsafe source_file:") for error in errors), value


def test_model_source_asset_must_use_a_fixed_asset_root() -> None:
    errors = validate_recipe(source_recipe("docs/untrusted.step"), asset_policy="model")

    assert any("must stay under artifacts/" in error for error in errors)


def test_safe_relative_asset_is_allowed_and_optional_existence_check_is_strict() -> None:
    recipe = source_recipe("artifacts/materialized_inputs/example.step")

    assert validate_recipe(recipe, check_assets=False, asset_policy="model") == []
    assert validate_recipe(recipe, check_assets=True, asset_policy="model") == [
        "source_file not found: artifacts/materialized_inputs/example.step"
    ]


def test_trusted_corpus_recipe_keeps_operator_bound_absolute_paths() -> None:
    assert validate_recipe(source_recipe(r"C:\trusted-corpus\part.step")) == []


def test_loaded_body_asset_uses_the_same_boundary() -> None:
    recipe = {
        "case_id": "loaded_body_security",
        "api": "api_boolean",
        "boolean_type": "SUBTRACTION",
        "modeling_tol": 0.001,
        "target_kind": "loaded_sgt",
        "target_source_file": r"\\server\share\target.sgt",
        "tool_kind": "solid_sphere",
        "tool_radius": 1.0,
    }

    errors = validate_recipe(recipe, asset_policy="model")

    assert any(error.startswith("unsafe target_source_file:") for error in errors)


def test_model_normalizer_rejects_nested_dsl_unc_asset(tmp_path) -> None:
    candidate = {
        "kind": "attack_dsl",
        "dsl": {
            "schema_version": 1,
            "defaults": {"api": "api_boolean", "boolean_type": "SUBTRACTION"},
            "cases": [
                {
                    "case_id": "unsafe_dsl_asset",
                    "target": {
                        "chain": [
                            {
                                "id": "load_target",
                                "op": "load_sgt",
                                "source_file": r"\\server\share\target.sgt",
                            }
                        ]
                    },
                    "tool": {"kind": "solid_sphere", "radius": 1.0},
                }
            ],
        },
    }

    report = normalize_loaded(candidate, "unsafe_dsl", tmp_path, "")

    assert report["ok"] is False
    assert any(item["error_code"] == "UNSAFE_SOURCE_FILE" for item in report["diagnostics"])


def test_dsl_compiler_model_policy_rejects_unc_loaded_sgt(tmp_path) -> None:
    dsl_path = tmp_path / "unsafe_asset_dsl.json"
    dsl_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "defaults": {
                    "api": "api_boolean",
                    "boolean_type": "SUBTRACTION",
                    "modeling_tol": 0.001,
                },
                "cases": [
                    {
                        "case_id": "unsafe_dsl_asset",
                        "target": {
                            "kind": "loaded_sgt",
                            "source_file": r"\\server\share\target.sgt",
                            "body_index": 0,
                        },
                        "tool": {"kind": "solid_sphere", "radius": 1.0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    record, recipes = check_dsl_file(dsl_path, True, asset_policy="model")

    assert record["ok"] is False
    assert record["validation_failure_count"] == 1
    assert any(error.startswith("unsafe target_source_file:") for error in record["recipes"][0]["errors"])
    assert recipes == []
