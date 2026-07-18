from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_harness.tools.generate_plugin_registry import generate
from test_harness.tools.harness_capabilities import load_capabilities, supported_recipe_apis
from test_harness.tools.plugin_catalog import PluginCatalogError, discover_plugins
from test_harness.tools.validate_recipe import validate_file

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "test_harness/api_plugins"


def test_pilot_new_api_is_discovered_without_base_registry_entry() -> None:
    base = json.loads(
        (REPO_ROOT / "test_harness/interface_capabilities.json").read_text(encoding="utf-8")
    )
    assert "api_combine_bodies" not in base["apis"]

    records = discover_plugins()
    discovered_apis = [record.api for record in records]
    assert "api_combine_bodies" in discovered_apis
    capabilities = load_capabilities()
    assert "api_combine_bodies" in supported_recipe_apis(capabilities)
    assert capabilities["apis"]["api_combine_bodies"]["plugin"]["hashes"][
        "manifest_sha256"
    ]


def test_plugin_recipe_uses_its_strict_schema() -> None:
    root = PLUGIN_ROOT / "api_combine_bodies/examples"
    assert validate_file(root / "smoke.json", asset_policy="model") == []
    errors = validate_file(root / "unknown_field.invalid.json", asset_policy="model")
    assert len(errors) == 1
    assert "model_command" in errors[0]
    assert "Additional properties" in errors[0]


def test_registry_generator_emits_adapter_entry_and_manifest_hash(tmp_path: Path) -> None:
    index = generate(PLUGIN_ROOT, tmp_path)
    assert index["plugin_count"] >= 1
    entries = (tmp_path / "generated_plugin_entries.inc").read_text(encoding="utf-8")
    metadata = (tmp_path / "generated_plugin_metadata.inc").read_text(encoding="utf-8")
    assert '"api_combine_bodies", &RunApiCombineBodiesPlugin' in entries
    combine = next(plugin for plugin in index["plugins"] if plugin["api"] == "api_combine_bodies")
    assert combine["hashes"]["manifest_sha256"] in metadata


def test_plugin_manifest_path_traversal_fails_closed(tmp_path: Path) -> None:
    plugin = tmp_path / "api_combine_bodies"
    plugin.mkdir()
    source = PLUGIN_ROOT / "api_combine_bodies"
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    unsafe = copy.deepcopy(manifest)
    unsafe["adapter_file"] = "../escape.inc"
    (plugin / "plugin.json").write_text(json.dumps(unsafe), encoding="utf-8")
    (plugin / "recipe.schema.json").write_bytes((source / "recipe.schema.json").read_bytes())

    with pytest.raises(PluginCatalogError, match="parent traversal"):
        discover_plugins(tmp_path)


def test_plugin_manifest_remote_schema_ref_fails_closed(tmp_path: Path) -> None:
    plugin = tmp_path / "api_combine_bodies"
    examples = plugin / "examples"
    examples.mkdir(parents=True)
    source = PLUGIN_ROOT / "api_combine_bodies"
    for name in ("plugin.json", "adapter.inc"):
        (plugin / name).write_bytes((source / name).read_bytes())
    for name in ("smoke.json", "unknown_field.invalid.json"):
        (examples / name).write_bytes((source / "examples" / name).read_bytes())
    schema = json.loads((source / "recipe.schema.json").read_text(encoding="utf-8"))
    schema["properties"]["expectations"] = {"$ref": "https://untrusted.invalid/schema.json"}
    (plugin / "recipe.schema.json").write_text(json.dumps(schema), encoding="utf-8")

    with pytest.raises(PluginCatalogError, match="local fragment reference"):
        discover_plugins(tmp_path)
