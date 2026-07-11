#!/usr/bin/env python3
"""Discover and validate compile-time SGGK harness API plugins.

Plugins are trusted, checked-in harness extensions. Model-authored candidates
may only be materialized under artifacts and must pass a separate promotion
gate before they can become catalog entries.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLUGIN_ROOT = REPO_ROOT / "test_harness" / "api_plugins"
API_ID_RE = re.compile(r"^api_[a-z][a-z0-9_]{1,63}$")
SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
HEADER_RE = re.compile(
    r"^(?!\.{1,2}/)(?!.*?/\.{1,2}(?:/|$))[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)+\.h(?:pp)?$"
)
ALLOWED_MANIFEST_KEYS = {
    "contract_version",
    "api",
    "version",
    "description",
    "archetype",
    "adapter_symbol",
    "adapter_file",
    "recipe_schema",
    "sdk_headers",
    "sdk_modules",
    "input_roles",
    "result_roles",
    "topotrack",
    "capability",
    "examples",
}
ALLOWED_ARCHETYPES = {
    "body_list_to_body",
    "custom_cpp_adapter",
    "unary_body_to_bodies",
    "binary_body_to_bodies",
    "binary_topology_to_topologies",
    "topology_query",
}
ALLOWED_SDK_MODULES = {
    "Foundation",
    "Math",
    "GeomBase",
    "Geometry",
    "GeomAlgo",
    "BSplineAlgo",
    "GeomInt",
    "GeomProject",
    "Topology",
    "ModelAnalysis",
    "ModelingBase",
    "ModelingPrim",
    "Heal",
    "StepExchange",
    "IgesExchange",
    "Boolean",
    "Offset",
    "HLR",
}
ALLOWED_TOPOTRACK_MODES = {"required", "supported", "status_only", "unavailable"}
CAPABILITY_KEYS = {
    "preferred_format",
    "runner_recipe_api",
    "body_required",
    "required_fields",
    "one_of_required_fields",
    "supported_body_builders",
    "source_kinds",
    "supported_oracles",
    "notes",
}


class PluginCatalogError(ValueError):
    """A plugin catalog entry violates a fixed harness boundary."""


@dataclass(frozen=True)
class PluginRecord:
    api: str
    root: Path
    manifest_path: Path
    adapter_path: Path
    recipe_schema_path: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    adapter_sha256: str
    recipe_schema_sha256: str

    @staticmethod
    def _path_text(path: Path) -> str:
        try:
            return path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return str(path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "api": self.api,
            "version": self.manifest["version"],
            "contract_version": self.manifest["contract_version"],
            "archetype": self.manifest["archetype"],
            "adapter_symbol": self.manifest["adapter_symbol"],
            "adapter_file": self._path_text(self.adapter_path),
            "recipe_schema": self._path_text(self.recipe_schema_path),
            "sdk_headers": list(self.manifest["sdk_headers"]),
            "sdk_modules": list(self.manifest["sdk_modules"]),
            "input_roles": list(self.manifest["input_roles"]),
            "result_roles": list(self.manifest["result_roles"]),
            "topotrack": copy.deepcopy(self.manifest["topotrack"]),
            "hashes": {
                "manifest_sha256": self.manifest_sha256,
                "adapter_sha256": self.adapter_sha256,
                "recipe_schema_sha256": self.recipe_schema_sha256,
            },
        }


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise PluginCatalogError(f"JSON root must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_child(root: Path, raw: Any, *, label: str, suffix: str) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise PluginCatalogError(f"{label} must be a non-empty relative path")
    if ".." in Path(raw).parts or "\\" in raw:
        raise PluginCatalogError(f"{label} must not contain parent traversal or backslashes")
    path = (root / raw).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise PluginCatalogError(f"{label} escapes plugin root: {raw}") from exc
    if path.suffix != suffix or not path.is_file():
        raise PluginCatalogError(f"{label} must reference an existing {suffix} file: {raw}")
    return path


def _string_list(value: Any, *, label: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise PluginCatalogError(f"{label} must be {'an' if allow_empty else 'a non-empty'} array")
    if any(not isinstance(item, str) or not item for item in value):
        raise PluginCatalogError(f"{label} must contain only non-empty strings")
    if len(set(value)) != len(value):
        raise PluginCatalogError(f"{label} must not contain duplicates")
    return list(value)


def _validate_schema_refs(value: Any, *, label: str) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_schema_refs(item, label=f"{label}[{index}]")
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        child_label = f"{label}.{key}"
        if key == "$ref":
            if not isinstance(item, str) or not item.startswith("#"):
                raise PluginCatalogError(f"{child_label} must be a local fragment reference")
        _validate_schema_refs(item, label=child_label)


def _validate_capability(api: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PluginCatalogError("capability must be an object")
    unknown = sorted(set(value) - CAPABILITY_KEYS)
    if unknown:
        raise PluginCatalogError(f"capability has unknown fields: {unknown}")
    if value.get("preferred_format") != "flat_recipe":
        raise PluginCatalogError("plugin capability preferred_format must be flat_recipe")
    if value.get("runner_recipe_api") is not True:
        raise PluginCatalogError("plugin capability runner_recipe_api must be true")
    for field in (
        "body_required",
        "required_fields",
        "supported_body_builders",
        "source_kinds",
        "supported_oracles",
        "notes",
    ):
        if field in value:
            _string_list(value[field], label=f"capability.{field}", allow_empty=True)
    required = value.get("required_fields", [])
    if "api" not in required or "case_id" not in required:
        raise PluginCatalogError(f"{api} capability.required_fields must contain api and case_id")
    return copy.deepcopy(value)


def _validate_manifest(plugin_root: Path, manifest_path: Path) -> PluginRecord:
    manifest = _read_object(manifest_path)
    unknown = sorted(set(manifest) - ALLOWED_MANIFEST_KEYS)
    missing = sorted(ALLOWED_MANIFEST_KEYS - set(manifest))
    if unknown or missing:
        raise PluginCatalogError(
            f"{manifest_path}: manifest fields mismatch; missing={missing} unknown={unknown}"
        )
    if manifest.get("contract_version") != 1:
        raise PluginCatalogError(f"{manifest_path}: contract_version must be 1")
    api = manifest.get("api")
    if not isinstance(api, str) or not API_ID_RE.fullmatch(api):
        raise PluginCatalogError(f"{manifest_path}: invalid api id {api!r}")
    if plugin_root.name != api:
        raise PluginCatalogError(f"plugin directory must match api id: {api}")
    version = manifest.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PluginCatalogError(f"{manifest_path}: version must be int >= 1")
    if not isinstance(manifest.get("description"), str) or not manifest["description"].strip():
        raise PluginCatalogError(f"{manifest_path}: description must be non-empty")
    if manifest.get("archetype") not in ALLOWED_ARCHETYPES:
        raise PluginCatalogError(f"{manifest_path}: unsupported archetype")
    symbol = manifest.get("adapter_symbol")
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise PluginCatalogError(f"{manifest_path}: invalid adapter_symbol")

    adapter_path = _safe_child(plugin_root, manifest.get("adapter_file"), label="adapter_file", suffix=".inc")
    schema_path = _safe_child(
        plugin_root,
        manifest.get("recipe_schema"),
        label="recipe_schema",
        suffix=".json",
    )
    headers = _string_list(manifest.get("sdk_headers"), label="sdk_headers")
    if any(not HEADER_RE.fullmatch(header) for header in headers):
        raise PluginCatalogError(f"{manifest_path}: sdk_headers contains an unsafe header")
    modules = _string_list(manifest.get("sdk_modules"), label="sdk_modules")
    unknown_modules = sorted(set(modules) - ALLOWED_SDK_MODULES)
    if unknown_modules:
        raise PluginCatalogError(f"{manifest_path}: unsupported sdk_modules {unknown_modules}")
    _string_list(manifest.get("input_roles"), label="input_roles", allow_empty=True)
    _string_list(manifest.get("result_roles"), label="result_roles", allow_empty=True)
    topotrack = manifest.get("topotrack")
    if (
        not isinstance(topotrack, dict)
        or set(topotrack) != {"mode", "reason"}
        or topotrack.get("mode") not in ALLOWED_TOPOTRACK_MODES
        or not isinstance(topotrack.get("reason"), str)
    ):
        raise PluginCatalogError(f"{manifest_path}: invalid topotrack object")
    _validate_capability(api, manifest.get("capability"))
    examples = manifest.get("examples")
    if not isinstance(examples, dict) or set(examples) != {"positive", "negative"}:
        raise PluginCatalogError(f"{manifest_path}: examples must contain positive and negative")
    for category in ("positive", "negative"):
        for raw in _string_list(examples[category], label=f"examples.{category}"):
            _safe_child(plugin_root, raw, label=f"examples.{category}", suffix=".json")

    schema = _read_object(schema_path)
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise PluginCatalogError(f"{schema_path}: root schema must be a strict object")
    api_schema = schema.get("properties", {}).get("api") if isinstance(schema.get("properties"), dict) else None
    if not isinstance(api_schema, dict) or api_schema.get("const") != api:
        raise PluginCatalogError(f"{schema_path}: properties.api.const must equal {api}")
    required_schema = schema.get("required")
    if not isinstance(required_schema, list) or not {"api", "case_id"}.issubset(required_schema):
        raise PluginCatalogError(f"{schema_path}: required must contain api and case_id")
    _validate_schema_refs(schema, label="$")

    return PluginRecord(
        api=api,
        root=plugin_root,
        manifest_path=manifest_path,
        adapter_path=adapter_path,
        recipe_schema_path=schema_path,
        manifest=manifest,
        manifest_sha256=_sha256(manifest_path),
        adapter_sha256=_sha256(adapter_path),
        recipe_schema_sha256=_sha256(schema_path),
    )


def discover_plugins(plugin_root: str | Path = DEFAULT_PLUGIN_ROOT) -> list[PluginRecord]:
    root = Path(plugin_root).resolve()
    if not root.exists():
        return []
    if not root.is_dir():
        raise PluginCatalogError(f"plugin root is not a directory: {root}")
    records: list[PluginRecord] = []
    seen: set[str] = set()
    for manifest_path in sorted(root.glob("*/plugin.json"), key=lambda path: path.as_posix()):
        record = _validate_manifest(manifest_path.parent.resolve(), manifest_path.resolve())
        if record.api in seen:
            raise PluginCatalogError(f"duplicate plugin api: {record.api}")
        seen.add(record.api)
        records.append(record)
    unexpected = [path for path in root.iterdir() if path.is_dir() and not (path / "plugin.json").is_file()]
    if unexpected:
        raise PluginCatalogError(f"plugin directories missing plugin.json: {[path.name for path in unexpected]}")
    return records


def plugin_map(plugin_root: str | Path = DEFAULT_PLUGIN_ROOT) -> dict[str, PluginRecord]:
    return {record.api: record for record in discover_plugins(plugin_root)}


def merge_capabilities(
    capabilities: dict[str, Any],
    plugin_root: str | Path = DEFAULT_PLUGIN_ROOT,
) -> dict[str, Any]:
    merged = copy.deepcopy(capabilities)
    apis = merged.setdefault("apis", {})
    families = merged.setdefault("interface_families", {})
    if not isinstance(apis, dict) or not isinstance(families, dict):
        raise PluginCatalogError("base capabilities must contain apis and interface_families objects")
    for record in discover_plugins(plugin_root):
        if record.api in apis:
            raise PluginCatalogError(f"plugin api conflicts with base capability: {record.api}")
        capability = _validate_capability(record.api, record.manifest["capability"])
        capability["plugin"] = record.as_dict()
        apis[record.api] = capability
        family_id = f"plugin_{record.api}"
        if family_id in families:
            raise PluginCatalogError(f"plugin interface family conflicts: {family_id}")
        families[family_id] = {
            "title": f"Plugin {record.api}",
            "target_apis": [record.api],
            "geometry_families": ["plugin"],
            "default_output_kind": "flat_recipe",
            "description": record.manifest["description"],
        }
    merged["plugin_catalog"] = {
        "schema_version": 1,
        "plugins": [record.as_dict() for record in discover_plugins(plugin_root)],
    }
    return merged
