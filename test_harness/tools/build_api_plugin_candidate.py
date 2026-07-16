#!/usr/bin/env python3
"""Build and replay one materialized API plugin in an isolated source copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_harness.msvc import detect_msvc_toolchain, find_cmake  # noqa: E402


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _inside(root: Path, path: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must stay under {root}") from exc
    return resolved


def _run(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    capture_stdout: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
        result = {
            "name": name,
            "argv": argv,
            "returncode": completed.returncode,
            "ok": completed.returncode == 0,
            "elapsed_seconds": round(time.perf_counter() - started, 6),
            "stdout_tail": completed.stdout[-8000:],
            "stderr_tail": completed.stderr[-8000:],
        }
        if capture_stdout:
            result["_captured_stdout"] = completed.stdout
        return result
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "argv": argv,
            "returncode": 124,
            "ok": False,
            "elapsed_seconds": round(time.perf_counter() - started, 6),
            "stdout_tail": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
            "timed_out": True,
        }


def _semantic_payload(case_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in ("status.json", "validation.json", "topo_check.json", "topo_track_summary.json"):
        path = case_dir / "report" / name
        if not path.is_file():
            payload[name] = None
            continue
        try:
            payload[name] = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            payload[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return payload


def _semantic_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sdk_identity(sdk_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Hash the headers and requested release binaries that define this adapter build."""

    files: set[Path] = {sdk_dir / "include/Foundation/init.h"}
    include_root = (sdk_dir / "include").resolve()
    for raw in manifest.get("sdk_headers", []):
        if not isinstance(raw, str):
            continue
        header = (include_root / raw).resolve()
        header.relative_to(include_root)
        files.add(header)
    for raw in manifest.get("sdk_modules", []):
        if not isinstance(raw, str):
            continue
        for candidate in (
            sdk_dir / "x64-win/lib" / f"SGGK_{raw}.lib",
            sdk_dir / "x64-win/bin" / f"SGGK_{raw}.dll",
        ):
            if candidate.is_file():
                files.add(candidate)
    records = []
    for path in sorted(files, key=lambda item: item.as_posix().lower()):
        if not path.is_file():
            raise ValueError(f"SDK identity input is missing: {path.relative_to(sdk_dir)}")
        records.append(
            {
                "path": path.resolve().relative_to(sdk_dir.resolve()).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return {
        "algorithm": "sggk_sdk_adapter_inputs_v1",
        "files": records,
        "sha256": hashlib.sha256(
            json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def build_candidate(
    *,
    plugin: Path,
    out: Path,
    sdk_dir: Path,
    smoke_replays: int,
    timeout: float,
) -> dict[str, Any]:
    plugin = _inside(REPO_ROOT / "artifacts", plugin, "plugin candidate")
    out = _inside(REPO_ROOT / "artifacts", out, "build output")
    manifest_path = plugin / "plugin.json"
    if not manifest_path.is_file():
        raise ValueError("plugin candidate is missing plugin.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("api"), str):
        raise ValueError("plugin manifest must contain api")
    api = manifest["api"]
    if plugin.name != api:
        raise ValueError("plugin directory name must equal manifest api")
    if not (sdk_dir / "include/Foundation/init.h").is_file():
        raise ValueError("sdk_dir does not contain include/Foundation/init.h")
    if not 1 <= smoke_replays <= 5:
        raise ValueError("smoke_replays must be between 1 and 5")
    out.mkdir(parents=True, exist_ok=True)
    commands: list[dict[str, Any]] = []
    semantic_hashes: list[str] = []
    semantic_evidence: list[dict[str, Any]] = []
    sdk_identity = _sdk_identity(sdk_dir, manifest)
    runner_sha256 = ""
    runtime_registry_sha256 = ""
    cmake = find_cmake(REPO_ROOT)
    if cmake is None:
        raise ValueError("no complete CMake installation found")
    toolchain = detect_msvc_toolchain(cmake)
    if not toolchain.get("ok") or not toolchain.get("generator"):
        raise ValueError(
            "no CMake-compatible MSVC C++ toolchain found: "
            + str(toolchain.get("detail") or "install VS 2022 or VS 2026 C++ workload")
        )
    cmake_generator = str(toolchain["generator"])
    with tempfile.TemporaryDirectory(prefix="sggk_plugin_build_") as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(
            REPO_ROOT,
            workspace,
            ignore=shutil.ignore_patterns(
                ".git",
                ".pytest_cache",
                "__pycache__",
                "artifacts",
                "build",
                "*.pyc",
            ),
        )
        plugin_root = workspace / "test_harness/api_plugins"
        target = _inside(plugin_root, plugin_root / api, "workspace plugin")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(plugin, target)
        smoke = target / "examples/smoke.json"
        negative = target / "examples/negative.invalid.json"
        # MSBuild's FileTracker still inherits the legacy Windows path-length
        # limit for its *.tlog files.  Candidate output paths deliberately carry
        # run/candidate provenance and can therefore be quite long; nesting the
        # generated Visual Studio project below ``out`` makes an otherwise valid
        # adapter fail during compiler identification.  Keep the disposable
        # build tree beside the already-isolated temporary workspace and retain
        # the durable command diagnostics in ``plugin_build_report.json``.
        build_root = Path(temporary) / "build"
        configure = _run(
            "cmake_configure",
            [
                str(cmake),
                "-S",
                str(workspace / "test_harness"),
                "-B",
                str(build_root),
                "-G",
                cmake_generator,
                "-A",
                "x64",
                f"-DSGGK_SDK_DIR={sdk_dir}",
            ],
            cwd=workspace,
            timeout=timeout,
        )
        commands.append(configure)
        if configure["ok"]:
            commands.append(
                _run(
                    "cmake_build",
                    [
                        str(cmake),
                        "--build",
                        str(build_root),
                        "--config",
                        "Release",
                        "--target",
                        "sggk_case_runner",
                    ],
                    cwd=workspace,
                    timeout=timeout,
                )
            )
        runner = build_root / "Release/sggk_case_runner.exe"
        if commands[-1]["ok"] and runner.is_file():
            runner_sha256 = _sha256_file(runner)
            commands.append(
                _run(
                    "validate_positive_recipe",
                    [
                        sys.executable,
                        str(workspace / "test_harness/tools/validate_recipe.py"),
                        str(smoke),
                        "--model-asset-policy",
                    ],
                    cwd=workspace,
                    timeout=timeout,
                )
            )
            negative_result = _run(
                "validate_negative_recipe",
                [
                    sys.executable,
                    str(workspace / "test_harness/tools/validate_recipe.py"),
                    str(negative),
                    "--model-asset-policy",
                ],
                cwd=workspace,
                timeout=timeout,
            )
            negative_result["ok"] = negative_result["returncode"] == 2
            commands.append(negative_result)
            runtime = _run(
                "list_adapters",
                [str(runner), "--list-adapters-json"],
                cwd=workspace,
                timeout=timeout,
                capture_stdout=True,
            )
            if runtime["returncode"] == 0:
                try:
                    payload = json.loads(runtime.pop("_captured_stdout", ""))
                    matches = [
                        item
                        for item in payload.get("adapters", [])
                        if isinstance(item, dict)
                        and item.get("api") == api
                        and item.get("source") == "plugin"
                    ]
                    runtime["ok"] = len(matches) == 1
                    runtime["adapter"] = matches[0] if matches else {}
                    runtime_registry_sha256 = _semantic_hash(payload)
                    runtime["registry_sha256"] = runtime_registry_sha256
                except json.JSONDecodeError:
                    runtime["ok"] = False
            runtime.pop("_captured_stdout", None)
            commands.append(runtime)
            if all(item["ok"] for item in commands):
                for index in range(1, smoke_replays + 1):
                    # Keep runtime artifact creation below the same short
                    # temporary root as the build. The runner creates several
                    # case/input/report levels, which can otherwise exceed the
                    # Windows path limit when ``out`` contains full provenance.
                    run_root = Path(temporary) / "smoke_replays" / f"a{index:02d}"
                    command = _run(
                        f"smoke_replay_{index:02d}",
                        [str(runner), "--recipe", str(smoke), "--out", str(run_root)],
                        cwd=workspace,
                        timeout=timeout,
                    )
                    commands.append(command)
                    case_id = json.loads(smoke.read_text(encoding="utf-8-sig")).get("case_id")
                    case_dir = run_root / str(case_id)
                    if command["ok"] and case_dir.is_dir():
                        semantic_payload = _semantic_payload(case_dir)
                        semantic_sha256 = _semantic_hash(semantic_payload)
                        semantic_hashes.append(semantic_sha256)
                        evidence = {
                            "attempt": index,
                            "case_id": case_id,
                            "semantic_sha256": semantic_sha256,
                            "reports": semantic_payload,
                        }
                        semantic_evidence.append(evidence)
                        _write(
                            out / "smoke_evidence" / f"attempt_{index:02d}.json",
                            evidence,
                        )
                    else:
                        break
    stable = len(semantic_hashes) == smoke_replays and len(set(semantic_hashes)) == 1
    ok = bool(commands) and all(item["ok"] for item in commands) and stable
    report = {
        "schema_version": 1,
        "ok": ok,
        "api": api,
        "candidate_plugin": str(plugin),
        "sdk_dir_sha256": hashlib.sha256(str(sdk_dir.resolve()).encode()).hexdigest(),
        "sdk_identity": sdk_identity,
        "runner_sha256": runner_sha256,
        "runtime_registry_sha256": runtime_registry_sha256,
        "cmake_generator": cmake_generator,
        "smoke_replays": smoke_replays,
        "stable_semantic_evidence": stable,
        "semantic_hashes": semantic_hashes,
        "semantic_evidence": semantic_evidence,
        "commands": commands,
    }
    _write(out / "plugin_build_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--sdk-dir", default=os.environ.get("SGGK_SDK_DIR", ""))
    parser.add_argument("--smoke-replays", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    try:
        if not args.sdk_dir:
            raise ValueError("set SGGK_SDK_DIR or pass --sdk-dir")
        report = build_candidate(
            plugin=Path(args.plugin),
            out=Path(args.out),
            sdk_dir=Path(args.sdk_dir).resolve(),
            smoke_replays=args.smoke_replays,
            timeout=args.timeout,
        )
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        report = {"schema_version": 1, "ok": False, "error": str(exc)}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
