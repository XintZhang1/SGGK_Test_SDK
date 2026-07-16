"""Typed, JSON-serializable contracts for Siemens NX runtime support."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class NxDiagnostic:
    """One stable diagnostic suitable for both a CLI and the local UI."""

    code: str
    severity: str
    message: str
    remediation: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class NxInstallation:
    """Static evidence for one NX installation candidate.

    Static discovery deliberately does not import ``NXOpen``.  The presence of
    runtime files is useful evidence, but only the isolated journal probe can
    mark the Python API as verified.
    """

    root: Path
    sources: tuple[str, ...]
    version_hint: str = ""
    bin_dir: Path | None = None
    ugraf_path: Path | None = None
    run_journal_path: Path | None = None
    python_evidence: tuple[Path, ...] = ()
    diagnostics: tuple[NxDiagnostic, ...] = ()

    @property
    def installed(self) -> bool:
        return self.ugraf_path is not None or self.run_journal_path is not None

    @property
    def status(self) -> str:
        if not self.installed:
            return "invalid_candidate"
        if self.run_journal_path is None:
            return "incomplete"
        return "ready_for_probe"

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "sources": list(self.sources),
            "version_hint": self.version_hint,
            "status": self.status,
            "paths": {
                "bin_dir": str(self.bin_dir) if self.bin_dir else "",
                "ugraf": str(self.ugraf_path) if self.ugraf_path else "",
                "run_journal": str(self.run_journal_path) if self.run_journal_path else "",
            },
            "python_evidence": [str(path) for path in self.python_evidence],
            "capabilities": {
                "nx_installed": self.installed,
                "gui_executable": self.ugraf_path is not None,
                "journal_runner": self.run_journal_path is not None,
                "python_runtime_evidence": bool(self.python_evidence),
                "python_api_verified": False,
            },
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class NxEnvironment:
    """Result of side-effect-free NX environment discovery."""

    platform: str
    supported_platform: bool
    status: str
    installations: tuple[NxInstallation, ...] = ()
    selected_index: int | None = None
    diagnostics: tuple[NxDiagnostic, ...] = ()
    checked_sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def selected(self) -> NxInstallation | None:
        if self.selected_index is None:
            return None
        if self.selected_index < 0 or self.selected_index >= len(self.installations):
            return None
        return self.installations[self.selected_index]

    @property
    def ok(self) -> bool:
        return self.status == "ready_for_probe" and self.selected is not None

    def as_dict(self) -> dict[str, Any]:
        selected = self.selected
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": "detect",
            "ok": self.ok,
            "status": self.status,
            "platform": self.platform,
            "supported_platform": self.supported_platform,
            "checked_sources": list(self.checked_sources),
            "selected_root": str(selected.root) if selected else "",
            "installations": [item.as_dict() for item in self.installations],
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


__all__ = [
    "SCHEMA_VERSION",
    "NxDiagnostic",
    "NxEnvironment",
    "NxInstallation",
]
