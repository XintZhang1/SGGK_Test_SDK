#!/usr/bin/env python3
"""Promote one attested API plugin build into the checked-in plugin catalog.

The tool first re-verifies the full hash-bound build attestation (build ok,
positive recipe accepted, negative recipe rejected, runtime registry presence,
and three smoke replays with one identical semantic hash) by reusing
``run_message_harness_pipeline.MessageHarnessPipeline._plugin_execution_attested``
semantics.  Only then does it copy the materialized plugin into
``test_harness/api_plugins/<api>/`` and merge the plugin capability entry into
``test_harness/interface_capabilities.json``.  Every check runs before any
mutation; a failed check exits nonzero with the tree untouched.  The C++
registry refreshes at the next CMake configure — this tool never runs cmake.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from plugin_catalog import (  # noqa: E402
    API_ID_RE,
    PluginCatalogError,
    discover_plugins,
    merge_capabilities,
    plugin_map,
)

BUILTIN_RECIPE_APIS = frozenset(
    {
        "api_boolean",
        "api_boolean_slice",
        "api_boolean_split",
        "api_offset2d",
        "api_offset_body",
        "api_topology_section",
        "check_sgt",
        "step_import",
        "iges_import",
        "step_roundtrip",
        "iges_roundtrip",
    }
)
REQUIRED_BUILD_COMMANDS = (
    "cmake_configure",
    "cmake_build",
    "validate_positive_recipe",
    "validate_negative_recipe",
    "list_adapters",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json_atomic(path: Path, value: Any) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_name(f"{path.name}.promote-{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def _inside(root: Path, raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} must be a non-empty path string")
    resolved = Path(raw).expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the repository: {raw}") from exc
    return resolved


def _attestation_errors(repo_root: Path, report_path: Path, report: dict[str, Any]) -> list[str]:
    """Reuse the pipeline's hash-bound plugin execution attestation semantics."""

    from run_message_harness_pipeline import (  # noqa: PLC0415
        ExecutionResult,
        MessageHarnessPipeline,
    )

    pipeline = MessageHarnessPipeline.__new__(MessageHarnessPipeline)
    pipeline.repo_root = repo_root
    semantic = report.get("semantic_hashes")
    sdk_identity = report.get("sdk_identity")
    artifacts = {
        "plugin_build_report": str(report_path),
        "plugin_build_report_sha256": _sha256_file(report_path),
        "runner_sha256": str(report.get("runner_sha256") or ""),
        "runtime_registry_sha256": str(report.get("runtime_registry_sha256") or ""),
        "sdk_identity_sha256": (
            str(sdk_identity.get("sha256") or "") if isinstance(sdk_identity, dict) else ""
        ),
        "semantic_sha256": (
            str(semantic[0])
            if isinstance(semantic, list) and semantic and len(set(semantic)) == 1
            else ""
        ),
    }
    execution = ExecutionResult(True, True, "passed", [], artifacts)
    if pipeline._plugin_execution_attested(execution):  # noqa: SLF001
        return []
    return [
        "plugin build report lacks the complete hash-bound build attestation "
        "(build ok, stable semantic evidence, 3 identical smoke replay hashes)"
    ]


def _command_errors(report: dict[str, Any], api: str) -> list[str]:
    errors: list[str] = []
    commands = report.get("commands")
    commands = commands if isinstance(commands, list) else []
    by_name = {
        str(item.get("name")): item for item in commands if isinstance(item, dict)
    }
    for name in REQUIRED_BUILD_COMMANDS:
        record = by_name.get(name)
        if record is None or record.get("ok") is not True:
            errors.append(f"build command did not succeed: {name}")
    for index in range(1, 4):
        name = f"smoke_replay_{index:02d}"
        record = by_name.get(name)
        if record is None or record.get("ok") is not True:
            errors.append(f"build command did not succeed: {name}")
    adapter = by_name.get("list_adapters", {}).get("adapter")
    if not isinstance(adapter, dict) or adapter.get("api") != api or adapter.get("source") != "plugin":
        errors.append("runtime registry does not list exactly this plugin api")
    return errors


def _validate_updated_tree(
    repo_root: Path,
    plugin_root: Path,
    capabilities: dict[str, Any],
    api: str,
) -> list[str]:
    """Run catalog discovery plus the interface-capability validator on one tree."""

    errors: list[str] = []
    try:
        merged = merge_capabilities(copy.deepcopy(capabilities), plugin_root)
    except (PluginCatalogError, OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"updated plugin catalog does not merge cleanly: {exc}"]
    apis = merged.get("apis") if isinstance(merged.get("apis"), dict) else {}
    if api not in apis or not isinstance(apis[api].get("plugin"), dict):
        errors.append("updated capability registry does not carry the promoted plugin api")
    import validate_interface_capabilities  # noqa: PLC0415

    runner_source_path = repo_root / "test_harness" / "src" / "sggk_case_runner.cpp"
    if not runner_source_path.is_file():
        return errors + ["runner source is missing; cannot cross-check capabilities"]
    implemented = set(BUILTIN_RECIPE_APIS)
    try:
        implemented |= {record.api for record in discover_plugins(plugin_root)}
    except (PluginCatalogError, OSError, json.JSONDecodeError) as exc:
        return errors + [f"updated plugin catalog discovery failed: {exc}"]
    original_plugin_map = validate_interface_capabilities.plugin_map
    validate_interface_capabilities.plugin_map = lambda: plugin_map(plugin_root)
    try:
        errors.extend(
            validate_interface_capabilities.validate_registry(
                merged,
                repo_root=repo_root,
                runner_source=runner_source_path.read_text(encoding="utf-8-sig"),
                implemented_recipe_apis=implemented,
            )
        )
    finally:
        validate_interface_capabilities.plugin_map = original_plugin_map
    return errors


def promote(
    build_report: Path,
    repo_root: Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Verify, stage, and commit one plugin promotion; never mutate on failure."""

    repo_root = repo_root.resolve()
    result: dict[str, Any] = {
        "schema_version": 1,
        "ok": False,
        "api": "",
        "plugin_dir": "",
        "capabilities": "",
        "replaced_existing": False,
        "errors": [],
        "note": "C++ 注册表将在下次 CMake configure 时刷新；本工具不执行 cmake。",
    }
    errors: list[str] = []
    try:
        report_path = _inside(repo_root, str(build_report), "build_report")
        if report_path.is_dir():
            report_path = report_path / "plugin_build_report.json"
        report_path = _inside(repo_root, str(report_path), "build_report")
    except ValueError as exc:
        result["errors"] = [str(exc)]
        return result
    if not report_path.is_file():
        result["errors"] = [f"plugin_build_report.json not found: {report_path}"]
        return result
    try:
        report = _read_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        result["errors"] = [f"plugin build report is unreadable: {exc}"]
        return result
    if not isinstance(report, dict) or report.get("ok") is not True:
        result["errors"] = ["plugin build report is not a passing build"]
        return result
    api = str(report.get("api") or "")
    if not API_ID_RE.fullmatch(api):
        result["errors"] = [f"plugin build report carries an invalid api id: {api!r}"]
        return result
    result["api"] = api
    errors.extend(_attestation_errors(repo_root, report_path, report))
    errors.extend(_command_errors(report, api))
    plugin_source: Path | None = None
    manifest: dict[str, Any] = {}
    if not errors:
        try:
            plugin_source = _inside(
                repo_root, str(report.get("candidate_plugin") or ""), "candidate_plugin"
            )
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if plugin_source.name != api or not (plugin_source / "plugin.json").is_file():
                errors.append("candidate plugin directory does not match the attested api")
            else:
                try:
                    records = discover_plugins(plugin_source.parent)
                except (PluginCatalogError, OSError, json.JSONDecodeError) as exc:
                    errors.append(f"candidate plugin catalog discovery failed: {exc}")
                else:
                    matching = [record for record in records if record.api == api]
                    if len(matching) != 1:
                        errors.append("candidate plugin catalog does not contain exactly this api")
                    else:
                        manifest = matching[0].manifest
    capabilities_path = repo_root / "test_harness" / "interface_capabilities.json"
    plugin_root = repo_root / "test_harness" / "api_plugins"
    target_dir = plugin_root / api
    capability = copy.deepcopy(manifest.get("capability")) if manifest else {}
    try:
        capabilities = _read_json(capabilities_path)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"interface_capabilities.json is unreadable: {exc}")
        capabilities = {}
    if not isinstance(capabilities, dict):
        errors.append("interface_capabilities.json root must be an object")
        capabilities = {}
    apis = capabilities.get("apis")
    if not isinstance(apis, dict):
        errors.append("interface_capabilities.json apis must be an object")
        apis = {}
    existing = apis.get(api)
    if not errors and existing is not None and existing != capability:
        errors.append(
            f"interface_capabilities.json already carries a different apis.{api} entry; "
            "refusing to overwrite a conflicting registration"
        )
    if not errors and target_dir.exists() and not replace:
        errors.append(
            f"plugin directory already exists: {target_dir}; pass --replace to overwrite"
        )
    if not errors and plugin_source is not None:
        # Stage the full updated tree in a temporary sibling and run every
        # post-check against it before touching the real catalog.
        with tempfile.TemporaryDirectory(
            prefix="sggk_promote_", dir=plugin_root.parent if plugin_root.parent.is_dir() else None
        ) as temporary:
            staged_root = Path(temporary) / "api_plugins"
            if plugin_root.is_dir():
                shutil.copytree(plugin_root, staged_root)
            else:
                staged_root.mkdir(parents=True)
            staged_target = staged_root / api
            if staged_target.exists():
                shutil.rmtree(staged_target)
            shutil.copytree(plugin_source, staged_target)
            staged_capabilities = copy.deepcopy(capabilities)
            staged_capabilities.setdefault("apis", {})[api] = capability
            errors.extend(
                _validate_updated_tree(repo_root, staged_root, staged_capabilities, api)
            )
    if errors:
        result["errors"] = errors
        return result

    # Commit: copy the plugin directory, then merge the capability entry.
    # Staging and backup live beside (never inside) the scanned plugin root.
    plugin_root.mkdir(parents=True, exist_ok=True)
    backup_dir: Path | None = None
    staging_dir = plugin_root.parent / f".{api}.promote-staging-{os.getpid()}"
    try:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        shutil.copytree(plugin_source, staging_dir)
        if target_dir.exists():
            backup_dir = plugin_root.parent / f".{api}.promote-backup-{os.getpid()}"
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            os.replace(target_dir, backup_dir)
        os.replace(staging_dir, target_dir)
        updated_capabilities = copy.deepcopy(capabilities)
        updated_capabilities.setdefault("apis", {})[api] = capability
        _write_json_atomic(capabilities_path, updated_capabilities)
    except OSError as exc:
        result["errors"] = [f"promotion commit failed before completion: {exc}"]
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if not target_dir.exists() and backup_dir is not None and backup_dir.exists():
            os.replace(backup_dir, target_dir)
        return result

    post_errors = _validate_updated_tree(repo_root, plugin_root, updated_capabilities, api)
    if post_errors:
        # Roll back to the pre-promotion tree so a failed check never leaves a
        # half-registered plugin behind.
        _write_json_atomic(capabilities_path, capabilities)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
            os.replace(backup_dir, target_dir)
        else:
            shutil.rmtree(target_dir, ignore_errors=True)
        result["errors"] = [f"post-promotion validation failed (rolled back): {post_errors}"]
        return result
    if backup_dir is not None and backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    result["ok"] = True
    result["plugin_dir"] = str(target_dir)
    result["capabilities"] = str(capabilities_path)
    result["replaced_existing"] = backup_dir is not None
    result["summary"] = (
        f"插件 {api} 已注册到 test_harness/api_plugins 并合并入 interface_capabilities.json。"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将已通过完整构建证明的 API 插件注册到 test_harness/api_plugins 与能力注册表。"
    )
    parser.add_argument(
        "--build-report",
        required=True,
        help="plugin_build_report.json 或其所在目录（必须位于仓库内）",
    )
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="仓库根目录")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="允许覆盖已存在的同名插件目录",
    )
    parser.add_argument("--report", default="", help="可选：写出 JSON 结果到该路径")
    args = parser.parse_args()
    try:
        outcome = promote(
            Path(args.build_report),
            Path(args.repo_root),
            replace=bool(args.replace),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        outcome = {"schema_version": 1, "ok": False, "api": "", "errors": [str(exc)]}
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(outcome, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(json.dumps(outcome, indent=2, ensure_ascii=False))
    if outcome.get("ok"):
        print(outcome["summary"])
        print("请重新构建 Runner（CMake configure 会刷新插件注册表），并人工核对 git diff 后提交。")
        return 0
    print("插件注册失败，仓库树未被修改。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
