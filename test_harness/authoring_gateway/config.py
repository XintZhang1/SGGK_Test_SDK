"""Environment-backed provider profiles for the authoring gateway."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit


class ConfigError(ValueError):
    """Raised when a provider profile is incomplete or unsafe."""


DEFAULT_PROFILE = "siliconflow"
SILICONFLOW_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_DEFAULT_MODEL = "zai-org/GLM-5.2"


@dataclass(frozen=True)
class ProfileSpec:
    """Names of environment variables used by one explicit provider profile."""

    name: str
    category: str
    base_url_env: str
    api_key_env: str
    model_env: str
    ca_bundle_env: str
    api_key_required: bool
    provenance_source_type: str
    default_base_url: str = ""
    default_model: str = ""
    require_https: bool = False
    base_url_locked: bool = False
    model_locked: bool = False
    default_thinking_mode: str = "omit"


PROFILE_SPECS: Mapping[str, ProfileSpec] = MappingProxyType(
    {
        "intranet": ProfileSpec(
            name="intranet",
            category="intranet",
            base_url_env="SGGK_QWEN_BASE_URL",
            api_key_env="SGGK_QWEN_API_KEY",
            model_env="SGGK_QWEN_MODEL",
            ca_bundle_env="SGGK_QWEN_CA_BUNDLE",
            api_key_required=False,
            provenance_source_type="intranet_message_api",
        ),
        "siliconflow": ProfileSpec(
            name="siliconflow",
            category="external",
            base_url_env="SILICONFLOW_BASE_URL",
            api_key_env="SILICONFLOW_API_KEY",
            model_env="SILICONFLOW_MODEL",
            ca_bundle_env="SILICONFLOW_CA_BUNDLE",
            api_key_required=True,
            provenance_source_type="siliconflow_message_api",
            default_base_url=SILICONFLOW_DEFAULT_BASE_URL,
            default_model=SILICONFLOW_DEFAULT_MODEL,
            require_https=True,
            base_url_locked=True,
            model_locked=True,
            default_thinking_mode="enabled",
        ),
    }
)


def _safe_base_url(value: str, env_name: str, *, require_https: bool = False) -> str:
    text = value.strip().rstrip("/")
    if not text:
        raise ConfigError(f"missing base URL: set {env_name}")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{env_name} must be an absolute http(s) URL")
    if require_https and parsed.scheme != "https":
        raise ConfigError(f"{env_name} must use https for this provider profile")
    if parsed.username or parsed.password:
        raise ConfigError(f"{env_name} must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{env_name} must not contain a query or fragment")
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def _optional_positive_float(value: float | None, default: float, label: str) -> float:
    result = default if value is None else float(value)
    if result <= 0:
        raise ConfigError(f"{label} must be positive")
    return result


def _optional_nonnegative_int(value: int | None, default: int, label: str) -> int:
    result = default if value is None else int(value)
    if result < 0:
        raise ConfigError(f"{label} must be >= 0")
    return result


@dataclass(frozen=True)
class GatewayConfig:
    """Resolved in-memory provider configuration.

    ``api_key`` is intentionally excluded from repr and from ``public_metadata``.
    All persisted records use only the corresponding environment-variable name
    and a boolean indicating whether authentication was configured.
    """

    profile: ProfileSpec
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    ca_bundle: str = ""
    request_timeout_seconds: float = 300.0
    max_retries: int = 2
    backoff_base_seconds: float = 1.0
    max_retry_delay_seconds: float = 30.0
    response_bytes_limit: int = 16 * 1024 * 1024

    @property
    def endpoint_url(self) -> str:
        if self.base_url.lower().endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.api_key,) if self.api_key else ()

    def public_metadata(self) -> dict[str, object]:
        endpoint_sha256 = hashlib.sha256(self.endpoint_url.encode("utf-8")).hexdigest()
        return {
            "profile": self.profile.name,
            "profile_category": self.profile.category,
            "endpoint_sha256": endpoint_sha256,
            "base_url_env": self.profile.base_url_env,
            "api_key_env": self.profile.api_key_env,
            "api_key_present": bool(self.api_key),
            "model": self.model,
            "model_env": self.profile.model_env,
            "base_url_locked": self.profile.base_url_locked,
            "model_locked": self.profile.model_locked,
            "default_thinking_mode": self.profile.default_thinking_mode,
            "ca_bundle_env": self.profile.ca_bundle_env,
            "ca_bundle_configured": bool(self.ca_bundle),
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
            "backoff_base_seconds": self.backoff_base_seconds,
            "max_retry_delay_seconds": self.max_retry_delay_seconds,
            "response_bytes_limit": self.response_bytes_limit,
        }


def load_gateway_config(
    profile_name: str = DEFAULT_PROFILE,
    *,
    environ: Mapping[str, str] | None = None,
    request_timeout_seconds: float | None = None,
    max_retries: int | None = None,
    backoff_base_seconds: float | None = None,
    max_retry_delay_seconds: float | None = None,
    response_bytes_limit: int | None = None,
) -> GatewayConfig:
    """Resolve a named profile from safe defaults plus environment overrides.

    The production SiliconFlow profile pins the public endpoint and GLM-5.2
    model as non-secret defaults. Credentials always come from the environment
    (or an equivalent in-memory mapping supplied by the UI). The legacy
    intranet profile remains fully explicit and fails closed when incomplete.
    """

    env = os.environ if environ is None else environ
    try:
        profile = PROFILE_SPECS[profile_name]
    except KeyError as exc:
        raise ConfigError(f"unknown profile {profile_name!r}; choose one of {sorted(PROFILE_SPECS)}") from exc

    configured_base_url = str(env.get(profile.base_url_env, "")).strip()
    base_url = _safe_base_url(
        configured_base_url or profile.default_base_url,
        profile.base_url_env,
        require_https=profile.require_https,
    )
    if profile.base_url_locked and base_url != profile.default_base_url:
        raise ConfigError(
            f"{profile.base_url_env} must be {profile.default_base_url!r} for profile {profile.name!r}"
        )
    model = str(env.get(profile.model_env, "")).strip() or profile.default_model
    if not model:
        raise ConfigError(f"missing model: set {profile.model_env}")
    if profile.model_locked and model != profile.default_model:
        raise ConfigError(
            f"{profile.model_env} must be {profile.default_model!r} for profile {profile.name!r}"
        )
    api_key = str(env.get(profile.api_key_env, "")).strip()
    if profile.api_key_required and not api_key:
        raise ConfigError(f"missing API key for {profile.name}: set {profile.api_key_env}")

    ca_bundle = str(env.get(profile.ca_bundle_env, "")).strip()
    if ca_bundle and not Path(ca_bundle).is_file():
        raise ConfigError(f"CA bundle does not exist: {profile.ca_bundle_env}={ca_bundle!r}")

    byte_limit = _optional_nonnegative_int(response_bytes_limit, 16 * 1024 * 1024, "response byte limit")
    if byte_limit == 0:
        raise ConfigError("response byte limit must be positive")
    return GatewayConfig(
        profile=profile,
        base_url=base_url,
        model=model,
        api_key=api_key,
        ca_bundle=ca_bundle,
        request_timeout_seconds=_optional_positive_float(
            request_timeout_seconds, 300.0, "request timeout"
        ),
        max_retries=_optional_nonnegative_int(max_retries, 2, "max retries"),
        backoff_base_seconds=_optional_positive_float(
            backoff_base_seconds, 1.0, "backoff base"
        ),
        max_retry_delay_seconds=_optional_positive_float(
            max_retry_delay_seconds, 30.0, "maximum retry delay"
        ),
        response_bytes_limit=byte_limit,
    )
