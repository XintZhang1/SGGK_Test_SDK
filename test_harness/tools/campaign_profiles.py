#!/usr/bin/env python3
"""Typed campaign profile registry and deterministic argv resolver.

Model output can select ``profile_id`` and bounded scalar arguments only. Tool
paths, runner/data/output bindings, command text, cwd, environment, and shell
mode are owned by fixed local code.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


class CampaignRequestError(ValueError):
    """Raised when a campaign request or fixed binding is invalid."""


@dataclass(frozen=True)
class CampaignProfile:
    profile_id: str
    run_profile_id: str
    tool: str
    args_schema: dict[str, Any]
    defaults: dict[str, Any]
    required_bindings: frozenset[str]


ABC_MASS_RECUT_ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "target_cases": {"type": "integer", "minimum": 1, "maximum": 200000},
        "preset": {"type": "string", "enum": ["smoke", "standard", "stress"]},
        "shard_count": {"type": "integer", "minimum": 1, "maximum": 4096},
        "shard_index": {"type": "integer", "minimum": 0, "maximum": 4095},
        "jobs": {"type": "integer", "minimum": 1, "maximum": 16},
        "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 3600},
        "resume": {"type": "boolean"},
    },
    "required": [],
}

CAMPAIGN_PROFILES: dict[str, CampaignProfile] = {
    "abc_boolean_mass_recut": CampaignProfile(
        profile_id="abc_boolean_mass_recut",
        run_profile_id="corpus",
        tool="test_harness/tools/run_abc_boolean_mass_recut.py",
        args_schema=ABC_MASS_RECUT_ARGS_SCHEMA,
        defaults={
            "target_cases": 100000,
            "preset": "stress",
            "shard_count": 100,
            "shard_index": 0,
            "jobs": 1,
            "timeout_seconds": 180.0,
            "resume": True,
        },
        required_bindings=frozenset({"runner", "dataset", "out"}),
    ),
}

FORBIDDEN_CANDIDATE_FIELDS = frozenset(
    {
        "command",
        "commands",
        "tool",
        "executable",
        "runner",
        "dataset",
        "out",
        "cwd",
        "env",
        "environment",
        "shell",
    }
)
CAMPAIGN_REQUEST_FIELDS = frozenset({"kind", "profile_id", "args", "notes", "expected_artifacts"})
UNSAFE_PATH_TEXT = re.compile(r"[\x00-\x1f<>|;&`]")


def profile_manifest(profile_id: str) -> dict[str, Any]:
    try:
        profile = CAMPAIGN_PROFILES[profile_id]
    except KeyError as exc:
        raise CampaignRequestError(f"unknown campaign profile {profile_id!r}") from exc
    return {
        "profile_id": profile.profile_id,
        "run_profile_id": profile.run_profile_id,
        "args_schema": profile.args_schema,
        "defaults": profile.defaults,
    }


def allowed_campaign_profiles(profile_ids: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
    return {profile_id: profile_manifest(profile_id) for profile_id in profile_ids}


def _validate_scalar(name: str, value: Any, schema: dict[str, Any]) -> str | None:
    expected = schema.get("type")
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"args.{name} must be an integer"
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"args.{name} must be a number"
    elif expected == "string":
        if not isinstance(value, str):
            return f"args.{name} must be a string"
    elif expected == "boolean":
        if not isinstance(value, bool):
            return f"args.{name} must be a boolean"
    else:
        return f"args.{name} has unsupported local schema type {expected!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"args.{name} must be one of {schema['enum']}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"args.{name} must be >= {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"args.{name} must be <= {schema['maximum']}"
    return None


def validate_campaign_request(
    value: Any,
    allowed_profiles: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(value, dict):
        return None, ["campaign_request must be one JSON object"]
    errors: list[str] = []
    forbidden = sorted(field for field in FORBIDDEN_CANDIDATE_FIELDS if field in value)
    if forbidden:
        errors.append(f"campaign_request contains forbidden executable/binding fields: {forbidden}")
    unknown_fields = sorted(str(field) for field in value if field not in CAMPAIGN_REQUEST_FIELDS)
    if unknown_fields:
        errors.append(f"campaign_request contains unknown fields: {unknown_fields}")
    if value.get("kind") != "campaign_request":
        errors.append("kind must be campaign_request")
    profile_id = value.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        errors.append("profile_id must be a non-empty string")
        return None, errors
    if allowed_profiles is not None and profile_id not in allowed_profiles:
        errors.append(f"profile_id {profile_id!r} is not allowed for this task")
    profile = CAMPAIGN_PROFILES.get(profile_id)
    if profile is None:
        errors.append(f"profile_id {profile_id!r} is not registered in fixed local code")
        return None, errors

    args = value.get("args", {})
    if not isinstance(args, dict):
        errors.append("args must be a JSON object")
        return None, errors
    properties = profile.args_schema["properties"]
    unknown_args = sorted(str(key) for key in args if key not in properties)
    if unknown_args:
        errors.append(f"unknown campaign args: {unknown_args}")
    normalized_args = dict(profile.defaults)
    for name, raw in args.items():
        schema = properties.get(name)
        if schema is None:
            continue
        issue = _validate_scalar(name, raw, schema)
        if issue:
            errors.append(issue)
        else:
            normalized_args[name] = raw
    if normalized_args["shard_index"] >= normalized_args["shard_count"]:
        errors.append("args.shard_index must be less than args.shard_count")

    notes = value.get("notes", [])
    expected_artifacts = value.get("expected_artifacts", [])
    if not isinstance(notes, list) or any(not isinstance(item, str) for item in notes):
        errors.append("notes must be a list of strings")
    if not isinstance(expected_artifacts, list) or any(not isinstance(item, str) for item in expected_artifacts):
        errors.append("expected_artifacts must be a list of strings")
    if errors:
        return None, errors
    return {
        "kind": "campaign_request",
        "profile_id": profile_id,
        "args": normalized_args,
        "notes": notes,
        "expected_artifacts": expected_artifacts,
    }, []


def _safe_binding(name: str, raw: Any, roots: frozenset[str]) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CampaignRequestError(f"binding {name} must be a non-empty repository-relative path")
    normalized = raw.replace("\\", "/").strip()
    unsafe = (
        UNSAFE_PATH_TEXT.search(normalized)
        or re.match(r"^[A-Za-z]:/", normalized)
        or normalized.startswith(("/", "//"))
    )
    if unsafe:
        raise CampaignRequestError(f"binding {name} is not a safe repository-relative path")
    pure = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in pure.parts) or not pure.parts or pure.parts[0] not in roots:
        raise CampaignRequestError(f"binding {name} must stay under {sorted(roots)}")
    return normalized


def resolve_campaign_argv(
    request: dict[str, Any],
    *,
    allowed_profiles: dict[str, Any],
    bindings: dict[str, Any],
) -> list[str]:
    normalized, errors = validate_campaign_request(request, allowed_profiles)
    if errors or normalized is None:
        raise CampaignRequestError("; ".join(errors))
    profile = CAMPAIGN_PROFILES[normalized["profile_id"]]
    missing = sorted(profile.required_bindings - set(bindings))
    unknown = sorted(set(bindings) - profile.required_bindings)
    if missing:
        raise CampaignRequestError(f"missing fixed campaign bindings: {missing}")
    if unknown:
        raise CampaignRequestError(f"unknown fixed campaign bindings: {unknown}")
    runner = _safe_binding("runner", bindings["runner"], frozenset({"build", "artifacts"}))
    dataset = _safe_binding("dataset", bindings["dataset"], frozenset({"artifacts", "test_harness"}))
    out = _safe_binding("out", bindings["out"], frozenset({"artifacts"}))
    args = normalized["args"]
    argv = [
        sys.executable,
        profile.tool,
        "--runner",
        runner,
        "--dataset",
        dataset,
        "--out",
        out,
        "--target-cases",
        str(args["target_cases"]),
        "--preset",
        str(args["preset"]),
        "--shard-count",
        str(args["shard_count"]),
        "--shard-index",
        str(args["shard_index"]),
        "--jobs",
        str(args["jobs"]),
        "--timeout",
        str(args["timeout_seconds"]),
    ]
    if args["resume"]:
        argv.append("--resume")
    return argv
