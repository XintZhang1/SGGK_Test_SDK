from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from test_harness.authoring_gateway.client import HttpResponse, OpenAICompatibleMessageClient
from test_harness.authoring_gateway.config import PROFILE_SPECS, GatewayConfig
from test_harness.authoring_gateway.contracts import validate_candidate
from test_harness.authoring_gateway.gateway import TaskSpec
from test_harness.tools.api_adaptation_contract import (
    build_adaptation_contract,
    sha256_json,
    validate_adaptation_contract,
)
from test_harness.tools.materialize_api_plugin_candidate import materialize, validate_candidate as validate_plugin_candidate
from test_harness.tools.run_message_harness_pipeline import MessageHarnessPipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "test_harness/api_plugin_candidates/api_combine_bodies.example.json"
INTAKE_VALUE: dict[str, object] = {
    "schema_version": 1,
    "request_id": "adapt_api_combine_bodies",
    "api": "api_combine_bodies",
    "sdk_header": "ModelingBase/API.h",
    "sdk_modules": ["ModelingBase", "Topology"],
    "function_signature": "BodyPtr api_combine_bodies(const BodyList& bodies, bool clone = true)",
    "adapter_archetype": "body_list_to_body",
    "behavior": "Combine target and tool BodyPtr inputs into one BodyPtr.",
    "input_roles": ["target", "tool"],
    "result_roles": ["result"],
    "required_oracles": ["result_bodies", "properties", "topocheck"],
    "smoke_guidance": "Use two separated solid spheres and require one valid result body.",
    "topotrack": {
        "mode": "unavailable",
        "reason": "The API returns BodyPtr and exposes no ModelingRet TopoTrack channel.",
    },
}


def candidate() -> dict[str, object]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def adaptation_metadata() -> dict[str, object]:
    intake = copy.deepcopy(INTAKE_VALUE)
    contract = build_adaptation_contract(intake)
    return {
        "target_api": intake["api"],
        "adapter_archetype": intake["adapter_archetype"],
        "intake_sha256": contract["intake_sha256"],
        "adaptation_contract": contract,
        "adaptation_contract_sha256": sha256_json(contract),
    }


def test_spec_only_plugin_candidate_materializes_fixed_cpp_without_model_code(tmp_path: Path) -> None:
    value = candidate()

    contract = validate_candidate(
        value,
        {"type": "json_object", "allowed_kinds": ["api_plugin_candidate"]},
    )
    report = materialize(value, tmp_path)

    assert contract.ok
    assert report["ok"]
    plugin = Path(report["materialized_plugin"])
    adapter = (plugin / "adapter.inc").read_text(encoding="utf-8")
    assert "sggk::api_combine_bodies(inputs, clone)" in adapter
    assert "system(" not in adapter
    assert "#include" not in adapter
    assert report["plugin"]["archetype"] == "body_list_to_body"


def test_model_execution_fields_are_rejected_recursively() -> None:
    value = candidate()
    value["adapter_spec"]["command"] = "cmake --build ."

    errors = validate_plugin_candidate(value)

    assert any("execution field is forbidden" in error for error in errors)
    assert any("Additional properties" in error for error in errors)


def test_remote_recipe_schema_ref_is_rejected() -> None:
    value = copy.deepcopy(candidate())
    value["recipe_schema"]["properties"]["expectations"] = {
        "$ref": "https://untrusted.invalid/expectations.json"
    }

    errors = validate_plugin_candidate(value)

    assert any("only local fragment references" in error for error in errors)


def test_sdk_header_cannot_escape_the_sdk_include_namespace() -> None:
    value = candidate()
    value["adapter_spec"]["sdk_header"] = "../../outside.h"

    errors = validate_plugin_candidate(value)

    assert any("not a safe SDK include" in error for error in errors)


def test_host_adaptation_contract_cannot_weaken_fixed_archetype_oracles() -> None:
    intake = copy.deepcopy(INTAKE_VALUE)
    intake["required_oracles"] = ["result_bodies"]
    contract = build_adaptation_contract(intake)

    errors = validate_adaptation_contract(contract, sha256_json(contract))

    assert any("is missing fixed host oracles" in error for error in errors)


def test_positive_and_negative_examples_must_discriminate() -> None:
    value = candidate()
    value["negative_recipe"] = copy.deepcopy(value["smoke_recipe"])

    errors = validate_plugin_candidate(value)

    assert any("plus exactly one added unknown field" in error for error in errors)


def test_body_list_to_body_smoke_oracles_cannot_be_weakened() -> None:
    value = candidate()
    expectations = value["smoke_recipe"]["expectations"]
    expectations["result_bodies"]["max"] = 2
    expectations["require_property_calculations"] = False
    expectations["require_finite_properties"] = False
    expectations["require_nonnegative_volume"] = False
    value["capability"]["supported_oracles"] = ["result_bodies"]

    errors = validate_plugin_candidate(value)

    assert any("must require exactly one body" in error for error in errors)
    assert any("require_property_calculations must be true" in error for error in errors)
    assert any("require_finite_properties must be true" in error for error in errors)
    assert any("require_nonnegative_volume must be true" in error for error in errors)
    assert any("missing fixed body_list_to_body host oracles" in error for error in errors)


def test_malformed_oracle_metadata_is_rejected_without_crashing() -> None:
    value = candidate()
    value["capability"]["supported_oracles"] = [{"not": "a string"}]

    errors = validate_plugin_candidate(value)

    assert any("missing fixed body_list_to_body host oracles" in error for error in errors)


def test_body_list_to_body_recipe_schema_must_lock_oracles() -> None:
    value = candidate()
    expectation_schema = value["recipe_schema"]["properties"]["expectations"]
    expectation_schema["required"].remove("require_nonnegative_volume")
    expectation_schema["properties"]["require_nonnegative_volume"] = {"type": "boolean"}
    expectation_schema["properties"]["result_bodies"]["properties"]["max"] = {
        "type": "integer"
    }

    errors = validate_plugin_candidate(value)

    assert any("must contain all fixed" in error for error in errors)
    assert any("strictly require min=1 and max=1" in error for error in errors)
    assert any("require_nonnegative_volume must use const=true" in error for error in errors)


def test_negative_recipe_must_be_one_unknown_field_addition_only() -> None:
    value = candidate()
    value["negative_recipe"]["case_id"] = "changed_case_id"

    changed_errors = validate_plugin_candidate(value)

    assert any("plus exactly one added unknown field" in error for error in changed_errors)

    value = candidate()
    value["negative_recipe"]["another_unknown"] = True

    multiple_errors = validate_plugin_candidate(value)

    assert any("plus exactly one added unknown field" in error for error in multiple_errors)

    value = candidate()
    value["negative_recipe"].pop("target_radius")

    removed_errors = validate_plugin_candidate(value)

    assert any("plus exactly one added unknown field" in error for error in removed_errors)


def test_negative_recipe_must_fail_only_additional_properties() -> None:
    value = candidate()
    value["recipe_schema"]["properties"]["target_raduis"] = {"type": "number"}

    errors = validate_plugin_candidate(value)

    assert any("must fail only the matching additionalProperties" in error for error in errors)


class OneResponseTransport:
    def __init__(self, value: dict[str, object]) -> None:
        body = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(value)},
                }
            ]
        }
        self.response = HttpResponse(
            200,
            {"content-type": "application/json"},
            json.dumps(body).encode(),
        )

    def post(self, **_kwargs: Any) -> HttpResponse:
        return self.response


def test_message_pipeline_fixed_gate_materializes_plugin_candidate(tmp_path: Path) -> None:
    config = GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="zai-org/GLM-5.2",
        api_key="plugin-test-key",
        request_timeout_seconds=1,
        max_retries=0,
    )
    harness = MessageHarnessPipeline(
        config,
        repo_root=tmp_path,
        tool_repo_root=REPO_ROOT,
        client=OpenAICompatibleMessageClient(config, transport=OneResponseTransport(candidate())),
        gate_timeout_seconds=30,
    )
    spec = TaskSpec(
        task_id="adapt_new_api",
        task_type="api_adaptation",
        prompt="Return one fixed-archetype API plugin candidate.",
        expected_output_path=tmp_path / "artifacts/accepted/api_combine_bodies.json",
        output_contract={
            "type": "json_object",
            "allowed_kinds": ["api_plugin_candidate"],
        },
        metadata=adaptation_metadata(),
    )

    result = harness.run_task(
        spec,
        run_id="plugin_gate",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="fixed_gate_only",
    )

    assert result.ok
    assert result.authoring_accepted
    assert result.candidates[0]["fixed_gate"]["kind"] == "api_plugin_candidate"
    materialized = result.candidates[0]["fixed_gate"]["artifacts"]["materialized_plugin"]
    assert (tmp_path / materialized / "adapter.inc").is_file()
    provenance = json.loads((tmp_path / result.provenance_path).read_text(encoding="utf-8"))
    assert provenance["api_adaptation"]["identity_bound"] is True
    assert provenance["api_adaptation"]["target_api"] == "api_combine_bodies"


def test_pipeline_rejects_valid_plugin_for_a_different_trusted_api(tmp_path: Path) -> None:
    wrong = copy.deepcopy(candidate())
    wrong["api"] = "api_wrong_but_legal"
    wrong["adapter_spec"]["function_name"] = "api_wrong_but_legal"
    wrong["recipe_schema"]["properties"]["api"]["const"] = "api_wrong_but_legal"
    wrong["smoke_recipe"]["api"] = "api_wrong_but_legal"
    wrong["negative_recipe"]["api"] = "api_wrong_but_legal"
    config = GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="zai-org/GLM-5.2",
        api_key="plugin-test-key",
        request_timeout_seconds=1,
        max_retries=0,
    )
    harness = MessageHarnessPipeline(
        config,
        repo_root=tmp_path,
        tool_repo_root=REPO_ROOT,
        client=OpenAICompatibleMessageClient(config, transport=OneResponseTransport(wrong)),
        gate_timeout_seconds=30,
    )
    spec = TaskSpec(
        task_id="adapt_requested_api",
        task_type="api_adaptation",
        prompt="Return one fixed-archetype API plugin candidate.",
        expected_output_path=tmp_path / "artifacts/accepted/wrong.json",
        output_contract={"type": "json_object", "allowed_kinds": ["api_plugin_candidate"]},
        metadata=adaptation_metadata(),
    )

    result = harness.run_task(
        spec,
        run_id="wrong_plugin_identity",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="fixed_gate_only",
    )

    assert not result.ok
    assert not result.authoring_accepted
    assert not (tmp_path / "artifacts/accepted/wrong.json").exists()
    diagnostics = result.candidates[0]["fixed_gate"]["diagnostics"]
    assert any("does not match trusted value" in item["message"] for item in diagnostics)


def test_existing_plugin_build_revalidation_requires_host_approval(
    tmp_path: Path,
) -> None:
    config = GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="zai-org/GLM-5.2",
        api_key="plugin-test-key",
        request_timeout_seconds=1,
        max_retries=0,
    )
    harness = MessageHarnessPipeline(
        config,
        repo_root=tmp_path,
        tool_repo_root=REPO_ROOT,
        client=OpenAICompatibleMessageClient(config, transport=OneResponseTransport(candidate())),
        gate_timeout_seconds=30,
    )
    accepted = tmp_path / "artifacts/accepted/reattest_plugin.json"
    spec = TaskSpec(
        task_id="reattest_plugin",
        task_type="api_adaptation",
        prompt="Return one fixed-archetype API plugin candidate.",
        expected_output_path=accepted,
        output_contract={"type": "json_object", "allowed_kinds": ["api_plugin_candidate"]},
        metadata=adaptation_metadata(),
    )
    first = harness.run_task(
        spec,
        run_id="plugin_fixed_only",
        max_contract_repairs=0,
        max_gate_repairs=0,
        selection_goal="fixed_gate_only",
    )
    assert first.ok
    provenance_path = accepted.with_name("reattest_plugin.provenance.json")
    reviewed_provenance = provenance_path.read_bytes()
    second = harness.run_task(
        spec,
        run_id="plugin_build_reattest",
        max_gate_repairs=0,
        selection_goal="adapter_build_pass",
        execute=True,
    )

    assert not second.ok
    assert second.execution.candidate_cause == "missing_or_stale_execution_approval"
    assert "host execution approval attestation" in second.error
    assert provenance_path.read_bytes() == reviewed_provenance
