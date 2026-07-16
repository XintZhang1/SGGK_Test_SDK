"""Validated UI settings and Windows-backed secret storage."""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from test_harness.authoring_gateway.config import (
    DEFAULT_PROFILE,
    PROFILE_SPECS,
    SILICONFLOW_DEFAULT_BASE_URL,
    SILICONFLOW_DEFAULT_MODEL,
)

CONFIG_SCHEMA_VERSION = 2
CREDENTIAL_TARGET_PREFIX = "SGGK.TestHarness.MessageApi"
DEFAULT_BASE_URL = SILICONFLOW_DEFAULT_BASE_URL
DEFAULT_MODEL = SILICONFLOW_DEFAULT_MODEL


class UiSettingsError(ValueError):
    """Settings cannot be accepted safely."""


class SecretStore(Protocol):
    def read(self, profile: str) -> str: ...

    def write(self, profile: str, secret: str) -> None: ...

    def delete(self, profile: str) -> None: ...


class MemorySecretStore:
    """Process-local secret storage used on non-Windows systems and in tests."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def read(self, profile: str) -> str:
        return self._values.get(profile, "")

    def write(self, profile: str, secret: str) -> None:
        self._values[profile] = secret

    def delete(self, profile: str) -> None:
        self._values.pop(profile, None)


if os.name == "nt":
    LPBYTE = ctypes.POINTER(ctypes.c_ubyte)

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", wintypes.LPVOID),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentialStore:
    """Store API keys in the current user's Windows Credential Manager."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Credential Manager is unavailable")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
        ]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [wintypes.LPVOID]
        self._advapi32.CredFree.restype = None

    @staticmethod
    def _target(profile: str) -> str:
        return f"{CREDENTIAL_TARGET_PREFIX}.{profile}"

    def read(self, profile: str) -> str:
        pointer = ctypes.POINTER(CREDENTIALW)()
        if not self._advapi32.CredReadW(
            self._target(profile),
            self.CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return ""
            raise OSError(error, "CredReadW failed")
        try:
            credential = pointer.contents
            if not credential.CredentialBlob or credential.CredentialBlobSize == 0:
                return ""
            payload = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            return payload.decode("utf-16-le")
        finally:
            self._advapi32.CredFree(pointer)

    def write(self, profile: str, secret: str) -> None:
        if not secret:
            self.delete(profile)
            return
        payload = secret.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        credential = CREDENTIALW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = self._target(profile)
        credential.CredentialBlobSize = len(payload)
        credential.CredentialBlob = ctypes.cast(buffer, LPBYTE)
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "SGGK Harness"
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()
            raise OSError(error, "CredWriteW failed")

    def delete(self, profile: str) -> None:
        if self._advapi32.CredDeleteW(
            self._target(profile),
            self.CRED_TYPE_GENERIC,
            0,
        ):
            return
        error = ctypes.get_last_error()
        if error != self.ERROR_NOT_FOUND:
            raise OSError(error, "CredDeleteW failed")


def default_secret_store() -> SecretStore:
    if os.name == "nt":
        return WindowsCredentialStore()
    return MemorySecretStore()


@dataclass(frozen=True)
class UiSettings:
    schema_version: int = CONFIG_SCHEMA_VERSION
    profile: str = DEFAULT_PROFILE
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    ca_bundle: str = ""
    sdk_dir: str = ""
    source_root: str = ""
    runner_path: str = ""
    campaign_dataset: str = ""
    nx_root_dir: str = ""
    nx_probe_timeout_seconds: float = 120.0
    candidate_count: int = 3
    candidate_parallelism: int = 3
    jobs: int = 1
    execution_timeout_seconds: float = 180.0
    thinking_mode: str = "enabled"

    def public_dict(self, *, api_key_configured: bool) -> dict[str, Any]:
        return {**asdict(self), "api_key_configured": api_key_configured}


class UiSettingsStore:
    """Persist non-secret settings atomically under ignored artifacts/."""

    def __init__(
        self,
        repo_root: Path,
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.path = self.repo_root / "artifacts" / "harness_ui" / "config.json"
        self.secret_store = secret_store or default_secret_store()

    def load(self) -> UiSettings:
        if not self.path.is_file():
            return UiSettings(runner_path=self._default_runner())
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UiSettingsError(f"UI settings cannot be read: {exc}") from exc
        if not isinstance(value, dict):
            raise UiSettingsError("UI settings root must be an object")
        value = self._migrate(value)
        allowed = {item.name for item in fields(UiSettings)}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise UiSettingsError(f"UI settings contain unknown fields: {unknown}")
        settings = UiSettings(**value)
        return self.validate(settings, require_existing_paths=False)

    def save(
        self,
        value: dict[str, Any],
        *,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> UiSettings:
        allowed = {item.name for item in fields(UiSettings)} - {"schema_version"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise UiSettingsError(f"unsupported UI settings: {unknown}")
        current = self.load()
        merged = asdict(current)
        merged.update(value)
        merged["schema_version"] = CONFIG_SCHEMA_VERSION
        settings = self.validate(UiSettings(**merged), require_existing_paths=False)
        if clear_api_key:
            self.secret_store.delete(settings.profile)
        elif api_key is not None and api_key.strip():
            self.secret_store.write(settings.profile, api_key.strip())
        self._atomic_write(json.dumps(asdict(settings), indent=2, ensure_ascii=False) + "\n")
        return settings

    def api_key(self, profile: str) -> str:
        stored = self.secret_store.read(profile)
        if stored:
            return stored
        spec = PROFILE_SPECS.get(profile)
        return str(os.environ.get(spec.api_key_env, "") if spec else "").strip()

    def public(self) -> dict[str, Any]:
        settings = self.load()
        return settings.public_dict(api_key_configured=bool(self.api_key(settings.profile)))

    def validate(
        self,
        settings: UiSettings,
        *,
        require_existing_paths: bool,
    ) -> UiSettings:
        if settings.schema_version != CONFIG_SCHEMA_VERSION:
            raise UiSettingsError("unsupported UI settings schema version")
        if settings.profile not in PROFILE_SPECS:
            raise UiSettingsError(f"profile must be one of {sorted(PROFILE_SPECS)}")
        base_url = settings.base_url.strip().rstrip("/")
        if base_url:
            parsed = urlsplit(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise UiSettingsError("Message API base URL must be absolute http(s)")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise UiSettingsError("Message API base URL cannot contain credentials, query, or fragment")
            if settings.profile == DEFAULT_PROFILE:
                if parsed.scheme != "https":
                    raise UiSettingsError("SiliconFlow base URL must use https")
                if base_url != DEFAULT_BASE_URL:
                    raise UiSettingsError(f"SiliconFlow base URL must be {DEFAULT_BASE_URL}")
        model = settings.model.strip()
        if settings.profile == DEFAULT_PROFILE and model != DEFAULT_MODEL:
            raise UiSettingsError(f"SiliconFlow model must be {DEFAULT_MODEL}")
        if not 1 <= int(settings.candidate_count) <= 8:
            raise UiSettingsError("candidate count must be between 1 and 8")
        if not 1 <= int(settings.candidate_parallelism) <= int(settings.candidate_count):
            raise UiSettingsError("candidate parallelism must be between 1 and candidate count")
        if not 1 <= int(settings.jobs) <= 64:
            raise UiSettingsError("runner jobs must be between 1 and 64")
        if float(settings.execution_timeout_seconds) <= 0:
            raise UiSettingsError("execution timeout must be positive")
        if not 5 <= float(settings.nx_probe_timeout_seconds) <= 600:
            raise UiSettingsError("NX probe timeout must be between 5 and 600 seconds")
        if settings.thinking_mode not in {"omit", "disabled", "enabled"}:
            raise UiSettingsError("thinking mode is invalid")
        normalized_paths: dict[str, str] = {}
        for name in ("ca_bundle", "sdk_dir", "source_root", "runner_path", "campaign_dataset"):
            raw = str(getattr(settings, name) or "").strip()
            if not raw:
                normalized_paths[name] = ""
                continue
            path = Path(raw).expanduser().resolve()
            if require_existing_paths and not path.exists():
                raise UiSettingsError(f"configured path does not exist: {name}")
            normalized_paths[name] = str(path)
        nx_root_dir = str(settings.nx_root_dir or "").strip()
        if nx_root_dir:
            nx_root_dir = str(Path(nx_root_dir).expanduser().resolve())
        source_root = normalized_paths["source_root"] if settings.profile == "intranet" else ""
        return UiSettings(
            schema_version=CONFIG_SCHEMA_VERSION,
            profile=settings.profile,
            base_url=base_url,
            model=model,
            ca_bundle=normalized_paths["ca_bundle"],
            sdk_dir=normalized_paths["sdk_dir"],
            source_root=source_root,
            runner_path=normalized_paths["runner_path"],
            campaign_dataset=normalized_paths["campaign_dataset"],
            nx_root_dir=nx_root_dir,
            nx_probe_timeout_seconds=float(settings.nx_probe_timeout_seconds),
            candidate_count=int(settings.candidate_count),
            candidate_parallelism=int(settings.candidate_parallelism),
            jobs=int(settings.jobs),
            execution_timeout_seconds=float(settings.execution_timeout_seconds),
            thinking_mode=settings.thinking_mode,
        )

    @staticmethod
    def _migrate(value: dict[str, Any]) -> dict[str, Any]:
        try:
            version = int(value.get("schema_version", 1) or 1)
        except (TypeError, ValueError) as exc:
            raise UiSettingsError("UI settings schema_version must be an integer") from exc
        if version == CONFIG_SCHEMA_VERSION:
            return value
        if version != 1:
            return value
        migrated = dict(value)
        if migrated.get("profile") in {None, "", "intranet", "siliconflow-test"}:
            migrated.update(
                {
                    "profile": DEFAULT_PROFILE,
                    "base_url": DEFAULT_BASE_URL,
                    "model": DEFAULT_MODEL,
                    "source_root": "",
                    "thinking_mode": "enabled",
                }
            )
        migrated.setdefault("nx_root_dir", "")
        migrated.setdefault("nx_probe_timeout_seconds", 120.0)
        migrated["schema_version"] = CONFIG_SCHEMA_VERSION
        return migrated

    def _default_runner(self) -> str:
        runner = self.repo_root / "build" / "test_harness" / "Release" / "sggk_case_runner.exe"
        return str(runner.resolve()) if runner.is_file() else ""

    def _atomic_write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


__all__ = [
    "MemorySecretStore",
    "SecretStore",
    "UiSettings",
    "UiSettingsError",
    "UiSettingsStore",
    "WindowsCredentialStore",
]
