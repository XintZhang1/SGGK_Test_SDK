from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from test_harness.nx import (
    NxEnvironmentDetector,
    NxJournalPolicyError,
    NxJournalRunner,
    NxRootCandidate,
    ProcessResult,
    SubprocessExecutor,
    detect_nx_environment,
    execute_nx_journal,
    probe_nx_python,
)
from test_harness.nx.runtime_probe import collect_probe
import test_harness.nx.runner as nx_runner_module


class StubRegistry:
    def __init__(self, candidates: Sequence[NxRootCandidate] = ()) -> None:
        self.candidates = candidates

    def discover(self) -> Sequence[NxRootCandidate]:
        return self.candidates


class RecordingExecutor:
    def __init__(
        self,
        *,
        result: ProcessResult | None = None,
        probe_payload: dict[str, object] | None = None,
    ) -> None:
        self.result = result or ProcessResult(returncode=0, timed_out=False, duration_ms=12)
        self.probe_payload = probe_payload
        self.calls: list[dict[str, object]] = []

    def execute(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProcessResult:
        self.calls.append(
            {
                "command": list(command),
                "cwd": cwd,
                "environment": dict(environment),
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.probe_payload is not None:
            payload = dict(self.probe_payload)
            payload.setdefault("schema_version", 1)
            payload.setdefault("kind", "sggk_nx_python_probe")
            payload.setdefault("nonce", environment["SGGK_NX_PROBE_NONCE"])
            Path(environment["SGGK_NX_PROBE_OUTPUT"]).write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
        return self.result


def make_detector(
    *,
    environment: Mapping[str, str] | None = None,
    registry: StubRegistry | None = None,
    program_files_roots: Sequence[Path] = (),
    platform_name: str = "Windows",
) -> NxEnvironmentDetector:
    return NxEnvironmentDetector(
        environment={} if environment is None else environment,
        platform_name=platform_name,
        registry=registry or StubRegistry(),
        program_files_roots=program_files_roots,
        path_lookup=lambda _name: None,
    )


def make_nx_installation(root: Path, *, journal: bool = True, python: bool = True) -> Path:
    nxbin = root / "NXBIN"
    nxbin.mkdir(parents=True)
    (nxbin / "ugraf.exe").write_bytes(b"")
    if journal:
        (nxbin / "run_journal.exe").write_bytes(b"")
    if python:
        python_dir = nxbin / "python"
        python_dir.mkdir()
        (python_dir / "NXOpen.pyd").write_bytes(b"")
        (python_dir / "python312.dll").write_bytes(b"")
    return root


def test_static_detection_reports_not_found_without_importing_nxopen(tmp_path: Path) -> None:
    sys.modules.pop("NXOpen", None)
    report = detect_nx_environment(detector=make_detector(program_files_roots=[tmp_path]))

    assert report["ok"] is False
    assert report["status"] == "not_found"
    assert report["diagnostics"][0]["code"] == "NX_INSTALLATION_NOT_FOUND"
    assert "NXOpen" not in sys.modules


def test_non_windows_detection_is_explicitly_unsupported() -> None:
    report = detect_nx_environment(detector=make_detector(platform_name="Linux"))

    assert report["status"] == "unsupported_platform"
    assert report["supported_platform"] is False
    assert report["diagnostics"][0]["code"] == "NX_PLATFORM_UNSUPPORTED"


def test_explicit_nxbin_path_produces_ready_structured_report(tmp_path: Path) -> None:
    root = make_nx_installation(tmp_path / "NX2512")

    report = detect_nx_environment(
        explicit_roots=[root / "NXBIN"],
        detector=make_detector(),
    )

    assert report["ok"] is True
    assert report["status"] == "ready_for_probe"
    assert report["selected_root"] == str(root.resolve())
    installation = report["installations"][0]
    assert installation["sources"] == ["explicit"]
    assert installation["version_hint"] == "2512"
    assert installation["capabilities"] == {
        "nx_installed": True,
        "gui_executable": True,
        "journal_runner": True,
        "python_runtime_evidence": True,
        "python_api_verified": False,
    }
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "nx_environment_report.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)


def test_missing_programming_tools_is_diagnosed(tmp_path: Path) -> None:
    root = make_nx_installation(tmp_path / "NX2306", journal=False)

    report = detect_nx_environment(explicit_roots=[root], detector=make_detector())

    assert report["status"] == "incomplete"
    assert report["installations"][0]["diagnostics"][0]["code"] == "NX_JOURNAL_RUNNER_MISSING"


def test_explicit_selection_wins_over_newer_environment_installation(tmp_path: Path) -> None:
    explicit = make_nx_installation(tmp_path / "NX2206")
    automatic = make_nx_installation(tmp_path / "NX2512")
    detector = make_detector(environment={"UGII_BASE_DIR": str(automatic)})

    report = detector.detect([explicit])

    assert report.selected is not None
    assert report.selected.root == explicit.resolve()


def test_invalid_explicit_root_does_not_silently_fall_back(tmp_path: Path) -> None:
    automatic = make_nx_installation(tmp_path / "NX2512")
    configured = tmp_path / "missing-nx"
    detector = make_detector(environment={"UGII_BASE_DIR": str(automatic)})

    report = detector.detect([configured])

    assert report.ok is False
    assert report.status == "not_found"
    assert report.selected is not None
    assert report.selected.root == configured.resolve()
    assert report.diagnostics[0].code == "NX_CONFIGURED_INSTALLATION_INVALID"


def test_duplicate_environment_and_registry_candidates_are_merged(tmp_path: Path) -> None:
    root = make_nx_installation(tmp_path / "NX2506")
    detector = make_detector(
        environment={"ugii_base_dir": str(root)},
        registry=StubRegistry([NxRootCandidate(root / "NXBIN", "registry:test", "2506")]),
    )

    report = detector.detect()

    assert len(report.installations) == 1
    assert report.installations[0].sources == ("environment:UGII_BASE_DIR", "registry:test")


def test_probe_uses_fixed_bundled_script_and_authenticates_result(tmp_path: Path) -> None:
    root = make_nx_installation(tmp_path / "NX2512")
    executor = RecordingExecutor(
        probe_payload={
            "ok": True,
            "python": {"version": "3.12.4"},
            "nxopen": {"session_type": "Session"},
            "error": "",
        }
    )

    report = probe_nx_python(
        explicit_roots=[root],
        detector=make_detector(),
        executor=executor,
        process_environment={"PATH": "original"},
        timeout_seconds=9,
    )

    assert report["ok"] is True
    assert report["status"] == "verified"
    assert report["probe"]["nxopen"]["session_type"] == "Session"
    call = executor.calls[0]
    command = call["command"]
    assert isinstance(command, list)
    assert command[0] == str((root / "NXBIN" / "run_journal.exe").resolve())
    assert Path(command[1]).name == "runtime_probe.py"
    assert call["timeout_seconds"] == 9
    process_environment = call["environment"]
    assert isinstance(process_environment, dict)
    assert process_environment["UGII_BASE_DIR"] == str(root.resolve())
    assert process_environment["PATH"].startswith(str((root / "NXBIN").resolve()) + os.pathsep)


def test_probe_rejects_spoofed_or_missing_result(tmp_path: Path) -> None:
    root = make_nx_installation(tmp_path / "NX2512")
    executor = RecordingExecutor(
        probe_payload={
            "schema_version": 1,
            "kind": "sggk_nx_python_probe",
            "nonce": "wrong",
            "ok": True,
        }
    )

    report = probe_nx_python(
        explicit_roots=[root],
        detector=make_detector(),
        executor=executor,
    )

    assert report["ok"] is False
    assert report["status"] == "invalid_result"
    assert report["diagnostics"][0]["code"] == "NX_RUNTIME_PROBE_RESULT_INVALID"


def test_probe_timeout_has_stable_diagnostic(tmp_path: Path) -> None:
    root = make_nx_installation(tmp_path / "NX2512")
    executor = RecordingExecutor(
        result=ProcessResult(returncode=1, timed_out=True, duration_ms=100),
    )

    report = probe_nx_python(
        explicit_roots=[root],
        detector=make_detector(),
        executor=executor,
        timeout_seconds=0.1,
    )

    assert report["status"] == "timed_out"
    assert report["diagnostics"][0]["code"] == "NX_RUNTIME_PROBE_TIMEOUT"


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_nx_runner_rejects_non_finite_timeout_before_launch(tmp_path: Path, timeout: float) -> None:
    root = make_nx_installation(tmp_path / "NX2512")
    executor = RecordingExecutor()

    with pytest.raises(ValueError, match="finite and positive"):
        probe_nx_python(
            explicit_roots=[root],
            detector=make_detector(),
            executor=executor,
            timeout_seconds=timeout,
        )

    assert executor.calls == []


def test_journal_path_must_be_inside_an_allowed_root(tmp_path: Path) -> None:
    root = make_nx_installation(tmp_path / "NX2512")
    selected = make_detector().detect([root]).selected
    assert selected is not None
    runner = NxJournalRunner(selected, executor=RecordingExecutor())
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")

    with pytest.raises(NxJournalPolicyError, match="outside"):
        runner.run_journal(outside, allowed_roots=[tmp_path / "allowed"])


def test_journal_command_is_argument_vector_not_shell_text(tmp_path: Path) -> None:
    root = make_nx_installation(tmp_path / "NX2512")
    journals = tmp_path / "journals"
    journals.mkdir()
    journal = journals / "safe & journal.py"
    journal.write_text("print('ok')\n", encoding="utf-8")
    executor = RecordingExecutor()

    report = execute_nx_journal(
        journal,
        allowed_roots=[journals],
        arguments=["value & calc.exe", "two words"],
        explicit_roots=[root],
        detector=make_detector(),
        executor=executor,
    )

    assert report["ok"] is True
    command = executor.calls[0]["command"]
    assert command == [
        str((root / "NXBIN" / "run_journal.exe").resolve()),
        str(journal.resolve()),
        "-args",
        "value & calc.exe",
        "two words",
    ]


def test_runtime_probe_can_be_unit_tested_without_nx(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("NXOpen")

    class FakeSession:
        @staticmethod
        def GetSession() -> object:
            return object()

    fake.Session = FakeSession  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "NXOpen", fake)

    payload = collect_probe("nonce")

    assert payload["ok"] is True
    assert payload["nonce"] == "nonce"
    assert payload["nxopen"]["session_type"] == "object"


def test_runtime_probe_returns_structured_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "NXOpen", raising=False)
    import builtins

    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "NXOpen":
            raise ImportError("NXOpen is intentionally absent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    payload = collect_probe("nonce")

    assert payload["ok"] is False
    assert payload["error_type"] == "ImportError"
    assert payload["error"] == "NXOpen is intentionally absent"


def test_subprocess_executor_bounds_captured_output(tmp_path: Path) -> None:
    executor = SubprocessExecutor(output_limit=32)

    result = executor.execute(
        [sys.executable, "-c", "print('x' * 1000)"],
        cwd=tmp_path,
        environment=os.environ,
        timeout_seconds=10,
    )

    assert result.returncode == 0
    assert result.timed_out is False
    assert len(result.stdout_tail.encode("utf-8")) <= 32
    assert result.stdout_tail.endswith("\n")


def test_subprocess_executor_terminates_on_timeout(tmp_path: Path) -> None:
    executor = SubprocessExecutor()

    result = executor.execute(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        environment=os.environ,
        timeout_seconds=0.1,
    )

    assert result.timed_out is True
    assert result.duration_ms < 10_000


@pytest.mark.skipif(os.name != "nt", reason="taskkill fallback is Windows-specific")
def test_failed_taskkill_falls_back_to_process_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyProcess:
        pid = 4321

        def __init__(self) -> None:
            self.killed = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

    process = DummyProcess()
    monkeypatch.setattr(
        nx_runner_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode=1),
    )

    SubprocessExecutor._terminate_process_tree(process)  # type: ignore[arg-type]

    assert process.killed is True
