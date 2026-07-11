from __future__ import annotations

import copy

from harness_capabilities import load_capabilities
from validate_interface_capabilities import validate_registry


def test_checked_in_capability_registry_matches_runner_and_validator() -> None:
    assert validate_registry(load_capabilities()) == []


def test_unknown_oracle_reference_is_rejected() -> None:
    registry = copy.deepcopy(load_capabilities())
    registry["apis"]["api_boolean"]["supported_oracles"].append("invented_oracle")

    errors = validate_registry(registry)

    assert any("unknown id: invented_oracle" in error for error in errors)


def test_runnable_claim_requires_both_validator_and_cpp_dispatch() -> None:
    registry = copy.deepcopy(load_capabilities())
    registry["apis"]["api_not_implemented"] = {
        "preferred_format": "flat_recipe",
        "runner_recipe_api": True,
        "supported_oracles": [],
    }

    errors = validate_registry(
        registry,
        runner_source="",
        implemented_recipe_apis={"api_not_implemented"},
    )

    assert any("C++ runner has no dispatch" in error for error in errors)


def test_static_adapter_table_counts_as_cpp_dispatch() -> None:
    registry = copy.deepcopy(load_capabilities())
    registry["apis"] = {
        "api_table_driven": {
            "preferred_format": "flat_recipe",
            "runner_recipe_api": True,
            "supported_oracles": [],
        }
    }

    errors = validate_registry(
        registry,
        runner_source='static const std::map adapters = {{"api_table_driven", &RunTableDrivenCase}};',
        implemented_recipe_apis={"api_table_driven"},
    )

    assert not any("C++ runner has no dispatch" in error for error in errors)
