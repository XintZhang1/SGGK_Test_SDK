from __future__ import annotations

from pathlib import Path

import pytest

from test_harness.orchestration.source_discovery import (
    SourceDiscoveryError,
    discover_function_definitions,
    discover_header_declarations,
    path_identity,
)


def test_discovery_uses_complete_definitions_not_strings_comments_or_calls(tmp_path: Path) -> None:
    source = tmp_path / "src" / "boolean.cpp"
    source.parent.mkdir()
    source.write_text(
        'const char* api_name = "api_boolean";\n'
        "// Result api_boolean(Fake value) { return value; }\n"
        "Result helper(Body a, Body b) {\n"
        "  return api_boolean(a, b);\n"
        "}\n"
        "Result api_boolean(Body target, Body tool) {\n"
        "  if (!target || !tool) {\n"
        "    return InvalidInput();\n"
        "  }\n"
        "  return RunBoolean(target, tool);\n"
        "}\n",
        encoding="utf-8",
    )

    definitions = discover_function_definitions("api_boolean", tmp_path)

    assert len(definitions) == 1
    assert definitions[0]["relative_path"] == "src/boolean.cpp"
    assert definitions[0]["match_line"] == 6
    assert definitions[0]["line_end"] == 11
    assert "Result api_boolean(Body target, Body tool)" in definitions[0]["signature"]


def test_discovery_binds_all_overloads_and_prefers_implementation_files(tmp_path: Path) -> None:
    header = tmp_path / "include" / "api.hpp"
    source = tmp_path / "src" / "api.cpp"
    header.parent.mkdir()
    source.parent.mkdir()
    header.write_text(
        "inline Result api_item(Body a) { return InlineRun(a); }\n",
        encoding="utf-8",
    )
    source.write_text(
        "Result api_item(Body a) { return Run(a); }\n"
        "Result api_item(Body a, Body b) { return Run(a, b); }\n",
        encoding="utf-8",
    )

    definitions = discover_function_definitions("api_item", tmp_path)

    assert len(definitions) == 3
    assert [item["relative_path"] for item in definitions[:2]] == ["src/api.cpp", "src/api.cpp"]
    assert definitions[2]["relative_path"] == "include/api.hpp"


def test_qualified_discovery_filters_same_leaf_in_other_namespaces(tmp_path: Path) -> None:
    source = tmp_path / "api.cpp"
    source.write_text(
        "namespace alpha {\n"
        "Result api_item(Body a) { return AlphaRun(a); }\n"
        "}\n"
        "namespace beta {\n"
        "Result api_item(Body a) { return BetaRun(a); }\n"
        "}\n",
        encoding="utf-8",
    )

    definitions = discover_function_definitions("alpha::api_item", tmp_path)

    assert len(definitions) == 1
    assert definitions[0]["qualified_name"] == "alpha::api_item"
    assert definitions[0]["match_line"] == 2


def test_qualified_discovery_combines_namespace_and_class_scope(tmp_path: Path) -> None:
    source = tmp_path / "widget.cpp"
    source.write_text(
        "namespace alpha {\n"
        "Result Widget::api_item(Body a) { return Run(a); }\n"
        "}\n",
        encoding="utf-8",
    )

    definitions = discover_function_definitions("alpha::Widget::api_item", tmp_path)

    assert len(definitions) == 1
    assert definitions[0]["qualified_name"] == "alpha::Widget::api_item"


def test_header_declarations_are_namespace_aware_and_ignore_comments(tmp_path: Path) -> None:
    include = tmp_path / "include"
    header = include / "api.hpp"
    header.parent.mkdir()
    header.write_text(
        "// Result api_item(Fake value);\n"
        "namespace alpha {\n"
        "Result api_item(Body a);\n"
        "Result api_item(Body a, Body b) noexcept;\n"
        "}\n"
        "namespace beta { Result api_item(Body a); }\n",
        encoding="utf-8",
    )

    declarations = discover_header_declarations("alpha::api_item", include)

    assert len(declarations) == 2
    assert all(item["qualified_name"] == "alpha::api_item" for item in declarations)
    assert declarations[0]["function_ref_id"] == "fn_001"


def test_discovery_prunes_generated_trees_and_fails_closed_on_budget(tmp_path: Path) -> None:
    generated = tmp_path / "build" / "generated.cpp"
    generated.parent.mkdir()
    generated.write_text("Result api_item(Body a) { return Generated(a); }\n", encoding="utf-8")
    source = tmp_path / "src" / "api.cpp"
    source.parent.mkdir()
    source.write_text("Result api_item(Body a) { return Run(a); }\n", encoding="utf-8")

    assert len(discover_function_definitions("api_item", tmp_path)) == 1
    with pytest.raises(SourceDiscoveryError, match="exceeded 0 C/C\\+\\+ files"):
        discover_function_definitions("api_item", tmp_path, max_files=0)


def test_path_identity_is_stable_without_exposing_the_path(tmp_path: Path) -> None:
    identity = path_identity(tmp_path)

    assert identity == path_identity(tmp_path / ".")
    assert str(tmp_path) not in identity
    assert len(identity) == 64
