"""OpenAI-compatible chat-completions transport with bounded retries."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import random
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

from .config import DEFAULT_STREAM_BYTES_LIMIT, GatewayConfig

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
MAX_SSE_EVENT_BYTES = 64 * 1024 * 1024


class TransportError(RuntimeError):
    """A transport failed without receiving an HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        timed_out: bool = False,
        stream_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.timed_out = timed_out
        self.stream_metadata = dict(stream_metadata or {})


class ClientError(RuntimeError):
    """A request could not be built within the fixed client budgets."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    stream_metadata: Mapping[str, Any] = field(default_factory=dict)


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
        stream_bytes_limit: int = DEFAULT_STREAM_BYTES_LIMIT,
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
        stream_bytes_limit: int = DEFAULT_STREAM_BYTES_LIMIT,
    ) -> HttpResponse:
        context = ssl.create_default_context(cafile=ca_bundle or None)
        opener = urllib.request.build_opener(
            _NoRedirectHandler(),
            urllib.request.HTTPSHandler(context=context),
        )
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                response_headers = {
                    str(key).lower(): str(value) for key, value in response.headers.items()
                }
                if "text/event-stream" in response_headers.get("content-type", "").lower():
                    response_body, stream_metadata = _aggregate_sse_chunks(
                        _response_chunks(response),
                        candidate_bytes_limit=response_bytes_limit,
                        wire_bytes_limit=stream_bytes_limit,
                    )
                else:
                    response_body = response.read(response_bytes_limit + 1)
                    stream_metadata = {}
                    if len(response_body) > response_bytes_limit:
                        raise TransportError(f"response exceeds {response_bytes_limit} bytes")
                return HttpResponse(
                    status=int(response.status),
                    headers=response_headers,
                    body=response_body,
                    stream_metadata=stream_metadata,
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
            reason = getattr(exc, "reason", None)
            error_text = str(exc)
            timed_out = (
                isinstance(exc, TimeoutError)
                or isinstance(reason, TimeoutError)
                or "timed out" in error_text.lower()
                or "timeout" in error_text.lower()
            )
            if timed_out:
                raise TransportError(
                    f"provider response timed out after {timeout_seconds:g} seconds",
                    timed_out=True,
                ) from exc
            raise TransportError(error_text) from exc


MAX_IMAGE_EDGE_PIXELS = 1600
IMAGE_SUFFIX_MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@dataclass(frozen=True)
class PreparedImage:
    """A host-supplied geometry render, budget-enforced and ready to send.

    ``sha256`` binds the original on-disk artifact (the provenance identity);
    ``data`` is the re-encoded PNG payload actually transmitted. Pixel data is
    never persisted by the client or the gateway — only hashes and sizes.
    """

    path: str
    sha256: str
    mime: str
    data: bytes = field(repr=False)
    source_bytes: int = 0
    prepared_bytes: int = 0


def prepare_images(
    paths: Iterable[str | Path],
    *,
    max_images: int = 4,
    max_image_bytes: int = 2 * 1024 * 1024,
    max_total_bytes: int = 12 * 1024 * 1024,
) -> list[PreparedImage]:
    """Re-encode host-supplied geometry renders into bounded PNG payloads.

    Paths must be absolute and already containment-checked by host code; they
    are never model-derived. Every image is downscaled to at most
    ``MAX_IMAGE_EDGE_PIXELS`` on its long edge and re-encoded as an optimized
    PNG (which also strips metadata). Any budget overflow fails closed.
    """

    from PIL import Image  # lazy: text-only flows never require Pillow

    items = list(paths)
    if not items:
        return []
    if max_images <= 0 or max_image_bytes <= 0 or max_total_bytes <= 0:
        raise ClientError("image budgets must be positive")
    if len(items) > max_images:
        raise ClientError(f"too many images: {len(items)} > {max_images}")
    prepared: list[PreparedImage] = []
    total_bytes = 0
    for raw in items:
        path = Path(raw)
        if not path.is_absolute():
            raise ClientError(f"image path must be absolute and host-checked: {raw}")
        if path.suffix.lower() not in IMAGE_SUFFIX_MIMES:
            raise ClientError(f"only .png/.jpg geometry renders are accepted: {path.name}")
        try:
            source = path.read_bytes()
        except OSError as exc:
            raise ClientError(f"image cannot be read: {path}: {exc}") from exc
        sha256 = sha256_bytes(source)
        try:
            with Image.open(io.BytesIO(source)) as decoded:
                frame = decoded.convert("RGB")
            if max(frame.size) > MAX_IMAGE_EDGE_PIXELS:
                frame.thumbnail(
                    (MAX_IMAGE_EDGE_PIXELS, MAX_IMAGE_EDGE_PIXELS),
                    Image.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG", optimize=True)
        except Exception as exc:  # noqa: BLE001 - Pillow raises many types; fail closed
            raise ClientError(f"image cannot be decoded as PNG/JPEG: {path.name}: {exc}") from exc
        data = buffer.getvalue()
        if len(data) > max_image_bytes:
            raise ClientError(
                f"prepared image exceeds {max_image_bytes} bytes after downscale: {path.name}"
            )
        total_bytes += len(data)
        if total_bytes > max_total_bytes:
            raise ClientError(f"prepared images exceed the total budget of {max_total_bytes} bytes")
        prepared.append(
            PreparedImage(
                path=str(path),
                sha256=sha256,
                # The payload is always the re-encoded PNG, regardless of the
                # source container format.
                mime="image/png",
                data=data,
                source_bytes=len(source),
                prepared_bytes=len(data),
            )
        )
    return prepared


@dataclass(frozen=True)
class CompletionOptions:
    response_mode: str = "auto"
    response_schema: dict[str, Any] | None = None
    schema_name: str = "sggk_authoring_candidate"
    temperature: float = 0.2
    max_tokens: int = 8192
    thinking_mode: str = "omit"
    stream: bool = False
    seed: int | None = None
    request_timeout_seconds: float | None = None
    # Host-supplied, budget-enforced geometry renders (see prepare_images).
    # Empty for the default text-only path; the gateway only ever fills this
    # from containment-checked TaskSpec.image_paths.
    images: tuple[PreparedImage, ...] = ()

    def __post_init__(self) -> None:
        if self.response_mode not in {"auto", "json_schema", "json_object", "none"}:
            raise ValueError("response_mode must be auto, json_schema, json_object, or none")
        if self.thinking_mode not in {"omit", "enabled", "disabled"}:
            raise ValueError("thinking_mode must be omit, enabled, or disabled")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if not isinstance(self.stream, bool):
            raise ValueError("stream must be a boolean")
        if self.request_timeout_seconds is not None and self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive when set")


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
    error_kind: str = ""
    image_count: int = 0
    image_sha256: list[str] = field(default_factory=list)
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


class _IncrementalSseAggregator:
    """Consume SSE without retaining reasoning or the raw response envelope."""

    def __init__(self, *, candidate_bytes_limit: int, wire_bytes_limit: int) -> None:
        self.candidate_bytes_limit = candidate_bytes_limit
        self.wire_bytes_limit = wire_bytes_limit
        self._raw_hasher = hashlib.sha256()
        self._reasoning_hasher = hashlib.sha256()
        self._raw_bytes = 0
        self._reasoning_bytes = 0
        self._reasoning_chars = 0
        self._candidate_bytes = 0
        self._refusal_bytes = 0
        self._event_count = 0
        self._line_buffer = bytearray()
        self._data_lines: list[bytes] = []
        self._event_bytes = 0
        self._content_parts: list[str] = []
        self._refusal_parts: list[str] = []
        self._usage: dict[str, Any] = {}
        self._response_id = ""
        self._response_model = ""
        self._finish_reason = ""
        self._saw_choice = False
        self._saw_done = False
        self._error = ""
        self._error_kind = ""
        self._discard_parser_input = False
        self._wire_limit_exceeded = False
        self._raw_stream_complete = False

    @property
    def wire_limit_exceeded(self) -> bool:
        return self._wire_limit_exceeded

    def mark_raw_stream_complete(self) -> None:
        self._raw_stream_complete = True

    def _fail(self, message: str, kind: str) -> None:
        if not self._error:
            self._error = message
            self._error_kind = kind
        self._content_parts.clear()
        self._refusal_parts.clear()
        self._line_buffer.clear()
        self._data_lines.clear()
        self._event_bytes = 0
        self._discard_parser_input = True

    def feed(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        self._raw_hasher.update(chunk)
        self._raw_bytes += len(chunk)
        if self._raw_bytes > self.wire_bytes_limit:
            self._wire_limit_exceeded = True
            self._fail(
                f"provider SSE wire stream exceeds {self.wire_bytes_limit} bytes",
                "stream_wire_too_large",
            )
            return
        if self._discard_parser_input:
            return
        self._line_buffer.extend(chunk)
        while True:
            newline = self._line_buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._line_buffer[:newline])
            del self._line_buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            self._process_line(line)
            if self._discard_parser_input:
                return
        if len(self._line_buffer) + self._event_bytes > MAX_SSE_EVENT_BYTES:
            self._fail(
                f"provider SSE event exceeds {MAX_SSE_EVENT_BYTES} bytes",
                "stream_event_too_large",
            )

    def _process_line(self, line: bytes) -> None:
        if not line:
            self._flush_event()
            return
        if line.startswith(b":"):
            return
        field, separator, value = line.partition(b":")
        if field != b"data":
            return
        if separator and value.startswith(b" "):
            value = value[1:]
        self._data_lines.append(value)
        self._event_bytes += len(value)
        if self._event_bytes > MAX_SSE_EVENT_BYTES:
            self._fail(
                f"provider SSE event exceeds {MAX_SSE_EVENT_BYTES} bytes",
                "stream_event_too_large",
            )

    def _flush_event(self) -> None:
        if not self._data_lines or self._discard_parser_input:
            return
        payload = b"\n".join(self._data_lines)
        self._data_lines.clear()
        self._event_bytes = 0
        self._event_count += 1
        if self._saw_done:
            self._fail("provider SSE stream contains data after [DONE]", "stream_invalid_event")
            return
        if payload.strip() == b"[DONE]":
            self._saw_done = True
            return
        try:
            event_text = payload.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            self._fail(
                "provider SSE event is not valid UTF-8: "
                f"invalid byte sequence at offset {exc.start}",
                "stream_invalid_utf8",
            )
            return
        try:
            event = json.loads(event_text)
        except json.JSONDecodeError:
            self._fail(
                f"provider SSE event {self._event_count} is not valid JSON",
                "stream_invalid_event",
            )
            return
        if not isinstance(event, dict):
            self._fail(
                f"provider SSE event {self._event_count} is not a JSON object",
                "stream_invalid_event",
            )
            return
        if "error" in event:
            provider_error = event.get("error")
            message = ""
            code = ""
            if isinstance(provider_error, dict):
                message = str(provider_error.get("message") or "").strip()
                code = str(provider_error.get("code") or "").strip()
            elif isinstance(provider_error, str):
                message = provider_error.strip()
            detail = ": ".join(item for item in (code, message[:500]) if item)
            self._fail(
                "provider SSE error" + (f": {detail}" if detail else ""),
                "provider_error",
            )
            return
        event_id = event.get("id")
        if isinstance(event_id, str):
            bounded_id = event_id[:4096]
            if self._response_id and bounded_id != self._response_id:
                self._fail("provider SSE response id changed mid-stream", "stream_invalid_event")
                return
            self._response_id = bounded_id
        event_model = event.get("model")
        if isinstance(event_model, str):
            bounded_model = event_model[:4096]
            if self._response_model and bounded_model != self._response_model:
                self._fail("provider SSE model changed mid-stream", "stream_invalid_event")
                return
            self._response_model = bounded_model
        if isinstance(event.get("usage"), dict):
            self._usage = dict(event["usage"])
        choices = event.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            choice_index = choice.get("index", 0)
            if choice_index is not None and choice_index != 0:
                continue
            self._saw_choice = True
            had_terminal_finish = bool(self._finish_reason)
            if choice.get("finish_reason") is not None:
                new_finish_reason = str(choice["finish_reason"])[:128]
                if (
                    self._finish_reason
                    and new_finish_reason
                    and new_finish_reason != self._finish_reason
                ):
                    self._fail(
                        "provider SSE finish_reason changed mid-stream",
                        "stream_invalid_event",
                    )
                    return
                if new_finish_reason:
                    self._finish_reason = new_finish_reason
            fragment = choice.get("delta")
            if not isinstance(fragment, dict):
                fragment = choice.get("message")
            if not isinstance(fragment, dict):
                continue
            content = _content_text(fragment.get("content"))
            reasoning = _content_text(
                fragment.get("reasoning_content") or fragment.get("reasoning")
            )
            refusal = _content_text(fragment.get("refusal"))
            if had_terminal_finish and (content or reasoning or refusal):
                self._fail(
                    "provider SSE contains assistant delta after finish_reason",
                    "stream_invalid_event",
                )
                return
            if reasoning:
                reasoning_bytes = reasoning.encode("utf-8")
                self._reasoning_hasher.update(reasoning_bytes)
                self._reasoning_bytes += len(reasoning_bytes)
                self._reasoning_chars += len(reasoning)
            if content:
                content_bytes = len(content.encode("utf-8"))
                attempted_bytes = self._candidate_bytes + content_bytes
                self._candidate_bytes = attempted_bytes
                if attempted_bytes > self.candidate_bytes_limit:
                    self._fail(
                        "assistant streamed candidate content exceeds "
                        f"{self.candidate_bytes_limit} bytes",
                        "stream_candidate_too_large",
                    )
                    return
                self._content_parts.append(content)
            if refusal:
                refusal_bytes = len(refusal.encode("utf-8"))
                attempted_bytes = self._refusal_bytes + refusal_bytes
                self._refusal_bytes = attempted_bytes
                if attempted_bytes > self.candidate_bytes_limit:
                    self._fail(
                        f"assistant streamed refusal exceeds {self.candidate_bytes_limit} bytes",
                        "stream_refusal_too_large",
                    )
                    return
                self._refusal_parts.append(refusal)

    def finish(self) -> tuple[bytes, dict[str, Any]]:
        if not self._discard_parser_input:
            if self._line_buffer:
                line = bytes(self._line_buffer)
                self._line_buffer.clear()
                if line.endswith(b"\r"):
                    line = line[:-1]
                self._process_line(line)
            self._flush_event()
        if not self._error and not self._saw_choice:
            self._fail("provider SSE response has no choices[0] deltas", "stream_invalid_event")
        if not self._error and (not self._saw_done or not self._finish_reason):
            missing: list[str] = []
            if not self._saw_done:
                missing.append("[DONE]")
            if not self._finish_reason:
                missing.append("finish_reason")
            self._fail(
                "provider SSE stream ended without " + " and ".join(missing),
                "stream_incomplete",
            )

        reasoning_sha256 = (
            self._reasoning_hasher.hexdigest() if self._reasoning_chars else ""
        )
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "" if self._error else "".join(self._content_parts),
        }
        if self._reasoning_chars:
            message["reasoning_content_metadata"] = {
                "present": True,
                "chars": self._reasoning_chars,
                "bytes": self._reasoning_bytes,
                "sha256": reasoning_sha256,
            }
        if self._refusal_parts and not self._error:
            message["refusal"] = "".join(self._refusal_parts)
        response: dict[str, Any] = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": self._finish_reason or "length",
                    "message": message,
                }
            ],
            "usage": self._usage,
        }
        if self._response_id:
            response["id"] = self._response_id
        if self._response_model:
            response["model"] = self._response_model
        if self._error:
            response["_stream_error"] = self._error
            response["_stream_error_kind"] = self._error_kind
            if self._error_kind == "provider_error":
                response["error"] = {"message": self._error}
        metadata = {
            "raw_stream_sha256": self._raw_hasher.hexdigest(),
            "raw_stream_bytes": self._raw_bytes,
            "raw_stream_complete": self._raw_stream_complete,
            "event_count": self._event_count,
            "done": self._saw_done,
            "finish_reason": self._finish_reason,
            "candidate_content_bytes": self._candidate_bytes,
            "refusal_bytes": self._refusal_bytes,
            "reasoning_content_sha256": reasoning_sha256,
            "reasoning_content_chars": self._reasoning_chars,
            "reasoning_content_bytes": self._reasoning_bytes,
            "error": self._error,
            "error_kind": self._error_kind,
        }
        return canonical_json_bytes(response), metadata


def _aggregate_sse_chunks(
    chunks: Any,
    *,
    candidate_bytes_limit: int,
    wire_bytes_limit: int,
) -> tuple[bytes, dict[str, Any]]:
    aggregator = _IncrementalSseAggregator(
        candidate_bytes_limit=candidate_bytes_limit,
        wire_bytes_limit=wire_bytes_limit,
    )
    for chunk in chunks:
        aggregator.feed(chunk)
        if aggregator.wire_limit_exceeded:
            break
    if not aggregator.wire_limit_exceeded:
        aggregator.mark_raw_stream_complete()
    return aggregator.finish()


def _response_chunks(response: Any, chunk_size: int = 64 * 1024) -> Any:
    """Prefer bounded reads so one malformed no-newline event cannot allocate a whole line."""

    read1 = getattr(response, "read1", None)
    if callable(read1):
        while True:
            chunk = read1(chunk_size)
            if not chunk:
                return
            yield chunk
    else:
        yield from response


def _parse_response(
    response: HttpResponse,
    *,
    candidate_bytes_limit: int,
    wire_bytes_limit: int,
) -> tuple[Any, str, dict[str, Any]]:
    content_type = str(response.headers.get("content-type", "")).lower()
    stream_metadata = dict(response.stream_metadata)
    if stream_metadata:
        parsed, _response_text = _parse_body(response.body)
        return parsed, "SSE stream", stream_metadata
    if "text/event-stream" in content_type or response.body.lstrip().startswith(b"data:"):
        synthetic_body, stream_metadata = _aggregate_sse_chunks(
            (response.body,),
            candidate_bytes_limit=candidate_bytes_limit,
            wire_bytes_limit=wire_bytes_limit,
        )
        parsed, _response_text = _parse_body(synthetic_body)
        return parsed, "SSE stream", stream_metadata
    parsed, response_text = _parse_body(response.body)
    return parsed, response_text, {}


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


def _candidate_from_response(
    response: Any,
    *,
    stream_metadata: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    metadata: dict[str, Any] = {
        "content": "",
        "reasoning_content_sha256": "",
        "reasoning_content_chars": 0,
        "candidate_source": "",
        "finish_reason": "",
        "usage": {},
        "error": "",
        "error_kind": "",
    }
    if not isinstance(response, dict):
        metadata["error"] = "provider response is not a JSON object"
        return None, metadata
    metadata["usage"] = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    if stream_metadata:
        metadata["reasoning_content_sha256"] = str(
            stream_metadata.get("reasoning_content_sha256") or ""
        )
        metadata["reasoning_content_chars"] = int(
            stream_metadata.get("reasoning_content_chars") or 0
        )
    stream_error = response.get("_stream_error")
    if isinstance(stream_error, str) and stream_error:
        metadata["error"] = stream_error
        metadata["error_kind"] = str(
            response.get("_stream_error_kind") or "stream_invalid_event"
        )
        metadata["finish_reason"] = str(
            (stream_metadata or {}).get("finish_reason") or ""
        )
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
    metadata["content"] = content
    if not stream_metadata:
        reasoning = _content_text(
            message.get("reasoning_content") or message.get("reasoning")
        )
        if reasoning:
            metadata["reasoning_content_sha256"] = sha256_bytes(reasoning.encode("utf-8"))
            metadata["reasoning_content_chars"] = len(reasoning)

    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        metadata["error"] = "assistant message contains a refusal"
        metadata["error_kind"] = "refusal"
        return None, metadata

    if metadata["finish_reason"] != "stop":
        metadata["error"] = (
            "completion did not end with finish_reason=stop: "
            f"finish_reason={metadata['finish_reason'] or '<missing>'}"
        )
        metadata["error_kind"] = "output_invalid"
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
        user_content: Any = user_prompt
        if options.images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
            for image in options.images:
                encoded = base64.b64encode(image.data).decode("ascii")
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image.mime};base64,{encoded}"},
                    }
                )
            user_content = parts
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": options.temperature,
            "max_tokens": options.max_tokens,
        }
        if options.seed is not None:
            payload["seed"] = options.seed
        if options.thinking_mode != "omit":
            payload["enable_thinking"] = options.thinking_mode == "enabled"
        if options.stream:
            payload["stream"] = True
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

    def _headers(self, *, stream: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream" if stream else "application/json",
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
        result.image_count = len(options.images)
        result.image_sha256 = [image.sha256 for image in options.images]
        modes = self._modes(options)
        timeout_seconds = (
            options.request_timeout_seconds
            if options.request_timeout_seconds is not None
            else self.config.request_timeout_seconds
        )
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
                        headers=self._headers(stream=options.stream),
                        body=payload_bytes,
                        timeout_seconds=timeout_seconds,
                        ca_bundle=self.config.ca_bundle,
                        response_bytes_limit=self.config.response_bytes_limit,
                        stream_bytes_limit=self.config.stream_bytes_limit,
                    )
                except TransportError as exc:
                    # A full read timeout is materially different from a quick
                    # connection failure: blindly repeating a long generation
                    # can turn one bounded wait into ten or fifteen minutes.
                    retry = not exc.timed_out and transport_try <= self.config.max_retries
                    delay = self._retry_delay(None, transport_try) if retry else 0.0
                    result.events.append(
                        {
                            "mode": mode,
                            "transport_try": transport_try,
                            "request_sha256": request_hash,
                            "status": None,
                            "transport_error": str(exc),
                            "transport_error_kind": "timeout" if exc.timed_out else "network",
                            "retry": retry,
                            "retry_delay_seconds": delay,
                        }
                    )
                    if retry:
                        self.sleeper(delay)
                        continue
                    if exc.timed_out:
                        result.error_kind = "transport_timeout"
                        result.error = (
                            f"Message API timed out after {timeout_seconds:g} seconds "
                            f"while waiting for {self.config.model} "
                            f"(thinking={options.thinking_mode}, max_tokens={options.max_tokens}); "
                            "the timeout was not retried to avoid another long wait"
                        )
                    else:
                        result.error_kind = "transport_error"
                        result.error = f"transport failed after {transport_try} try/tries: {exc}"
                    result.final_mode = mode
                    return result

                parsed_body, response_text, stream_metadata = _parse_response(
                    response,
                    candidate_bytes_limit=self.config.response_bytes_limit,
                    wire_bytes_limit=self.config.stream_bytes_limit,
                )
                raw_body_sha256 = str(
                    stream_metadata.get("raw_stream_sha256")
                    or sha256_bytes(response.body)
                )
                response_record = {
                    "mode": mode,
                    "stream": options.stream,
                    "transport_try": transport_try,
                    "status": response.status,
                    "headers": _safe_headers(response.headers),
                    "body": _without_reasoning_content(parsed_body),
                    "body_sha256": raw_body_sha256,
                }
                if stream_metadata:
                    response_record["body_bytes"] = int(
                        stream_metadata.get("raw_stream_bytes") or 0
                    )
                    response_record["synthetic_body_sha256"] = sha256_bytes(
                        response.body
                    )
                    response_record["stream_metadata"] = dict(stream_metadata)
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
                    candidate, metadata = _candidate_from_response(
                        parsed_body,
                        stream_metadata=stream_metadata,
                    )
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
                    result.error_kind = (
                        ""
                        if result.ok
                        else str(metadata.get("error_kind") or "output_invalid")
                    )
                    return result
                if mode_index + 1 < len(modes) and self._structured_output_rejected(response.status, response_text):
                    result.events[-1]["downgrade_to"] = modes[mode_index + 1]
                    downgrade = True
                    break
                result.final_mode = mode
                result.error_kind = "http_error"
                result.error = f"HTTP {response.status}: {response_text[:500]}"
                return result
            if downgrade:
                continue
        result.error = result.error or "all response modes failed"
        return result
