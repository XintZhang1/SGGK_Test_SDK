from __future__ import annotations

import json
from pathlib import Path
import sys


TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from build_api_test_task import build_task  # noqa: E402
from compile_attack_dsl import (  # noqa: E402
    compile_one_case,
    diagnostic_from_compile_error,
    resolve_constants,
    resolve_key_points,
)
from model_fixed_gate_contracts import api_boolean_fixed_gate_example  # noqa: E402
from validate_recipe import diagnostic_from_validation_error, validate_recipe  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_api_boolean_prompt_explains_exact_fixed_gate_schema() -> None:
    form_path = REPO_ROOT / "test_harness/forms/interface_distillation/01_boolean_primitive_source_guided.json"
    form = json.loads(form_path.read_text(encoding="utf-8-sig"))

    prompt = build_task(form_path, form, [])["prompt"]

    assert "Deterministic api_boolean fixed-gate contract" in prompt
    assert "`point_relation` -> `expectations.point_relations`" in prompt
    assert "`topocheck` -> no expectation field" in prompt
    assert "`expected` (not\n  `expect`)" in prompt
    assert "`side` (not\n  `direction`)" in prompt
    assert '"point_ref": "target_center"' in prompt
    assert '"plane_extreme_checks"' in prompt
    assert "strictly greater than zero" in prompt


def test_prompt_reference_example_passes_compiler_and_recipe_validator() -> None:
    dsl = api_boolean_fixed_gate_example()
    scope = resolve_constants(dsl["constants"])
    key_points = resolve_key_points(dsl["key_points"], scope, "key_points")

    recipes = compile_one_case(
        dsl["cases"][0],
        dsl["defaults"],
        scope,
        Path("oracle_checks_smoke.json"),
        key_points,
    )

    assert recipes
    assert all(validate_recipe(recipe) == [] for recipe in recipes)


def test_model_diagnostics_include_actionable_nested_shapes() -> None:
    compile_diagnostic = diagnostic_from_compile_error(
        "candidate.json",
        "expectations.topocheck: unsupported expectation/oracle key",
    )
    nested_diagnostic = diagnostic_from_validation_error(
        "normalized.json",
        "unknown field: expectations.point_relations[0].points",
    )
    numeric_diagnostic = diagnostic_from_validation_error(
        "normalized.json",
        "tool_height must be > 0",
    )

    assert compile_diagnostic["expected_shape"]["point_relations"][0]["point"] == [
        "x",
        "y",
        "z",
    ]
    assert nested_diagnostic["expected_shape"]["point_relations"][0]["role"] == (
        "result|target|tool"
    )
    assert numeric_diagnostic["expected_shape"] == {"tool_height": "number > 0"}
