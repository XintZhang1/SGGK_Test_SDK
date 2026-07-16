"""Detect an installed MSVC C++ toolchain that CMake can actually generate for."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


VC_TOOLS_COMPONENT = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"


def vswhere_path(environ: Mapping[str, str] | None = None) -> Path:
    values = environ or os.environ
    return (
        Path(values.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe"
    )


def _cmake_has_core_modules(path: Path) -> bool:
    root = path.parent.parent
    return any(
        candidate.is_file()
        for candidate in root.glob("share/cmake-*/Modules/CMakeDetermineCXXCompiler.cmake")
    )


def find_cmake(
    repo_root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Find a complete CMake, preferring the copy aligned with installed Visual Studio."""

    values = environ or os.environ
    program_files = Path(values.get("ProgramFiles", "C:/Program Files"))
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(Path(repo_root) / ".offline_runtime/cmake/bin/cmake.exe")
    candidates.extend(
        sorted(
            program_files.glob(
                "Microsoft Visual Studio/*/*/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
            ),
            reverse=True,
        )
    )
    found = shutil.which("cmake")
    if found:
        candidates.append(Path(found))
    candidates.append(program_files / "CMake/bin/cmake.exe")
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file() and _cmake_has_core_modules(candidate):
            return candidate.resolve()
    return None


def _run_json(argv: list[str]) -> Any:
    completed = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        shell=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(detail)
    return json.loads(completed.stdout.lstrip("\ufeff"))


def _version_key(value: Any) -> tuple[int, ...]:
    parts: list[int] = []
    for item in str(value or "").split("."):
        try:
            parts.append(int(item))
        except ValueError:
            break
    return tuple(parts)


def detect_msvc_toolchain(
    cmake: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Select the newest installed VS C++ workload supported by this CMake."""

    locator = vswhere_path(environ)
    if not locator.is_file():
        return {
            "ok": False,
            "generator": "",
            "detail": f"vswhere.exe not found: {locator}",
            "vswhere_path": str(locator),
        }
    try:
        installations = _run_json(
            [
                str(locator),
                "-all",
                "-products",
                "*",
                "-requires",
                VC_TOOLS_COMPONENT,
                "-format",
                "json",
                "-utf8",
            ]
        )
        capabilities = _run_json([str(cmake), "-E", "capabilities"])
    except (OSError, RuntimeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "generator": "",
            "detail": f"MSVC/CMake detection failed: {exc}",
            "vswhere_path": str(locator),
        }

    generator_records = capabilities.get("generators") if isinstance(capabilities, dict) else None
    cmake_version_record = capabilities.get("version") if isinstance(capabilities, dict) else None
    cmake_version = (
        str(cmake_version_record.get("string") or "")
        if isinstance(cmake_version_record, dict)
        else ""
    )
    cmake_version_key = _version_key(cmake_version)
    cmake_supports_fresh = cmake_version_key >= (3, 24)
    supported_generators = {
        str(item.get("name") or "")
        for item in generator_records or []
        if isinstance(item, dict)
        and str(item.get("name") or "").startswith("Visual Studio ")
        and item.get("platformSupport") is True
        and "x64" in (item.get("supportedPlatforms") or [])
    }
    candidates = sorted(
        (item for item in installations or [] if isinstance(item, dict)),
        key=lambda item: _version_key(item.get("installationVersion")),
        reverse=True,
    )
    for installation in candidates:
        version = str(installation.get("installationVersion") or "")
        version_key = _version_key(version)
        if not version_key:
            continue
        prefix = f"Visual Studio {version_key[0]} "
        generator = next(
            (name for name in sorted(supported_generators) if name.startswith(prefix)),
            "",
        )
        if not generator:
            continue
        display_name = str(installation.get("displayName") or generator)
        return {
            "ok": True,
            "generator": generator,
            "architecture": "x64",
            "installation_version": version,
            "installation_path": str(installation.get("installationPath") or ""),
            "display_name": display_name,
            "detail": f"{display_name} ({version}) - {generator}",
            "cmake_version": cmake_version,
            "cmake_supports_fresh": cmake_supports_fresh,
            "vswhere_path": str(locator),
        }

    installed_versions = [
        str(item.get("installationVersion") or "unknown") for item in candidates
    ]
    return {
        "ok": False,
        "generator": "",
        "detail": (
            "No installed Visual Studio C++ workload matches a CMake x64 generator. "
            f"Installed versions: {installed_versions or ['none']}; "
            f"CMake generators: {sorted(supported_generators) or ['none']}"
        ),
        "vswhere_path": str(locator),
    }


__all__ = ["VC_TOOLS_COMPONENT", "detect_msvc_toolchain", "find_cmake", "vswhere_path"]
