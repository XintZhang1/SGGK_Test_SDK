from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import pytest

TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from run_message_harness_pipeline import MessageHarnessPipeline  # noqa: E402
from build_model_prompt_pack import source_prompt  # noqa: E402

from test_harness.authoring_gateway.client import (  # noqa: E402
    HttpResponse,
    OpenAICompatibleMessageClient,
)
from test_harness.authoring_gateway.config import PROFILE_SPECS, GatewayConfig  # noqa: E402
from test_harness.authoring_gateway.gateway import AuthoringGateway, GatewayError, TaskSpec  # noqa: E402
from test_harness.authoring_gateway.source_evidence import (  # noqa: E402
    build_source_contract,
    build_source_contract_from_ranges,
    validate_source_review,
    verify_current_source,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _source_fixture(tmp_path: Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    root = tmp_path / "approved-source"
    root.mkdir()
    source = root / "risk.cpp"
    source.write_text(
        "double apply(double value, double tolerance) {\n"
        "  if (tolerance < 0.01) tolerance = 0.01;\n"
        "  return value + tolerance;\n"
        "}\n",
        encoding="utf-8",
    )
    finding = {"id": "risk_001", "categories": ["tolerance"], "source": {"line": 2}}
    contract, bindings = build_source_contract(
        task_id="source_task_001",
        finding=finding,
        source_path=source,
        source_root=root,
        line_start=1,
        line_end=4,
    )
    return source, contract, bindings


def _review(contract: dict[str, Any], case_ids: list[str]) -> dict[str, Any]:
    source_ref = contract["source_refs"][0]
    return {
        "schema_version": 1,
        "task_id": contract["task_id"],
        "finding_id": contract["finding_id"],
        "source_contract_sha256": contract["source_contract_sha256"],
        "summary": "The tolerance lower-bound branch can change geometry classification near the threshold.",
        "source_refs": [
            {
                "source_ref_id": source_ref["source_ref_id"],
                "line_start": source_ref["line_start"],
                "line_end": source_ref["line_end"],
                "content_sha256": source_ref["content_sha256"],
            }
        ],
        "risky_branches": [
            {
                "branch_id": "branch_01",
                "source_ref_ids": [source_ref["source_ref_id"]],
                "condition": "tolerance is below the lower bound",
                "risk": "classification changes at the threshold",
            }
        ],
        "failure_hypotheses": [
            {
                "hypothesis_id": "hyp_01",
                "branch_ids": ["branch_01"],
                "trigger": "exact threshold contact",
                "observable_failure": "unexpected empty result body",
            },
            {
                "hypothesis_id": "hyp_02",
                "branch_ids": ["branch_01"],
                "trigger": "signed tolerance offset",
                "observable_failure": "invalid or unstable topology",
            },
        ],
        "test_enhancements": [
            {
                "enhancement_id": "enh_01",
                "hypothesis_ids": ["hyp_01", "hyp_02"],
                "case_ids": case_ids,
                "strategy": "compare exact and signed tolerance bands",
                "perturbations": ["plus and minus geometry and topology tolerances"],
                "oracles": ["result body count and finite properties"],
            }
        ],
    }


def _config(profile: str = "intranet") -> GatewayConfig:
    return GatewayConfig(
        profile=PROFILE_SPECS[profile],
        base_url="https://message-api.invalid/v1",
        model="Qwen/Qwen3.6-35B-A3B",
        api_key="test-only-key",
        request_timeout_seconds=1.0,
        max_retries=0,
    )


class OneResponseTransport:
    def __init__(self, candidate: dict[str, Any]) -> None:
        payload = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(candidate)},
                }
            ]
        }
        self.response = HttpResponse(
            200,
            {"content-type": "application/json"},
            json.dumps(payload).encode(),
        )
        self.calls = 0

    def post(self, **_kwargs: Any) -> HttpResponse:
        self.calls += 1
        return self.response


def test_source_contract_revalidates_current_bytes_and_relationship_graph(tmp_path: Path) -> None:
    source, contract, bindings = _source_fixture(tmp_path)
    review = _review(contract, ["case_01", "case_02"])

    errors, current = verify_current_source(contract, bindings)
    assert not errors
    assert [item["source_ref_id"] for item in current] == [contract["source_refs"][0]["source_ref_id"]]
    assert not validate_source_review(review, contract, ["case_01", "case_02"])

    broken = json.loads(json.dumps(review))
    broken["test_enhancements"][0]["case_ids"] = ["invented_case"]
    assert any("unknown ids" in item for item in validate_source_review(broken, contract, ["case_01"]))

    source.write_text(source.read_text(encoding="utf-8").replace("0.01", "0.02"), encoding="utf-8")
    stale_errors, _ = verify_current_source(contract, bindings)
    assert any("changed" in item for item in stale_errors)


def test_source_contract_can_bind_multiple_overload_definitions(tmp_path: Path) -> None:
    root = tmp_path / "approved-source"
    root.mkdir()
    source = root / "overloads.cpp"
    source.write_text(
        "Result api_item(Body a) { return Run(a); }\n"
        "Result api_item(Body a, Body b) { return Run(a, b); }\n",
        encoding="utf-8",
    )

    contract, bindings = build_source_contract_from_ranges(
        task_id="source_task_overloads",
        finding={"id": "risk_overloads"},
        source_root=root,
        source_ranges=[
            {"source_path": source, "line_start": 1, "line_end": 1},
            {"source_path": source, "line_start": 2, "line_end": 2},
        ],
    )

    assert len(contract["source_refs"]) == 2
    assert len({item["source_ref_id"] for item in contract["source_refs"]}) == 2
    errors, current = verify_current_source(contract, bindings)
    assert not errors
    assert len(current) == 2


def test_source_prompt_maps_every_bound_definition_to_a_branch_example() -> None:
    refs = [
        {
            "source_ref_id": "src_overload_1",
            "line_start": 10,
            "line_end": 20,
            "content_sha256": "a" * 64,
        },
        {
            "source_ref_id": "src_overload_2",
            "line_start": 30,
            "line_end": 45,
            "content_sha256": "b" * 64,
        },
    ]
    prompt = source_prompt(
        {
            "task_id": "source_task_overloads",
            "finding": {"id": "risk_overloads"},
            "source_contract": {
                "finding_id": "risk_overloads",
                "source_contract_sha256": "c" * 64,
                "source_refs": refs,
            },
            "source_excerpts": [
                {"path": "api.cpp", "start_line": 10, "end_line": 20, "text": "first"},
                {"path": "api.cpp", "start_line": 30, "end_line": 45, "text": "second"},
            ],
        },
        "artifacts/candidate.json",
    )

    assert '"source_ref_ids"' in prompt
    assert '"src_overload_1"' in prompt
    assert '"src_overload_2"' in prompt
    assert '"branch_01"' in prompt
    assert '"branch_02"' in prompt
    assert "### Definition 1" in prompt
    assert "### Definition 2" in prompt


@pytest.mark.parametrize("task_type", ["source_attack", "sggk_source_attack", "interface_form"])
def test_low_level_gateway_rejects_external_source_before_transport(
    tmp_path: Path,
    task_type: str,
) -> None:
    transport = OneResponseTransport({"kind": "needs_harness_extension"})
    config = _config("siliconflow-test")
    gateway = AuthoringGateway(
        config,
        repo_root=tmp_path,
        client=OpenAICompatibleMessageClient(config, transport=transport),
    )
    task = TaskSpec(
        task_id="source_task_001",
        task_type=task_type,
        prompt="proprietary excerpt",
        expected_output_path=tmp_path / "artifacts/output.json",
        output_contract={"type": "json_object", "allowed_kinds": ["needs_harness_extension"]},
        metadata={
            "data_classification": "proprietary_source",
            "allowed_profile_categories": ["intranet"],
        },
    )

    with pytest.raises(GatewayError, match="restricted to the intranet"):
        gateway.run_task(task)
    assert transport.calls == 0


def test_low_level_gateway_rejects_replayed_provider_bound_prompt_before_transport(
    tmp_path: Path,
) -> None:
    transport = OneResponseTransport({"kind": "needs_harness_extension"})
    config = _config("siliconflow-test")
    gateway = AuthoringGateway(
        config,
        repo_root=tmp_path,
        client=OpenAICompatibleMessageClient(config, transport=transport),
    )
    task = TaskSpec(
        task_id="provider_bound_interface",
        task_type="interface_form",
        prompt="intranet-authored interface evidence",
        expected_output_path=tmp_path / "artifacts/output.json",
        output_contract={"type": "json_object", "allowed_kinds": ["needs_harness_extension"]},
        metadata={
            "provider_profile": "intranet",
            "provider_profile_category": "intranet",
        },
    )

    with pytest.raises(GatewayError, match="provider_profile does not match"):
        gateway.run_task(task)
    assert transport.calls == 0


def test_low_level_gateway_rejects_unclassified_external_task_before_transport(
    tmp_path: Path,
) -> None:
    transport = OneResponseTransport({"kind": "needs_harness_extension"})
    config = _config("siliconflow-test")
    gateway = AuthoringGateway(
        config,
        repo_root=tmp_path,
        client=OpenAICompatibleMessageClient(config, transport=transport),
    )
    task = TaskSpec(
        task_id="unbound_external_interface",
        task_type="interface_form",
        prompt="legacy prompt with unknown data provenance",
        expected_output_path=tmp_path / "artifacts/output.json",
        output_contract={"type": "json_object", "allowed_kinds": ["needs_harness_extension"]},
    )

    with pytest.raises(GatewayError, match="must be explicitly bound"):
        gateway.run_task(task)
    assert transport.calls == 0


def test_low_level_gateway_allows_explicitly_bound_public_external_task(tmp_path: Path) -> None:
    candidate = {
        "kind": "needs_harness_extension",
        "api": "api_new",
        "why_needed": "the public interface is not registered",
        "extension_summary": "add one bounded public-interface adapter",
        "proposed_recipe_fields": {},
        "proposed_artifacts": [],
        "validation_oracle": {},
        "minimum_smoke_case": {},
        "patch_plan": [],
    }
    transport = OneResponseTransport(candidate)
    config = _config("siliconflow-test")
    gateway = AuthoringGateway(
        config,
        repo_root=tmp_path,
        client=OpenAICompatibleMessageClient(config, transport=transport),
    )
    task = TaskSpec(
        task_id="bound_external_interface",
        task_type="interface_form",
        prompt="public interface metadata only",
        expected_output_path=tmp_path / "artifacts/output.json",
        output_contract={"type": "json_object", "allowed_kinds": ["needs_harness_extension"]},
        metadata={
            "provider_profile": "siliconflow-test",
            "provider_profile_category": "explicit_external_test",
            "data_classification": "public_interface",
        },
    )

    result = gateway.run_task(task, max_repairs=0)

    assert result.ok
    assert transport.calls == 1


@pytest.mark.parametrize("task_type", ["source_attack", "sggk_source_attack"])
def test_source_pipeline_attests_review_and_rejects_stale_source(
    tmp_path: Path,
    task_type: str,
) -> None:
    source, contract, bindings = _source_fixture(tmp_path)
    dsl = json.loads(
        (REPO_ROOT / "test_harness/interface_example_packs/api_boolean_primitives.example_dsl.json").read_text(
            encoding="utf-8"
        )
    )
    case_ids = [item["case_id"] for item in dsl["cases"]]
    dsl["source_review"] = _review(contract, case_ids)
    candidate = {"kind": "attack_dsl", "dsl": dsl, "notes": []}
    transport = OneResponseTransport(candidate)
    config = _config()
    client = OpenAICompatibleMessageClient(config, transport=transport)
    pipeline = MessageHarnessPipeline(
        config,
        repo_root=tmp_path,
        tool_repo_root=REPO_ROOT,
        client=client,
        gate_timeout_seconds=30.0,
    )
    output = tmp_path / "artifacts/accepted/source_task_001.json"
    task = TaskSpec(
        task_id="source_task_001",
        task_type=task_type,
        prompt="Review the bound source and return a linked source_review plus attack DSL.",
        expected_output_path=output,
        output_contract={
            "type": "json_object",
            "kind_field": "kind",
            "allowed_kinds": ["attack_dsl"],
        },
        metadata={
            "data_classification": "proprietary_source",
            "allowed_profile_categories": ["intranet"],
            "source_contract": contract,
            "host_source_bindings": bindings,
        },
    )

    result = pipeline.run_task(task, run_id="source_review_accept", max_contract_repairs=0)
    assert result.ok
    provenance = json.loads(output.with_name("source_task_001.provenance.json").read_text(encoding="utf-8"))
    source_attestation = provenance["source_review_attestation"]
    assert source_attestation["ok"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", source_attestation["sha256"])

    source.write_text(source.read_text(encoding="utf-8").replace("0.01", "0.02"), encoding="utf-8")
    resumed = pipeline.run_task(task, run_id="source_review_stale", max_contract_repairs=0)
    assert not resumed.ok
    assert "verified fixed-gate accepted pair" in resumed.error
    assert transport.calls == 1


def test_repository_has_no_kernel_specific_reference_residuals() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()
    def word(*codes: int) -> str:
        return "".join(chr(code) for code in codes)

    short = word(111, 99, 99)
    brand_first = word(111, 112, 101, 110)
    brand_second = word(99, 97, 115, 99, 97, 100, 101)
    forbidden = [
        brand_first + r"[ -]?" + brand_second,
        rf"\b{short}{word(116)}\b",
        rf"\b{short}\b",
        short + r"[_-]",
        word(98, 111, 112, 97, 108, 103, 111),
        word(98, 114, 101, 112, 97, 108, 103, 111, 97, 112, 105),
        word(112, 97, 118, 101, 102, 105, 108, 108, 101, 114),
        word(112, 114, 101, 99, 105, 115, 105, 111, 110) + r"::" + word(99, 111, 110, 102, 117, 115, 105, 111, 110),
        word(105, 103, 101, 115, 99, 111, 110, 116, 114, 111, 108, 95, 119, 114, 105, 116, 101, 114),
        word(115, 117, 114, 114, 111, 103, 97, 116, 101),
    ]
    pattern = re.compile("|".join(forbidden), re.IGNORECASE)
    findings: list[str] = []
    for relative in tracked:
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        if pattern.search(relative):
            findings.append(relative)
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        if pattern.search(text):
            findings.append(relative)
    assert not findings
