from __future__ import annotations

import json

import pytest

from test_harness.ui.settings import MemorySecretStore, UiSettingsError, UiSettingsStore


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
