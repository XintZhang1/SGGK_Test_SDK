from __future__ import annotations

from pathlib import Path

import pytest

from harness_capabilities import load_capabilities
from validate_provenance_metadata import validate_provenance_object


def message_api_provenance(
    *,
    source_type: str = "intranet_message_api",
    model_calls: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "request_id": "message_api_case",
        "source_type": source_type,
        "output_path": "artifacts/model_outputs/message_api_case.json",
        "acceptance": {
            "authoring_accepted": True,
            "requires_fixed_gate": False,
            "accepted_by": "message_harness_pipeline",
        },
        "fixed_gate": {"ok": True, "kind": "flat_recipe"},
        "boundary": {
            "model_calls": model_calls,
            "direct_api_calls": model_calls,
            "runs_sdk": False,
            "executes_commands": False,
            "applies_patches": False,
            "commits_changes": False,
            "wired_into_harness": False,
        },
    }


@pytest.mark.parametrize("source_type", ["intranet_message_api"])
def test_endpoint_provenance_uses_the_same_non_execution_boundary(
    tmp_path: Path,
    source_type: str,
) -> None:
    record = validate_provenance_object(
        message_api_provenance(source_type=source_type),
        context="model_output",
        path=tmp_path / "output.provenance.json",
        capabilities=load_capabilities(),
    )

    assert record["ok"] is True
    assert not [item for item in record["findings"] if item["severity"] == "blocker"]


def test_message_api_provenance_requires_truthful_call_flags(tmp_path: Path) -> None:
    record = validate_provenance_object(
        message_api_provenance(model_calls=False),
        context="model_output",
        path=tmp_path / "output.provenance.json",
        capabilities=load_capabilities(),
    )

    assert record["ok"] is False
    assert {item["code"] for item in record["findings"]} == {
        "PROVENANCE_MESSAGE_API_BOUNDARY_INCOMPLETE"
    }


def test_unaccepted_gateway_provenance_is_rejected(tmp_path: Path) -> None:
    provenance = message_api_provenance()
    provenance.pop("acceptance")
    provenance.pop("fixed_gate")

    record = validate_provenance_object(
        provenance,
        context="model_output",
        path=tmp_path / "output.provenance.json",
        capabilities=load_capabilities(),
    )

    assert record["ok"] is False
    assert {item["code"] for item in record["findings"]} == {
        "PROVENANCE_AUTHORING_ACCEPTANCE_MISSING"
    }


def test_repair_provenance_binds_parent_attempt(tmp_path: Path) -> None:
    provenance = message_api_provenance()
    provenance["repair"] = {"is_repair_output": True, "iteration": 1, "parent_attempt": 1}

    record = validate_provenance_object(
        provenance,
        context="model_output",
        path=tmp_path / "output.provenance.json",
        capabilities=load_capabilities(),
    )

    assert record["ok"] is True
