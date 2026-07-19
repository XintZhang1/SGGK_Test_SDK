"""Deterministic Chinese glosses for failure-analysis tokens.

Single source of truth for the 失败分析 UI tab, ``analysis.md`` and
``pre_analysis.json`` notes: triage reasons, failure-signature kinds/phases,
oracle failure strings, and point-relation/clash value glosses.  Every mapping
is a fixed table — no model involvement, no fuzzy matching.  Strings that do
not match a known pattern pass through unchanged so raw tokens stay greppable.
"""

from __future__ import annotations

import re

TRIAGE_REASON_ZH = {
    "runner_nonzero_exit": "运行器非零退出",
    "validation_failed": "校验失败",
    "missing_status": "缺少状态报告",
    "runner_timeout": "运行超时",
    "topology_failed": "拓扑检查失败",
    "status_succeeded_false": "状态报告显示执行未成功",
}

SIGNATURE_KIND_ZH = {
    "oracle_failure": "校验失败",
    "crash": "崩溃",
    "sdk_api_error": "SDK 接口错误",
    "timeout": "超时",
    "topology_failure": "拓扑错误",
    "runner_error": "运行器错误",
    "pass": "通过",
}

PHASE_ZH = {
    "parse": "解析",
    "build_inputs": "构造输入",
    "invoke_api": "接口调用",
    "serialize_result": "序列化结果",
    "topocheck": "拓扑检查",
    "oracle": "校验",
}

FAULT_MODULE_LABEL_ZH = {
    "distance_oracle": "距离测量工具",
    "point_relation_oracle": "点关系测量工具",
    "clash_oracle": "干涉测量工具",
    "plane_extreme_oracle": "平面极值测量工具",
    "step_import": "STEP 导入环节",
    "step_export": "STEP 导出环节",
    "api_under_test": "被测接口（SDK）",
    "test_authoring": "测试预期编写",
    "unclassified": "未分类",
}

# Chinese glosses for oracle enum values, embedded in translated sentences as
# ``Inside（内部）`` so the raw token stays visible next to its meaning.
POINT_RELATION_GLOSS_ZH = {
    "Unknown": "未知",
    "OnVertex": "在顶点上",
    "OnEdge": "在边上",
    "OnFace": "在面上",
    "Inside": "内部",
    "Outside": "外部",
    "OnBoundary": "在边界上",
    "OnModel": "在模型上",
}

CLASH_GLOSS_ZH = {
    "AnyClash": "存在干涉",
    "Clash_None": "无干涉",
    "Clash_Unknown": "干涉未知",
}


def gloss_value(token: str) -> str:
    """Return ``token（中文释义）`` when a gloss exists, else the raw token."""

    gloss = POINT_RELATION_GLOSS_ZH.get(token) or CLASH_GLOSS_ZH.get(token)
    return f"{token}（{gloss}）" if gloss else token


def _fmt_number(text: str) -> str:
    """Compact a raw numeric token for display without changing its meaning."""

    try:
        value = float(text)
    except ValueError:
        return text
    if value == 0:
        return "0"
    if value != 0 and (abs(value) >= 1e6 or abs(value) < 1e-3):
        return f"{value:.6g}"
    return f"{value:g}"


_MISMATCH_RE = re.compile(
    r"^(?P<family>point_relation|face_point_relation|clash_check)_(?P<rest>.+)$"
)
_DISTANCE_RE = re.compile(
    r"^distance_check_(?P<id>.+)_(?P<side>above_max|below_min) "
    r"actual=(?P<actual>\S+) (?P<bound>max|min)=(?P<value>\S+)$"
)
_PLANE_EXTREME_RE = re.compile(
    r"^plane_extreme_(?P<id>.+)_not_expected actual=(?P<actual>\S+) expected=(?P<expected>\S+) tol=(?P<tol>\S+)$"
)
_BODY_COUNT_RE = re.compile(
    r"^result_body_count_(?P<side>above_max|below_min) actual=(?P<actual>\S+) (?P<bound>max|min)=(?P<value>\S+)$"
)
_REASON_SUFFIXES = {
    "exception": "执行异常",
    "role_unavailable": "角色不可用",
    "body_unavailable": "几何体不可用",
    "null_return": "接口返回为空",
    "calculation_failed": "计算失败",
    "bbox_for_probe_plane_unavailable": "探测平面包围盒不可用",
}


def _translate_mismatch(family: str, rest: str) -> str | None:
    """Translate ``<id>_mismatch expected=X actual=Y`` style failures."""

    if rest.endswith("_mismatch"):
        check_id = rest[: -len("_mismatch")]
        return None  # handled by the caller with expected/actual fields
    marker = "_mismatch expected="
    if marker not in rest:
        return None
    check_id, values = rest.split(marker, 1)
    if " actual=" not in values:
        return None
    expected, actual = values.split(" actual=", 1)
    if family == "clash_check":
        if check_id.endswith("_clash"):
            check_id = check_id[: -len("_clash")]
        return (
            f"干涉校验 {check_id} 不一致：期望 {gloss_value(expected)}，实际 {gloss_value(actual)}"
        )
    label = "点关系校验" if family == "point_relation" else "面点关系校验"
    return f"{label} {check_id} 不一致：期望 {gloss_value(expected)}，实际 {gloss_value(actual)}"


def translate_oracle_failure(failure: str) -> str:
    """Translate one deterministic oracle failure string to natural Chinese.

    Unknown formats pass through unchanged; translation is purely cosmetic
    and never alters the recorded raw token.
    """

    text = str(failure or "").strip()
    if not text:
        return ""
    distance = _DISTANCE_RE.match(text)
    if distance:
        actual = _fmt_number(distance.group("actual"))
        bound = _fmt_number(distance.group("value"))
        if distance.group("side") == "above_max":
            return f"距离校验 {distance.group('id')} 超出上限：实际 {actual}，上限 {bound}"
        return f"距离校验 {distance.group('id')} 低于下限：实际 {actual}，下限 {bound}"
    plane = _PLANE_EXTREME_RE.match(text)
    if plane:
        return (
            f"平面极值校验 {plane.group('id')} 与预期不符：实际 {_fmt_number(plane.group('actual'))}，"
            f"期望 {_fmt_number(plane.group('expected'))}，容差 {_fmt_number(plane.group('tol'))}"
        )
    body_count = _BODY_COUNT_RE.match(text)
    if body_count:
        actual = _fmt_number(body_count.group("actual"))
        bound = _fmt_number(body_count.group("value"))
        if body_count.group("side") == "above_max":
            return f"结果体数量超出上限：实际 {actual}，上限 {bound}"
        return f"结果体数量低于下限：实际 {actual}，下限 {bound}"
    mismatch = _MISMATCH_RE.match(text)
    if mismatch:
        translated = _translate_mismatch(mismatch.group("family"), mismatch.group("rest"))
        if translated:
            return translated
    for prefix in ("point_relation_", "face_point_relation_", "distance_check_", "clash_check_", "plane_extreme_"):
        if text.startswith(prefix):
            rest = text[len(prefix):]
            for suffix, reason_zh in _REASON_SUFFIXES.items():
                marker = f"_{suffix}"
                if rest.endswith(marker):
                    check_id = rest[: -len(marker)]
                    kind_zh = {
                        "point_relation_": "点关系校验",
                        "face_point_relation_": "面点关系校验",
                        "distance_check_": "距离校验",
                        "clash_check_": "干涉校验",
                        "plane_extreme_": "平面极值校验",
                    }[prefix]
                    return f"{kind_zh} {check_id} {reason_zh}"
    return text


def translate_reasons(reasons: list[str]) -> list[str]:
    """Map triage reason tokens to Chinese; unknown tokens pass through."""

    return [TRIAGE_REASON_ZH.get(reason, reason) for reason in reasons]


def signature_kind_label(kind: str) -> str:
    return SIGNATURE_KIND_ZH.get(kind, kind)


def phase_label(phase: str) -> str:
    return PHASE_ZH.get(phase, phase)


def fault_module_label(module: str) -> str:
    return FAULT_MODULE_LABEL_ZH.get(module, module)
