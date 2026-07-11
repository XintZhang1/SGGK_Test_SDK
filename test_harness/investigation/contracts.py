"""Schema and semantic gates for model-authored investigation JSON."""

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = REPO_ROOT / "test_harness" / "schemas"
FORBIDDEN_EXECUTION_KEYS = {
    "argv",
    "command",
    "commands",
    "cwd",
    "env",
    "environment",
    "executable",
    "runner",
    "shell",
    "tool_choice",
    "tool_calls_native",
    "url",
}


def _schema(name: str) -> dict[str, Any]:
    value = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"schema root must be object: {name}")
    return value


INVESTIGATION_TURN_SCHEMA = _schema("investigation_turn.schema.json")
BUG_HYPOTHESIS_REPORT_SCHEMA = _schema("bug_hypothesis_report.schema.json")
MODEL_RESPONSE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [INVESTIGATION_TURN_SCHEMA, BUG_HYPOTHESIS_REPORT_SCHEMA],
}


def _forbidden_fields(value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_forbidden_fields(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in FORBIDDEN_EXECUTION_KEYS:
                errors.append(f"{path}.{key}: model-authored execution field is forbidden")
            errors.extend(_forbidden_fields(item, f"{path}.{key}"))
    return errors


def _schema_errors(schema: dict[str, Any], value: Any) -> list[str]:
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for item in sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path)):
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in item.absolute_path
        )
        errors.append(f"{path}: {item.message}")
    return errors


def validate_investigation_turn(
    value: Any,
    *,
    expected_session_id: str,
    expected_round: int,
    tool_ids: set[str],
) -> list[str]:
    errors = _schema_errors(INVESTIGATION_TURN_SCHEMA, value)
    errors.extend(_forbidden_fields(value))
    if not isinstance(value, dict):
        return errors
    if value.get("session_id") != expected_session_id:
        errors.append("$.session_id: does not match the active host session")
    if value.get("round") != expected_round:
        errors.append("$.round: does not match the active host round")
    call_ids: set[str] = set()
    for index, call in enumerate(value.get("tool_calls", [])):
        if not isinstance(call, dict):
            continue
        call_id = call.get("call_id")
        if isinstance(call_id, str):
            if call_id in call_ids:
                errors.append(f"$.tool_calls[{index}].call_id: duplicate call id")
            call_ids.add(call_id)
        tool_id = call.get("tool_id")
        if isinstance(tool_id, str) and tool_id not in tool_ids:
            errors.append(f"$.tool_calls[{index}].tool_id: unregistered tool {tool_id!r}")
    return errors


def normalize_investigation_turn(
    value: dict[str, Any],
    *,
    session_id: str,
    round_index: int,
) -> dict[str, Any]:
    """Canonicalize common OpenAI-style tool-call aliases from smaller models."""

    normalized = copy.deepcopy(value)
    normalized.setdefault("schema_version", 1)
    normalized.setdefault("kind", "investigation_turn")
    normalized.setdefault("decision", "request_tools")
    normalized.setdefault("session_id", session_id)
    normalized.setdefault("round", round_index)
    tool_calls = normalized.get("tool_calls")
    if not isinstance(tool_calls, list):
        return normalized
    for index, call in enumerate(tool_calls, 1):
        if not isinstance(call, dict):
            continue
        if "call_id" not in call and isinstance(call.get("id"), str):
            call["call_id"] = call.pop("id")
        if "args" not in call and isinstance(call.get("arguments"), dict):
            call["args"] = call.pop("arguments")
        call.setdefault("call_id", f"call_{round_index}_{index}")
        call.setdefault("args", {})
        call.setdefault("related_hypothesis_ids", [])
        call.setdefault("reason", "Request registered evidence needed for candidate-only analysis.")
    return normalized


def validate_hypothesis_report(
    value: Any,
    *,
    failure_id: str,
    evidence_ids: set[str],
    source_ref_ids: set[str],
    reproduction_ref_ids: set[str],
    tool_ids: set[str] | None = None,
    expected_signature_id: str = "",
    expected_stable_attempts: int | None = None,
) -> list[str]:
    errors = _schema_errors(BUG_HYPOTHESIS_REPORT_SCHEMA, value)
    errors.extend(_forbidden_fields(value))
    if not isinstance(value, dict):
        return errors
    if value.get("failure_id") != failure_id:
        errors.append("$.failure_id: does not match the bound failure")
    lowered = json.dumps(value, ensure_ascii=False).lower()
    for forbidden_claim in ("confirmed_bug", "confirmed_root_cause", '"claim_status": "confirmed'):
        if forbidden_claim in lowered:
            errors.append(f"$: model may not claim {forbidden_claim}")
    report_evidence = value.get("evidence_ids") if isinstance(value.get("evidence_ids"), list) else []
    for evidence_id in report_evidence:
        if isinstance(evidence_id, str) and evidence_id not in evidence_ids:
            errors.append(f"$.evidence_ids: unknown evidence id {evidence_id!r}")
    hypothesis_ids: set[str] = set()
    for index, hypothesis in enumerate(value.get("hypotheses", [])):
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = hypothesis.get("hypothesis_id")
        if isinstance(hypothesis_id, str):
            if hypothesis_id in hypothesis_ids:
                errors.append(f"$.hypotheses[{index}].hypothesis_id: duplicate id")
            hypothesis_ids.add(hypothesis_id)
        confidence = hypothesis.get("confidence") if isinstance(hypothesis.get("confidence"), dict) else {}
        score = confidence.get("score")
        contradicting = hypothesis.get("contradicting_evidence")
        if isinstance(score, (int, float)) and score >= 0.7 and not contradicting:
            errors.append(
                f"$.hypotheses[{index}].contradicting_evidence: high-confidence hypotheses must cite counter-evidence"
            )
        for bucket in ("supporting_evidence", "contradicting_evidence"):
            for item in hypothesis.get(bucket, []):
                if not isinstance(item, dict):
                    continue
                evidence_id = item.get("evidence_id")
                if isinstance(evidence_id, str) and evidence_id not in evidence_ids:
                    errors.append(
                        f"$.hypotheses[{index}].{bucket}: unknown evidence id {evidence_id!r}"
                    )
        for location_index, location in enumerate(hypothesis.get("suspect_locations", [])):
            if not isinstance(location, dict):
                continue
            source_ref_id = location.get("source_ref_id")
            if source_ref_id == "source_unavailable":
                if location.get("role") != "source_unavailable":
                    errors.append(
                        f"$.hypotheses[{index}].suspect_locations[{location_index}]: source_unavailable must use matching role"
                    )
            elif isinstance(source_ref_id, str) and source_ref_id not in source_ref_ids:
                errors.append(
                    f"$.hypotheses[{index}].suspect_locations[{location_index}]: unknown source_ref_id {source_ref_id!r}"
                )
        reproduction = hypothesis.get("reproduction_path")
        if isinstance(reproduction, dict):
            reproduction_ref = reproduction.get("reproduction_ref_id")
            if isinstance(reproduction_ref, str) and reproduction_ref not in reproduction_ref_ids:
                errors.append(
                    f"$.hypotheses[{index}].reproduction_path: unknown reproduction_ref_id {reproduction_ref!r}"
                )
            signature_id = reproduction.get("expected_signature_id")
            if expected_signature_id and signature_id != expected_signature_id:
                errors.append(
                    f"$.hypotheses[{index}].reproduction_path.expected_signature_id: "
                    "does not match the immutable host signature"
                )
            stable_attempts = reproduction.get("stable_attempts")
            if (
                expected_stable_attempts is not None
                and stable_attempts != expected_stable_attempts
            ):
                errors.append(
                    f"$.hypotheses[{index}].reproduction_path.stable_attempts: "
                    "does not match the immutable host replay count"
                )
        if tool_ids is not None:
            for test_index, experiment in enumerate(hypothesis.get("falsification_tests", [])):
                if not isinstance(experiment, dict):
                    continue
                tool_id = experiment.get("tool_id")
                if isinstance(tool_id, str) and tool_id not in tool_ids:
                    errors.append(
                        f"$.hypotheses[{index}].falsification_tests[{test_index}].tool_id: "
                        f"unregistered tool {tool_id!r}"
                    )
    return errors


def normalize_hypothesis_report(value: dict[str, Any]) -> dict[str, Any]:
    """Apply narrow, semantics-preserving aliases useful for smaller local models."""

    normalized = copy.deepcopy(value)
    category_aliases = {
        "test_generation_defect": "test_generation",
        "oracle_defect": "validation_oracle",
        "sdk_bug_candidate": "sdk_implementation",
        "infrastructure": "harness_infrastructure",
    }
    unavailable_aliases = {"", "unavailable", "none", "n/a", "source unavailable"}
    hypotheses = normalized.get("hypotheses")
    if not isinstance(hypotheses, list):
        return normalized
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            continue
        category = hypothesis.get("category")
        if isinstance(category, str) and category in category_aliases:
            hypothesis["category"] = category_aliases[category]
        locations = hypothesis.get("suspect_locations")
        if not isinstance(locations, list):
            continue
        if not locations:
            locations.append(
                {
                    "source_ref_id": "source_unavailable",
                    "symbol": "",
                    "line_start": 0,
                    "line_end": 0,
                    "role": "source_unavailable",
                    "rationale": "No trusted source-location evidence was available to this investigator.",
                }
            )
        for location in locations:
            if not isinstance(location, dict):
                continue
            source_ref_id = location.get("source_ref_id")
            if isinstance(source_ref_id, str) and source_ref_id.strip().lower() in unavailable_aliases:
                location["source_ref_id"] = "source_unavailable"
                location["role"] = "source_unavailable"
                location["line_start"] = 0
                location["line_end"] = 0
    return normalized


def bind_host_reproduction_facts(
    value: dict[str, Any],
    *,
    stable_attempts: int,
) -> dict[str, Any]:
    """Overwrite model-authored replay counts with immutable host evidence."""

    bound = copy.deepcopy(value)
    hypotheses = bound.get("hypotheses")
    if not isinstance(hypotheses, list):
        return bound
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            continue
        reproduction = hypothesis.get("reproduction_path")
        if isinstance(reproduction, dict):
            reproduction["stable_attempts"] = stable_attempts
    return bound


def compact_diagnostics(errors: list[str], *, limit: int = 24) -> list[dict[str, str]]:
    return [
        {
            "error_code": "INVESTIGATION_CONTRACT_REJECTED",
            "message": error[:1200],
            "repair_hint": "Return a complete corrected JSON object using only registered evidence and tool IDs.",
        }
        for error in errors[:limit]
    ]


def validate_tool_args(
    args: Any,
    *,
    required: Mapping[str, type],
    optional: Mapping[str, type] | None = None,
) -> list[str]:
    if not isinstance(args, dict):
        return ["tool args must be an object"]
    optional = optional or {}
    errors = _forbidden_fields(args, "$.args")
    unknown = sorted(set(args) - set(required) - set(optional))
    if unknown:
        errors.append(f"unknown tool args: {unknown}")
    for key, expected in required.items():
        if key not in args:
            errors.append(f"missing required tool arg: {key}")
        elif not isinstance(args[key], expected) or (expected is int and isinstance(args[key], bool)):
            errors.append(f"tool arg {key} must be {expected.__name__}")
    for key, expected in optional.items():
        if key in args and (
            not isinstance(args[key], expected) or (expected is int and isinstance(args[key], bool))
        ):
            errors.append(f"tool arg {key} must be {expected.__name__}")
    return errors
