"""Bounded, definition-aware discovery for intranet C/C++ source evidence."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from typing import Any


HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx"})
IMPLEMENTATION_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx"})
SOURCE_SUFFIXES = IMPLEMENTATION_SUFFIXES | HEADER_SUFFIXES
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vs",
        ".vscode",
        "__pycache__",
        "artifacts",
        "bazel-bin",
        "bazel-out",
        "build",
        "cmake-build-debug",
        "cmake-build-release",
        "dist",
        "external",
        "node_modules",
        "out",
        "third_party",
        "vendor",
    }
)
CONTROL_PREFIX_RE = re.compile(
    r"(?:\b(?:if|for|while|switch|catch|sizeof|decltype|static_assert)\s*\([^;{}]*"
    r"|\b(?:return|co_return)\s*)$"
)
NAMESPACE_OPEN_RE = re.compile(
    r"\bnamespace\s+([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)\s*\{"
)


class SourceDiscoveryError(ValueError):
    """Source discovery cannot make a complete bounded claim."""


def path_identity(path: Path | None) -> str:
    """Return a stable identity for a configured local root without exposing it."""

    if path is None:
        return ""
    resolved = str(path.resolve(strict=True))
    if os.name == "nt":
        resolved = resolved.casefold()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def _mask_cpp_source(text: str) -> str:
    """Blank comments and literals while preserving offsets and newlines."""

    chars = list(text)
    index = 0
    state = "code"
    quote = ""
    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if current == "/" and following == "/":
                chars[index] = chars[index + 1] = " "
                state = "line_comment"
                index += 2
                continue
            if current == "/" and following == "*":
                chars[index] = chars[index + 1] = " "
                state = "block_comment"
                index += 2
                continue
            if current in {'"', "'"}:
                quote = current
                chars[index] = " "
                state = "literal"
                index += 1
                continue
            index += 1
            continue
        if state == "line_comment":
            if current == "\n":
                state = "code"
            else:
                chars[index] = " "
            index += 1
            continue
        if state == "block_comment":
            if current == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                state = "code"
                index += 2
                continue
            if current != "\n":
                chars[index] = " "
            index += 1
            continue
        if state == "literal":
            if current == "\\":
                chars[index] = " "
                if index + 1 < len(chars):
                    if chars[index + 1] != "\n":
                        chars[index + 1] = " "
                    index += 2
                    continue
            if current == quote:
                chars[index] = " "
                state = "code"
            elif current != "\n":
                chars[index] = " "
            index += 1
    return "".join(chars)


def _matching_delimiter(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _next_body_open(text: str, start: int, *, max_lookahead: int = 4096) -> int | None:
    """Find a definition body, rejecting declarations and call/control tails."""

    parenthesis_depth = 0
    bracket_depth = 0
    limit = min(len(text), start + max_lookahead)
    for index in range(start, limit):
        char = text[index]
        if char == "(":
            parenthesis_depth += 1
        elif char == ")":
            if parenthesis_depth == 0:
                return None
            parenthesis_depth -= 1
        elif char == "[":
            bracket_depth += 1
        elif char == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif char == ";" and parenthesis_depth == 0 and bracket_depth == 0:
            return None
        elif char == "{" and parenthesis_depth == 0 and bracket_depth == 0:
            return index
    return None


def _signature_start(masked: str, token_start: int) -> int:
    boundary = max(
        masked.rfind(";", 0, token_start),
        masked.rfind("{", 0, token_start),
        masked.rfind("}", 0, token_start),
    )
    start = boundary + 1
    lines = masked[start:token_start].splitlines(keepends=True)
    if len(lines) > 6:
        start += sum(len(line) for line in lines[:-6])
    while start < token_start and masked[start].isspace():
        start += 1
    return start


def _enclosing_namespace(masked: str, offset: int) -> str:
    scopes: list[tuple[int, str]] = []
    for match in NAMESPACE_OPEN_RE.finditer(masked, 0, offset):
        body_open = masked.find("{", match.start(), match.end())
        body_close = _matching_delimiter(masked, body_open, "{", "}")
        if body_close is not None and body_open < offset < body_close:
            scopes.append((body_open, match.group(1)))
    return "::".join(name for _start, name in sorted(scopes))


def _qualified_definition_name(signature: str, namespace: str, leaf: str) -> str:
    head = signature.split("(", 1)[0].strip()
    match = re.search(
        rf"((?:~?[A-Za-z_][A-Za-z0-9_]*\s*::\s*)*{re.escape(leaf)})\s*$",
        head,
    )
    local = re.sub(r"\s+", "", match.group(1)) if match else leaf
    if not namespace or local == namespace or local.startswith(namespace + "::"):
        return local
    return f"{namespace}::{local}"


def _definition_candidates(text: str, public_function: str) -> list[dict[str, Any]]:
    masked = _mask_cpp_source(text)
    leaf = public_function.rsplit("::", 1)[-1]
    token = re.compile(rf"\b{re.escape(leaf)}\b")
    definitions: list[dict[str, Any]] = []
    for match in token.finditer(masked):
        after_name = match.end()
        while after_name < len(masked) and masked[after_name].isspace():
            after_name += 1
        if after_name >= len(masked) or masked[after_name] != "(":
            continue
        close_parenthesis = _matching_delimiter(masked, after_name, "(", ")")
        if close_parenthesis is None:
            continue
        body_open = _next_body_open(masked, close_parenthesis + 1)
        if body_open is None:
            continue
        start = _signature_start(masked, match.start())
        prefix = masked[start : match.start()]
        if not prefix.strip() or "=" in prefix or CONTROL_PREFIX_RE.search(prefix):
            continue
        body_close = _matching_delimiter(masked, body_open, "{", "}")
        if body_close is None:
            continue
        line_start = text.count("\n", 0, start) + 1
        match_line = text.count("\n", 0, match.start()) + 1
        line_end = text.count("\n", 0, body_close) + 1
        excerpt = "\n".join(text.splitlines()[line_start - 1 : line_end])
        signature = " ".join(text[start:body_open].split())
        namespace = _enclosing_namespace(masked, match.start())
        qualified_name = _qualified_definition_name(signature, namespace, leaf)
        if "::" in public_function and qualified_name != public_function:
            continue
        definitions.append(
            {
                "match_line": match_line,
                "line_start": line_start,
                "line_end": line_end,
                "signature": signature[:2000],
                "qualified_name": qualified_name,
                "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                "excerpt_chars": len(excerpt),
                "definition_kind": "function_definition",
            }
        )
    return definitions


def _declaration_candidates(text: str, public_function: str) -> list[dict[str, Any]]:
    masked = _mask_cpp_source(text)
    leaf = public_function.rsplit("::", 1)[-1]
    token = re.compile(rf"\b{re.escape(leaf)}\b")
    declarations: list[dict[str, Any]] = []
    for match in token.finditer(masked):
        after_name = match.end()
        while after_name < len(masked) and masked[after_name].isspace():
            after_name += 1
        if after_name >= len(masked) or masked[after_name] != "(":
            continue
        close_parenthesis = _matching_delimiter(masked, after_name, "(", ")")
        if close_parenthesis is None:
            continue
        terminator = close_parenthesis + 1
        parenthesis_depth = 0
        bracket_depth = 0
        while terminator < min(len(masked), close_parenthesis + 4096):
            char = masked[terminator]
            if char == "(":
                parenthesis_depth += 1
            elif char == ")":
                if parenthesis_depth == 0:
                    break
                parenthesis_depth -= 1
            elif char == "[":
                bracket_depth += 1
            elif char == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif char == "{" and parenthesis_depth == 0 and bracket_depth == 0:
                break
            elif char == ";" and parenthesis_depth == 0 and bracket_depth == 0:
                start = _signature_start(masked, match.start())
                prefix = masked[start : match.start()]
                if not prefix.strip() or "=" in prefix or CONTROL_PREFIX_RE.search(prefix):
                    break
                declaration = " ".join(text[start : terminator + 1].split())
                namespace = _enclosing_namespace(masked, match.start())
                qualified_name = _qualified_definition_name(declaration, namespace, leaf)
                if "::" in public_function and qualified_name != public_function:
                    break
                declarations.append(
                    {
                        "line": text.count("\n", 0, match.start()) + 1,
                        "declaration": declaration[:2000],
                        "declaration_sha256": hashlib.sha256(
                            declaration.encode("utf-8")
                        ).hexdigest(),
                        "qualified_name": qualified_name,
                    }
                )
                break
            terminator += 1
    return declarations


def _source_files(
    source_root: Path,
    *,
    max_files: int,
    max_total_bytes: int,
    max_file_bytes: int,
) -> list[tuple[Path, str]]:
    resolved_root = source_root.resolve(strict=True)
    candidates: list[tuple[Path, str]] = []
    total_bytes = 0
    scanned_files = 0
    for current_raw, directory_names, file_names in os.walk(resolved_root, followlinks=False):
        current = Path(current_raw)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name.casefold() not in EXCLUDED_DIRECTORY_NAMES
            and not (current / name).is_symlink()
        )
        for name in sorted(file_names):
            candidate = current / name
            if candidate.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            scanned_files += 1
            if scanned_files > max_files:
                raise SourceDiscoveryError(
                    f"source discovery exceeded {max_files} C/C++ files; narrow SGGK_SOURCE_ROOT"
                )
            try:
                resolved = candidate.resolve(strict=True)
                relative = resolved.relative_to(resolved_root).as_posix()
                size = resolved.stat().st_size
            except (OSError, ValueError):
                continue
            if size > max_file_bytes:
                raise SourceDiscoveryError(
                    f"source file exceeds the {max_file_bytes}-byte discovery budget: {relative}"
                )
            total_bytes += size
            if total_bytes > max_total_bytes:
                raise SourceDiscoveryError(
                    "source discovery byte budget exceeded; narrow SGGK_SOURCE_ROOT"
                )
            candidates.append((resolved, relative))
    return sorted(
        candidates,
        key=lambda item: (item[0].suffix.lower() in HEADER_SUFFIXES, item[1].casefold()),
    )


def discover_function_definitions(
    public_function: str,
    source_root: Path | None,
    *,
    limit: int = 16,
    max_files: int = 12_000,
    max_total_bytes: int = 128 * 1024 * 1024,
    max_file_bytes: int = 16 * 1024 * 1024,
    max_excerpt_chars: int = 120_000,
) -> list[dict[str, Any]]:
    """Return complete, high-confidence definition ranges for one public function."""

    if source_root is None or not source_root.is_dir():
        return []
    definitions: list[dict[str, Any]] = []
    for path, relative_path in _source_files(
        source_root,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        max_file_bytes=max_file_bytes,
    ):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for definition in _definition_candidates(text, public_function):
            definitions.append(
                {
                    **definition,
                    "relative_path": relative_path,
                    "implementation_file": path.suffix.lower() in IMPLEMENTATION_SUFFIXES,
                }
            )
    definitions.sort(
        key=lambda item: (
            not bool(item["implementation_file"]),
            str(item["relative_path"]).casefold(),
            int(item["line_start"]),
        )
    )
    if len(definitions) > limit:
        raise SourceDiscoveryError(
            f"found {len(definitions)} definitions for {public_function}; "
            "overload set exceeds the bounded source contract"
        )
    if sum(int(item["excerpt_chars"]) for item in definitions) > max_excerpt_chars:
        raise SourceDiscoveryError(
            f"definitions for {public_function} exceed the source prompt budget"
        )
    return [
        {
            **item,
            "source_ref_id": f"implementation_{index:03d}",
        }
        for index, item in enumerate(definitions, 1)
    ]


def discover_header_declarations(
    public_function: str,
    include_root: Path | None,
    *,
    limit: int = 32,
    max_files: int = 12_000,
    max_total_bytes: int = 128 * 1024 * 1024,
    max_file_bytes: int = 16 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Return bounded, namespace-aware public-header overload declarations."""

    if include_root is None or not include_root.is_dir():
        return []
    declarations: list[dict[str, Any]] = []
    for path, relative_path in _source_files(
        include_root,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        max_file_bytes=max_file_bytes,
    ):
        if path.suffix.lower() not in HEADER_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for declaration in _declaration_candidates(text, public_function):
            declarations.append({**declaration, "header": relative_path})
    declarations.sort(key=lambda item: (str(item["header"]).casefold(), int(item["line"])))
    if len(declarations) > limit:
        raise SourceDiscoveryError(
            f"found {len(declarations)} declarations for {public_function}; "
            "overload set exceeds the bounded interface contract"
        )
    return [
        {**item, "function_ref_id": f"fn_{index:03d}"}
        for index, item in enumerate(declarations, 1)
    ]
