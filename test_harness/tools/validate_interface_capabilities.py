#!/usr/bin/env python3
"""Validate capability registry structure and cross-layer implementation claims."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO_ROOT / "test_harness" / "interface_capabilities.json"
RUNNER_SOURCE = REPO_ROOT / "test_harness" / "src" / "sggk_case_runner.cpp"


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def add_unknown(errors: list[str], label: str, values: list[str], known: set[str]) -> None:
    for value in values:
        if value not in known:
            errors.append(f"{label} references unknown id: {value}")


def runner_dispatches(api: str, source: str) -> bool:
    quoted = re.escape(api)
    if re.search(rf'\{{\s*"{quoted}"\s*,\s*&[A-Za-z_][A-Za-z0-9_]*\s*\}}', source):
        return True
    if re.search(rf'recipe\.api\s*==\s*"{quoted}"', source):
        return True
    if re.search(rf'recipe\.api\s*!=\s*"{quoted}"', source):
        return True
    return False


def validate_registry(
    registry: dict[str, Any],
    *,
    repo_root: Path = REPO_ROOT,
    runner_source: str | None = None,
    implemented_recipe_apis: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if registry.get("schema_version") != 2:
        errors.append("schema_version must be 2")

    required_objects = (
        "provenance_source_types",
        "output_kinds",
        "interface_families",
        "run_profiles",
        "example_packs",
        "apis",
        "body_builders",
        "oracles",
    )
    for key in required_objects:
        if not isinstance(registry.get(key), dict):
            errors.append(f"{key} must be an object")
    if errors:
        return errors

    output_kinds = set(registry["output_kinds"])
    families = set(registry["interface_families"])
    apis = set(registry["apis"])
    builders = set(registry["body_builders"])
    oracles = set(registry["oracles"])

    if implemented_recipe_apis is None:
        from validate_recipe import IMPLEMENTED_RECIPE_APIS

        implemented_recipe_apis = set(IMPLEMENTED_RECIPE_APIS)
    if runner_source is None:
        runner_source = RUNNER_SOURCE.read_text(encoding="utf-8-sig")

    for api, record in registry["apis"].items():
        if not isinstance(record, dict):
            errors.append(f"apis.{api} must be an object")
            continue
        preferred = record.get("preferred_format")
        if preferred not in output_kinds:
            errors.append(f"apis.{api}.preferred_format references unknown output kind: {preferred}")
        add_unknown(errors, f"apis.{api}.supported_body_builders", string_list(record.get("supported_body_builders")), builders)
        add_unknown(errors, f"apis.{api}.source_kinds", string_list(record.get("source_kinds")), builders)
        add_unknown(errors, f"apis.{api}.supported_oracles", string_list(record.get("supported_oracles")), oracles)
        if record.get("runner_recipe_api") is True:
            if api not in implemented_recipe_apis:
                errors.append(f"apis.{api} claims runner_recipe_api but Python validator does not implement it")
            if not runner_dispatches(api, runner_source):
                errors.append(f"apis.{api} claims runner_recipe_api but C++ runner has no dispatch")

    for family, record in registry["interface_families"].items():
        if not isinstance(record, dict):
            errors.append(f"interface_families.{family} must be an object")
            continue
        add_unknown(errors, f"interface_families.{family}.target_apis", string_list(record.get("target_apis")), apis)
        add_unknown(errors, f"interface_families.{family}.oracles_any", string_list(record.get("oracles_any")), oracles)
        default_kind = record.get("default_output_kind")
        if default_kind not in output_kinds:
            errors.append(f"interface_families.{family}.default_output_kind references unknown output kind: {default_kind}")

    for pack, record in registry["example_packs"].items():
        if not isinstance(record, dict):
            errors.append(f"example_packs.{pack} must be an object")
            continue
        family = record.get("interface_family")
        if family not in families:
            errors.append(f"example_packs.{pack}.interface_family references unknown id: {family}")
        match = record.get("match") if isinstance(record.get("match"), dict) else {}
        add_unknown(errors, f"example_packs.{pack}.match.target_apis", string_list(match.get("target_apis")), apis)
        add_unknown(errors, f"example_packs.{pack}.match.oracles_any", string_list(match.get("oracles_any")), oracles)
        for field in ("manifest_path", "path", "example_dsl_path"):
            value = record.get(field)
            if isinstance(value, str) and value and not (repo_root / value).is_file():
                errors.append(f"example_packs.{pack}.{field} does not exist: {value}")
        for field in ("example_paths", "negative_example_paths"):
            for value in string_list(record.get(field)):
                if not (repo_root / value).is_file():
                    errors.append(f"example_packs.{pack}.{field} does not exist: {value}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    path = Path(args.registry)
    try:
        registry = read_object(path)
        errors = validate_registry(registry)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors = [str(exc)]
    report = {"ok": not errors, "registry": str(path), "error_count": len(errors), "errors": errors}
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
