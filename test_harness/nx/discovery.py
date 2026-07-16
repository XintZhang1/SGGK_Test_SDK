"""Side-effect-free Siemens NX installation discovery for Windows."""

from __future__ import annotations

import os
import platform
import re
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .contracts import NxDiagnostic, NxEnvironment, NxInstallation

ENVIRONMENT_ROOT_KEYS = (
    "UGII_BASE_DIR",
    "UGII_ROOT_DIR",
    "NX_ROOT_DIR",
)
REGISTRY_VALUE_NAMES = (
    "UGII_BASE_DIR",
    "NX_ROOT_DIR",
    "InstallPath",
    "InstallDir",
)
REGISTRY_ROOTS = (
    r"SOFTWARE\Unigraphics Solutions\NX",
    r"SOFTWARE\Siemens\NX",
)


@dataclass(frozen=True, slots=True)
class NxRootCandidate:
    path: Path
    source: str
    version_hint: str = ""


class RegistryCandidateProvider(Protocol):
    def discover(self) -> Sequence[NxRootCandidate]: ...


class EmptyRegistryCandidateProvider:
    def discover(self) -> Sequence[NxRootCandidate]:
        return ()


class WindowsRegistryCandidateProvider:
    """Read conventional machine-wide NX registry keys.

    ``winreg`` is imported only when discovery is explicitly run on Windows;
    importing this module remains safe on non-Windows test hosts.
    """

    def discover(self) -> Sequence[NxRootCandidate]:
        try:
            import winreg
        except ImportError:  # pragma: no cover - only possible off Windows
            return ()

        found: list[NxRootCandidate] = []
        views = (
            getattr(winreg, "KEY_WOW64_64KEY", 0),
            getattr(winreg, "KEY_WOW64_32KEY", 0),
        )
        for registry_path in REGISTRY_ROOTS:
            for view in dict.fromkeys(views):
                access = winreg.KEY_READ | view
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, registry_path, 0, access) as key:
                        found.extend(self._values_from_key(winreg, key, registry_path, ""))
                        index = 0
                        while True:
                            try:
                                child_name = winreg.EnumKey(key, index)
                            except OSError:
                                break
                            index += 1
                            try:
                                with winreg.OpenKey(key, child_name, 0, access) as child:
                                    found.extend(
                                        self._values_from_key(
                                            winreg,
                                            child,
                                            registry_path,
                                            child_name,
                                        )
                                    )
                            except OSError:
                                continue
                except OSError:
                    continue
        return found

    @staticmethod
    def _values_from_key(
        winreg: Any,
        key: Any,
        registry_path: str,
        version_hint: str,
    ) -> list[NxRootCandidate]:
        found: list[NxRootCandidate] = []
        for value_name in REGISTRY_VALUE_NAMES:
            try:
                raw, _ = winreg.QueryValueEx(key, value_name)
            except OSError:
                continue
            if isinstance(raw, str) and raw.strip():
                found.append(
                    NxRootCandidate(
                        Path(raw.strip().strip('"')),
                        f"registry:HKLM\\{registry_path}\\{version_hint}:{value_name}",
                        version_hint,
                    )
                )
        return found


class NxEnvironmentDetector:
    """Discover NX without importing its Python module or launching NX."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        platform_name: str | None = None,
        registry: RegistryCandidateProvider | None = None,
        program_files_roots: Sequence[str | Path] | None = None,
        path_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self.environment = dict(os.environ if environment is None else environment)
        self.platform_name = platform_name or platform.system()
        self.registry = registry or (
            WindowsRegistryCandidateProvider()
            if self.platform_name.casefold() == "windows"
            else EmptyRegistryCandidateProvider()
        )
        self.program_files_roots = (
            tuple(Path(item) for item in program_files_roots)
            if program_files_roots is not None
            else self._default_program_files_roots()
        )
        self.path_lookup = path_lookup or shutil.which

    def detect(self, explicit_roots: Iterable[str | Path] = ()) -> NxEnvironment:
        checked_sources = (
            "explicit",
            "environment",
            "registry",
            "PATH",
            "Program Files\\Siemens",
        )
        if self.platform_name.casefold() != "windows":
            diagnostic = NxDiagnostic(
                "NX_PLATFORM_UNSUPPORTED",
                "error",
                f"NX Python execution is supported only on Windows; detected {self.platform_name}.",
                "Run the NX integration on a supported Windows workstation.",
            )
            return NxEnvironment(
                platform=self.platform_name,
                supported_platform=False,
                status="unsupported_platform",
                diagnostics=(diagnostic,),
                checked_sources=checked_sources,
            )

        candidates: list[NxRootCandidate] = []
        candidates.extend(NxRootCandidate(Path(item), "explicit") for item in explicit_roots if str(item).strip())
        for key in ENVIRONMENT_ROOT_KEYS:
            value = self._environment_value(key)
            if value:
                candidates.append(NxRootCandidate(Path(value.strip().strip('"')), f"environment:{key}"))
        try:
            candidates.extend(self.registry.discover())
        except OSError:
            # A locked or corrupt registry entry must not make UI state loading fail.
            pass
        for executable in ("run_journal.exe", "ugraf.exe"):
            found = self.path_lookup(executable)
            if found:
                candidates.append(NxRootCandidate(Path(found), f"PATH:{executable}"))
        candidates.extend(self._common_path_candidates())

        grouped = self._group_candidates(candidates)
        installations = [self._inspect_candidate(root, records) for root, records in grouped]
        installations = self._sort_installations(installations)
        explicitly_configured = [item for item in installations if "explicit" in item.sources]
        selection_scope = explicitly_configured or installations
        installed = [item for item in selection_scope if item.installed]
        runnable = [item for item in installed if item.run_journal_path is not None]

        diagnostics: list[NxDiagnostic] = []
        if not installed:
            status = "not_found"
            selected_index = installations.index(selection_scope[0]) if selection_scope else None
            diagnostics.append(
                NxDiagnostic(
                    "NX_CONFIGURED_INSTALLATION_INVALID" if explicitly_configured else "NX_INSTALLATION_NOT_FOUND",
                    "error",
                    "The configured Siemens NX directory is not a usable installation."
                    if explicitly_configured
                    else "No Siemens NX installation was found in the configured or standard locations.",
                    "Correct or clear the configured NX directory."
                    if explicitly_configured
                    else "Install NX or choose its installation directory in the Harness UI.",
                )
            )
        elif not runnable:
            status = "incomplete"
            selected_index = installations.index(installed[0])
            diagnostics.append(
                NxDiagnostic(
                    "NX_JOURNAL_RUNNER_MISSING",
                    "error",
                    "NX was found, but run_journal.exe is unavailable.",
                    "Modify the NX installation and enable NX Open Programming Tools.",
                )
            )
        else:
            status = "ready_for_probe"
            selected_index = installations.index(runnable[0])
            diagnostics.append(
                NxDiagnostic(
                    "NX_RUNTIME_PROBE_REQUIRED",
                    "info",
                    "Static checks passed; run the isolated probe to verify NXOpen and licensing.",
                    "Use the NX Python probe action when no production NX job is running.",
                )
            )

        return NxEnvironment(
            platform=self.platform_name,
            supported_platform=True,
            status=status,
            installations=tuple(installations),
            selected_index=selected_index,
            diagnostics=tuple(diagnostics),
            checked_sources=checked_sources,
        )

    def _default_program_files_roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            value = self._environment_value(key)
            if value:
                roots.append(Path(value))
        return tuple(dict.fromkeys(roots))

    def _environment_value(self, key: str) -> str:
        wanted = key.casefold()
        for actual, value in self.environment.items():
            if actual.casefold() == wanted and isinstance(value, str):
                return value
        return ""

    def _common_path_candidates(self) -> list[NxRootCandidate]:
        found: list[NxRootCandidate] = []
        for program_files in self.program_files_roots:
            siemens = program_files / "Siemens"
            try:
                children = list(siemens.iterdir())
            except OSError:
                continue
            for child in children:
                looks_like_nx = child.name.casefold().startswith(("nx", "unigraphics", "designcenternx"))
                has_nxbin = (child / "NXBIN" / "ugraf.exe").is_file() or (child / "NXBIN" / "run_journal.exe").is_file()
                if child.is_dir() and (looks_like_nx or has_nxbin):
                    found.append(NxRootCandidate(child, "common_path"))
        return found

    @staticmethod
    def _normalize_root(path: Path) -> Path:
        candidate = path.expanduser()
        if candidate.name.casefold() in {"run_journal.exe", "ugraf.exe"}:
            candidate = candidate.parent
        if candidate.name.casefold() in {"nxbin", "ugii"}:
            candidate = candidate.parent
        return candidate.resolve(strict=False)

    def _group_candidates(
        self,
        candidates: Iterable[NxRootCandidate],
    ) -> list[tuple[Path, tuple[NxRootCandidate, ...]]]:
        grouped: dict[str, tuple[Path, list[NxRootCandidate]]] = {}
        for candidate in candidates:
            root = self._normalize_root(candidate.path)
            key = str(root).casefold()
            if key not in grouped:
                grouped[key] = (root, [])
            grouped[key][1].append(candidate)
        return [(root, tuple(records)) for root, records in grouped.values()]

    @staticmethod
    def _first_file(paths: Iterable[Path]) -> Path | None:
        for path in paths:
            try:
                if path.is_file():
                    return path.resolve()
            except OSError:
                continue
        return None

    def _inspect_candidate(
        self,
        root: Path,
        records: Sequence[NxRootCandidate],
    ) -> NxInstallation:
        bin_candidates = (root / "NXBIN", root / "UGII", root)
        run_journal = self._first_file(path / "run_journal.exe" for path in bin_candidates)
        ugraf = self._first_file(path / "ugraf.exe" for path in bin_candidates)
        bin_dir = (run_journal or ugraf).parent if run_journal or ugraf else None
        evidence = self._python_evidence(bin_candidates)
        diagnostics: list[NxDiagnostic] = []
        if run_journal is None and ugraf is None:
            diagnostics.append(
                NxDiagnostic(
                    "NX_INSTALLATION_CANDIDATE_INVALID",
                    "warning",
                    f"The candidate directory does not contain NX executables: {root}",
                    "Choose the NX installation root, NXBIN directory, or run_journal.exe.",
                )
            )
        elif run_journal is None:
            diagnostics.append(
                NxDiagnostic(
                    "NX_JOURNAL_RUNNER_MISSING",
                    "error",
                    f"run_journal.exe was not found below {root}.",
                    "Enable NX Open Programming Tools in the NX installer.",
                )
            )
        elif not evidence:
            diagnostics.append(
                NxDiagnostic(
                    "NX_PYTHON_RUNTIME_EVIDENCE_NOT_FOUND",
                    "warning",
                    "run_journal.exe exists, but no conventional NXOpen Python runtime file was found.",
                    "Run the isolated probe; if it fails, repair the NX Python programming tools.",
                )
            )
        version_hint = next((item.version_hint for item in records if item.version_hint), "")
        version_hint = version_hint or self._version_hint(root.name)
        return NxInstallation(
            root=root,
            sources=tuple(dict.fromkeys(item.source for item in records)),
            version_hint=version_hint,
            bin_dir=bin_dir,
            ugraf_path=ugraf,
            run_journal_path=run_journal,
            python_evidence=evidence,
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _python_evidence(bin_candidates: Sequence[Path]) -> tuple[Path, ...]:
        evidence: list[Path] = []
        for bin_dir in bin_candidates:
            python_dir = bin_dir / "python"
            exact = (
                python_dir / "NXOpen.pyd",
                python_dir / "NXOpen.dll",
                python_dir / "NXOpen" / "__init__.py",
                bin_dir / "NXOpen.pyd",
            )
            for path in exact:
                try:
                    if path.is_file():
                        evidence.append(path.resolve())
                except OSError:
                    continue
            try:
                evidence.extend(path.resolve() for path in python_dir.glob("python*.dll") if path.is_file())
            except OSError:
                continue
        return tuple(dict.fromkeys(evidence))

    @staticmethod
    def _version_hint(name: str) -> str:
        match = re.search(r"(?i)(?:NX[\s_-]*)?(\d{2,4}(?:\.\d+)*)", name)
        return match.group(1) if match else ""

    @classmethod
    def _sort_installations(cls, installations: Sequence[NxInstallation]) -> list[NxInstallation]:
        def version_key(item: NxInstallation) -> tuple[int, ...]:
            return tuple(int(value) for value in re.findall(r"\d+", item.version_hint))

        def source_priority(item: NxInstallation) -> int:
            priorities = []
            for source in item.sources:
                if source == "explicit":
                    priorities.append(0)
                elif source.startswith("environment:"):
                    priorities.append(1)
                elif source.startswith("registry:"):
                    priorities.append(2)
                elif source.startswith("PATH:"):
                    priorities.append(3)
                else:
                    priorities.append(4)
            return min(priorities, default=5)

        by_version = sorted(installations, key=version_key, reverse=True)
        return sorted(by_version, key=source_priority)


__all__ = [
    "EmptyRegistryCandidateProvider",
    "NxEnvironmentDetector",
    "NxRootCandidate",
    "RegistryCandidateProvider",
    "WindowsRegistryCandidateProvider",
]
