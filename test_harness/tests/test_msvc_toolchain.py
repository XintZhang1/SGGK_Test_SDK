from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from test_harness.msvc import detect_msvc_toolchain, find_cmake
from test_harness.ui.application import HarnessUiApplication


def _installation(version: str, name: str) -> dict[str, object]:
    return {
        "installationVersion": version,
        "installationPath": f"C:/Program Files/Microsoft Visual Studio/{version.split('.')[0]}",
        "displayName": name,
    }


def _capabilities(*generators: str) -> dict[str, object]:
    return {
        "version": {"major": 4, "minor": 3, "patch": 1, "string": "4.3.1"},
        "generators": [
            {
                "name": generator,
                "platformSupport": True,
                "supportedPlatforms": ["x64", "Win32"],
            }
            for generator in generators
        ]
    }


def _fake_tool_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    program_files = tmp_path / "Program Files (x86)"
    locator = program_files / "Microsoft Visual Studio/Installer/vswhere.exe"
    locator.parent.mkdir(parents=True)
    locator.write_bytes(b"fake")
    cmake = tmp_path / "cmake.exe"
    cmake.write_bytes(b"fake")
    return program_files, locator, cmake


def test_detection_selects_newest_installed_supported_visual_studio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program_files, locator, cmake = _fake_tool_paths(tmp_path)
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(argv)
        payload = (
            [
                _installation("17.11.1", "Visual Studio 2022"),
                _installation("18.7.2", "Visual Studio 2026"),
            ]
            if argv[0] == str(locator)
            else _capabilities("Visual Studio 17 2022", "Visual Studio 18 2026")
        )
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("test_harness.msvc.subprocess.run", run)

    detected = detect_msvc_toolchain(
        cmake,
        environ={"ProgramFiles(x86)": str(program_files)},
    )

    assert detected["ok"] is True
    assert detected["generator"] == "Visual Studio 18 2026"
    assert detected["architecture"] == "x64"
    assert detected["cmake_supports_fresh"] is True
    assert len(calls) == 2


def test_detection_falls_back_to_vs2022_when_cmake_does_not_support_vs2026(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program_files, locator, cmake = _fake_tool_paths(tmp_path)

    def run(argv, **_kwargs):
        payload = (
            [
                _installation("18.7.2", "Visual Studio 2026"),
                _installation("17.11.1", "Visual Studio 2022"),
            ]
            if argv[0] == str(locator)
            else _capabilities("Visual Studio 17 2022")
        )
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("test_harness.msvc.subprocess.run", run)

    detected = detect_msvc_toolchain(
        cmake,
        environ={"ProgramFiles(x86)": str(program_files)},
    )

    assert detected["ok"] is True
    assert detected["generator"] == "Visual Studio 17 2022"


def test_ui_runner_build_uses_detected_generator(tmp_path: Path, monkeypatch) -> None:
    app = HarnessUiApplication(tmp_path)
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    cmake = tmp_path / "cmake.exe"
    cmake.write_bytes(b"fake")
    app.settings.save({"sdk_dir": str(sdk)})
    monkeypatch.setattr(app, "_cmake", lambda: cmake)
    monkeypatch.setattr(
        app,
        "_msvc_toolchain",
        lambda _cmake: {
            "ok": True,
            "generator": "Visual Studio 18 2026",
            "detail": "Visual Studio 2026",
            "cmake_supports_fresh": True,
        },
    )
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        build_log = tmp_path / "artifacts/harness_ui/build_runner.log"
        assert "COMMAND:" in build_log.read_text(encoding="utf-8")
        if "--build" in command:
            runner = tmp_path / "build/test_harness/Release/sggk_case_runner.exe"
            runner.parent.mkdir(parents=True, exist_ok=True)
            runner.write_bytes(b"runner")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("test_harness.ui.application.subprocess.run", run)

    result = app.build_runner()

    assert "--fresh" in calls[0]
    assert calls[0][calls[0].index("-G") + 1] == "Visual Studio 18 2026"
    assert result["cmake_generator"] == "Visual Studio 18 2026"


def test_ui_runner_build_discards_stale_generator_cache(tmp_path: Path, monkeypatch) -> None:
    app = HarnessUiApplication(tmp_path)
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    cmake = tmp_path / "cmake.exe"
    cmake.write_bytes(b"fake")
    build = tmp_path / "build/test_harness"
    build.mkdir(parents=True)
    (build / "CMakeCache.txt").write_text(
        "CMAKE_GENERATOR:INTERNAL=Visual Studio 17 2022\n",
        encoding="utf-8",
    )
    app.settings.save({"sdk_dir": str(sdk)})
    monkeypatch.setattr(app, "_cmake", lambda: cmake)
    monkeypatch.setattr(
        app,
        "_msvc_toolchain",
        lambda _cmake: {
            "ok": True,
            "generator": "Visual Studio 18 2026",
            "detail": "Visual Studio 2026",
            "cmake_supports_fresh": True,
        },
    )
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if "--fresh" in command:
            (build / "CMakeCache.txt").write_text(
                "CMAKE_GENERATOR:INTERNAL=Visual Studio 18 2026\n",
                encoding="utf-8",
            )
        if "--build" in command:
            runner = build / "Release/sggk_case_runner.exe"
            runner.parent.mkdir(parents=True, exist_ok=True)
            runner.write_bytes(b"runner")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("test_harness.ui.application.subprocess.run", run)

    result = app.build_runner()

    assert calls[0][1] == "--fresh"
    assert "Visual Studio 18 2026" in (build / "CMakeCache.txt").read_text(encoding="utf-8")
    assert result["cmake_generator"] == "Visual Studio 18 2026"


def test_ui_runner_old_cmake_uses_generator_specific_build_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = HarnessUiApplication(tmp_path)
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    cmake = tmp_path / "cmake.exe"
    cmake.write_bytes(b"fake")
    stale = tmp_path / "build/test_harness/CMakeCache.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("CMAKE_GENERATOR:INTERNAL=Visual Studio 17 2022\n", encoding="utf-8")
    app.settings.save({"sdk_dir": str(sdk)})
    monkeypatch.setattr(app, "_cmake", lambda: cmake)
    monkeypatch.setattr(
        app,
        "_msvc_toolchain",
        lambda _cmake: {
            "ok": True,
            "generator": "Visual Studio 18 2026",
            "detail": "Visual Studio 2026 with CMake 3.20",
            "cmake_supports_fresh": False,
        },
    )
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if "--build" in command:
            build_root = Path(command[command.index("--build") + 1])
            runner = build_root / "Release/sggk_case_runner.exe"
            runner.parent.mkdir(parents=True, exist_ok=True)
            runner.write_bytes(b"runner")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("test_harness.ui.application.subprocess.run", run)

    result = app.build_runner()

    assert "--fresh" not in calls[0]
    configured_build = Path(calls[0][calls[0].index("-B") + 1])
    assert configured_build.name == "test_harness-visual-studio-18-2026"
    assert Path(result["runner_path"]).is_file()
    assert "Visual Studio 17 2022" in stale.read_text(encoding="utf-8")


def test_ui_readiness_uses_version_neutral_msvc_check(tmp_path: Path, monkeypatch) -> None:
    app = HarnessUiApplication(tmp_path)
    cmake = tmp_path / "cmake.exe"
    cmake.write_bytes(b"fake")
    monkeypatch.setattr(app, "_cmake", lambda: cmake)
    monkeypatch.setattr(
        app,
        "_msvc_toolchain",
        lambda _cmake: {
            "ok": True,
            "generator": "Visual Studio 18 2026",
            "detail": "Visual Studio Professional 2026",
            "cmake_supports_fresh": True,
        },
    )

    checks = app.readiness(
        app.settings.load(),
        {"detection": {}, "probe": {}},
    )
    by_id = {item["id"]: item for item in checks}

    assert "msvc" in by_id
    assert "vs2022" not in by_id
    assert by_id["msvc"]["ok"] is True
    assert "2022 / 2026+" in by_id["msvc"]["label"]


def test_find_cmake_skips_incomplete_bundle_and_uses_visual_studio_copy(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    incomplete = repo / ".offline_runtime/cmake/bin/cmake.exe"
    incomplete.parent.mkdir(parents=True)
    incomplete.write_bytes(b"incomplete")
    program_files = tmp_path / "Program Files"
    visual_studio_cmake = (
        program_files
        / "Microsoft Visual Studio/18/Professional/Common7/IDE/CommonExtensions"
        / "Microsoft/CMake/CMake/bin/cmake.exe"
    )
    visual_studio_cmake.parent.mkdir(parents=True)
    visual_studio_cmake.write_bytes(b"complete")
    modules = (
        visual_studio_cmake.parent.parent
        / "share/cmake-4.3/Modules/CMakeDetermineCXXCompiler.cmake"
    )
    modules.parent.mkdir(parents=True)
    modules.write_text("# module", encoding="utf-8")

    selected = find_cmake(
        repo,
        environ={"ProgramFiles": str(program_files)},
    )

    assert selected == visual_studio_cmake.resolve()
