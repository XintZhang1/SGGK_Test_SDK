"""Backend entry points for NX detection, probing, and Harness execution."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .contracts import SCHEMA_VERSION, NxDiagnostic, NxEnvironment
from .discovery import NxEnvironmentDetector
from .runner import NxJournalRunner, ProcessExecutor


def inspect_nx_environment(
    *,
    explicit_roots: Sequence[str | Path] = (),
    detector: NxEnvironmentDetector | None = None,
) -> NxEnvironment:
    return (detector or NxEnvironmentDetector()).detect(explicit_roots)


def detect_nx_environment(
    *,
    explicit_roots: Sequence[str | Path] = (),
    detector: NxEnvironmentDetector | None = None,
) -> dict[str, Any]:
    return inspect_nx_environment(explicit_roots=explicit_roots, detector=detector).as_dict()


def probe_nx_python(
    *,
    explicit_roots: Sequence[str | Path] = (),
    timeout_seconds: float = 120.0,
    detector: NxEnvironmentDetector | None = None,
    executor: ProcessExecutor | None = None,
    process_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = inspect_nx_environment(explicit_roots=explicit_roots, detector=detector)
    selected = environment.selected
    if selected is None or selected.run_journal_path is None:
        return _unavailable_report("probe", environment)
    runner = NxJournalRunner(
        selected,
        executor=executor,
        environment=os.environ if process_environment is None else process_environment,
    )
    result = runner.probe(timeout_seconds=timeout_seconds)
    result["environment"] = environment.as_dict()
    return result


def execute_nx_journal(
    journal_path: str | Path,
    *,
    allowed_roots: Sequence[str | Path],
    arguments: Sequence[str] = (),
    explicit_roots: Sequence[str | Path] = (),
    timeout_seconds: float = 300.0,
    detector: NxEnvironmentDetector | None = None,
    executor: ProcessExecutor | None = None,
    process_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = inspect_nx_environment(explicit_roots=explicit_roots, detector=detector)
    selected = environment.selected
    if selected is None or selected.run_journal_path is None:
        return _unavailable_report("run", environment)
    runner = NxJournalRunner(
        selected,
        executor=executor,
        environment=os.environ if process_environment is None else process_environment,
    )
    result = runner.run_journal(
        journal_path,
        allowed_roots=allowed_roots,
        arguments=arguments,
        timeout_seconds=timeout_seconds,
    )
    result["environment"] = environment.as_dict()
    return result


def _unavailable_report(operation: str, environment: NxEnvironment) -> dict[str, Any]:
    diagnostic = NxDiagnostic(
        "NX_EXECUTION_UNAVAILABLE",
        "error",
        "No NX installation with run_journal.exe is available.",
        "Resolve the environment diagnostics before starting an NX operation.",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "ok": False,
        "status": "unavailable",
        "environment": environment.as_dict(),
        "execution": {},
        "probe": {} if operation == "probe" else None,
        "diagnostics": [diagnostic.as_dict(), *environment.as_dict()["diagnostics"]],
    }


__all__ = [
    "detect_nx_environment",
    "execute_nx_journal",
    "inspect_nx_environment",
    "probe_nx_python",
]
