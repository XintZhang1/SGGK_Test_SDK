#!/usr/bin/env python3
"""Build a JSON-only Qwen task for a fixed-archetype new API adapter candidate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from api_adaptation_contract import (
    BODY_LIST_TO_BODY_REQUIRED_ORACLES,
    build_adaptation_contract,
    sha256_json,
)
from plugin_catalog import ALLOWED_SDK_MODULES, API_ID_RE, HEADER_RE, plugin_map


REPO_ROOT = Path(__file__).resolve().parents[2]
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SUPPORTED_ARCHETYPES = {"body_list_to_body"}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("API intake root must be an object")
    return value


def _validate(intake: dict[str, Any], sdk_dir: Path | None, allow_existing: bool) -> list[str]:
    errors: list[str] = []
    required = {
        "schema_version",
        "request_id",
        "api",
        "sdk_header",
        "sdk_modules",
        "function_signature",
        "adapter_archetype",
        "behavior",
        "input_roles",
        "result_roles",
        "required_oracles",
        "smoke_guidance",
        "topotrack",
    }
    unknown = sorted(set(intake) - required)
    missing = sorted(required - set(intake))
    if unknown or missing:
        errors.append(f"intake fields mismatch: missing={missing} unknown={unknown}")
    if intake.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    request_id = intake.get("request_id")
    if not isinstance(request_id, str) or not SAFE_ID_RE.fullmatch(request_id):
        errors.append("request_id is invalid")
    api = intake.get("api")
    if not isinstance(api, str) or not API_ID_RE.fullmatch(api):
        errors.append("api is invalid")
    if not allow_existing and isinstance(api, str) and api in plugin_map():
        errors.append(f"api {api} already has a checked-in plugin")
    header = intake.get("sdk_header")
    if not isinstance(header, str) or not HEADER_RE.fullmatch(header):
        errors.append("sdk_header is invalid")
    modules = intake.get("sdk_modules")
    if not isinstance(modules, list) or not modules or any(not isinstance(item, str) for item in modules):
        errors.append("sdk_modules must be a non-empty string array")
    elif len(set(modules)) != len(modules):
        errors.append("sdk_modules must not contain duplicates")
    elif set(modules) - ALLOWED_SDK_MODULES:
        errors.append(f"sdk_modules contains unsupported modules: {sorted(set(modules) - ALLOWED_SDK_MODULES)}")
    if intake.get("adapter_archetype") not in SUPPORTED_ARCHETYPES:
        errors.append(f"adapter_archetype must be one of {sorted(SUPPORTED_ARCHETYPES)}")
    if intake.get("input_roles") != ["target", "tool"] or intake.get("result_roles") != ["result"]:
        errors.append("body_list_to_body requires input_roles=[target, tool] and result_roles=[result]")
    required_oracles = intake.get("required_oracles")
    if (
        not isinstance(required_oracles, list)
        or not required_oracles
        or any(not isinstance(item, str) or not item for item in required_oracles)
        or len(set(required_oracles)) != len(required_oracles)
    ):
        errors.append("required_oracles must be a non-empty unique string array")
    elif not BODY_LIST_TO_BODY_REQUIRED_ORACLES.issubset(set(required_oracles)):
        errors.append(
            "body_list_to_body required_oracles must contain "
            f"{sorted(BODY_LIST_TO_BODY_REQUIRED_ORACLES)}"
        )
    for key in ("function_signature", "behavior", "smoke_guidance"):
        if not isinstance(intake.get(key), str) or not intake[key].strip():
            errors.append(f"{key} must be non-empty")
    if isinstance(api, str) and isinstance(intake.get("function_signature"), str):
        if api not in intake["function_signature"]:
            errors.append("function_signature must name the requested api")
    topotrack = intake.get("topotrack")
    if not isinstance(topotrack, dict) or set(topotrack) != {"mode", "reason"}:
        errors.append("topotrack must contain mode and reason")
    elif (
        topotrack.get("mode") not in {"unavailable", "status_only"}
        or not isinstance(topotrack.get("reason"), str)
        or not topotrack["reason"].strip()
    ):
        errors.append("topotrack mode/reason is invalid for body_list_to_body")
    if sdk_dir is not None and isinstance(header, str) and isinstance(api, str):
        header_path = (sdk_dir / "include" / header).resolve()
        try:
            header_path.relative_to((sdk_dir / "include").resolve())
        except ValueError:
            errors.append("sdk_header escapes SDK include root")
        else:
            if not header_path.is_file():
                errors.append(f"SDK header does not exist: {header}")
            elif api not in header_path.read_text(encoding="utf-8-sig", errors="replace"):
                errors.append(f"SDK header does not contain function identifier {api}")
    return errors


def build(intake_path: Path, out: Path, sdk_dir: Path | None, allow_existing: bool) -> dict[str, Any]:
    intake = _read(intake_path)
    errors = _validate(intake, sdk_dir, allow_existing)
    if errors:
        return {"schema_version": 1, "ok": False, "errors": errors}
    candidate_schema = _read(REPO_ROOT / "test_harness/schemas/api_plugin_candidate.schema.json")
    reference = _read(
        REPO_ROOT / "test_harness/api_plugin_candidates/api_combine_bodies.example.json"
    )
    request_id = str(intake["request_id"])
    adaptation_contract = build_adaptation_contract(intake)
    adaptation_contract_sha256 = sha256_json(adaptation_contract)
    prompt = f"""# SGGK fixed-archetype API adapter task

Return exactly one JSON object in message.content. No markdown or explanation.

You are authoring an untrusted api_plugin_candidate spec, not C++, a patch, a command, or CMake.
The host generates C++ from a fixed template, materializes each candidate under artifacts, builds it
in an isolated source copy, validates positive/negative schemas, queries the compiled adapter registry,
and runs the smoke recipe three times. Never return command, argv, executable, runner, cwd, env, shell,
path maps, source code, CMake, or link flags.

## Trusted API intake

{json.dumps(intake, indent=2, ensure_ascii=False)}

## Host acceptance identity

The fixed gate will bind the candidate to this exact trusted contract SHA-256:
`{adaptation_contract_sha256}`. Candidate API, archetype, function, SDK header/modules,
TopoTrack mode, and required oracles must match the intake; another valid API is rejected.

## Required candidate schema

{json.dumps(candidate_schema, indent=2, ensure_ascii=False)}

## Reviewed archetype example

{json.dumps(reference, indent=2, ensure_ascii=False)}

Adapt the reviewed example to the trusted intake. Keep recipe_schema strict with
additionalProperties=false. The smoke recipe must pass; the negative recipe must fail only because
of a misspelled/unknown recipe field, not because it contains commands. Use semantic property and
TopoCheck oracles rather than API return status alone.
"""
    prompt_path = out / "prompts" / f"{request_id}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    expected_output = out / "model_outputs" / f"{request_id}.json"
    manifest = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "task_count": 1,
        "tasks": [
            {
                "task_type": "api_adaptation",
                "task_id": request_id,
                "request_id": request_id,
                "intake_path": intake_path.resolve().relative_to(REPO_ROOT).as_posix(),
                "prompt_path": prompt_path.resolve().relative_to(REPO_ROOT).as_posix(),
                "expected_output_path": expected_output.resolve().relative_to(REPO_ROOT).as_posix(),
                "output_contract": {
                    "type": "json_object",
                    "kind_field": "kind",
                    "allowed_kinds": ["api_plugin_candidate"],
                },
                "target_api": intake["api"],
                "adapter_archetype": intake["adapter_archetype"],
                "intake_sha256": adaptation_contract["intake_sha256"],
                "adaptation_contract": adaptation_contract,
                "adaptation_contract_sha256": adaptation_contract_sha256,
                "allowed_campaign_profiles": {},
            }
        ],
    }
    manifest_path = out / "model_task_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "schema_version": 1,
        "ok": True,
        "manifest": str(manifest_path),
        "prompt": str(prompt_path),
        "expected_output": str(expected_output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("intake")
    parser.add_argument("--out", required=True)
    parser.add_argument("--sdk-dir", default=os.environ.get("SGGK_SDK_DIR", ""))
    parser.add_argument("--allow-existing-for-regression", action="store_true")
    args = parser.parse_args()
    try:
        sdk_dir = Path(args.sdk_dir).resolve() if args.sdk_dir else None
        out = Path(args.out).resolve()
        out.relative_to((REPO_ROOT / "artifacts").resolve())
        result = build(
            Path(args.intake).resolve(),
            out,
            sdk_dir,
            args.allow_existing_for_regression,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"schema_version": 1, "ok": False, "errors": [str(exc)]}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
