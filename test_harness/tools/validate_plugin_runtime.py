#!/usr/bin/env python3
"""Compare validated plugin manifests with adapters compiled into a runner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

from plugin_catalog import PluginCatalogError, discover_plugins


def validate_runtime(runner: Path, *, timeout: float = 30.0) -> dict[str, Any]:
    completed = subprocess.run(
        [str(runner.resolve()), "--list-adapters-json"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        shell=False,
    )
    errors: list[str] = []
    payload: Any = {}
    if completed.returncode != 0:
        errors.append(f"runner adapter query returned {completed.returncode}: {completed.stderr[-1000:]}")
    else:
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            errors.append(f"runner adapter query did not return JSON: {exc}")
    adapters: dict[str, dict[str, Any]] = {}
    raw_adapters = payload.get("adapters") if isinstance(payload, dict) else None
    if not isinstance(raw_adapters, list):
        errors.append("runner adapter catalog must contain an adapters array")
        raw_adapters = []
    for item in raw_adapters:
        if not isinstance(item, dict) or not isinstance(item.get("api"), str):
            errors.append("runner adapter catalog contains an invalid entry")
            continue
        api = item["api"]
        if api in adapters:
            errors.append(f"runner adapter catalog contains duplicate api {api}")
        adapters[api] = item
    plugins = discover_plugins()
    for plugin in plugins:
        actual = adapters.get(plugin.api)
        if actual is None:
            errors.append(f"runner is missing plugin adapter {plugin.api}")
            continue
        if actual.get("source") != "plugin":
            errors.append(f"runner adapter {plugin.api} is not marked as plugin")
        if actual.get("manifest_sha256") != plugin.manifest_sha256:
            errors.append(f"runner adapter {plugin.api} manifest hash is stale")
        if actual.get("contract_version") != plugin.manifest["contract_version"]:
            errors.append(f"runner adapter {plugin.api} contract version differs from manifest")
        if actual.get("plugin_version") != plugin.manifest["version"]:
            errors.append(f"runner adapter {plugin.api} plugin version differs from manifest")
    compiled_plugins = {
        api for api, record in adapters.items() if record.get("source") == "plugin"
    }
    manifest_plugins = {plugin.api for plugin in plugins}
    for api in sorted(compiled_plugins - manifest_plugins):
        errors.append(f"runner contains unregistered plugin adapter {api}")
    return {
        "schema_version": 1,
        "ok": not errors,
        "runner": str(runner.resolve()),
        "plugin_count": len(plugins),
        "adapter_count": len(adapters),
        "errors": errors,
        "plugins": [plugin.as_dict() for plugin in plugins],
        "runtime": payload if isinstance(payload, dict) else {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    try:
        report = validate_runtime(Path(args.runner), timeout=args.timeout)
    except (OSError, subprocess.TimeoutExpired, PluginCatalogError, ValueError) as exc:
        report = {"schema_version": 1, "ok": False, "errors": [str(exc)]}
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
