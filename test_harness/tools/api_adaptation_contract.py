#!/usr/bin/env python3
"""Trusted identity contract for fixed-archetype API adaptation tasks.

The Message API sees this contract in the prompt, but only host-generated task
metadata is authoritative.  The fixed gate uses it to prevent a valid candidate
for a different API from being promoted for the requested intake.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


CONTRACT_VERSION = 1
BODY_LIST_TO_BODY_REQUIRED_ORACLES = frozenset(
    {"result_bodies", "properties", "topocheck"}
)
CONTRACT_FIELDS = {
    "schema_version",
    "request_id",
    "target_api",
    "adapter_archetype",
    "function_name",
    "function_signature",
    "function_signature_sha256",
    "sdk_header",
    "sdk_modules",
    "input_roles",
    "result_roles",
    "required_oracles",
    "topotrack_mode",
    "intake_sha256",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_adaptation_contract(intake: Mapping[str, Any]) -> dict[str, Any]:
    """Create the immutable host-side contract from validated resolver evidence."""

    function_signature = str(intake["function_signature"])
    return {
        "schema_version": CONTRACT_VERSION,
        "request_id": str(intake["request_id"]),
        "target_api": str(intake["api"]),
        "adapter_archetype": str(intake["adapter_archetype"]),
        "function_name": str(intake["api"]),
        "function_signature": function_signature,
        "function_signature_sha256": sha256_text(function_signature),
        "sdk_header": str(intake["sdk_header"]),
        "sdk_modules": sorted(str(item) for item in intake["sdk_modules"]),
        "input_roles": list(intake["input_roles"]),
        "result_roles": list(intake["result_roles"]),
        "required_oracles": sorted(str(item) for item in intake["required_oracles"]),
        "topotrack_mode": str(intake["topotrack"]["mode"]),
        "intake_sha256": sha256_json(dict(intake)),
    }


def validate_adaptation_contract(
    contract: Any,
    expected_sha256: str,
) -> list[str]:
    """Validate structure and both the contract and embedded signature hashes."""

    if not isinstance(contract, dict):
        return ["trusted adaptation_contract must be an object"]
    errors: list[str] = []
    missing = sorted(CONTRACT_FIELDS - set(contract))
    unknown = sorted(set(contract) - CONTRACT_FIELDS)
    if missing or unknown:
        errors.append(f"adaptation_contract fields mismatch: missing={missing} unknown={unknown}")
    if contract.get("schema_version") != CONTRACT_VERSION:
        errors.append(f"adaptation_contract.schema_version must be {CONTRACT_VERSION}")
    for key in (
        "request_id",
        "target_api",
        "adapter_archetype",
        "function_name",
        "function_signature",
        "function_signature_sha256",
        "sdk_header",
        "topotrack_mode",
        "intake_sha256",
    ):
        if not isinstance(contract.get(key), str) or not contract[key]:
            errors.append(f"adaptation_contract.{key} must be a non-empty string")
    for key in ("sdk_modules", "input_roles", "result_roles", "required_oracles"):
        value = contract.get(key)
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item for item in value)
            or len(set(value)) != len(value)
        ):
            errors.append(f"adaptation_contract.{key} must be a non-empty unique string array")
    if contract.get("adapter_archetype") == "body_list_to_body":
        required_oracles = contract.get("required_oracles")
        missing_oracles = (
            BODY_LIST_TO_BODY_REQUIRED_ORACLES - set(required_oracles)
            if isinstance(required_oracles, list)
            and all(isinstance(item, str) for item in required_oracles)
            else BODY_LIST_TO_BODY_REQUIRED_ORACLES
        )
        if missing_oracles:
            errors.append(
                "body_list_to_body adaptation_contract.required_oracles is missing fixed host "
                f"oracles {sorted(missing_oracles)!r}"
            )
    signature = contract.get("function_signature")
    signature_hash = contract.get("function_signature_sha256")
    if isinstance(signature, str) and isinstance(signature_hash, str):
        if sha256_text(signature) != signature_hash:
            errors.append("adaptation_contract.function_signature_sha256 does not match")
    if isinstance(expected_sha256, str) and len(expected_sha256) == 64:
        if sha256_json(contract) != expected_sha256:
            errors.append("adaptation_contract_sha256 does not match trusted contract")
    else:
        errors.append("adaptation_contract_sha256 must be a 64-character SHA-256 hex digest")
    return errors


def validate_candidate_identity(
    candidate: Any,
    contract: Mapping[str, Any],
) -> list[str]:
    """Bind every candidate identity field represented by the fixed archetype."""

    if not isinstance(candidate, dict):
        return ["candidate root must be an object"]
    errors: list[str] = []
    spec = candidate.get("adapter_spec") if isinstance(candidate.get("adapter_spec"), dict) else {}
    exact = {
        "$.api": (candidate.get("api"), contract.get("target_api")),
        "$.adapter_spec.archetype": (spec.get("archetype"), contract.get("adapter_archetype")),
        "$.adapter_spec.function_name": (spec.get("function_name"), contract.get("function_name")),
        "$.adapter_spec.sdk_header": (spec.get("sdk_header"), contract.get("sdk_header")),
    }
    for path, (actual, expected) in exact.items():
        if actual != expected:
            errors.append(f"{path}={actual!r} does not match trusted value {expected!r}")
    actual_modules = spec.get("sdk_modules")
    expected_modules = contract.get("sdk_modules")
    modules_are_strings = isinstance(actual_modules, list) and all(
        isinstance(item, str) for item in actual_modules
    )
    if not modules_are_strings or sorted(actual_modules) != sorted(expected_modules or []):
        errors.append(
            "$.adapter_spec.sdk_modules does not match trusted SDK module set "
            f"{sorted(expected_modules or [])!r}"
        )
    topotrack = candidate.get("topotrack") if isinstance(candidate.get("topotrack"), dict) else {}
    if topotrack.get("mode") != contract.get("topotrack_mode"):
        errors.append(
            "$.topotrack.mode does not match trusted mode "
            f"{contract.get('topotrack_mode')!r}"
        )
    capability = candidate.get("capability") if isinstance(candidate.get("capability"), dict) else {}
    supported = capability.get("supported_oracles")
    required = set(contract.get("required_oracles") or [])
    if contract.get("adapter_archetype") == "body_list_to_body":
        required.update(BODY_LIST_TO_BODY_REQUIRED_ORACLES)
    supported_are_strings = isinstance(supported, list) and all(
        isinstance(item, str) for item in supported
    )
    if not supported_are_strings or not required.issubset(set(supported)):
        errors.append(
            "$.capability.supported_oracles must contain trusted required_oracles "
            f"{sorted(required)!r}"
        )
    return errors
