"""Provider-neutral authoring gateway for OpenAI-compatible message APIs.

This package is deliberately outside the fixed SDK execution tools. It calls a
configured model endpoint and stages contract-valid JSON candidates, but it
never accepts them as harness inputs or invokes SDK/patch helpers. Acceptance
belongs to ``run_message_harness_pipeline.py`` after deterministic fixed gates.
"""

from .config import ConfigError, GatewayConfig, ProfileSpec, load_gateway_config
from .gateway import AuthoringGateway, GatewayBatchResult, GatewayError, GatewayRunResult, TaskSpec

__all__ = [
    "AuthoringGateway",
    "ConfigError",
    "GatewayConfig",
    "GatewayBatchResult",
    "GatewayError",
    "GatewayRunResult",
    "ProfileSpec",
    "TaskSpec",
    "load_gateway_config",
]
