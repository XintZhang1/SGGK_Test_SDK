#!/usr/bin/env python3
"""Deterministic public-header signature to fixed adapter archetype mapping.

The mapper is conservative by design: it parses ONE C++ declaration string and
only recognizes signatures that a fixed host adapter template can drive without
per-API C++ authoring.  Anything else (overloads the caller cannot
disambiguate, topology/geometry parameters, non-body returns, unfamiliar
types) maps to ``None`` so the caller falls back to the interface-design
backlog path.  Raw header text never leaves the host; only the normalized
public-interface signature string is returned.
"""

from __future__ import annotations

import re
from typing import Any

from plugin_catalog import ALLOWED_SDK_MODULES, HEADER_RE

ARCHETYPE_BODY_LIST_TO_BODY = "body_list_to_body"
ARCHETYPE_UNARY_BODY_TO_BODIES = "unary_body_to_bodies"
SUPPORTED_ARCHETYPES = (ARCHETYPE_BODY_LIST_TO_BODY, ARCHETYPE_UNARY_BODY_TO_BODIES)

# Scalar/bool C++ parameter types a fixed template can bind to recipe fields.
_BODY_LIST_SCALAR_TYPES = {"bool", "double", "float", "int", "Integer"}
_UNARY_SCALAR_TYPES = {"bool", "double", "float", "int", "Integer", "std::string"}
# Canonical cpp_type used by the materializer for each parsed scalar type.
_CPP_TYPE_CANONICAL = {
    "bool": "bool",
    "double": "double",
    "float": "float",
    "int": "int",
    "Integer": "int",
    "std::string": "std::string",
}

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DECLSPEC_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_RET_PTR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*RetPtr$")

_ARCHETYPE_INTAKE: dict[str, dict[str, Any]] = {
    ARCHETYPE_BODY_LIST_TO_BODY: {
        "behavior": (
            "Pass the constructed target and tool bodies as one BodyList to the API "
            "and validate the single returned BodyPtr."
        ),
        "input_roles": ["target", "tool"],
        "result_roles": ["result"],
        "required_oracles": ["result_bodies", "properties", "topocheck"],
        "smoke_guidance": "Use two separated solid spheres and require one valid result body.",
        "topotrack": {
            "mode": "unavailable",
            "reason": "The API returns BodyPtr and exposes no ModelingRet TopoTrack channel.",
        },
    },
    ARCHETYPE_UNARY_BODY_TO_BODIES: {
        "behavior": (
            "Apply the API to one constructed target body and validate every result body "
            "returned through the ModelingRet channel."
        ),
        "input_roles": ["target"],
        "result_roles": ["result"],
        "required_oracles": ["result_bodies", "properties", "topocheck"],
        "smoke_guidance": (
            "Use one solid sphere target and require at least one valid result body with "
            "finite properties."
        ),
        "topotrack": {
            "mode": "status_only",
            "reason": (
                "The fixed unary_body_to_bodies adapter records ModelingRet status; topology "
                "tracking is captured as status and topocheck artifacts only."
            ),
        },
    },
}


def _split_top_level(text: str, delimiter: str) -> list[str]:
    """Split on one delimiter character at parenthesis depth zero."""

    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char in "(<[":
            depth += 1
        elif char in ")>]":
            depth = max(0, depth - 1)
        elif char == delimiter and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _parse_param(raw: str) -> dict[str, Any] | None:
    """Parse one parameter into name/type/default parts, or reject it."""

    text = raw.strip()
    if not text or text == "...":
        return None
    halves = _split_top_level(text, "=")
    if len(halves) > 2:
        return None
    declaration = halves[0].strip()
    default = halves[1].strip() if len(halves) == 2 else ""
    match = re.fullmatch(r"(?P<type>.*?)(?P<name>[A-Za-z_][A-Za-z0-9_]*)", declaration)
    if match is None:
        return None
    raw_type = match.group("type").strip()
    name = match.group("name")
    if not raw_type or not name:
        return None
    compact = re.sub(r"\s+", "", raw_type)
    base = compact.removeprefix("const").rstrip("&*")
    if not base or not _IDENTIFIER_RE.fullmatch(base.replace("::", "_")):
        return None
    return {
        "name": name,
        "raw_type": raw_type,
        "compact_type": compact,
        "base_type": base,
        "has_default": bool(default),
        "default": default,
    }


def _matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _map_params_body_list(params: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Require ``const BodyList&`` plus only defaulted scalar/bool parameters."""

    if not params:
        return None
    first = params[0]
    if first["compact_type"] != "constBodyList&" or first["base_type"] != "BodyList":
        return None
    scalars: list[dict[str, Any]] = []
    for param in params[1:]:
        if not param["has_default"] or param["base_type"] not in _BODY_LIST_SCALAR_TYPES:
            return None
        scalars.append(
            {
                "name": param["name"],
                "cpp_type": _CPP_TYPE_CANONICAL[param["base_type"]],
                "has_default": True,
            }
        )
    return scalars


def _map_params_unary(
    params: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None] | None:
    """Require ``const BodyPtr&``/``BodyPtr`` plus scalars and one defaulted Opts."""

    if not params:
        return None
    first = params[0]
    if first["base_type"] != "BodyPtr" or first["compact_type"] not in {"constBodyPtr&", "BodyPtr"}:
        return None
    scalars: list[dict[str, Any]] = []
    opts_param: dict[str, Any] | None = None
    for param in params[1:]:
        if param["base_type"] in _UNARY_SCALAR_TYPES:
            scalars.append(
                {
                    "name": param["name"],
                    "cpp_type": _CPP_TYPE_CANONICAL[param["base_type"]],
                    "has_default": bool(param["has_default"]),
                }
            )
            continue
        is_defaulted_opts = (
            param["base_type"].endswith("Opts")
            and param["compact_type"].startswith("const")
            and param["compact_type"].endswith("&")
            and param["has_default"]
        )
        if is_defaulted_opts and opts_param is None:
            opts_param = {"name": param["name"], "type": param["base_type"]}
            continue
        return None
    return scalars, opts_param


def map_signature(function_name: str, declaration: str) -> dict[str, Any] | None:
    """Map one C++ declaration to a fixed adapter archetype, or return ``None``.

    The parser accepts an optional leading all-caps export macro (for example
    ``OFFSET_DECLSPEC``) and an optional trailing semicolon; anything with
    trailing qualifiers, templates, or unrecognized parameter shapes is
    rejected conservatively.
    """

    if not _IDENTIFIER_RE.fullmatch(str(function_name)):
        return None
    text = " ".join(str(declaration).split()).strip()
    if text.endswith(";"):
        text = text[:-1].strip()
    marker = re.search(rf"(?<![A-Za-z0-9_:]){re.escape(function_name)}\s*\(", text)
    if marker is None:
        return None
    close = _matching_paren(text, marker.end() - 1)
    if close is None:
        return None
    if text[close + 1 :].strip():
        return None
    head = text[: marker.start()].strip()
    tokens = head.split()
    while tokens and _DECLSPEC_TOKEN_RE.fullmatch(tokens[0]):
        tokens.pop(0)
    if len(tokens) != 1:
        return None
    return_type = tokens[0].removeprefix("sggk::")
    params_text = text[marker.end() : close].strip()
    if params_text in {"", "void"}:
        params: list[dict[str, Any]] = []
    else:
        params = []
        for raw in _split_top_level(params_text, ","):
            param = _parse_param(raw)
            if param is None:
                return None
            params.append(param)
    normalized_signature = f"{return_type} {function_name}({params_text})"
    if return_type == "BodyPtr":
        scalars = _map_params_body_list(params)
        if scalars is None:
            return None
        return {
            "adapter_archetype": ARCHETYPE_BODY_LIST_TO_BODY,
            "function_name": function_name,
            "function_signature": normalized_signature,
            "return_type": return_type,
            "scalar_params": scalars,
            "opts_param": None,
        }
    if _RET_PTR_RE.fullmatch(return_type):
        mapped = _map_params_unary(params)
        if mapped is None:
            return None
        scalars, opts_param = mapped
        return {
            "adapter_archetype": ARCHETYPE_UNARY_BODY_TO_BODIES,
            "function_name": function_name,
            "function_signature": normalized_signature,
            "return_type": return_type,
            "scalar_params": scalars,
            "opts_param": opts_param,
        }
    return None


def build_intake(
    function_name: str,
    declaration: str,
    sdk_header: str,
    request_id: str,
) -> dict[str, Any] | None:
    """Build the ``build_adaptation_contract`` intake for one mappable declaration."""

    mapped = map_signature(function_name, declaration)
    if mapped is None:
        return None
    header = str(sdk_header).strip()
    if not HEADER_RE.fullmatch(header):
        return None
    module = header.split("/", 1)[0]
    if module not in ALLOWED_SDK_MODULES:
        return None
    archetype = str(mapped["adapter_archetype"])
    fixed = _ARCHETYPE_INTAKE[archetype]
    return {
        "schema_version": 1,
        "request_id": str(request_id),
        "api": str(function_name),
        "sdk_header": header,
        "sdk_modules": sorted({module, "Topology"}),
        "function_signature": str(mapped["function_signature"]),
        "adapter_archetype": archetype,
        "behavior": fixed["behavior"],
        "input_roles": list(fixed["input_roles"]),
        "result_roles": list(fixed["result_roles"]),
        "required_oracles": list(fixed["required_oracles"]),
        "smoke_guidance": fixed["smoke_guidance"],
        "topotrack": dict(fixed["topotrack"]),
    }
