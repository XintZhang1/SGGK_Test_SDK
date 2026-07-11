"""OpenAI-compatible chat-completions transport with bounded retries."""

from __future__ import annotations

import hashlib
import json
import random
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from .config import GatewayConfig

RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
STRUCTURED_OUTPUT_REJECTION_CODES = {400, 404, 415, 422}
STRUCTURED_OUTPUT_ERROR_MARKERS = (
    "response_format",
    "response format",
    "json_schema",
    "json schema",
    "unsupported",
    "not supported",
    "unknown field",
    "unknown parameter",
    "invalid parameter",
)
SAFE_RESPONSE_HEADERS = {
    "content-type",
    "date",
    "retry-after",
    "request-id",
    "x-request-id",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}


class TransportError(RuntimeError):
    """A transport failed without receiving an HTTP response."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class MessageTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        ca_bundle: str,
        response_bytes_limit: int,
    ) -> HttpResponse: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class UrllibMessageTransport:
    """Small stdlib transport that refuses redirects and bounds response size."""

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        ca_bundle: str,
        response_bytes_limit: int,
    ) -> HttpResponse:
        context = ssl.create_default_context(cafile=ca_bundle or None)
        opener = urllib.request.build_opener(
            _NoRedirectHandler(),
            urllib.request.HTTPSHandler(context=context),
        )
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                response_body = response.read(response_bytes_limit + 1)
                if len(response_body) > response_bytes_limit:
                    raise TransportError(f"response exceeds {response_bytes_limit} bytes")
                return HttpResponse(
                    status=int(response.status),
                    headers={str(key).lower(): str(value) for key, value in response.headers.items()},
                    body=response_body,
                )
        except urllib.error.HTTPError as exc:
            response_body = exc.read(response_bytes_limit + 1)
            if len(response_body) > response_bytes_limit:
                raise TransportError(f"error response exceeds {response_bytes_limit} bytes") from exc
            return HttpResponse(
                status=int(exc.code),
                headers={str(key).lower(): str(value) for key, value in exc.headers.items()},
                body=response_body,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(str(exc)) from exc


@dataclass(frozen=True)
class CompletionOptions:
    response_mode: str = "auto"
    response_schema: dict[str, Any] | None = None
    schema_name: str = "sggk_authoring_candidate"
    temperature: float = 0.2
    max_tokens: int = 8192
    thinking_mode: str = "omit"
    seed: int | None = None

    def __post_init__(self) -> None:
        if self.response_mode not in {"auto", "json_schema", "json_object", "none"}:
            raise ValueError("response_mode must be auto, json_schema, json_object, or none")
        if self.thinking_mode not in {"omit", "enabled", "disabled"}:
            raise ValueError("thinking_mode must be omit, enabled, or disabled")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")


@dataclass
class CompletionResult:
    ok: bool
    candidate: dict[str, Any] | None = None
    candidate_source: str = ""
    content: str = ""
    reasoning_content_sha256: str = ""
    reasoning_content_chars: int = 0
    final_mode: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    response_records: list[dict[str, Any]] = field(default_factory=list)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() in SAFE_RESPONSE_HEADERS
    }


def _parse_body(body: bytes) -> tuple[Any, str]:
    try:
        text = body.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        message = (
            "provider response is not valid UTF-8: "
            f"invalid byte sequence at offset {exc.start}"
        )
        return {"_decode_error": message}, message
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        return {"_raw_text": text}, text


def _retry_after_seconds(value: str, now: Callable[[], datetime]) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - now()).total_seconds())


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def _without_reasoning_content(value: Any) -> Any:
    """Return a persistence-safe response tree without chain-of-thought text."""

    if isinstance(value, list):
        return [_without_reasoning_content(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"reasoning_content", "reasoning"}:
            text = _content_text(item)
            result[f"{key}_metadata"] = {
                "present": bool(text),
                "chars": len(text),
                "sha256": sha256_bytes(text.encode("utf-8")) if text else "",
            }
        else:
            result[key] = _without_reasoning_content(item)
    return result


def _strict_json_object(text: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant {value!r} is forbidden")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    loaded = json.loads(
        text,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(loaded, dict):
        raise ValueError("top-level JSON value must be an object")
    return loaded


def _candidate_from_response(response: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    metadata: dict[str, Any] = {
        "content": "",
        "reasoning_content_sha256": "",
        "reasoning_content_chars": 0,
        "candidate_source": "",
        "finish_reason": "",
        "usage": {},
        "error": "",
    }
    if not isinstance(response, dict):
        metadata["error"] = "provider response is not a JSON object"
        return None, metadata
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        metadata["error"] = "provider response has no choices[0] object"
        return None, metadata
    choice = choices[0]
    metadata["finish_reason"] = str(choice.get("finish_reason") or "")
    message = choice.get("message")
    if not isinstance(message, dict):
        metadata["error"] = "provider response choice has no message object"
        return None, metadata
    raw_content = message.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    reasoning = _content_text(message.get("reasoning_content") or message.get("reasoning"))
    metadata["content"] = content
    if reasoning:
        metadata["reasoning_content_sha256"] = sha256_bytes(reasoning.encode("utf-8"))
        metadata["reasoning_content_chars"] = len(reasoning)
    metadata["usage"] = response.get("usage") if isinstance(response.get("usage"), dict) else {}

    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        metadata["error"] = "assistant message contains a refusal"
        return None, metadata

    if metadata["finish_reason"] in {"length", "content_filter"}:
        metadata["error"] = f"completion ended with finish_reason={metadata['finish_reason']}"
        return None, metadata
    if not isinstance(raw_content, str):
        metadata["error"] = "assistant choices[0].message.content must be a string containing exact JSON"
        return None, metadata
    candidate_text = content.strip()
    metadata["candidate_source"] = "message.content" if candidate_text else ""
    if not candidate_text:
        metadata["error"] = "assistant choices[0].message.content is empty"
        return None, metadata
    try:
        candidate = _strict_json_object(candidate_text)
    except (json.JSONDecodeError, ValueError) as exc:
        metadata["error"] = f"assistant message.content is not exact JSON: {exc}"
        return None, metadata
    return candidate, metadata


class OpenAICompatibleMessageClient:
    """Provider-neutral chat-completions client.

    The client has no knowledge of the SDK runner, patch tools, formal artifact
    promotion, or repository state.  It only performs message API calls and
    returns structured in-memory results to the authoring gateway.
    """

    def __init__(
        self,
        config: GatewayConfig,
        *,
        transport: MessageTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibMessageTransport()
        self.sleeper = sleeper
        self.random_source = random_source
        self.now = now or (lambda: datetime.now(UTC))

    def _modes(self, options: CompletionOptions) -> list[str]:
        if options.response_mode == "auto":
            return ["json_schema", "json_object", "none"] if options.response_schema else ["json_object", "none"]
        if options.response_mode == "json_schema":
            return ["json_schema", "json_object", "none"]
        if options.response_mode == "json_object":
            return ["json_object", "none"]
        return ["none"]

    def _payload(
        self,
        system_prompt: str,
        user_prompt: str,
        options: CompletionOptions,
        mode: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": options.temperature,
            "max_tokens": options.max_tokens,
        }
        if options.seed is not None:
            payload["seed"] = options.seed
        if options.thinking_mode != "omit":
            payload["enable_thinking"] = options.thinking_mode == "enabled"
        if mode == "json_schema":
            if not isinstance(options.response_schema, dict):
                raise ValueError("json_schema response mode requires response_schema")
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": options.schema_name,
                    "strict": True,
                    "schema": options.response_schema,
                },
            }
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "sggk-authoring-gateway/1",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _retry_delay(self, response: HttpResponse | None, retry_index: int) -> float:
        if response is not None:
            retry_after = _retry_after_seconds(str(response.headers.get("retry-after", "")), self.now)
            if retry_after is not None:
                return min(self.config.max_retry_delay_seconds, retry_after)
        base = self.config.backoff_base_seconds * (2 ** max(0, retry_index - 1))
        jitter = 0.5 + (self.random_source() * 0.5)
        return min(self.config.max_retry_delay_seconds, base * jitter)

    @staticmethod
    def _structured_output_rejected(status: int, response_text: str) -> bool:
        if status not in STRUCTURED_OUTPUT_REJECTION_CODES:
            return False
        lowered = response_text.lower()
        return any(marker in lowered for marker in STRUCTURED_OUTPUT_ERROR_MARKERS)

    def create_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        options: CompletionOptions,
    ) -> CompletionResult:
        result = CompletionResult(ok=False)
        modes = self._modes(options)
        for mode_index, mode in enumerate(modes):
            payload = self._payload(system_prompt, user_prompt, options, mode)
            payload_bytes = canonical_json_bytes(payload)
            request_hash = sha256_bytes(payload_bytes)
            downgrade = False
            for transport_try in range(1, self.config.max_retries + 2):
                response: HttpResponse | None = None
                try:
                    response = self.transport.post(
                        url=self.config.endpoint_url,
                        headers=self._headers(),
                        body=payload_bytes,
                        timeout_seconds=self.config.request_timeout_seconds,
                        ca_bundle=self.config.ca_bundle,
                        response_bytes_limit=self.config.response_bytes_limit,
                    )
                except TransportError as exc:
                    retry = transport_try <= self.config.max_retries
                    delay = self._retry_delay(None, transport_try) if retry else 0.0
                    result.events.append(
                        {
                            "mode": mode,
                            "transport_try": transport_try,
                            "request_sha256": request_hash,
                            "status": None,
                            "transport_error": str(exc),
                            "retry": retry,
                            "retry_delay_seconds": delay,
                        }
                    )
                    if retry:
                        self.sleeper(delay)
                        continue
                    result.error = f"transport failed after {transport_try} try/tries: {exc}"
                    return result

                parsed_body, response_text = _parse_body(response.body)
                response_record = {
                    "mode": mode,
                    "transport_try": transport_try,
                    "status": response.status,
                    "headers": _safe_headers(response.headers),
                    "body": _without_reasoning_content(parsed_body),
                    "body_sha256": sha256_bytes(response.body),
                }
                result.response_records.append(response_record)
                retry = response.status in RETRYABLE_STATUS_CODES and transport_try <= self.config.max_retries
                delay = self._retry_delay(response, transport_try) if retry else 0.0
                result.events.append(
                    {
                        "mode": mode,
                        "transport_try": transport_try,
                        "request_sha256": request_hash,
                        "status": response.status,
                        "retry": retry,
                        "retry_delay_seconds": delay,
                    }
                )
                if retry:
                    self.sleeper(delay)
                    continue
                if 200 <= response.status < 300:
                    candidate, metadata = _candidate_from_response(parsed_body)
                    result.final_mode = mode
                    result.candidate = candidate
                    result.candidate_source = str(metadata["candidate_source"])
                    result.content = str(metadata["content"])
                    result.reasoning_content_sha256 = str(metadata["reasoning_content_sha256"])
                    result.reasoning_content_chars = int(metadata["reasoning_content_chars"])
                    result.finish_reason = str(metadata["finish_reason"])
                    result.usage = dict(metadata["usage"])
                    result.error = str(metadata["error"])
                    result.ok = candidate is not None and not result.error
                    return result
                if mode_index + 1 < len(modes) and self._structured_output_rejected(response.status, response_text):
                    result.events[-1]["downgrade_to"] = modes[mode_index + 1]
                    downgrade = True
                    break
                result.final_mode = mode
                result.error = f"HTTP {response.status}: {response_text[:500]}"
                return result
            if downgrade:
                continue
        result.error = result.error or "all response modes failed"
        return result
