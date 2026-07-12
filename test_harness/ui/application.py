"""Application boundary connecting UI requests to the Harness workflow."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from test_harness.authoring_gateway.config import PROFILE_SPECS, load_gateway_config
from test_harness.orchestration.runtime import MessageApiRuntime
from test_harness.orchestration.workflow import HarnessWorkflow

from .settings import UiSettings, UiSettingsStore
from .state import active_session_root, read_artifact, session_snapshot


class HarnessUiApplication:
    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.settings = UiSettingsStore(self.repo_root)

    def readiness(self, settings: UiSettings | None = None) -> list[dict[str, Any]]:
        value = settings or self.settings.load()
        vswhere = (
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Microsoft Visual Studio"
            / "Installer"
            / "vswhere.exe"
        )
        cmake = self._cmake()
        checks = [
            self._check(
                "python",
                "Python 依赖",
                all(importlib.util.find_spec(x) for x in ("jsonschema", "PIL")),
                "jsonschema / Pillow",
            ),
            self._check("message_api", "Message API", bool(value.base_url and value.model), "填写内网地址和模型名"),
            self._check(
                "sdk", "SGGK SDK", bool(value.sdk_dir and Path(value.sdk_dir).is_dir()), value.sdk_dir or "尚未选择"
            ),
            self._check(
                "source",
                "SDK 源码",
                not value.source_root or Path(value.source_root).is_dir(),
                value.source_root or "可选",
            ),
            self._check(
                "runner",
                "用例运行器",
                bool(value.runner_path and Path(value.runner_path).is_file()),
                value.runner_path or "构建后自动出现",
            ),
            self._check("vs2022", "Visual Studio 2022 C++", vswhere.is_file(), str(vswhere)),
            self._check("cmake", "CMake", bool(cmake), str(cmake) if cmake else "未找到"),
        ]
        return checks

    @staticmethod
    def _check(key: str, label: str, ok: bool, detail: str) -> dict[str, Any]:
        return {"id": key, "label": label, "ok": ok, "detail": detail}

    def public_state(self) -> dict[str, Any]:
        value = self.settings.load()
        snapshot = session_snapshot(self.repo_root)
        snapshot["settings"] = self.settings.public()
        snapshot["readiness"] = self.readiness(value)
        snapshot["ready_to_start"] = all(
            check["ok"]
            for check in snapshot["readiness"]
            if check["id"] in {"python", "message_api", "sdk", "runner", "vs2022", "cmake"}
        )
        return snapshot

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = payload.get("settings")
        if not isinstance(values, dict):
            raise ValueError("settings must be an object")
        api_key = payload.get("api_key")
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError("api_key must be a string")
        saved = self.settings.save(values, api_key=api_key, clear_api_key=payload.get("clear_api_key") is True)
        return saved.public_dict(api_key_configured=bool(self.settings.api_key(saved.profile)))

    def workflow(self) -> HarnessWorkflow:
        settings = self.settings.validate(self.settings.load(), require_existing_paths=True)
        spec = PROFILE_SPECS[settings.profile]
        environ = {
            spec.base_url_env: settings.base_url,
            spec.model_env: settings.model,
            spec.api_key_env: self.settings.api_key(settings.profile),
            spec.ca_bundle_env: settings.ca_bundle,
        }
        config = load_gateway_config(settings.profile, environ=environ)
        runtime = MessageApiRuntime(
            repo_root=self.repo_root,
            profile=settings.profile,
            config=config,
            candidate_count=settings.candidate_count,
            candidate_parallelism=settings.candidate_parallelism,
            thinking_mode=settings.thinking_mode,
            jobs=settings.jobs,
            execution_timeout_seconds=settings.execution_timeout_seconds,
            campaign_dataset=settings.campaign_dataset,
        )
        return HarnessWorkflow(
            runtime,
            repo_root=self.repo_root,
            profile=settings.profile,
            sdk_dir=settings.sdk_dir,
            source_root=settings.source_root or None,
            runner_path=settings.runner_path or None,
        )

    def start(self, public_function: str) -> dict[str, Any]:
        return self.workflow().start(public_function)

    def comment(self, value: str) -> dict[str, Any]:
        return self.workflow().comment(value)

    def approve(self) -> dict[str, Any]:
        return self.workflow().comment("I approve the current candidate. Please execute the SDK test now.")

    def retry(self) -> dict[str, Any]:
        return self.workflow().retry()

    def _cmake(self) -> Path | None:
        bundled = self.repo_root / ".offline_runtime" / "cmake" / "bin" / "cmake.exe"
        if bundled.is_file():
            return bundled
        found = shutil.which("cmake")
        return Path(found).resolve() if found else None

    def build_runner(self) -> dict[str, Any]:
        settings = self.settings.load()
        if not settings.sdk_dir or not Path(settings.sdk_dir).is_dir():
            raise ValueError("请先配置有效的 SGGK SDK 目录")
        cmake = self._cmake()
        if cmake is None:
            raise ValueError("CMake 不可用，请先运行离线安装")
        build = self.repo_root / "build" / "test_harness"
        python = Path(os.sys.executable).resolve()
        commands = [
            [
                str(cmake),
                "-S",
                str(self.repo_root / "test_harness"),
                "-B",
                str(build),
                "-G",
                "Visual Studio 17 2022",
                "-A",
                "x64",
                f"-DSGGK_SDK_DIR={settings.sdk_dir}",
                f"-DPython3_EXECUTABLE={python}",
            ],
            [str(cmake), "--build", str(build), "--config", "Release", "--parallel"],
        ]
        log_path = self.repo_root / "artifacts" / "harness_ui" / "build_runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            for command in commands:
                log.write("COMMAND: " + " ".join(command) + "\n\n")
                result = subprocess.run(
                    command,
                    cwd=self.repo_root,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=900,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError(f"runner 构建失败（exit {result.returncode}），日志：{log_path}")
        runner = build / "Release" / "sggk_case_runner.exe"
        if not runner.is_file():
            raise RuntimeError(f"构建完成但未找到 runner：{runner}")
        self.settings.save({"runner_path": str(runner)})
        return {"runner_path": str(runner), "log_path": str(log_path)}

    def artifact(self, relative: str) -> dict[str, Any]:
        return read_artifact(active_session_root(self.repo_root), relative)


__all__ = ["HarnessUiApplication"]
