"""Timeout-bounded and policy-checked execution through NX run_journal.exe."""

from __future__ import annotations

import json
import math
import os
import secrets
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .contracts import SCHEMA_VERSION, NxDiagnostic, NxInstallation

PROBE_KIND = "sggk_nx_python_probe"
PROBE_PREFIX = "SGGK_NX_PROBE_JSON="
MAX_ARGUMENTS = 32
MAX_ARGUMENT_CHARS = 4096
DEFAULT_OUTPUT_LIMIT = 64 * 1024


class NxJournalPolicyError(ValueError):
    """Raised when a requested journal violates the local execution policy."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int | None
    timed_out: bool
    duration_ms: int
    stdout_tail: str = ""
    stderr_tail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


class ProcessExecutor(Protocol):
    def execute(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProcessResult: ...


class SubprocessExecutor:
    """Run without a shell, spool output to disk, and terminate on timeout."""

    def __init__(self, *, output_limit: int = DEFAULT_OUTPUT_LIMIT) -> None:
        if output_limit <= 0:
            raise ValueError("output_limit must be positive")
        self.output_limit = output_limit

    def execute(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProcessResult:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        started = time.monotonic()
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0,
            )
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                creationflags=creationflags,
            )
            timed_out = False
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_process_tree(process)
                try:
                    returncode = process.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover - last-resort OS failure
                    process.kill()
                    returncode = process.wait()
            except BaseException:
                self._terminate_process_tree(process)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover - last-resort OS failure
                    process.kill()
                    process.wait()
                raise
            duration_ms = int((time.monotonic() - started) * 1000)
            return ProcessResult(
                returncode=returncode,
                timed_out=timed_out,
                duration_ms=duration_ms,
                stdout_tail=self._read_tail(stdout_file),
                stderr_tail=self._read_tail(stderr_file),
            )

    def _read_tail(self, stream: Any) -> str:
        stream.seek(0, os.SEEK_END)
        length = stream.tell()
        stream.seek(max(0, length - self.output_limit))
        return stream.read().decode("utf-8", errors="replace")

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    shell=False,
                )
                if completed.returncode != 0 and process.poll() is None:
                    process.kill()
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:  # pragma: no cover - NX execution is Windows-only
            process.terminate()


class NxJournalRunner:
    """Execute only validated journals through one selected NX installation."""

    def __init__(
        self,
        installation: NxInstallation,
        *,
        executor: ProcessExecutor | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if installation.run_journal_path is None:
            raise ValueError("the selected NX installation has no run_journal.exe")
        self.installation = installation
        self.executor = executor or SubprocessExecutor()
        self.environment = dict(os.environ if environment is None else environment)

    def probe(self, *, timeout_seconds: float = 120.0) -> dict[str, Any]:
        """Run the bundled minimal probe; never import NXOpen in this process."""

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        probe_script = Path(__file__).with_name("runtime_probe.py").resolve()
        nonce = secrets.token_urlsafe(24)
        with tempfile.TemporaryDirectory(prefix="sggk-nx-probe-") as temporary:
            output_path = Path(temporary) / "probe_result.json"
            environment = self._nx_environment()
            environment["SGGK_NX_PROBE_OUTPUT"] = str(output_path)
            environment["SGGK_NX_PROBE_NONCE"] = nonce
            try:
                execution = self.executor.execute(
                    [
                        str(self.installation.run_journal_path),
                        "-nx",
                        str(probe_script),
                        "-args",
                        nonce,
                        str(output_path),
                    ],
                    cwd=probe_script.parent,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                )
            except OSError as exc:
                return self._launch_failure("probe", exc)
            payload = self._read_probe_payload(output_path, execution.stdout_tail)

        diagnostics: list[NxDiagnostic] = []
        if execution.timed_out:
            status = "timed_out"
            diagnostics.append(
                NxDiagnostic(
                    "NX_RUNTIME_PROBE_TIMEOUT",
                    "error",
                    f"The NX Python probe exceeded {timeout_seconds:g} seconds and was terminated.",
                    "Check NX startup customizations, license availability, and stale NX processes.",
                )
            )
        elif not self._valid_probe_payload(payload, nonce):
            status = "invalid_result"
            validation_detail = self._probe_validation_detail(payload, nonce)
            diagnostics.append(
                NxDiagnostic(
                    "NX_RUNTIME_PROBE_RESULT_INVALID",
                    "error",
                    "The NX journal process did not return a Harness-authenticated "
                    "(nonce-validated) probe result; this is not a Siemens license "
                    "authentication status. "
                    + validation_detail,
                    "Inspect the bounded stdout/stderr tails and repair the NX Python runtime.",
                )
            )
        elif execution.returncode != 0 or not payload.get("ok"):
            status = "unavailable"
            diagnostics.append(
                NxDiagnostic(
                    "NX_PYTHON_API_UNAVAILABLE",
                    "error",
                    str(payload.get("error") or "NXOpen could not be initialized."),
                    "Verify NX Open Python is installed and an NX license can be checked out.",
                )
            )
        else:
            status = "verified"
            diagnostics.append(
                NxDiagnostic(
                    "NX_PYTHON_API_VERIFIED",
                    "info",
                    "NXOpen imported and returned a live Session from the isolated journal process.",
                )
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": "probe",
            "ok": status == "verified",
            "status": status,
            "installation": self.installation.as_dict(),
            "execution": execution.as_dict(),
            "probe": payload if isinstance(payload, dict) else {},
            "diagnostics": [item.as_dict() for item in diagnostics],
        }

    def run_journal(
        self,
        journal_path: str | Path,
        *,
        allowed_roots: Sequence[str | Path],
        arguments: Sequence[str] = (),
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Run a Python journal after allow-list and argument validation.

        This protects the Harness command boundary from path/argument injection;
        it is not a sandbox for NXOpen code, which has the user's OS privileges.
        """

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        journal = self.validate_journal_path(journal_path, allowed_roots=allowed_roots)
        safe_arguments = self.validate_arguments(arguments)
        command = [str(self.installation.run_journal_path), str(journal)]
        if safe_arguments:
            command.extend(["-args", *safe_arguments])
        try:
            execution = self.executor.execute(
                command,
                cwd=journal.parent,
                environment=self._nx_environment(),
                timeout_seconds=timeout_seconds,
            )
        except OSError as exc:
            return self._launch_failure("run", exc, journal=journal)
        status = "timed_out" if execution.timed_out else "completed" if execution.returncode == 0 else "failed"
        diagnostic = (
            NxDiagnostic("NX_JOURNAL_COMPLETED", "info", "The NX Python journal completed successfully.")
            if status == "completed"
            else NxDiagnostic(
                "NX_JOURNAL_TIMEOUT" if status == "timed_out" else "NX_JOURNAL_FAILED",
                "error",
                "The NX Python journal timed out and was terminated."
                if status == "timed_out"
                else f"The NX Python journal returned exit code {execution.returncode}.",
                "Inspect the bounded stdout/stderr tails and the journal's own artifacts.",
            )
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": "run",
            "ok": status == "completed",
            "status": status,
            "installation": self.installation.as_dict(),
            "journal": str(journal),
            "argument_count": len(safe_arguments),
            "execution": execution.as_dict(),
            "diagnostics": [diagnostic.as_dict()],
        }

    @staticmethod
    def validate_journal_path(
        journal_path: str | Path,
        *,
        allowed_roots: Sequence[str | Path],
    ) -> Path:
        if not allowed_roots:
            raise NxJournalPolicyError("at least one allowed journal root is required")
        journal = Path(journal_path).expanduser().resolve(strict=False)
        if journal.suffix.casefold() != ".py":
            raise NxJournalPolicyError("NX journals must use the .py extension")
        if not journal.is_file():
            raise NxJournalPolicyError(f"NX journal does not exist: {journal}")
        roots = [Path(root).expanduser().resolve(strict=False) for root in allowed_roots]
        if not any(journal.is_relative_to(root) for root in roots):
            raise NxJournalPolicyError("NX journal is outside the configured allowed roots")
        return journal

    @staticmethod
    def validate_arguments(arguments: Sequence[str]) -> list[str]:
        if len(arguments) > MAX_ARGUMENTS:
            raise NxJournalPolicyError(f"NX journal accepts at most {MAX_ARGUMENTS} arguments")
        safe: list[str] = []
        for argument in arguments:
            if not isinstance(argument, str):
                raise NxJournalPolicyError("NX journal arguments must be strings")
            if "\x00" in argument:
                raise NxJournalPolicyError("NX journal arguments may not contain NUL bytes")
            if len(argument) > MAX_ARGUMENT_CHARS:
                raise NxJournalPolicyError(f"each NX journal argument must be at most {MAX_ARGUMENT_CHARS} characters")
            safe.append(argument)
        return safe

    def _nx_environment(self) -> dict[str, str]:
        environment = dict(self.environment)
        bin_dir = self.installation.bin_dir
        environment["UGII_BASE_DIR"] = str(self.installation.root)
        if bin_dir is not None:
            environment["UGII_ROOT_DIR"] = str(bin_dir) + os.sep
            prior_path = environment.get("PATH", "")
            environment["PATH"] = str(bin_dir) + (os.pathsep + prior_path if prior_path else "")
        return environment

    @staticmethod
    def _read_probe_payload(output_path: Path, stdout_tail: str) -> dict[str, Any]:
        try:
            loaded = json.loads(output_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                return loaded
        except (OSError, json.JSONDecodeError):
            pass
        for line in reversed(stdout_tail.splitlines()):
            if line.startswith(PROBE_PREFIX):
                try:
                    loaded = json.loads(line[len(PROBE_PREFIX) :])
                except json.JSONDecodeError:
                    return {}
                return loaded if isinstance(loaded, dict) else {}
        return {}

    @staticmethod
    def _valid_probe_payload(payload: Mapping[str, Any], nonce: str) -> bool:
        return (
            payload.get("kind") == PROBE_KIND
            and payload.get("schema_version") == SCHEMA_VERSION
            and secrets.compare_digest(str(payload.get("nonce") or ""), nonce)
        )

    @staticmethod
    def _probe_validation_detail(payload: Mapping[str, Any], nonce: str) -> str:
        if not payload:
            return "No structured probe payload was captured."
        failures: list[str] = []
        if payload.get("kind") != PROBE_KIND:
            failures.append("kind mismatch")
        if payload.get("schema_version") != SCHEMA_VERSION:
            failures.append("schema version mismatch")
        if not secrets.compare_digest(str(payload.get("nonce") or ""), nonce):
            failures.append("nonce mismatch")
        return "Validation failed: " + ", ".join(failures or ["unknown reason"]) + "."

    def _launch_failure(
        self,
        operation: str,
        error: OSError,
        *,
        journal: Path | None = None,
    ) -> dict[str, Any]:
        diagnostic = NxDiagnostic(
            "NX_PROCESS_LAUNCH_FAILED",
            "error",
            f"Could not launch run_journal.exe: {error}",
            "Verify the selected NX installation and local execution permissions.",
        )
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "operation": operation,
            "ok": False,
            "status": "launch_failed",
            "installation": self.installation.as_dict(),
            "execution": {},
            "diagnostics": [diagnostic.as_dict()],
        }
        if journal is not None:
            result["journal"] = str(journal)
        if operation == "probe":
            result["probe"] = {}
        return result


__all__ = [
    "DEFAULT_OUTPUT_LIMIT",
    "MAX_ARGUMENTS",
    "MAX_ARGUMENT_CHARS",
    "PROBE_KIND",
    "PROBE_PREFIX",
    "NxJournalPolicyError",
    "NxJournalRunner",
    "ProcessExecutor",
    "ProcessResult",
    "SubprocessExecutor",
]
