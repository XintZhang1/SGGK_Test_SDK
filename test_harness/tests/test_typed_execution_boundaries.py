from __future__ import annotations

from pathlib import Path

import pytest
from campaign_profiles import CampaignRequestError, resolve_campaign_argv, validate_campaign_request
from normalize_model_output import normalize_loaded

ALLOWED_CAMPAIGNS = {"abc_boolean_mass_recut": {}}


def campaign_request() -> dict[str, object]:
    return {
        "kind": "campaign_request",
        "profile_id": "abc_boolean_mass_recut",
        "args": {"target_cases": 1000, "preset": "smoke", "shard_count": 4, "shard_index": 1},
        "notes": [],
        "expected_artifacts": [],
    }


def test_campaign_request_resolves_only_fixed_argv() -> None:
    normalized, errors = validate_campaign_request(campaign_request(), ALLOWED_CAMPAIGNS)
    assert errors == []
    assert normalized is not None

    argv = resolve_campaign_argv(
        normalized,
        allowed_profiles=ALLOWED_CAMPAIGNS,
        bindings={
            "runner": "build/test_harness/Release/sggk_case_runner.exe",
            "dataset": "artifacts/abc_dataset",
            "out": "artifacts/campaign_run",
        },
    )

    assert isinstance(argv, list)
    assert argv[1] == "test_harness/tools/run_abc_boolean_mass_recut.py"
    assert "--target-cases" in argv
    assert "1000" in argv


@pytest.mark.parametrize(
    "extra",
    [
        {"command": "python arbitrary.py"},
        {"cwd": "artifacts"},
        {"unexpected": "ignored"},
    ],
)
def test_campaign_request_rejects_executable_and_unknown_fields(extra: dict[str, str]) -> None:
    request = {**campaign_request(), **extra}
    normalized, errors = validate_campaign_request(request, ALLOWED_CAMPAIGNS)
    assert normalized is None
    assert errors


def test_campaign_binding_rejects_shell_metacharacters() -> None:
    with pytest.raises(CampaignRequestError):
        resolve_campaign_argv(
            campaign_request(),
            allowed_profiles=ALLOWED_CAMPAIGNS,
            bindings={
                "runner": "build/test_harness/Release/sggk_case_runner.exe;whoami",
                "dataset": "artifacts/abc_dataset",
                "out": "artifacts/campaign_run",
            },
        )


def test_cluster_seed_geometry_tool_is_not_treated_as_executable(tmp_path: Path) -> None:
    report = normalize_loaded(
        {
            "kind": "cluster_seed",
            "cluster_id": "tool_geometry_seed",
            "target": {"kind": "solid_sphere", "radius": 10.0},
            "tool": {"kind": "solid_cylinder", "radius": 2.0, "height": 20.0},
        },
        "tool_geometry_seed",
        tmp_path,
        "",
    )
    assert report["ok"] is True
