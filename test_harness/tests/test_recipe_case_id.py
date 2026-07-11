from __future__ import annotations

import copy

import pytest

from validate_recipe import validate_recipe


BASE_RECIPE = {
    "api": "api_boolean",
    "case_id": "safe_case-001",
    "boolean_type": "SUBTRACTION",
    "modeling_tol": 1e-5,
    "target_kind": "solid_cylinder",
    "target_radius": 10.0,
    "target_height": 20.0,
    "tool_kind": "solid_cylinder",
    "tool_radius": 4.0,
    "tool_height": 20.0,
}


def errors_for(case_id: str) -> list[str]:
    recipe = copy.deepcopy(BASE_RECIPE)
    recipe["case_id"] = case_id
    return validate_recipe(recipe)


def test_safe_case_id_is_accepted() -> None:
    assert errors_for("safe_case-001") == []


@pytest.mark.parametrize(
    "case_id",
    [
        "../escape",
        "..\\escape",
        "absolute/path",
        "contains space",
        ".hidden",
        "x" * 129,
        "",
    ],
)
def test_unsafe_case_id_is_rejected(case_id: str) -> None:
    assert any("case_id must match" in error for error in errors_for(case_id))
