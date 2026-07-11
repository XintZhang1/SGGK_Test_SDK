"""Bounded multi-round Message API investigation session with controlled tools."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from test_harness.authoring_gateway.client import CompletionOptions, OpenAICompatibleMessageClient

from .contracts import (
    BUG_HYPOTHESIS_REPORT_SCHEMA,
    MODEL_RESPONSE_SCHEMA,
    bind_host_reproduction_facts,
    compact_diagnostics,
    normalize_hypothesis_report,
    normalize_investigation_turn,
    validate_hypothesis_report,
    validate_investigation_turn,
)
from .evidence_ledger import EvidenceLedger
from .tool_registry import InvestigationToolRegistry


INVESTIGATOR_ROLES: dict[str, str] = {
    "reproduction_analyst": (
        "Prioritize stable reproduction, signature fidelity, lifecycle phase, and the shortest path "
        "that preserves the failure. Do not assume an SDK defect when the recipe or oracle is wrong."
    ),
    "topology_analyst": (
        "Prioritize TopoCheck, TopoTrack descendants/ancestors, ambiguous or unresolved ancestry, "
        "and instrumentation sensitivity. Treat TopoTrack as diagnostic evidence, not causal proof."
    ),
    "source_analyst": (
        "Use source search/excerpt tools to identify one or more plausible implementation sites. "
        "Never invent a file, symbol, line, evidence id, or source_ref_id."
    ),
    "skeptical_oracle_analyst": (
        "Actively test whether the generated case, expected geometry, validation oracle, or harness "
        "contract is wrong before assigning probability to an SDK implementation defect."
    ),
}


SYSTEM_PROMPT = """You are one bounded SGGK bug-investigation subagent running against an immutable failure bundle.

All provider output must be exactly one JSON object in choices[0].message.content. You have no shell,
command, filesystem path, network, patch, executable, runner, environment, or native function-call
authority. To inspect evidence, return kind=investigation_turn and request only registered tool IDs.
The host executes tools with fixed code and returns append-only evidence IDs.

Your final output must be kind=bug_hypothesis_report and assessment_status=candidate_only. Provide
multiple plausible root-cause hypotheses when evidence supports alternatives. Every assertion must
cite real evidence IDs; every source location must use a source_ref_id returned by a source tool.
Never claim confirmed_bug or confirmed_root_cause. Include counter-evidence, limitations, a bounded
falsification experiment, and an immutable reproduction reference for every hypothesis.
"""


def _redact_secrets(value: Any, secrets: tuple[str, ...]) -> Any:
    """Return a recursively redacted JSON-compatible copy for persistence."""

    def redact_text(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, "<redacted>")
        return text

    if isinstance(value, dict):
        return {
            redact_text(str(key)): _redact_secrets(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_secrets(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _provider_response_metadata(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep transport diagnostics without persisting provider response bodies."""

    safe_fields = ("mode", "transport_try", "status", "headers", "body_sha256")
    return [
        {key: record[key] for key in safe_fields if key in record}
        for record in records
        if isinstance(record, dict)
    ]


def _persistence_safe_completion_error(
    error: str,
    records: list[dict[str, Any]],
) -> str:
    """Do not persist the response-text suffix included in HTTP errors."""

    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        if isinstance(status, int) and not 200 <= status < 300:
            return f"HTTP {status}: provider response body omitted"
    return error


@dataclass
class InvestigationOutcome:
    ok: bool
    session_id: str
    role_id: str
    rounds: int
    tool_calls: int
    report: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    diagnostics: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "role_id": self.role_id,
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "report": self.report,
            "error": self.error,
            "diagnostics": self.diagnostics,
            "artifacts": self.artifacts,
        }


class InvestigationSession:
    def __init__(
        self,
        *,
        client: OpenAICompatibleMessageClient,
        registry: InvestigationToolRegistry,
        role_id: str,
        output_root: Path,
        completion_options: CompletionOptions | None = None,
        max_rounds: int = 16,
        max_tool_calls: int = 32,
        max_prompt_chars: int = 240_000,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        if role_id not in INVESTIGATOR_ROLES:
            raise ValueError(f"unknown investigator role {role_id!r}")
        if not 1 <= max_rounds <= 64:
            raise ValueError("max_rounds must be between 1 and 64")
        if not 1 <= max_tool_calls <= 128:
            raise ValueError("max_tool_calls must be between 1 and 128")
        if max_prompt_chars < 8_000:
            raise ValueError("max_prompt_chars must be at least 8000")
        self.client = client
        self.registry = registry
        self.role_id = role_id
        self.output_root = output_root
        self.max_rounds = max_rounds
        self.max_tool_calls = max_tool_calls
        self.max_prompt_chars = max_prompt_chars
        self.session_id = f"inv_{registry.failure_id}_{role_id}"[:96]
        self.options = completion_options or CompletionOptions(
            response_mode="auto",
            response_schema=MODEL_RESPONSE_SCHEMA,
            schema_name="sggk_investigation_action",
            temperature=0.2,
            max_tokens=16_384,
            thinking_mode="enabled",
        )
        self.secret_values = tuple(
            sorted({value for value in secret_values if value}, key=len, reverse=True)
        )
        self.ledger = EvidenceLedger(
            prefix=role_id,
            output_path=output_root / "evidence_ledger.json",
            secret_values=self.secret_values,
        )
        self.diagnostics: list[str] = []
        self.tool_call_count = 0

    def _persist_json(self, path: Path, value: Any) -> Any:
        safe_value = _redact_secrets(value, self.secret_values)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(safe_value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return safe_value

    def _persist_turn(self, round_index: int, value: dict[str, Any]) -> None:
        path = self.output_root / "turns" / f"turn_{round_index:02d}.json"
        self._persist_json(path, value)

    def _prefetch(self) -> None:
        for tool_id in (
            "failure.get_summary",
            "failure.get_reproduction",
            "failure.get_topotrack",
            "geometry.get_bbox_relation",
        ):
            result = self.registry.execute(tool_id, {})
            self.ledger.append(kind="tool_observation", source=f"host_prefetch:{tool_id}", payload=result)

    def _prompt(self, round_index: int) -> str:
        payload = {
            "session": {
                "session_id": self.session_id,
                "round": round_index,
                "failure_id": self.registry.failure_id,
                "role_id": self.role_id,
                "role_instruction": INVESTIGATOR_ROLES[self.role_id],
                "assessment_status": "candidate_only",
                "remaining_tool_calls": self.max_tool_calls - self.tool_call_count,
            },
            "registered_tools": self.registry.catalog(),
            "evidence_ledger": self.ledger.prompt_view(max_chars=max(20_000, self.max_prompt_chars - 60_000)),
            "contract_diagnostics": compact_diagnostics(
                _redact_secrets(self.diagnostics[-24:], self.secret_values)
            ),
            "final_contract": {
                "schema": BUG_HYPOTHESIS_REPORT_SCHEMA,
                "known_evidence_ids": sorted(self.ledger.evidence_ids),
                "known_source_ref_ids": ["source_unavailable", *sorted(self.registry.source_ref_ids)],
                "reproduction_ref_ids": sorted(self.registry.reproduction_ref_ids),
                "expected_signature_id": self.registry.signature_id,
                "expected_stable_attempts": self.registry.stable_attempts,
            },
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        if len(text) <= self.max_prompt_chars:
            return text
        compact = dict(payload)
        compact["final_contract"] = {
            "schema_name": "bug_hypothesis_report.schema.json",
            "known_evidence_ids": sorted(self.ledger.evidence_ids),
            "known_source_ref_ids": ["source_unavailable", *sorted(self.registry.source_ref_ids)],
            "reproduction_ref_ids": sorted(self.registry.reproduction_ref_ids),
            "expected_signature_id": self.registry.signature_id,
            "expected_stable_attempts": self.registry.stable_attempts,
        }
        text = json.dumps(compact, separators=(",", ":"), ensure_ascii=False)
        if len(text) <= self.max_prompt_chars:
            return text
        # Never byte/character-slice JSON: an invalid prompt envelope wastes a
        # model round and makes repair behavior provider-dependent. Retain as
        # many oldest (host-prefetched) evidence records as fit and append a
        # deterministic compaction marker.
        evidence = list(compact["evidence_ledger"])
        for retained in range(len(evidence), -1, -1):
            compact["evidence_ledger"] = evidence[:retained]
            if retained < len(evidence):
                compact["evidence_ledger"].append(
                    {
                        "evidence_id": "ledger_prompt_compacted",
                        "kind": "compaction",
                        "source": "host",
                        "payload": {"omitted_records": len(evidence) - retained},
                    }
                )
            text = json.dumps(compact, separators=(",", ":"), ensure_ascii=False)
            if len(text) <= self.max_prompt_chars:
                return text
        raise ValueError("max_prompt_chars cannot fit the minimum investigation contract")

    def run(self) -> InvestigationOutcome:
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._prefetch()
        for round_index in range(1, self.max_rounds + 1):
            completion = self.client.create_completion(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=self._prompt(round_index),
                options=self.options,
            )
            safe_completion_error = _persistence_safe_completion_error(
                completion.error,
                completion.response_records,
            )
            turn_record: dict[str, Any] = {
                "schema_version": 1,
                "round": round_index,
                "ok": completion.ok,
                "candidate": completion.candidate,
                "candidate_source": completion.candidate_source,
                "finish_reason": completion.finish_reason,
                "usage": completion.usage,
                "reasoning_content_chars": completion.reasoning_content_chars,
                "reasoning_content_sha256": completion.reasoning_content_sha256,
                "error": safe_completion_error,
                "events": completion.events,
                "provider_response_metadata": _provider_response_metadata(
                    completion.response_records
                ),
            }
            self._persist_turn(round_index, turn_record)
            if not completion.ok or not isinstance(completion.candidate, dict):
                message = safe_completion_error or "Message API returned no investigation JSON"
                self.diagnostics.append(message)
                self.ledger.append(
                    kind="model_contract_error",
                    source=f"round:{round_index}",
                    payload={"error": message},
                )
                continue
            candidate = completion.candidate
            if candidate.get("kind") == "investigation_turn" or (
                "tool_calls" in candidate and candidate.get("kind") != "bug_hypothesis_report"
            ):
                candidate = normalize_investigation_turn(
                    candidate,
                    session_id=self.session_id,
                    round_index=round_index,
                )
                errors = validate_investigation_turn(
                    candidate,
                    expected_session_id=self.session_id,
                    expected_round=round_index,
                    tool_ids=self.registry.tool_ids,
                )
                requested = candidate.get("tool_calls") if isinstance(candidate.get("tool_calls"), list) else []
                if self.tool_call_count + len(requested) > self.max_tool_calls:
                    errors.append("$.tool_calls: session tool-call budget would be exceeded")
                if errors:
                    self.diagnostics.extend(errors)
                    self.ledger.append(
                        kind="model_contract_error",
                        source=f"round:{round_index}",
                        payload={"diagnostics": compact_diagnostics(errors)},
                    )
                    continue
                for call in requested:
                    if not isinstance(call, dict):
                        continue
                    self.tool_call_count += 1
                    result = self.registry.execute(str(call.get("tool_id") or ""), call.get("args"))
                    self.ledger.append(
                        kind="tool_observation",
                        source=f"model_tool_call:{call.get('call_id')}",
                        payload={
                            "call_id": call.get("call_id"),
                            "related_hypothesis_ids": call.get("related_hypothesis_ids", []),
                            "reason": call.get("reason", ""),
                            **result,
                        },
                    )
                continue
            if candidate.get("kind") == "bug_hypothesis_report":
                candidate = normalize_hypothesis_report(candidate)
                candidate = bind_host_reproduction_facts(
                    candidate,
                    stable_attempts=self.registry.stable_attempts,
                )
                errors = validate_hypothesis_report(
                    candidate,
                    failure_id=self.registry.failure_id,
                    evidence_ids=self.ledger.evidence_ids,
                    source_ref_ids=self.registry.source_ref_ids,
                    reproduction_ref_ids=self.registry.reproduction_ref_ids,
                    tool_ids=self.registry.tool_ids,
                    expected_signature_id=self.registry.signature_id,
                    expected_stable_attempts=self.registry.stable_attempts,
                )
                if errors:
                    self.diagnostics.extend(errors)
                    self.ledger.append(
                        kind="model_contract_error",
                        source=f"round:{round_index}",
                        payload={"diagnostics": compact_diagnostics(errors)},
                    )
                    continue
                report_path = self.output_root / "hypothesis_report.json"
                safe_candidate = self._persist_json(
                    report_path,
                    candidate,
                )
                outcome = InvestigationOutcome(
                    True,
                    self.session_id,
                    self.role_id,
                    round_index,
                    self.tool_call_count,
                    report=safe_candidate,
                    diagnostics=_redact_secrets(list(self.diagnostics), self.secret_values),
                    artifacts={
                        "report": str(report_path),
                        "evidence_ledger": str(self.ledger.output_path),
                    },
                )
                self._persist_json(
                    self.output_root / "session_summary.json",
                    outcome.as_dict(),
                )
                return outcome
            error = "$.kind: expected investigation_turn or bug_hypothesis_report"
            self.diagnostics.append(error)
            self.ledger.append(
                kind="model_contract_error",
                source=f"round:{round_index}",
                payload={"error": error},
            )
        outcome = InvestigationOutcome(
            False,
            self.session_id,
            self.role_id,
            self.max_rounds,
            self.tool_call_count,
            error="investigation round budget exhausted without a valid candidate-only report",
            diagnostics=_redact_secrets(list(self.diagnostics), self.secret_values),
            artifacts={"evidence_ledger": str(self.ledger.output_path)},
        )
        self._persist_json(
            self.output_root / "session_summary.json",
            outcome.as_dict(),
        )
        return outcome
