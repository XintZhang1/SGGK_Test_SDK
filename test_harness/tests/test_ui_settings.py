from __future__ import annotations

import json

import pytest

from test_harness.ui.settings import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    MemorySecretStore,
    UiSettingsError,
    UiSettingsStore,
)


def test_ui_settings_default_to_siliconflow_glm52(tmp_path) -> None:
    store = UiSettingsStore(tmp_path, secret_store=MemorySecretStore())

    settings = store.load()

    assert settings.profile == "siliconflow"
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.model == DEFAULT_MODEL
    assert settings.thinking_mode == "disabled"


def test_ui_settings_use_siliconflow_environment_key_without_persisting_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "environment-secret")
    store = UiSettingsStore(tmp_path, secret_store=MemorySecretStore())

    assert store.api_key("siliconflow") == "environment-secret"
    assert store.public()["api_key_configured"] is True
    assert not store.path.exists()


def test_ui_settings_keep_key_out_of_json(tmp_path) -> None:
    secrets = MemorySecretStore()
    store = UiSettingsStore(tmp_path, secret_store=secrets)
    saved = store.save(
        {
            "profile": "intranet",
            "base_url": "http://qwen.intranet/v1/",
            "model": "Qwen3.6-35B-A3B",
            "candidate_count": 2,
            "candidate_parallelism": 2,
        },
        api_key="top-secret",
    )

    assert saved.base_url == "http://qwen.intranet/v1"
    assert store.api_key("intranet") == "top-secret"
    assert "top-secret" not in store.path.read_text(encoding="utf-8")
    assert json.loads(store.path.read_text(encoding="utf-8"))["model"] == "Qwen3.6-35B-A3B"


def test_ui_settings_expose_only_intranet_profile(tmp_path) -> None:
    store = UiSettingsStore(tmp_path, secret_store=MemorySecretStore())
    with pytest.raises(UiSettingsError, match="profile must be"):
        store.save({"profile": "external", "base_url": "https://example.invalid/v1", "model": "x"})


def test_ui_settings_reject_credentials_in_url(tmp_path) -> None:
    store = UiSettingsStore(tmp_path, secret_store=MemorySecretStore())
    with pytest.raises(UiSettingsError, match="cannot contain credentials"):
        store.save({"base_url": "http://user:password@qwen.intranet/v1", "model": "x"})


def test_ui_settings_lock_siliconflow_endpoint(tmp_path) -> None:
    store = UiSettingsStore(tmp_path, secret_store=MemorySecretStore())
    with pytest.raises(UiSettingsError, match="api.siliconflow.cn"):
        store.save({"base_url": "https://attacker.invalid/v1"})


def test_ui_settings_migrate_legacy_intranet_ui_config_to_external_defaults(tmp_path) -> None:
    store = UiSettingsStore(tmp_path, secret_store=MemorySecretStore())
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "intranet",
                "base_url": "http://qwen.intranet/v1",
                "model": "Qwen3.6-35B-A3B",
                "source_root": "D:/private/source",
            }
        ),
        encoding="utf-8",
    )

    settings = store.load()

    assert settings.profile == "siliconflow"
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.model == DEFAULT_MODEL
    assert settings.thinking_mode == "disabled"
    assert settings.source_root == ""


def test_ui_settings_validate_nx_probe_timeout_without_requiring_nx_path(tmp_path) -> None:
    store = UiSettingsStore(tmp_path, secret_store=MemorySecretStore())
    saved = store.save({"nx_root_dir": str(tmp_path / "not-installed-yet"), "nx_probe_timeout_seconds": 60})
    assert saved.nx_root_dir.endswith("not-installed-yet")
    with pytest.raises(UiSettingsError, match="between 5 and 600"):
        store.save({"nx_probe_timeout_seconds": 2})


def test_ui_settings_migrate_v2_external_thinking_default_to_disabled(tmp_path) -> None:
    store = UiSettingsStore(tmp_path, secret_store=MemorySecretStore())
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": "siliconflow",
                "base_url": DEFAULT_BASE_URL,
                "model": DEFAULT_MODEL,
                "thinking_mode": "enabled",
            }
        ),
        encoding="utf-8",
    )

    settings = store.load()

    assert settings.schema_version == 3
    assert settings.thinking_mode == "disabled"


def test_ui_settings_reject_invalid_schema_version(tmp_path) -> None:
    store = UiSettingsStore(tmp_path, secret_store=MemorySecretStore())
    store.path.parent.mkdir(parents=True)
    store.path.write_text('{"schema_version": "broken"}', encoding="utf-8")

    with pytest.raises(UiSettingsError, match="schema_version"):
        store.load()
