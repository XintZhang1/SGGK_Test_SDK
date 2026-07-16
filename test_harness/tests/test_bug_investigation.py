from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from test_harness.authoring_gateway.client import HttpResponse, OpenAICompatibleMessageClient
from test_harness.authoring_gateway.config import PROFILE_SPECS, GatewayConfig
from test_harness.investigation.contracts import (
    normalize_hypothesis_report,
    normalize_investigation_turn,
    validate_hypothesis_report,
    validate_investigation_turn,
)
from test_harness.investigation.session import InvestigationSession
from test_harness.investigation.tool_registry import InvestigationToolRegistry


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_bundle(tmp_path: Path) -> dict[str, Any]:
    bundle_dir = tmp_path / "bundle"
    reports = bundle_dir / "report"
    recipe = bundle_dir / "recipes/reduced_recipe.json"
    write_json(recipe, {"case_id": "case", "api": "api_boolean"})
    write_json(
        reports / "input_properties.json",
        {
            "target": [{"bbox": {"min": [-1, -1, -1], "max": [1, 1, 1]}}],
            "tool": [{"bbox": {"min": [0, 0, 0], "max": [2, 2, 2]}}],
        },
    )
    write_json(
        reports / "topo_track_summary.json",
        {"skipped": True, "item_count": 0, "ancestor_count": 0},
    )
    write_json(
        reports / "topo_track.json",
        {"items": []},
    )
    probe_reports = bundle_dir / "topotrack_probe"
    write_json(
        probe_reports / "topo_track_summary.json",
        {"skipped": False, "item_count": 1, "ancestor_count": 2},
    )
    write_json(
        probe_reports / "topo_track.json",
        {"items": [{"descendent": {"type": "Face", "id": 7}, "ancestors": [{"type": "Face", "id": 2}]}]},
    )
    write_json(reports / "status.json", {"succeeded": False, "error_code": 42})
    signature = {
        "schema_version": 1,
        "kind": "sdk_status",
        "returncode": 2,
        "phase": "invoke_api",
        "sdk_error_code": 42,
    }
    manifest = {
        "fingerprint": "fp_case",
        "representative_case_id": "case",
        "api": "api_boolean",
        "reasons": ["api_error"],
        "status": {"succeeded": False, "error_code": 42},
        "failure_signature": signature,
        "investigation_eligibility": {
            "root_cause": True,
            "reason": "verified_stable_same_failure",
        },
        "topotrack_probe": {
            "classification": "topotrack_capture_available_with_failure",
            "evidence_quality": "diagnostic_not_causal_proof",
            "capture_topotrack": {"available": True, "item_count": 1},
        },
        "validation_failures": [],
        "validation_oracle_details": [],
        "roundtrip_failures": [],
        "replay": {
            "status": "stable_same_failure",
            "attempt_count": 3,
            "stable_attempts": 3,
            "signature_verified": True,
        },
        "copied": {
            "reports": {
                "input_properties.json": str(reports / "input_properties.json"),
                "topo_track_summary.json": str(reports / "topo_track_summary.json"),
                "topo_track.json": str(reports / "topo_track.json"),
                "status.json": str(reports / "status.json"),
            },
            "recipes": {"reduced": str(recipe)},
            "topotrack_probe": {
                "topo_track_summary.json": str(probe_reports / "topo_track_summary.json"),
                "topo_track.json": str(probe_reports / "topo_track.json"),
            },
        },
    }
    manifest_path = bundle_dir / "bundle_manifest.json"
    localization_path = bundle_dir / "localization_summary.json"
    write_json(manifest_path, manifest)
    write_json(localization_path, {"topo_track": {"status": "available"}})
    return {
        "fingerprint": "fp_case",
        "representative_case_id": "case",
        "replay_status": "stable_same_failure",
        "stable_attempts": 3,
        "investigation_eligible": True,
        "investigation_lane": "stable_root_cause",
        "bundle_dir": str(bundle_dir),
        "bundle_manifest": str(manifest_path),
        "localization_summary": str(localization_path),
        "bug_report": str(bundle_dir / "bug_report.md"),
    }


def test_tool_registry_rejects_execution_fields_and_external_source_is_off(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "kernel.cpp").write_text("void api_boolean_impl() {}\n", encoding="utf-8")
    registry = InvestigationToolRegistry(
        bundle_record=bundle,
        source_roots=[source_root],
        allow_source_content=False,
    )

    rejected = registry.execute(
        "source.search_literal",
        {"query": "api_boolean_impl", "command": "type kernel.cpp"},
    )
    assert not rejected["ok"]
    assert "execution field" in rejected["error"] or "unknown tool args" in rejected["error"]

    blocked = registry.execute("source.search_literal", {"query": "api_boolean_impl"})
    assert blocked["ok"]
    assert blocked["result"]["available"] is False
    assert blocked["result"]["results"] == []

    topotrack = registry.execute("failure.get_topotrack", {})
    assert topotrack["ok"]
    assert (
        topotrack["result"]["isolated_probe"]["classification"]
        == "topotrack_capture_available_with_failure"
    )
    assert topotrack["result"]["tracking_items_source"] == "isolated_paired_capture"
    assert topotrack["result"]["tracking_items"][0]["descendent"]["id"] == 7


def test_tool_registry_rejects_an_unstable_root_cause_bundle(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    manifest_path = Path(bundle["bundle_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["replay"]["status"] = "changed_failure"
    manifest["replay"]["signature_verified"] = False
    write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="verified stable_same_failure"):
        InvestigationToolRegistry(
            bundle_record=bundle,
            source_roots=[],
            allow_source_content=False,
        )


def test_source_excerpt_requires_opaque_ref_from_prior_search(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "kernel.cpp").write_text(
        "int helper() { return 0; }\nint api_boolean_impl() { return helper(); }\n",
        encoding="utf-8",
    )
    registry = InvestigationToolRegistry(
        bundle_record=bundle,
        source_roots=[source_root],
        allow_source_content=True,
    )

    arbitrary = registry.execute("source.read_excerpt", {"source_ref_id": "C:/source/kernel.cpp"})
    assert not arbitrary["ok"]
    search = registry.execute("source.search_literal", {"query": "api_boolean_impl"})
    source_ref = search["result"]["results"][0]["source_ref_id"]
    excerpt = registry.execute(
        "source.read_excerpt",
        {"source_ref_id": source_ref, "before": 1, "after": 1},
    )
    assert excerpt["ok"]
    assert excerpt["result"]["source_path"] == "source_root_0:kernel.cpp"
    assert any("api_boolean_impl" in line["text"] for line in excerpt["result"]["lines"])


class QueueTransport:
    def __init__(self, values: list[dict[str, Any]], hidden_reasoning: str) -> None:
        self.values = list(values)
        self.hidden_reasoning = hidden_reasoning

    def post(self, **_kwargs: Any) -> HttpResponse:
        value = self.values.pop(0)
        body = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(value),
                        "reasoning_content": self.hidden_reasoning,
                    },
                }
            ]
        }
        return HttpResponse(200, {"content-type": "application/json"}, json.dumps(body).encode())


class EchoingSecretTransport:
    def __init__(self, secret: str, report: dict[str, Any]) -> None:
        self.secret = secret
        self.report = report
        self.calls = 0

    def post(self, **_kwargs: Any) -> HttpResponse:
        self.calls += 1
        headers = {
            "content-type": "application/json",
            "x-request-id": f"provider-{self.secret}",
        }
        if self.calls == 1:
            body = {
                "error": {
                    "message": f"provider error echoed {self.secret}",
                    "raw_body": f"sensitive provider body {self.secret}",
                }
            }
            return HttpResponse(500, headers, json.dumps(body).encode())
        body = {
            "provider_debug": {
                "echoed_credential": self.secret,
                "raw_body": f"sensitive provider body {self.secret}",
            },
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(self.report),
                    },
                }
            ],
        }
        return HttpResponse(200, headers, json.dumps(body).encode())


def gateway_config() -> GatewayConfig:
    return GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="zai-org/GLM-5.2",
        api_key="investigation-secret-key",
        request_timeout_seconds=1.0,
        max_retries=0,
    )


def candidate_report(
    registry: InvestigationToolRegistry,
    *,
    summary: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "bug_hypothesis_report",
        "failure_id": "fp_case",
        "assessment_status": "candidate_only",
        "issue_classification": {
            "kind": "sdk_bug_candidate",
            "confidence": 0.55,
            "alternatives": ["oracle_defect", "harness_defect"],
        },
        "summary": summary,
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "claim_status": "candidate_root_cause",
                "category": "sdk_implementation",
                "statement": "The API implementation may reject this topology before returning a result.",
                "confidence": {
                    "score": 0.55,
                    "band": "medium",
                    "basis": "Stable SDK status across replay.",
                },
                "suspect_locations": [
                    {
                        "source_ref_id": "source_unavailable",
                        "symbol": "",
                        "line_start": 0,
                        "line_end": 0,
                        "role": "source_unavailable",
                        "rationale": "No trusted source snapshot was configured.",
                    }
                ],
                "supporting_evidence": [
                    {
                        "evidence_id": "ev_reproduction_analyst_0001",
                        "assertion": "The bound failure summary reports sdk_error_code 42.",
                    }
                ],
                "contradicting_evidence": [],
                "assumptions": ["The fixed runner and SDK build are compatible."],
                "falsification_tests": [
                    {
                        "tool_id": "failure.get_topotrack",
                        "experiment": "Compare the stable signature with TopoTrack evidence.",
                        "expected_discriminator": "A changed signature indicates instrumentation sensitivity.",
                    }
                ],
                "reproduction_path": {
                    "reproduction_ref_id": "repro_fp_case",
                    "expected_signature_id": registry.signature_id,
                    "stable_attempts": 99,
                },
            }
        ],
        "unresolved_questions": ["Which SDK source symbol emits status code 42?"],
        "evidence_ids": ["ev_reproduction_analyst_0001"],
    }


def test_provider_echoed_secret_and_raw_response_body_are_not_persisted(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    registry = InvestigationToolRegistry(
        bundle_record=bundle,
        source_roots=[],
        allow_source_content=False,
    )
    config = gateway_config()
    report = candidate_report(
        registry,
        summary=f"Provider echoed {config.api_key}; the root cause remains only a candidate.",
    )
    session = InvestigationSession(
        client=OpenAICompatibleMessageClient(
            config,
            transport=EchoingSecretTransport(config.api_key, report),
        ),
        registry=registry,
        role_id="reproduction_analyst",
        output_root=tmp_path / "investigation",
        max_rounds=2,
        secret_values=(config.api_key,),
    )

    outcome = session.run()

    assert outcome.ok
    assert outcome.report["hypotheses"][0]["reproduction_path"]["stable_attempts"] == 3
    assert config.api_key not in json.dumps(outcome.as_dict())
    output_root = tmp_path / "investigation"
    artifacts = {
        path.relative_to(output_root).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in output_root.rglob("*.json")
    }
    artifact_text = json.dumps(artifacts, ensure_ascii=False)
    assert config.api_key not in artifact_text
    assert "provider_debug" not in artifact_text
    assert "raw_body" not in artifact_text
    assert "sensitive provider body" not in artifact_text

    first_turn = artifacts["turns/turn_01.json"]
    assert "response_records" not in first_turn
    response_metadata = first_turn["provider_response_metadata"]
    assert response_metadata[0]["headers"]["x-request-id"] == "provider-<redacted>"
    assert all("body" not in record for record in response_metadata)
    assert first_turn["error"] == "HTTP 500: provider response body omitted"
    assert "<redacted>" in artifacts["hypothesis_report.json"]["summary"]
    assert "<redacted>" in artifacts["session_summary.json"]["report"]["summary"]


def test_multi_round_tool_request_then_candidate_report(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    registry = InvestigationToolRegistry(
        bundle_record=bundle,
        source_roots=[],
        allow_source_content=True,
    )
    session_id = "inv_fp_case_reproduction_analyst"
    turn = {
        "schema_version": 1,
        "kind": "investigation_turn",
        "decision": "request_tools",
        "session_id": session_id,
        "round": 1,
        "tool_calls": [
            {
                "call_id": "call_1",
                "tool_id": "artifact.list_reports",
                "args": {},
                "related_hypothesis_ids": ["H1"],
                "reason": "Check which deterministic reports can falsify the initial hypothesis.",
            }
        ],
    }
    final = {
        "schema_version": 1,
        "kind": "bug_hypothesis_report",
        "failure_id": "fp_case",
        "assessment_status": "candidate_only",
        "issue_classification": {
            "kind": "sdk_bug_candidate",
            "confidence": 0.55,
            "alternatives": ["oracle_defect", "harness_defect"],
        },
        "summary": "The stable invoke_api status is an SDK candidate, but source evidence is unavailable.",
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "claim_status": "candidate_root_cause",
                "category": "sdk_implementation",
                "statement": "The API implementation may reject this topology before producing a ModelingRet result.",
                "confidence": {"score": 0.55, "band": "medium", "basis": "Stable SDK status across replay."},
                "suspect_locations": [
                    {
                        "source_ref_id": "source_unavailable",
                        "symbol": "",
                        "line_start": 0,
                        "line_end": 0,
                        "role": "source_unavailable",
                        "rationale": "No trusted source snapshot was configured for this session."
                    }
                ],
                "supporting_evidence": [
                    {
                        "evidence_id": "ev_reproduction_analyst_0001",
                        "assertion": "The bound failure summary reports sdk_error_code 42."
                    }
                ],
                "contradicting_evidence": [],
                "assumptions": ["The fixed runner and SDK build are compatible."],
                "falsification_tests": [
                    {
                        "tool_id": "failure.get_topotrack",
                        "experiment": "Compare the same stable signature with focused TopoTrack evidence.",
                        "expected_discriminator": "A changed signature would indicate instrumentation sensitivity."
                    }
                ],
                "reproduction_path": {
                    "reproduction_ref_id": "repro_fp_case",
                    "expected_signature_id": registry.signature_id,
                    "stable_attempts": 3
                }
            }
        ],
        "unresolved_questions": ["Which SDK source symbol emits status code 42?"],
        "evidence_ids": ["ev_reproduction_analyst_0001", "ev_reproduction_analyst_0005"]
    }
    hidden = "private chain of thought investigation-secret-key"
    config = gateway_config()
    client = OpenAICompatibleMessageClient(
        config,
        transport=QueueTransport([turn, final], hidden),
    )
    session = InvestigationSession(
        client=client,
        registry=registry,
        role_id="reproduction_analyst",
        output_root=tmp_path / "investigation",
        max_rounds=3,
        secret_values=(config.api_key,),
    )

    outcome = session.run()

    assert outcome.ok
    assert outcome.rounds == 2
    assert outcome.tool_calls == 1
    assert outcome.report["assessment_status"] == "candidate_only"
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "investigation").rglob("*.json")
    )
    assert hidden not in artifact_text
    assert config.api_key not in artifact_text


def test_hallucinated_evidence_and_confirmed_claims_are_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    registry = InvestigationToolRegistry(
        bundle_record=bundle,
        source_roots=[],
        allow_source_content=False,
    )
    invalid = {
        "schema_version": 1,
        "kind": "bug_hypothesis_report",
        "failure_id": "fp_case",
        "assessment_status": "candidate_only",
        "issue_classification": {"kind": "inconclusive", "confidence": 0.1, "alternatives": []},
        "summary": "confirmed_root_cause in an invented location",
        "hypotheses": [],
        "unresolved_questions": [],
        "evidence_ids": ["ev_invented"],
    }

    errors = validate_hypothesis_report(
        invalid,
        failure_id="fp_case",
        evidence_ids=set(),
        source_ref_ids=registry.source_ref_ids,
        reproduction_ref_ids=registry.reproduction_ref_ids,
        tool_ids=registry.tool_ids,
        expected_signature_id=registry.signature_id,
    )

    assert any("confirmed_root_cause" in error for error in errors)
    assert any("unknown evidence id" in error for error in errors)
    assert any("should be non-empty" in error for error in errors)


def test_small_model_semantic_aliases_are_normalized_without_weakening_refs() -> None:
    candidate = {
        "hypotheses": [
            {
                "category": "test_generation_defect",
                "suspect_locations": [
                    {
                        "source_ref_id": "unavailable",
                        "role": "possible_failure_site",
                        "line_start": 99,
                        "line_end": 101,
                    }
                ],
            }
        ]
    }

    normalized = normalize_hypothesis_report(candidate)

    hypothesis = normalized["hypotheses"][0]
    assert hypothesis["category"] == "test_generation"
    assert hypothesis["suspect_locations"][0] == {
        "source_ref_id": "source_unavailable",
        "role": "source_unavailable",
        "line_start": 0,
        "line_end": 0,
    }

    no_source = normalize_hypothesis_report(
        {"hypotheses": [{"category": "unknown", "suspect_locations": []}]}
    )
    assert no_source["hypotheses"][0]["suspect_locations"][0]["source_ref_id"] == (
        "source_unavailable"
    )


def test_openai_style_tool_alias_is_canonicalized_to_controlled_turn() -> None:
    raw = {
        "kind": "investigation_turn",
        "tool_calls": [
            {
                "id": "call_reports",
                "tool_id": "artifact.list_reports",
                "arguments": {},
            }
        ],
    }

    normalized = normalize_investigation_turn(raw, session_id="inv_test", round_index=1)
    errors = validate_investigation_turn(
        normalized,
        expected_session_id="inv_test",
        expected_round=1,
        tool_ids={"artifact.list_reports"},
    )

    assert errors == []
    assert normalized["decision"] == "request_tools"
    assert normalized["tool_calls"][0]["call_id"] == "call_reports"
    assert normalized["tool_calls"][0]["args"] == {}


def test_final_report_rejects_unregistered_falsification_tool_and_signature(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    registry = InvestigationToolRegistry(
        bundle_record=bundle,
        source_roots=[],
        allow_source_content=False,
    )
    report = {
        "schema_version": 1,
        "kind": "bug_hypothesis_report",
        "failure_id": "fp_case",
        "assessment_status": "candidate_only",
        "issue_classification": {"kind": "inconclusive", "confidence": 0.2, "alternatives": []},
        "summary": "One bounded candidate hypothesis.",
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "claim_status": "candidate_root_cause",
                "category": "unknown",
                "statement": "The available evidence is insufficient.",
                "confidence": {"score": 0.2, "band": "low", "basis": "Only host-prefetched evidence is available."},
                "suspect_locations": [],
                "supporting_evidence": [{"evidence_id": "ev_known", "assertion": "Known evidence."}],
                "contradicting_evidence": [],
                "assumptions": [],
                "falsification_tests": [
                    {
                        "tool_id": "shell.run",
                        "experiment": "Attempt an unregistered command.",
                        "expected_discriminator": "This must be rejected by the host contract.",
                    }
                ],
                "reproduction_path": {
                    "reproduction_ref_id": "repro_fp_case",
                    "expected_signature_id": "sig_invented",
                    "stable_attempts": 3,
                },
            }
        ],
        "unresolved_questions": [],
        "evidence_ids": ["ev_known"],
    }

    errors = validate_hypothesis_report(
        report,
        failure_id="fp_case",
        evidence_ids={"ev_known"},
        source_ref_ids=registry.source_ref_ids,
        reproduction_ref_ids=registry.reproduction_ref_ids,
        tool_ids=registry.tool_ids,
        expected_signature_id=registry.signature_id,
    )

    assert any("unregistered tool 'shell.run'" in error for error in errors)
    assert any("immutable host signature" in error for error in errors)


def test_prompt_compaction_remains_valid_json(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    registry = InvestigationToolRegistry(
        bundle_record=bundle,
        source_roots=[],
        allow_source_content=False,
    )
    session = InvestigationSession(
        client=OpenAICompatibleMessageClient(gateway_config(), transport=QueueTransport([], "")),
        registry=registry,
        role_id="reproduction_analyst",
        output_root=tmp_path / "investigation",
        max_prompt_chars=8_000,
    )
    session._prefetch()
    for index in range(20):
        session.ledger.append(
            kind="tool_observation",
            source=f"test:{index}",
            payload={"large": "x" * 2_000},
        )

    prompt = session._prompt(1)

    parsed = json.loads(prompt)
    assert len(prompt) <= 8_000
    assert parsed["session"]["failure_id"] == "fp_case"
