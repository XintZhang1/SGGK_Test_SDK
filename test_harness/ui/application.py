"""Application boundary connecting UI requests to the Harness workflow."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from test_harness.authoring_gateway.config import PROFILE_SPECS, load_gateway_config
from test_harness.msvc import detect_msvc_toolchain, find_cmake
from test_harness.nx import detect_nx_environment, probe_nx_python
from test_harness.orchestration.runtime import MessageApiRuntime
from test_harness.orchestration.workflow import HarnessWorkflow

from .abc_dataset import AbcDatasetBackend
from .settings import UiSettings, UiSettingsStore
from .state import active_session_root, read_artifact, session_snapshot


class HarnessUiApplication:
    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.settings = UiSettingsStore(self.repo_root)
        self.abc = AbcDatasetBackend(self.repo_root)
        self._nx_lock = threading.Lock()
        self._nx_detection_key = ""
        self._nx_detection: dict[str, Any] = {}
        self._nx_probe: dict[str, Any] = {"schema_version": 1, "operation": "probe", "status": "not_run"}
        self._nx_probe_identity = ""
        self._msvc_lock = threading.Lock()
        self._msvc_detection_key = ""
        self._msvc_detection: dict[str, Any] = {}

    def readiness(
        self,
        settings: UiSettings | None = None,
        nx: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        value = settings or self.settings.load()
        cmake = self._cmake()
        msvc = self._msvc_toolchain(cmake)
        profile = PROFILE_SPECS[value.profile]
        api_key_ready = bool(self.settings.api_key(value.profile)) or not profile.api_key_required
        nx_state = nx or self.nx_state(value)
        nx_detection = nx_state.get("detection") if isinstance(nx_state, dict) else {}
        nx_probe = nx_state.get("probe") if isinstance(nx_state, dict) else {}
        dataset_ready = bool(value.campaign_dataset and Path(value.campaign_dataset).is_file())
        source_check = (
            self._check("source_boundary", "外网数据边界", not value.source_root, "不向 SiliconFlow 发送 SDK 源码")
            if value.profile != "intranet"
            else self._check(
                "source",
                "SDK 源码",
                not value.source_root or Path(value.source_root).is_dir(),
                value.source_root or "可选",
            )
        )
        checks = [
            self._check(
                "python",
                "Python 依赖",
                all(importlib.util.find_spec(x) for x in ("jsonschema", "PIL")),
                "jsonschema / Pillow",
            ),
            self._check(
                "message_api",
                "SiliconFlow GLM-5.2",
                bool(value.base_url and value.model and api_key_ready),
                value.model if api_key_ready else "请保存 SiliconFlow API Key",
            ),
            self._check(
                "sdk", "SGGK SDK", bool(value.sdk_dir and Path(value.sdk_dir).is_dir()), value.sdk_dir or "尚未选择"
            ),
            source_check,
            self._check(
                "runner",
                "用例运行器",
                bool(value.runner_path and Path(value.runner_path).is_file()),
                value.runner_path or "构建后自动出现",
            ),
            self._check(
                "msvc",
                "MSVC C++（VS 2022 / 2026+）",
                bool(msvc.get("ok")),
                str(msvc.get("detail") or "未找到兼容的 Visual Studio C++ 工具链"),
            ),
            self._check("cmake", "CMake", bool(cmake), str(cmake) if cmake else "未找到"),
            self._check(
                "abc_dataset",
                "ABC 数据集（可选）",
                dataset_ready,
                value.campaign_dataset or "可下载全量数据，或绑定含可验证索引的 fetch 根目录",
            ),
            self._check(
                "nx_journal",
                "NX Journal 环境（可选）",
                bool(isinstance(nx_detection, dict) and nx_detection.get("ok")),
                self._nx_detail(nx_detection),
            ),
            self._check(
                "nx_python",
                "NX Python API 验证（可选）",
                bool(isinstance(nx_probe, dict) and nx_probe.get("ok")),
                str(nx_probe.get("status") or "尚未运行真实探针")
                if isinstance(nx_probe, dict)
                else "尚未运行真实探针",
            ),
        ]
        return checks

    @staticmethod
    def _check(key: str, label: str, ok: bool, detail: str) -> dict[str, Any]:
        return {"id": key, "label": label, "ok": ok, "detail": detail}

    def public_state(self) -> dict[str, Any]:
        value = self.settings.load()
        nx = self.nx_state(value)
        snapshot = session_snapshot(self.repo_root)
        snapshot["settings"] = self.settings.public()
        snapshot["abc"] = self.abc.snapshot()
        snapshot["nx"] = nx
        snapshot["readiness"] = self.readiness(value, nx)
        snapshot["ready_to_start"] = all(
            check["ok"]
            for check in snapshot["readiness"]
            if check["id"] in {"python", "message_api", "sdk", "runner"}
        )
        snapshot["ready_to_build"] = all(
            check["ok"]
            for check in snapshot["readiness"]
            if check["id"] in {"sdk", "msvc", "cmake"}
        )
        return snapshot

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = payload.get("settings")
        if not isinstance(values, dict):
            raise ValueError("settings must be an object")
        values = dict(values)
        requested_dataset = str(values.get("campaign_dataset") or "").strip()
        if requested_dataset:
            report = self.abc.inspect_existing(requested_dataset)
            if not report.get("ready"):
                errors = report.get("errors") or report.get("warnings") or [
                    "ABC dataset index is not campaign-ready"
                ]
                raise ValueError("; ".join(str(item) for item in errors[:4]))
            values["campaign_dataset"] = str(report.get("campaign_dataset") or "")
        api_key = payload.get("api_key")
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError("api_key must be a string")
        previous = self.settings.load()
        saved = self.settings.save(values, api_key=api_key, clear_api_key=payload.get("clear_api_key") is True)
        if saved.nx_root_dir != previous.nx_root_dir:
            with self._nx_lock:
                self._nx_detection_key = ""
                self._nx_detection = {}
                self._nx_probe = {"schema_version": 1, "operation": "probe", "status": "not_run"}
                self._nx_probe_identity = ""
        return saved.public_dict(api_key_configured=bool(self.settings.api_key(saved.profile)))

    @staticmethod
    def _nx_detail(detection: Any) -> str:
        if not isinstance(detection, dict):
            return "尚未检测"
        selected = str(detection.get("selected_root") or "")
        if selected:
            return selected
        diagnostics = detection.get("diagnostics")
        if isinstance(diagnostics, list) and diagnostics and isinstance(diagnostics[0], dict):
            return str(diagnostics[0].get("message") or detection.get("status") or "未找到 NX")
        return str(detection.get("status") or "未找到 NX")

    def nx_state(self, settings: UiSettings | None = None, *, refresh: bool = False) -> dict[str, Any]:
        value = settings or self.settings.load()
        key = value.nx_root_dir
        with self._nx_lock:
            cached = bool(self._nx_detection) and self._nx_detection_key == key and not refresh
            if cached:
                return {"detection": deepcopy(self._nx_detection), "probe": deepcopy(self._nx_probe)}
        roots = [key] if key else []
        try:
            detection = detect_nx_environment(explicit_roots=roots)
        except Exception as exc:  # keep the local UI available when vendor discovery fails
            detection = {
                "schema_version": 1,
                "operation": "detect",
                "ok": False,
                "status": "detection_failed",
                "selected_root": "",
                "installations": [],
                "diagnostics": [{"code": "NX_DETECTION_FAILED", "severity": "error", "message": str(exc)}],
            }
        with self._nx_lock:
            if self._nx_detection_key != key:
                self._nx_probe = {"schema_version": 1, "operation": "probe", "status": "not_run"}
                self._nx_probe_identity = ""
            detection_identity = self._nx_identity(detection)
            if (
                self._nx_probe.get("status") not in {"not_run", "running"}
                and self._nx_probe_identity != detection_identity
            ):
                self._nx_probe = {"schema_version": 1, "operation": "probe", "status": "not_run"}
                self._nx_probe_identity = ""
            self._nx_detection_key = key
            self._nx_detection = detection
            return {"detection": deepcopy(detection), "probe": deepcopy(self._nx_probe)}

    @staticmethod
    def _nx_identity(detection: Any) -> str:
        if not isinstance(detection, dict):
            return ""
        selected_root = str(detection.get("selected_root") or "")
        installations = detection.get("installations")
        run_journal = ""
        if isinstance(installations, list):
            selected = next(
                (
                    item
                    for item in installations
                    if isinstance(item, dict) and str(item.get("root") or "") == selected_root
                ),
                None,
            )
            paths = selected.get("paths") if isinstance(selected, dict) else None
            if isinstance(paths, dict):
                run_journal = str(paths.get("run_journal") or "")
        return f"{selected_root.casefold()}|{run_journal.casefold()}" if selected_root else ""

    def probe_nx(self) -> dict[str, Any]:
        value = self.settings.load()
        roots = [value.nx_root_dir] if value.nx_root_dir else []
        with self._nx_lock:
            self._nx_probe = {"schema_version": 1, "operation": "probe", "status": "running"}
            self._nx_probe_identity = ""
        try:
            result = probe_nx_python(
                explicit_roots=roots,
                timeout_seconds=value.nx_probe_timeout_seconds,
            )
        except Exception as exc:
            result = {
                "schema_version": 1,
                "operation": "probe",
                "ok": False,
                "status": "launch_failed",
                "error": str(exc),
                "diagnostics": [{"code": "NX_PROBE_FAILED", "severity": "error", "message": str(exc)}],
            }
            with self._nx_lock:
                self._nx_probe = result
                self._nx_probe_identity = ""
            raise
        with self._nx_lock:
            self._nx_probe = result
            environment = result.get("environment")
            if isinstance(environment, dict):
                self._nx_detection_key = value.nx_root_dir
                self._nx_detection = environment
                self._nx_probe_identity = self._nx_identity(environment)
        return result

    def inspect_abc(self, path: str) -> dict[str, Any]:
        return self.abc.inspect_existing(path)

    def use_existing_abc(self, path: str) -> dict[str, Any]:
        report = self.abc.inspect_existing(path)
        if not report.get("ready"):
            errors = report.get("errors") or report.get("warnings") or ["目录尚未形成可用数据集索引"]
            raise ValueError("; ".join(str(item) for item in errors[:4]))
        campaign_dataset = str(report.get("campaign_dataset") or "")
        if not campaign_dataset:
            raise ValueError("ABC dataset inspection returned no campaign dataset path")
        saved = self.settings.save({"campaign_dataset": campaign_dataset})
        return {
            "inspection": report,
            "settings": saved.public_dict(api_key_configured=bool(self.settings.api_key(saved.profile))),
        }

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
            use_memory=settings.use_memory,
            nx_root=settings.nx_root_dir or None,
        )

    def start(self, public_function: str, *, use_memory: bool | None = None) -> dict[str, Any]:
        return self.workflow().start(public_function, use_memory=use_memory)

    def comment(self, value: str) -> dict[str, Any]:
        return self.workflow().comment(value)

    def approve(self) -> dict[str, Any]:
        return self.workflow().comment("I approve the current candidate. Please execute the SDK test now.")

    def retry(self) -> dict[str, Any]:
        return self.workflow().retry()

    def _cmake(self) -> Path | None:
        return find_cmake(self.repo_root)

    def _msvc_toolchain(self, cmake: Path | None) -> dict[str, Any]:
        key = str(cmake or "")
        with self._msvc_lock:
            if key == self._msvc_detection_key and self._msvc_detection:
                return deepcopy(self._msvc_detection)
            detection = (
                detect_msvc_toolchain(cmake)
                if cmake is not None
                else {"ok": False, "generator": "", "detail": "CMake 不可用"}
            )
            self._msvc_detection_key = key
            self._msvc_detection = dict(detection)
            return deepcopy(self._msvc_detection)

    def build_runner(self) -> dict[str, Any]:
        settings = self.settings.load()
        if not settings.sdk_dir or not Path(settings.sdk_dir).is_dir():
            raise ValueError("请先配置有效的 SGGK SDK 目录")
        cmake = self._cmake()
        if cmake is None:
            raise ValueError("CMake 不可用，请先运行离线安装")
        msvc = self._msvc_toolchain(cmake)
        if not msvc.get("ok") or not msvc.get("generator"):
            raise ValueError(
                "未找到 CMake 可用的 MSVC C++ 工具链："
                + str(msvc.get("detail") or "请安装 VS 2022 或 VS 2026 C++ workload")
            )
        generator = str(msvc["generator"])
        supports_fresh = bool(msvc.get("cmake_supports_fresh"))
        generator_tag = "-".join(
            part for part in generator.casefold().replace("/", " ").split() if part
        )
        build = self.repo_root / "build" / (
            "test_harness" if supports_fresh else f"test_harness-{generator_tag}"
        )
        python = Path(os.sys.executable).resolve()
        configure_command = [str(cmake)]
        if supports_fresh:
            configure_command.append("--fresh")
        configure_command.extend(
            [
                "-S",
                str(self.repo_root / "test_harness"),
                "-B",
                str(build),
                "-G",
                generator,
                "-A",
                "x64",
                f"-DSGGK_SDK_DIR={settings.sdk_dir}",
                f"-DPython3_EXECUTABLE={python}",
            ]
        )
        commands = [
            configure_command,
            [str(cmake), "--build", str(build), "--config", "Release", "--parallel"],
        ]
        log_path = self.repo_root / "artifacts" / "harness_ui" / "build_runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            for command in commands:
                log.write("COMMAND: " + " ".join(command) + "\n\n")
                log.flush()
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
        return {
            "runner_path": str(runner),
            "log_path": str(log_path),
            "cmake_generator": generator,
            "build_root": str(build),
        }

    def artifact(self, relative: str) -> dict[str, Any]:
        return read_artifact(active_session_root(self.repo_root), relative)


__all__ = ["HarnessUiApplication"]
