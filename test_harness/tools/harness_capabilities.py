#!/usr/bin/env python3
"""Load the machine-readable SGGK harness capability registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CAPABILITIES_PATH = Path(__file__).resolve().parents[1] / "interface_capabilities.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_capabilities(path: str | Path | None = None) -> dict[str, Any]:
    loaded = read_json(Path(path) if path else CAPABILITIES_PATH)
    if not isinstance(loaded, dict):
        raise ValueError("interface capabilities registry must be a JSON object")
    if not isinstance(loaded.get("apis"), dict):
        raise ValueError("interface capabilities registry must contain an apis object")
    if path is not None:
        return loaded
    try:
        from plugin_catalog import merge_capabilities
    except ModuleNotFoundError:  # pragma: no cover - package import fallback
        from test_harness.tools.plugin_catalog import merge_capabilities
    return merge_capabilities(loaded)


def _string_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [key for key in value if isinstance(key, str) and key]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def supported_apis(capabilities: dict[str, Any] | None = None) -> list[str]:
    registry = capabilities or load_capabilities()
    return _string_keys(registry.get("apis"))


def supported_recipe_apis(capabilities: dict[str, Any] | None = None) -> list[str]:
    registry = capabilities or load_capabilities()
    apis = registry.get("apis") if isinstance(registry.get("apis"), dict) else {}
    return [
        name
        for name, record in apis.items()
        if isinstance(name, str) and isinstance(record, dict) and record.get("runner_recipe_api") is True
    ]


def supported_body_builders(capabilities: dict[str, Any] | None = None) -> list[str]:
    registry = capabilities or load_capabilities()
    return _string_keys(registry.get("body_builders"))


def supported_oracles(capabilities: dict[str, Any] | None = None) -> list[str]:
    registry = capabilities or load_capabilities()
    return _string_keys(registry.get("oracles"))


def interface_families(capabilities: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    registry = capabilities or load_capabilities()
    raw_families = registry.get("interface_families") if isinstance(registry.get("interface_families"), dict) else {}
    return {
        name: record
        for name, record in raw_families.items()
        if isinstance(name, str) and isinstance(record, dict)
    }


def run_profiles(capabilities: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    registry = capabilities or load_capabilities()
    raw_profiles = registry.get("run_profiles") if isinstance(registry.get("run_profiles"), dict) else {}
    return {
        name: record
        for name, record in raw_profiles.items()
        if isinstance(name, str) and isinstance(record, dict)
    }


def provenance_source_types(capabilities: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    registry = capabilities or load_capabilities()
    raw_sources = registry.get("provenance_source_types") if isinstance(registry.get("provenance_source_types"), dict) else {}
    return {
        name: record
        for name, record in raw_sources.items()
        if isinstance(name, str) and isinstance(record, dict)
    }


def provenance_source_metadata(source_type: str, capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = capabilities or load_capabilities()
    record = provenance_source_types(registry).get(source_type)
    if isinstance(record, dict):
        result = dict(record)
        result["id"] = source_type
        result["known"] = True
        return result
    return {
        "id": source_type,
        "known": False,
        "title": source_type or "unknown",
        "category": "unknown",
        "allowed_contexts": [],
        "production_boundary": "Source type is not listed in interface_capabilities.json.",
        "description": "Unknown provenance source type.",
    }


def example_pack_interface_family(pack_id: str, capabilities: dict[str, Any] | None = None) -> str:
    registry = capabilities or load_capabilities()
    packs = registry.get("example_packs") if isinstance(registry.get("example_packs"), dict) else {}
    pack = packs.get(pack_id) if isinstance(pack_id, str) else None
    if isinstance(pack, dict) and isinstance(pack.get("interface_family"), str):
        return pack["interface_family"]
    return ""


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def derive_interface_family(
    form: dict[str, Any],
    selected_example_pack: str = "",
    capabilities: dict[str, Any] | None = None,
) -> str:
    registry = capabilities or load_capabilities()
    family_from_pack = example_pack_interface_family(selected_example_pack, registry)
    if family_from_pack:
        return family_from_pack

    target_api = form.get("target_api")
    geometry = form.get("geometry") if isinstance(form.get("geometry"), dict) else {}
    geometry_family = geometry.get("family")
    form_oracles = set(_string_list(form.get("oracles")))
    for family_id, record in interface_families(registry).items():
        target_apis = set(_string_list(record.get("target_apis")))
        geometry_families = set(_string_list(record.get("geometry_families")))
        oracle_terms = set(_string_list(record.get("oracles_any")))
        if target_apis and target_api not in target_apis:
            continue
        if oracle_terms and form_oracles.intersection(oracle_terms):
            return family_id
        if geometry_families and geometry_family in geometry_families:
            return family_id
        if target_apis and not geometry_families and not oracle_terms:
            return family_id
    return "harness_extension" if target_api not in supported_apis(registry) else str(target_api or "unknown")


def run_profile_metadata(profile_id: str, capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = capabilities or load_capabilities()
    record = run_profiles(registry).get(profile_id)
    if isinstance(record, dict):
        result = dict(record)
        result["id"] = profile_id
        result["known"] = True
        return result
    return {
        "id": profile_id,
        "known": False,
        "title": profile_id or "unknown",
        "scale": "unknown",
        "requires_campaign_profile": True,
        "description": "Run profile is not listed in interface_capabilities.json.",
    }


def api_guidance(capabilities: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    registry = capabilities or load_capabilities()
    apis = registry.get("apis") if isinstance(registry.get("apis"), dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for name, record in apis.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            continue
        notes = record.get("notes") if isinstance(record.get("notes"), list) else []
        body_required = record.get("body_required") if isinstance(record.get("body_required"), list) else []
        result[name] = {
            "preferred_format": str(record.get("preferred_format") or "needs_harness_extension"),
            "body_required": [item for item in body_required if isinstance(item, str)],
            "notes": [item for item in notes if isinstance(item, str)],
        }
    result.setdefault(
        "needs_harness_extension",
        {
            "preferred_format": "needs_harness_extension",
            "body_required": [],
            "notes": [
                "Return an extension request instead of pretending the runner supports the API.",
                "Include the minimal new recipe shape and one concrete smoke case.",
            ],
        },
    )
    return result


def oracle_guidance(capabilities: dict[str, Any] | None = None) -> dict[str, str]:
    registry = capabilities or load_capabilities()
    oracles = registry.get("oracles") if isinstance(registry.get("oracles"), dict) else {}
    result: dict[str, str] = {}
    for name, record in oracles.items():
        if isinstance(name, str) and isinstance(record, dict) and isinstance(record.get("guidance"), str):
            result[name] = record["guidance"]
    return result
