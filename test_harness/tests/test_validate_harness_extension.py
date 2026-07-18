from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "test_harness" / "tools"
for entry in (str(REPO_ROOT), str(TOOLS_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from validate_harness_extension import validate_extension_request  # noqa: E402


def _base_extension() -> dict:
    return {
        "kind": "needs_harness_extension",
        "api": "api_srf_srf_int",
        "why_needed": "GeomInt::SrfSrfInt is not a runner recipe api",
        "extension_summary": "add surface pair intersection recipe support",
        "proposed_recipe_fields": {
            "target_surface": "surface spec object",
            "tool_surface": "surface spec object",
        },
        "proposed_artifacts": ["intersection_result.json", "validation.json"],
        "validation_oracle": {"oracle_family": "intersection curve count and topology properties"},
        "minimum_smoke_case": {
            "case_id": "srf_srf_int_smoke_001",
            "api": "api_srf_srf_int",
            "target_surface": {"kind": "plane"},
            "tool_surface": {"kind": "cylinder", "radius": 100.0},
        },
        "patch_plan": [
            {"layer": "schema", "change": "add recipe fields", "files": ["test_harness/interface_capabilities.json"]},
            {"layer": "validator", "change": "reject missing fields", "files": ["test_harness/tools/validate_recipe.py"]},
            {"layer": "normalizer", "change": "normalize safe aliases", "files": ["test_harness/tools/normalize_model_output.py"]},
            {"layer": "runner", "change": "route recipe to fixed support", "files": ["test_harness/src/sggk_case_runner.cpp"]},
            {"layer": "tests", "change": "add smoke coverage", "files": ["test_harness/suites/api_smoke_suite.txt"]},
        ],
    }


def _design_fields() -> dict:
    return {
        "interface_signature": {
            "parameters": [
                {"name": "srf1", "type": "const Surface&", "role": "input"},
                {"name": "srf2", "type": "const Surface&", "role": "input"},
                {"name": "options", "type": "const SrfSrfIntOpts&", "role": "option"},
            ],
            "return_type": "IntSrfSrfRet",
            "return_channels": ["curves", "points"],
        },
        "builder_requirements": [
            {
                "builder_id": "plane_surface",
                "geometry_kind": "plane",
                "parameters": {"origin": "point3d", "normal": "vector3d"},
                "rationale": "analytic intersection baseline",
            }
        ],
        "archetype_match": {"archetype": "binary_geometry_intersection", "fit": "exact", "gaps": []},
        "parameter_cluster_plan": [
            {"cluster_type": "translate_axis", "target_parameter": "tool_surface.translate_x", "rationale": "contact distance", "estimated_cases": 50},
            {"cluster_type": "contact_band", "target_parameter": "tool_surface.translate_z", "rationale": "tolerance band", "estimated_cases": 33},
        ],
        "complexity_plan": {
            "dimensions": ["tolerance_band", "degenerate_or_negative"],
            "degenerate_inputs": ["tangent surfaces", "coincident surfaces"],
            "tolerance_boundaries": ["exact contact +/- geom_tol"],
        },
    }


def _errors(diagnostics: list[dict]) -> list[str]:
    return [item["error_code"] for item in diagnostics if item["severity"] == "error"]


def test_extension_without_design_fields_still_validates() -> None:
    _normalized, diagnostics = validate_extension_request(_base_extension(), "$")
    assert "MISSING_DESIGN_FIELD" not in _errors(diagnostics)


def test_require_design_needs_all_design_fields() -> None:
    _normalized, diagnostics = validate_extension_request(_base_extension(), "$", require_design=True)
    assert "MISSING_DESIGN_FIELD" in _errors(diagnostics)


def test_complete_design_passes_require_design() -> None:
    extension = {**_base_extension(), **_design_fields()}
    _normalized, diagnostics = validate_extension_request(extension, "$", require_design=True)
    assert _errors(diagnostics) == []


def test_design_field_semantics_are_checked() -> None:
    design = _design_fields()
    design["archetype_match"] = {"archetype": "invented_archetype", "fit": "exact", "gaps": []}
    extension = {**_base_extension(), **design}
    _normalized, diagnostics = validate_extension_request(extension, "$", require_design=True)
    assert "INVALID_ARCHETYPE_MATCH" in _errors(diagnostics)


def test_cluster_plan_uses_registered_types_and_cap() -> None:
    design = _design_fields()
    design["parameter_cluster_plan"] = [
        {"cluster_type": "invented_type", "target_parameter": "x", "estimated_cases": 10},
    ]
    extension = {**_base_extension(), **design}
    _normalized, diagnostics = validate_extension_request(extension, "$", require_design=True)
    assert "INVALID_CLUSTER_PLAN" in _errors(diagnostics)

    design["parameter_cluster_plan"] = [
        {"cluster_type": "translate_axis", "target_parameter": "x", "estimated_cases": 51},
    ]
    extension = {**_base_extension(), **design}
    _normalized, diagnostics = validate_extension_request(extension, "$", require_design=True)
    assert "INVALID_CLUSTER_PLAN" in _errors(diagnostics)


def test_interface_signature_roles_and_channels_are_checked() -> None:
    design = _design_fields()
    design["interface_signature"] = {
        "parameters": [{"name": "srf1", "type": "const Surface&", "role": "king"}],
        "return_type": "IntSrfSrfRet",
        "return_channels": ["curves", "magic"],
    }
    extension = {**_base_extension(), **design}
    _normalized, diagnostics = validate_extension_request(extension, "$", require_design=True)
    assert "INVALID_INTERFACE_SIGNATURE" in _errors(diagnostics)
