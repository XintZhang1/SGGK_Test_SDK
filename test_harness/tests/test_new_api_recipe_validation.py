from __future__ import annotations

from pathlib import Path
import json

import pytest

from validate_recipe import validate_file, validate_recipe


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "relative_path",
    [
        "test_harness/recipes/boolean_split_plane_smoke.json",
        "test_harness/recipes/boolean_slice_smoke.json",
        "test_harness/recipes/offset2d_line_smoke.json",
        "test_harness/recipes/offset2d_cannot_connect_smoke.json",
        "test_harness/recipes/offset2d_crv_degenerate_smoke.json",
        "test_harness/interface_example_packs/api_offset_body.example_recipe.json",
        "test_harness/recipes/topology_section_spheres_smoke.json",
    ],
)
def test_new_api_positive_recipes_pass_validation(relative_path: str) -> None:
    assert validate_file(REPO_ROOT / relative_path) == []


@pytest.mark.parametrize(
    ("relative_path", "expected_fragments"),
    [
        (
            "test_harness/interface_example_packs/api_boolean_split.invalid_recipe.json",
            ("missing required key: tool_width", "split_total_bodies.max must be >= min"),
        ),
        (
            "test_harness/interface_example_packs/api_boolean_slice.invalid_recipe.json",
            ("boolean_type must be one of", "slice_result_bodies must be >= 0"),
        ),
        (
            "test_harness/interface_example_packs/api_offset2d.invalid_recipe.json",
            (
                "exactly one of offset2d_distance or offset2d_distances is required",
                "line start and end must differ",
                "offset2d_status must be one of",
                "offset2d_result_paths.max must be >= min",
            ),
        ),
        (
            "test_harness/interface_example_packs/api_topology_section.invalid_recipe.json",
            (
                "unknown field: tool_raduis",
                "unknown field: expectations.topology_section_edegs",
                "missing required key: tool_radius",
            ),
        ),
    ],
)
def test_new_api_negative_fixtures_are_rejected(
    relative_path: str,
    expected_fragments: tuple[str, ...],
) -> None:
    errors = validate_file(REPO_ROOT / relative_path)
    assert errors
    for fragment in expected_fragments:
        assert any(fragment in error for error in errors), errors


@pytest.mark.parametrize(
    ("relative_path", "typo_field"),
    [
        ("test_harness/recipes/boolean_split_plane_smoke.json", "split_strcit_split"),
        ("test_harness/recipes/boolean_slice_smoke.json", "target_raduis"),
        ("test_harness/recipes/offset2d_line_smoke.json", "offset2d_distnace"),
        ("test_harness/interface_example_packs/api_offset_body.example_recipe.json", "offset_distnace"),
        ("test_harness/recipes/topology_section_spheres_smoke.json", "topology_section_expectationss"),
    ],
)
def test_api_specific_allowlists_reject_model_field_typos(relative_path: str, typo_field: str) -> None:
    recipe = json.loads((REPO_ROOT / relative_path).read_text(encoding="utf-8-sig"))
    recipe[typo_field] = True

    errors = validate_recipe(recipe)

    assert f"unknown field: {typo_field}" in errors


def test_common_provenance_fields_remain_allowed() -> None:
    recipe = json.loads(
        (REPO_ROOT / "test_harness/recipes/topology_section_spheres_smoke.json").read_text(encoding="utf-8-sig")
    )
    recipe.update(
        {
            "dsl_source": "staged/model-message.json",
            "dsl_case_id": "topology_section_spheres_smoke",
            "dsl_variant": "nominal",
            "hypothesis": "Two overlapping spheres return one circular section edge.",
            "source_ref": "Boolean/API.h:api_topology_section",
            "source_task_id": "message-api-run-001",
        }
    )

    assert validate_recipe(recipe) == []
