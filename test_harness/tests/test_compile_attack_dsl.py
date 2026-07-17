from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_harness.tools.compile_attack_dsl import DslError, compile_dsl_file


def _write_dsl(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "case.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _base_dsl(case: dict) -> dict:
    return {
        "dsl_version": 1,
        "constants": {"topo_tol": 0.01},
        "defaults": {"api": "api_boolean", "boolean_type": "SUBTRACTION", "modeling_tol": "topo_tol"},
        "cases": [case],
    }


def _primitive_case(**extra: object) -> dict:
    case: dict[str, object] = {
        "case_id": "demo",
        "target": {"kind": "solid_cylinder", "radius": 10.0, "height": 20.0},
        "tool": {"kind": "solid_wedge", "length": 5.0, "width": 6.0, "height": 7.0},
    }
    case.update(extra)
    return case


def test_case_level_chain_fails_closed(tmp_path: Path) -> None:
    path = _write_dsl(
        tmp_path,
        _base_dsl(
            _primitive_case(
                chain=[
                    {"id": "step_1", "op": "solid_wedge", "length": 3.0, "width": 4.0, "height": 5.0},
                    {"id": "step_2", "op": "boolean", "base": "target", "tool": "step_1"},
                ]
            )
        ),
    )

    with pytest.raises(DslError, match="unsupported case fields"):
        compile_dsl_file(path)


def test_unknown_option_field_fails_closed(tmp_path: Path) -> None:
    path = _write_dsl(
        tmp_path,
        _base_dsl(_primitive_case(options={"boolean_type": "SUBTRACTION", "repeat": 2})),
    )

    with pytest.raises(DslError, match="unsupported fields"):
        compile_dsl_file(path)


def test_known_case_and_option_fields_still_compile(tmp_path: Path) -> None:
    path = _write_dsl(
        tmp_path,
        _base_dsl(
            _primitive_case(
                hypothesis="baseline",
                options={"boolean_type": "SUBTRACTION", "check_valid": True},
                expectations={"result_bodies": {"min": 1, "max": 1}},
            )
        ),
    )

    recipes = compile_dsl_file(path)

    assert len(recipes) == 1
    assert recipes[0]["case_id"] == "demo"
    assert recipes[0]["api"] == "api_boolean"
