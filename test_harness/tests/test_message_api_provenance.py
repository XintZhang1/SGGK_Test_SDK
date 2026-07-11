from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from harness_capabilities import load_capabilities
from validate_provenance_metadata import validate_provenance_object
from run_interface_distillation import pipeline_acceptance_error


def message_api_provenance(*, source_type: str = "intranet_message_api", model_calls: bool = True) -> dict[str, object]:
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


@pytest.mark.parametrize("source_type", ["intranet_message_api", "siliconflow_test_message_api"])
def test_message_api_provenance_records_transport_without_crossing_execution_boundary(
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
    assert {item["code"] for item in record["findings"]} == {"PROVENANCE_MESSAGE_API_BOUNDARY_INCOMPLETE"}


def test_low_level_gateway_provenance_is_not_authoring_accepted(tmp_path: Path) -> None:
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
    assert {item["code"] for item in record["findings"]} == {"PROVENANCE_AUTHORING_ACCEPTANCE_MISSING"}


def test_gateway_repair_provenance_uses_parent_attempt(tmp_path: Path) -> None:
    provenance = message_api_provenance()
    provenance["repair"] = {"is_repair_output": True, "iteration": 1, "parent_attempt": 1}
    record = validate_provenance_object(
        provenance,
        context="model_output",
        path=tmp_path / "output.provenance.json",
        capabilities=load_capabilities(),
    )
    assert record["ok"] is True


def test_post_promotion_driver_rejects_missing_or_hash_mismatched_provenance(
    tmp_path: Path,
) -> None:
    output = tmp_path / "candidate.json"
    candidate = {"kind": "flat_recipe", "recipe": {"case_id": "probe"}}
    output.write_text(json.dumps(candidate), encoding="utf-8")
    digest = hashlib.sha256(
        json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    accepted = {
        "found": True,
        "schema_version": 3,
        "source_type": "intranet_message_api",
        "interface": "openai_compatible_chat_completions_message_content_json",
        "model": "Qwen3.6-35B-A3B",
        "run_id": "message_api_run",
        "prompt_sha256": "1" * 64,
        "message_content_sha256": "2" * 64,
        "authoring_accepted": True,
        "accepted_by": "message_harness_pipeline",
        "requires_fixed_gate": False,
        "fixed_gate_ok": True,
        "fixed_gate_kind": "flat_recipe",
        "fixed_gate_report_path": str(tmp_path / "fixed_gate_report.json"),
        "model_calls": True,
        "direct_api_calls": True,
        "production_flow": "message_api_fixed_gate_repair_atomic_acceptance",
        "candidate_sha256": digest,
    }
    (tmp_path / "fixed_gate_report.json").write_text(
        json.dumps({"ok": True, "kind": "flat_recipe"}),
        encoding="utf-8",
    )

    assert "provenance is missing" in pipeline_acceptance_error(output, {"found": False})
    assert pipeline_acceptance_error(output, accepted) == ""
    accepted["candidate_sha256"] = "0" * 64
    assert "does not match" in pipeline_acceptance_error(output, accepted)


def test_post_promotion_driver_rejects_fixture_style_forged_sidecar(tmp_path: Path) -> None:
    output = tmp_path / "candidate.json"
    output.write_text(json.dumps({"kind": "flat_recipe", "recipe": {}}), encoding="utf-8")
    forged = {
        "found": True,
        "source_type": "intranet_message_api",
        "authoring_accepted": True,
        "accepted_by": "message_harness_pipeline",
        "requires_fixed_gate": False,
        "fixed_gate_ok": True,
    }

    error = pipeline_acceptance_error(output, forged)

    assert "schema_version=3" in error
