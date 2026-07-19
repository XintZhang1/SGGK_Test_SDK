from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_harness.tools import export_failure_gtest


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def make_showcase_case(
    root: Path,
    case_id: str = "cone_torus_sub",
    *,
    recipe: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    topo_check: dict[str, Any] | None = None,
    pre_analysis: dict[str, Any] | None = None,
    with_sgt: bool = True,
) -> Path:
    case_dir = root / case_id
    write_json(
        case_dir / "input" / "recipe.json",
        recipe
        if recipe is not None
        else {
            "api": "api_boolean",
            "case_id": case_id,
            "boolean_type": "SUBTRACTION",
            "check_valid": True,
            "non_destructive": True,
            "modeling_tol": 0.01,
            "target_kind": "solid_cone",
            "target_bottom_radius": 60.0,
            "target_top_radius": 20.0,
            "target_height": 180.0,
            "tool_kind": "solid_torus",
            "tool_long_radius": 80.0,
            "tool_short_radius": 20.0,
            "tool_translate_x": 99.99999,
            "expectations": {
                "result_bodies": {"min": 1, "max": 1},
                "point_relations": [
                    {
                        "id": "cone_base_inside",
                        "role": "result",
                        "body_index": 0,
                        "point": [0.0, 0.0, 0.0],
                        "expected": "Inside",
                        "tolerance": 0.01,
                        "check_boundary": True,
                    }
                ],
                "distance_checks": [
                    {
                        "id": "cone_torus_dist",
                        "role_a": "target",
                        "role_b": "tool",
                        "kind": "minimum",
                        "threshold": 200.0,
                        "distance": {"max": 0.0, "abs_tol": 1e-5},
                    }
                ],
            },
        },
    )
    write_json(
        case_dir / "report" / "validation.json",
        validation
        if validation is not None
        else {
            "ok": False,
            "failures": [
                "point_relation_cone_base_inside_mismatch expected=Inside actual=OnEdge",
                "distance_check_cone_torus_dist_above_max actual=1.0853e-05 max=0",
            ],
            "point_relations": [
                {
                    "id": "cone_base_inside",
                    "role": "result",
                    "body_index": 0,
                    "point": [0.0, 0.0, 0.0],
                    "expected": "Inside",
                    "tolerance": 0.01,
                    "check_boundary": True,
                    "actual": "OnEdge",
                    "ok": False,
                }
            ],
            "distance_checks": [
                {
                    "id": "cone_torus_dist",
                    "role_a": "target",
                    "role_b": "tool",
                    "body_index_a": 0,
                    "body_index_b": 0,
                    "kind": "minimum",
                    "threshold": 200.0,
                    "expectation": {"min_set": False, "max_set": True, "max": 0.0, "abs_tol": 1e-5},
                    "actual": 1.0853e-05,
                    "ok": False,
                }
            ],
        },
    )
    write_json(
        case_dir / "report" / "topo_check.json",
        topo_check if topo_check is not None else {"bodies": [{"ok": True}], "topologies": []},
    )
    if pre_analysis is not None:
        write_json(case_dir / "pre_analysis.json", pre_analysis)
    if with_sgt:
        (case_dir / "input").mkdir(parents=True, exist_ok=True)
        (case_dir / "input" / "target.sgt").write_bytes(b"sgt-target")
        (case_dir / "input" / "tool.sgt").write_bytes(b"sgt-tool")
        (case_dir / "output").mkdir(parents=True, exist_ok=True)
        (case_dir / "output" / "result_1.sgt").write_bytes(b"sgt-result")
    return case_dir


def test_full_repro_structure(tmp_path: Path) -> None:
    case_dir = make_showcase_case(tmp_path)
    result = export_failure_gtest.generate_repro_cpp(case_dir)
    cpp = result["cpp"]
    assert result["file_name"] == "cone_torus_sub_repro.cpp"
    assert result["simplified"] is False
    assert "TEST(SggkFailureRepro, ConeTorusSub)" in cpp
    assert "// 用例：cone_torus_sub" in cpp
    assert "诊断性证据，不构成 SDK 缺陷定论" in cpp
    # 三段式结构。
    assert "输入构造（人工复核区，可按分割线整体删减）" in cpp
    assert "被测接口调用" in cpp
    assert "校验（EXPECT_*）" in cpp
    # 构造参数与 recipe 完全一致。
    assert "sggk::api_make_solid_cone(sggk::Ucs3D(), 60.0, 20.0, 180.0, 6.283185307179586, true)" in cpp
    assert "sggk::api_make_solid_torus(sggk::Ucs3D(), 80.0, 20.0, 6.283185307179586, true)" in cpp
    assert "tool->Transform(sggk::Matrix4::MakeTranslation(99.99999, 0.0, 0.0));" in cpp
    # 被测接口与选项。
    assert "sggk::BooleanOpts opts(sggk::BooleanType::SUBTRACTION);" in cpp
    assert "opts.SetModelingTol(0.01);" in cpp
    assert "auto ret = sggk::api_boolean(target, tool, opts);" in cpp
    # 校验：成功标志 + 体数 + 两个失败 oracle（带中文校验项注释）。
    assert "EXPECT_TRUE(ret->Succeeded())" in cpp
    assert "EXPECT_GE(resultBodies.size(), 1u)" in cpp
    assert "EXPECT_LE(resultBodies.size(), 1u)" in cpp
    assert "点关系校验 cone_base_inside" in cpp
    assert "EXPECT_EQ(info.relation, sggk::BodyPtRelType::Inside)" in cpp
    assert "距离校验 cone_torus_dist" in cpp
    assert "sggk::api_topo_minimum_distance(" in cpp
    assert "200.0" in cpp


def test_simplified_branch_loads_sgt(tmp_path: Path) -> None:
    case_dir = make_showcase_case(
        tmp_path,
        pre_analysis={"fault_module": "step_export", "fault_domain": "transport_suspect"},
    )
    result = export_failure_gtest.generate_repro_cpp(case_dir)
    cpp = result["cpp"]
    assert result["simplified"] is True
    assert "裁剪说明：几何结果与 Parasolid 一致，仅保留可疑工具环节" in cpp
    # 构造链整段被注释。
    assert "// sggk::PrimitivesRetPtr targetPrimRet = sggk::api_make_solid_cone" in cpp
    # 改从证据目录载入 .sgt。
    assert 'DeserializeBodiesFromFile("input/target.sgt")' in cpp
    assert 'DeserializeBodiesFromFile("input/tool.sgt")' in cpp
    assert 'DeserializeBodiesFromFile("output/result_1.sgt")' in cpp
    # 裁剪版不再调用被测接口，但保留可疑校验。
    assert "auto ret = sggk::api_boolean(target, tool, opts);" not in cpp
    assert "点关系校验 cone_base_inside" in cpp


def test_no_simplify_when_geometry_invariants_failed(tmp_path: Path) -> None:
    case_dir = make_showcase_case(
        tmp_path,
        topo_check={"bodies": [{"ok": False, "error_string": "non-manifold"}], "topologies": []},
        pre_analysis={"fault_module": "step_export", "fault_domain": "transport_suspect"},
    )
    result = export_failure_gtest.generate_repro_cpp(case_dir)
    assert result["simplified"] is False
    assert "auto ret = sggk::api_boolean(target, tool, opts);" in result["cpp"]


def test_no_simplify_for_api_under_test(tmp_path: Path) -> None:
    case_dir = make_showcase_case(
        tmp_path,
        pre_analysis={"fault_module": "api_under_test", "fault_domain": "geometry_result_suspect"},
    )
    result = export_failure_gtest.generate_repro_cpp(case_dir)
    assert result["simplified"] is False


def test_combine_bodies_api(tmp_path: Path) -> None:
    case_dir = make_showcase_case(
        tmp_path,
        case_id="combine_case",
        recipe={
            "api": "api_combine_bodies",
            "case_id": "combine_case",
            "combine_clone": False,
            "target_kind": "solid_sphere",
            "target_radius": 50.0,
            "tool_kind": "solid_wedge",
            "tool_length": 10.0,
            "tool_width": 20.0,
            "tool_height": 30.0,
            "expectations": {},
        },
        validation={"ok": False, "failures": ["result_body_count_above_max actual=2 max=1"]},
    )
    result = export_failure_gtest.generate_repro_cpp(case_dir)
    cpp = result["cpp"]
    assert "sggk::api_combine_bodies(combineInputs, false);" in cpp
    assert "sggk::api_make_solid_sphere(sggk::Ucs3D(), 50.0, true)" in cpp
    assert "sggk::api_make_solid_wedge(sggk::Ucs3D(), 10.0, 20.0, 30.0)" in cpp
    assert "EXPECT_TRUE(static_cast<bool>(result))" in cpp


def test_thicken_body_api(tmp_path: Path) -> None:
    case_dir = make_showcase_case(
        tmp_path,
        case_id="thicken_case",
        recipe={
            "api": "api_thicken_body",
            "case_id": "thicken_case",
            "min_dist": -2.0,
            "max_dist": 5.0,
            "target_kind": "plane_sheet",
            "target_length": 40.0,
            "target_width": 30.0,
            "expectations": {},
        },
        validation={"ok": False, "failures": ["result_body_count_below_min actual=0 min=1"]},
    )
    result = export_failure_gtest.generate_repro_cpp(case_dir)
    cpp = result["cpp"]
    assert "sggk::api_create_face(targetPlane, sggk::UVRange(" in cpp
    assert "sggk::api_topo_to_body(targetFace)" in cpp
    assert "auto ret = sggk::api_thicken_body(target, -2.0, 5.0);" in cpp
    # api_thicken_body 为单元接口：不构造 tool。
    assert "toolPrimRet" not in cpp


def test_loaded_sgt_kind(tmp_path: Path) -> None:
    case_dir = make_showcase_case(
        tmp_path,
        case_id="loaded_case",
        recipe={
            "api": "api_boolean",
            "case_id": "loaded_case",
            "boolean_type": "UNION",
            "target_kind": "loaded_sgt",
            "target_source_file": "input/target.sgt",
            "target_body_index": 0,
            "tool_kind": "solid_cylinder",
            "tool_radius": 10.0,
            "tool_height": 20.0,
            "tool_angle": 6.283185307179586,
            "expectations": {},
        },
        validation={"ok": False, "failures": ["result_body_count_above_max actual=2 max=1"]},
    )
    result = export_failure_gtest.generate_repro_cpp(case_dir)
    cpp = result["cpp"]
    assert 'DeserializeBodiesFromFile("input/target.sgt")' in cpp
    assert "sggk::BooleanOpts opts(sggk::BooleanType::UNION);" in cpp


def test_unknown_kind_skips(tmp_path: Path) -> None:
    case_dir = make_showcase_case(
        tmp_path,
        case_id="unknown_case",
        recipe={
            "api": "api_boolean",
            "case_id": "unknown_case",
            "target_kind": "future_kind_2030",
            "tool_kind": "solid_sphere",
            "expectations": {},
        },
        validation={"ok": False, "failures": ["result_body_count_above_max actual=2 max=1"]},
    )
    result = export_failure_gtest.generate_repro_cpp(case_dir)
    assert "GTEST_SKIP()" in result["cpp"]
    assert "future_kind_2030" in result["cpp"]


def test_plane_extreme_and_clash_checks(tmp_path: Path) -> None:
    case_dir = make_showcase_case(
        tmp_path,
        validation={
            "ok": False,
            "failures": [
                "plane_extreme_zmax_not_expected actual=55 expected=80 tol=0.02",
                "clash_check_c1_clash_mismatch expected=AnyClash actual=Clash_None",
            ],
            "plane_extreme_checks": [
                {
                    "id": "zmax",
                    "role": "result",
                    "body_index": 0,
                    "axis": "z",
                    "side": "max",
                    "expected": 80.0,
                    "tolerance": 0.02,
                    "actual_extreme": 55.0,
                    "ok": False,
                }
            ],
            "clash_checks": [
                {
                    "id": "c1",
                    "role_a": "target",
                    "role_b": "tool",
                    "body_index_a": 0,
                    "body_index_b": 0,
                    "expected": "AnyClash",
                    "mode": "ClashClassify",
                    "tolerance": 0.01,
                    "actual": "Clash_None",
                    "ok": False,
                }
            ],
        },
    )
    result = export_failure_gtest.generate_repro_cpp(case_dir)
    cpp = result["cpp"]
    assert "平面极值校验 zmax" in cpp
    assert "checkBox.MaxPoint().Z()" in cpp
    assert "EXPECT_NEAR(checkBox.MaxPoint().Z(), 80.0, 0.02)" in cpp
    assert "干涉校验 c1" in cpp
    assert "sggk::api_body_clash(" in cpp
    assert "EXPECT_NE(clashRet->GetClashType(), sggk::ClashType::Clash_None)" in cpp


def test_deterministic_output(tmp_path: Path) -> None:
    case_dir = make_showcase_case(tmp_path)
    first = export_failure_gtest.generate_repro_cpp(case_dir)["cpp"]
    second = export_failure_gtest.generate_repro_cpp(case_dir)["cpp"]
    assert first == second


def test_export_writes_file(tmp_path: Path) -> None:
    case_dir = make_showcase_case(tmp_path)
    name = export_failure_gtest.export_case_repro(case_dir, source_label="测试来源")
    out = case_dir / name
    assert name == "cone_torus_sub_repro.cpp"
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "测试来源" in text
    # 生成的文件只含一个 TEST 用例：头文件与 init 由宿主测试工程提供。
    assert "#include" not in text
    assert "ReproSessionGuard" not in text
    assert text.count("TEST(SggkFailureRepro,") == 1
