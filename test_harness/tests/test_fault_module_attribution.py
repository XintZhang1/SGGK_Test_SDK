from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from test_harness.tools import analyze_failure_cases


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def bbox(mn: tuple[float, float, float], mx: tuple[float, float, float]) -> dict[str, Any]:
    return {"empty": False, "min": list(mn), "max": list(mx)}


def input_props(
    target: tuple = ((0, 0, 0), (10, 10, 10)),
    tool: tuple = ((0, 0, 0), (10, 10, 10)),
) -> dict[str, Any]:
    return {
        "target": [{"index": 0, "bbox": bbox(*target)}],
        "tool": [{"index": 0, "bbox": bbox(*tool)}],
    }


def result_props(box: tuple = ((0, 0, 0), (10, 10, 10))) -> dict[str, Any]:
    return {"bodies": [{"index": 0, "bbox": bbox(*box)}]}


def make_case_capsule(
    cases_root: Path,
    case_id: str,
    *,
    validation: dict[str, Any] | None = None,
    topo_check: dict[str, Any] | None = None,
    input_properties: dict[str, Any] | None = None,
    properties: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
    recipe: dict[str, Any] | None = None,
) -> Path:
    case_dir = cases_root / case_id
    write_json(case_dir / "manifest.json", {"case_id": case_id, "api": "api_boolean"})
    write_json(case_dir / "run_state.json", {"case_id": case_id, "returncode": 2, "timed_out": False})
    write_json(case_dir / "report" / "status.json", {"succeeded": False, "error_code": 3})
    write_json(
        case_dir / "report" / "validation.json",
        validation if validation is not None else {"ok": False, "failures": []},
    )
    write_json(
        case_dir / "report" / "topo_check.json",
        topo_check if topo_check is not None else {"bodies": [], "topologies": []},
    )
    if input_properties is not None:
        write_json(case_dir / "report" / "input_properties.json", input_properties)
    if properties is not None:
        write_json(case_dir / "report" / "properties.json", properties)
    write_json(
        case_dir / "input" / "recipe.json",
        recipe if recipe is not None else {"api": "api_boolean", "case_id": case_id, "expectations": {}},
    )
    if comparison is not None:
        write_json(case_dir / "comparison" / "comparison.json", comparison)
    return case_dir


def make_step_exports(case_dir: Path, case_id: str, *, attempt_root: Path | None = None) -> Path:
    """Create a fake parasolid_compare export tree that _locate_case_steps accepts."""

    if attempt_root is None:
        # cases_root/<case> → 往上四级放在 cases_root 旁边。
        attempt_root = case_dir.parent.parent
    export_dir = attempt_root / "parasolid_compare" / case_id / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    exports: dict[str, Any] = {}
    for role in ("target", "tool", "result_1"):
        step = export_dir / f"{role}.step"
        step.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
        exports[role] = {"step": str(step), "ok": True}
    write_json(export_dir / "export_manifest.json", {"case_dir": str(case_dir), "exports": exports})
    return export_dir


def failed_distance_capsule(cases_root: Path, case_id: str = "case_dist") -> Path:
    return make_case_capsule(
        cases_root,
        case_id,
        validation={
            "ok": False,
            "failures": ["distance_check_gap_above_max actual=13.59 max=0"],
            "distance_checks": [
                {
                    "id": "gap",
                    "role_a": "target",
                    "role_b": "tool",
                    "body_index_a": 0,
                    "body_index_b": 0,
                    "kind": "minimum",
                    "threshold": 0,
                    "expectation": {
                        "min_set": False,
                        "min": 0,
                        "max_set": True,
                        "max": 0,
                        "abs_tol": 1e-5,
                        "rel_tol": 1e-8,
                    },
                    "actual": 13.59,
                    "ok": False,
                }
            ],
        },
        input_properties=input_props(),
        properties=result_props(),
    )


def failed_point_capsule(cases_root: Path, case_id: str = "case_point") -> Path:
    return make_case_capsule(
        cases_root,
        case_id,
        validation={
            "ok": False,
            "failures": ["point_relation_p1_mismatch expected=Inside actual=OnEdge"],
            "point_relations": [
                {
                    "id": "p1",
                    "role": "result",
                    "body_index": 0,
                    "point": [1, 1, 1],
                    "expected": "Inside",
                    "tolerance": 0.01,
                    "check_boundary": True,
                    "actual": "OnEdge",
                    "ok": False,
                }
            ],
        },
        input_properties=input_props(),
        properties=result_props(),
    )


def fake_runner(records_by_id: dict[str, dict[str, Any]]):
    def run(
        case_dir: Path,
        case_id: str,
        checks: list[dict[str, Any]],
        steps: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        results = []
        for check in checks:
            record = records_by_id.get(str(check.get("id")), {"status": "unsupported", "actual": None})
            results.append(analyze_failure_cases._classify_recheck(check, record))
        return {"ran": True, "note": "", "checks": results}

    return run


# ---------------------------------------------------------------------------
# fault_module 三分支归因规则
# ---------------------------------------------------------------------------


def test_module_test_authoring_from_expectation_rule(tmp_path: Path) -> None:
    case_dir = make_case_capsule(
        tmp_path / "cases",
        "case_x",
        validation={
            "ok": False,
            "failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
            "point_relations": [
                {
                    "id": "p1",
                    "role": "result",
                    "point": [500, 500, 500],
                    "expected": "Inside",
                    "actual": "OnFace",
                    "ok": False,
                }
            ],
        },
        input_properties=input_props(),
        properties=result_props(),
    )
    result = analyze_failure_cases.analyze_case(case_dir, skip_recheck=True)
    assert result["fault_domain"] == "test_expectation_suspect"
    assert result["fault_module"] == "test_authoring"
    assert "归因模块" in result["notes"]
    assert "不构成 SDK 缺陷定论" in result["notes"]


def test_module_step_export_from_transport_drift(tmp_path: Path) -> None:
    comparison = {
        "verdict": "parasolid_correct",
        "signals": {"parasolid_available": True, "sggk_valid": False},
        "sggk": {"api_ok": True, "result_body_count": 1},
        "parasolid": {"import_ok": True, "boolean_ok": True, "measurement_ok": True},
        "sggk_result_nx": {"available": False, "import_ok": False},
    }
    case_dir = make_case_capsule(
        tmp_path / "cases",
        "case_x",
        comparison=comparison,
        input_properties=input_props(),
        properties=result_props(),
    )
    result = analyze_failure_cases.analyze_case(case_dir, skip_recheck=True)
    assert result["fault_domain"] == "transport_suspect"
    assert result["fault_module"] == "step_export"


def test_module_step_import_from_import_limited(tmp_path: Path) -> None:
    comparison = {
        "verdict": "parasolid_correct",
        "signals": {"parasolid_available": True},
        "sggk": {
            "api_ok": True,
            "self_measurement_ok": True,
            "self_total_area": 1000.0,
            "self_total_abs_volume": 500.0,
        },
        "parasolid": {"import_ok": True, "measurement_ok": True},
        "sggk_result_nx": {
            "available": True,
            "import_ok": True,
            "measurement_ok": True,
            "total_area": 1500.0,
            "total_abs_volume": 500.0,
        },
        "tolerances": {"abs_tol": 0.01, "rel_tol": 1e-5},
    }
    # NX 可导入但自报与 NX 测量漂移 → transport_suspect；输入坐标超大 → step_import。
    huge = ((399800.0, -10.0, -10.0), (399900.0, 10.0, 10.0))
    case_dir = make_case_capsule(
        tmp_path / "cases",
        "case_x",
        comparison=comparison,
        input_properties=input_props(target=huge, tool=huge),
        properties=result_props(),
    )
    result = analyze_failure_cases.analyze_case(case_dir, skip_recheck=True)
    assert result["fault_domain"] == "transport_suspect"
    assert result["fault_module"] == "step_import"
    assert any("module_rule=input_step_huge_coordinates" in line for line in result["evidence"])


def test_module_distance_oracle_from_recheck_disagree(tmp_path: Path) -> None:
    case_dir = failed_distance_capsule(tmp_path / "cases")
    make_step_exports(case_dir, "case_dist")
    result = analyze_failure_cases.analyze_case(
        case_dir,
        recheck_runner=fake_runner({"gap": {"status": "measured", "actual": 0.0001}}),
    )
    assert result["fault_module"] == "distance_oracle"
    recheck = result["recheck"]
    assert recheck["ran"] is True
    assert recheck["checks"][0]["relation"] == "disagree"
    assert any("module_rule=oracle_vs_nx_recheck" in line for line in result["evidence"])


def test_module_point_relation_oracle_from_recheck_disagree(tmp_path: Path) -> None:
    case_dir = failed_point_capsule(tmp_path / "cases")
    make_step_exports(case_dir, "case_point")
    result = analyze_failure_cases.analyze_case(
        case_dir,
        recheck_runner=fake_runner({"p1": {"status": "measured", "actual": "Inside"}}),
    )
    assert result["fault_module"] == "point_relation_oracle"
    assert result["recheck"]["checks"][0]["relation"] == "disagree"


def test_module_test_authoring_from_recheck_agree(tmp_path: Path) -> None:
    case_dir = failed_point_capsule(tmp_path / "cases")
    make_step_exports(case_dir, "case_point")
    result = analyze_failure_cases.analyze_case(
        case_dir,
        recheck_runner=fake_runner({"p1": {"status": "measured", "actual": "OnEdge"}}),
    )
    # SGGK oracle ≈ NX 复核，且 NX 复核同样不满足模型预期 → 预期编写问题。
    assert result["fault_module"] == "test_authoring"
    assert result["recheck"]["checks"][0]["nx_satisfies_expectation"] is False


def test_module_api_under_test_from_geometry(tmp_path: Path) -> None:
    case_dir = make_case_capsule(
        tmp_path / "cases",
        "case_x",
        topo_check={"bodies": [{"ok": False, "error_string": "non-manifold edge"}]},
    )
    result = analyze_failure_cases.analyze_case(case_dir, skip_recheck=True)
    assert result["fault_domain"] == "geometry_result_suspect"
    assert result["fault_module"] == "api_under_test"


def test_module_unclassified_divergent_geometry(tmp_path: Path) -> None:
    comparison = {
        "verdict": "inconclusive",
        "signals": {
            "parasolid_available": True,
            "sggk_valid": True,
            "parasolid_valid": True,
            "sggk_result_measurable": True,
        },
        "sggk": {"api_ok": True, "result_body_count": 1},
        "parasolid": {"import_ok": True, "boolean_ok": True, "measurement_ok": True, "body_count": 1},
        "sggk_result_nx": {"available": True, "import_ok": True, "all_solid_closed": True, "bodies": []},
    }
    case_dir = make_case_capsule(
        tmp_path / "cases",
        "case_x",
        comparison=comparison,
        validation={"ok": False, "failures": ["result_body_count_above_max actual=2 max=1"]},
        input_properties=input_props(),
        properties=result_props(),
    )
    result = analyze_failure_cases.analyze_case(case_dir, skip_recheck=True)
    # 两内核结果分歧但 Parasolid 内部自洽：不强行归因到布尔。
    assert result["fault_module"] == "unclassified"
    assert any("module_rule=cross_kernel_divergence_self_consistent" in line for line in result["evidence"])


def test_module_unsupported_recheck_falls_back(tmp_path: Path) -> None:
    case_dir = failed_point_capsule(tmp_path / "cases")
    make_step_exports(case_dir, "case_point")
    result = analyze_failure_cases.analyze_case(
        case_dir,
        recheck_runner=fake_runner({"p1": {"status": "unsupported", "actual": None}}),
    )
    assert result["recheck"]["checks"][0]["relation"] == "unmeasured"
    # 无有效复核票；validation 失败且无工具/传输证据 → 几何结果侧 → api_under_test。
    assert result["fault_module"] == "api_under_test"


def test_recheck_missing_steps_skips(tmp_path: Path) -> None:
    case_dir = failed_point_capsule(tmp_path / "cases")
    result = analyze_failure_cases.analyze_case(case_dir)
    assert result["recheck"]["ran"] is False
    assert "STEP" in result["recheck"]["note"]


# ---------------------------------------------------------------------------
# 复核执行器（NX 调用全部 mock 掉）与 journal 形状
# ---------------------------------------------------------------------------


def test_default_recheck_runner_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case_dir = failed_point_capsule(tmp_path / "cases")
    export_dir = make_step_exports(case_dir, "case_point")
    steps = analyze_failure_cases._locate_case_steps(case_dir, "case_point")
    assert steps["target"].endswith("target.step")
    assert steps["result_dir"] == str(export_dir)

    monkeypatch.setattr(analyze_failure_cases, "_nx_recheck_available", lambda: True)
    captured: dict[str, Any] = {}

    def fake_execute(journal, *, allowed_roots, arguments, timeout_seconds):
        captured["journal"] = str(journal)
        captured["arguments"] = list(arguments)
        spec = json.loads(Path(arguments[3]).read_text(encoding="utf-8"))
        captured["spec"] = spec
        report = {
            "schema_version": 1,
            "kind": "sggk_nx_oracle_recheck",
            "ok": True,
            "status": "completed",
            "checks": [
                {"id": "p1", "kind": "point_relation", "status": "measured", "ok": True, "actual": "OnEdge"},
            ],
            "diagnostics": [],
        }
        Path(arguments[4]).write_text(json.dumps(report), encoding="utf-8")
        return {"ok": True, "status": "completed"}

    import test_harness.nx as nx_api

    monkeypatch.setattr(nx_api, "execute_nx_journal", fake_execute)
    checks = analyze_failure_cases._build_recheck_checks(analyze_failure_cases._collect(case_dir))
    outcome = analyze_failure_cases.run_oracle_recheck(case_dir, "case_point", checks)
    assert outcome["ran"] is True
    # journal 调用固定为 oracle_recheck.py，STEP 通过 --arg 传递。
    assert captured["journal"].endswith("oracle_recheck.py")
    assert captured["arguments"][0].endswith("target.step")
    assert captured["arguments"][4].endswith("recheck_out.json")
    # spec 只携带 journal 字段（不含 oracle_actual/expected 等归因辅助字段）。
    spec_check = captured["spec"]["checks"][0]
    assert captured["spec"]["kind"] == "sggk_nx_oracle_recheck_request"
    assert spec_check["kind"] == "point_relation"
    assert "oracle_actual" not in spec_check and "expected" not in spec_check
    assert spec_check["point"] == [1, 1, 1]
    assert outcome["checks"][0]["relation"] == "agree"
    assert outcome["checks"][0]["nx_satisfies_expectation"] is False


def test_classify_recheck_shapes() -> None:
    # distance：容差内一致/超差不一致。
    check = {
        "id": "d1",
        "family": "distance_checks",
        "kind": "distance",
        "oracle_actual": 13.59,
        "expectation": {"min": None, "max": 0.0, "abs_tol": 1e-5, "rel_tol": 1e-8},
    }
    agree = analyze_failure_cases._classify_recheck(check, {"status": "measured", "actual": 13.590001})
    assert agree["relation"] == "agree"
    disagree = analyze_failure_cases._classify_recheck(check, {"status": "measured", "actual": 0.0})
    assert disagree["relation"] == "disagree"
    assert disagree["nx_satisfies_expectation"] is True
    error = analyze_failure_cases._classify_recheck(check, {"status": "error", "actual": None})
    assert error["relation"] == "unmeasured"
    # plane_extreme：期望值容差判断。
    plane = {
        "id": "px",
        "family": "plane_extreme_checks",
        "kind": "plane_extreme",
        "oracle_actual": 399884.85,
        "expected": 399890.0,
        "tolerance": 0.02,
    }
    plane_result = analyze_failure_cases._classify_recheck(plane, {"status": "measured", "actual": 399890.01})
    assert plane_result["relation"] == "disagree"
    assert plane_result["nx_satisfies_expectation"] is True
