from __future__ import annotations

from test_harness.tools import oracle_text_zh


def test_point_relation_translation() -> None:
    text = oracle_text_zh.translate_oracle_failure(
        "point_relation_cone_base_inside_mismatch expected=Inside actual=OnEdge"
    )
    assert text == "点关系校验 cone_base_inside 不一致：期望 Inside（内部），实际 OnEdge（在边上）"


def test_face_point_relation_translation() -> None:
    text = oracle_text_zh.translate_oracle_failure(
        "face_point_relation_f1_mismatch expected=OnFace actual=Outside"
    )
    assert text == "面点关系校验 f1 不一致：期望 OnFace（在面上），实际 Outside（外部）"


def test_distance_translation() -> None:
    over_max = oracle_text_zh.translate_oracle_failure(
        "distance_check_bspline_wedge_dist_above_max actual=13.590832461262222 max=0"
    )
    assert over_max == "距离校验 bspline_wedge_dist 超出上限：实际 13.5908，上限 0"
    assert (
        oracle_text_zh.translate_oracle_failure("distance_check_gap_dist_below_min actual=1.09e-05 min=0.01")
        == "距离校验 gap_dist 低于下限：实际 1.09e-05，下限 0.01"
    )


def test_clash_translation() -> None:
    text = oracle_text_zh.translate_oracle_failure(
        "clash_check_cyl_wedge_overlap_clash_mismatch expected=AnyClash actual=Clash_None"
    )
    assert text == "干涉校验 cyl_wedge_overlap 不一致：期望 AnyClash（存在干涉），实际 Clash_None（无干涉）"


def test_plane_extreme_translation() -> None:
    text = oracle_text_zh.translate_oracle_failure(
        "plane_extreme_large_coord_x_max_not_expected actual=399884.85281374241 expected=399890 tol=0.02"
    )
    assert text == "平面极值校验 large_coord_x_max 与预期不符：实际 399885，期望 399890，容差 0.02"


def test_result_body_count_translation() -> None:
    assert (
        oracle_text_zh.translate_oracle_failure("result_body_count_above_max actual=2 max=1")
        == "结果体数量超出上限：实际 2，上限 1"
    )
    assert (
        oracle_text_zh.translate_oracle_failure("result_body_count_below_min actual=0 min=1")
        == "结果体数量低于下限：实际 0，下限 1"
    )


def test_reason_suffix_translation() -> None:
    assert (
        oracle_text_zh.translate_oracle_failure("point_relation_p1_exception")
        == "点关系校验 p1 执行异常"
    )
    assert (
        oracle_text_zh.translate_oracle_failure("distance_check_d1_role_unavailable")
        == "距离校验 d1 角色不可用"
    )


def test_passthrough_unknown() -> None:
    raw = "boolean_volume_relation_skipped_missing_input_properties"
    assert oracle_text_zh.translate_oracle_failure(raw) == raw
    assert oracle_text_zh.translate_oracle_failure("") == ""


def test_token_maps() -> None:
    assert oracle_text_zh.TRIAGE_REASON_ZH["runner_nonzero_exit"] == "运行器非零退出"
    assert oracle_text_zh.TRIAGE_REASON_ZH["validation_failed"] == "校验失败"
    assert oracle_text_zh.TRIAGE_REASON_ZH["missing_status"] == "缺少状态报告"
    assert oracle_text_zh.TRIAGE_REASON_ZH["runner_timeout"] == "运行超时"
    assert oracle_text_zh.SIGNATURE_KIND_ZH["oracle_failure"] == "校验失败"
    assert oracle_text_zh.SIGNATURE_KIND_ZH["crash"] == "崩溃"
    assert oracle_text_zh.SIGNATURE_KIND_ZH["sdk_api_error"] == "SDK 接口错误"
    assert oracle_text_zh.SIGNATURE_KIND_ZH["timeout"] == "超时"
    assert oracle_text_zh.SIGNATURE_KIND_ZH["topology_failure"] == "拓扑错误"
    assert oracle_text_zh.SIGNATURE_KIND_ZH["runner_error"] == "运行器错误"
    assert oracle_text_zh.SIGNATURE_KIND_ZH["pass"] == "通过"
    for phase, label in (
        ("parse", "解析"),
        ("build_inputs", "构造输入"),
        ("invoke_api", "接口调用"),
        ("serialize_result", "序列化结果"),
        ("topocheck", "拓扑检查"),
        ("oracle", "校验"),
    ):
        assert oracle_text_zh.PHASE_ZH[phase] == label
    assert oracle_text_zh.translate_reasons(["validation_failed", "custom_reason"]) == ["校验失败", "custom_reason"]


def test_fault_module_labels_complete() -> None:
    for module in (
        "distance_oracle",
        "point_relation_oracle",
        "clash_oracle",
        "plane_extreme_oracle",
        "step_import",
        "step_export",
        "api_under_test",
        "test_authoring",
        "unclassified",
    ):
        assert module in oracle_text_zh.FAULT_MODULE_LABEL_ZH
        assert oracle_text_zh.FAULT_MODULE_LABEL_ZH[module]
    assert oracle_text_zh.FAULT_MODULE_LABEL_ZH["unclassified"] == "未分类"


def test_value_glosses() -> None:
    assert oracle_text_zh.gloss_value("Inside") == "Inside（内部）"
    assert oracle_text_zh.gloss_value("Outside") == "Outside（外部）"
    assert oracle_text_zh.gloss_value("OnFace") == "OnFace（在面上）"
    assert oracle_text_zh.gloss_value("OnEdge") == "OnEdge（在边上）"
    assert oracle_text_zh.gloss_value("Clash_None") == "Clash_None（无干涉）"
    assert oracle_text_zh.gloss_value("AnyClash") == "AnyClash（存在干涉）"
    assert oracle_text_zh.gloss_value("SomethingElse") == "SomethingElse"
