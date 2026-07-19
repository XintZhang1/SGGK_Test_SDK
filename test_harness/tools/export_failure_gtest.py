"""Export a showcased failure case as a drop-in google-test repro TU.

For each failed case in the failure showcase, generate ``<case_id>_repro.cpp``:
a self-contained google-test translation unit that rebuilds the exact recipe
inputs with SGGK API calls (mirroring ``sggk_case_runner.cpp``'s Make*
builders), invokes the API under test with the recipe's exact options, and
re-runs the failed oracle checks as EXPECT_* assertions so kernel developers
can compile and debug the case inside the SGGK source tree.

When the deterministic module attribution points at a tooling/transport
module and the geometry invariants passed, the generator emits the 裁剪版:
the construction chain is commented out, inputs are loaded from the copied
.sgt files instead, and only the suspect oracle step remains active.

Everything here is deterministic host-side codegen from the recorded recipe
and validation artifacts — no model output is consulted, and the generated
file itself is diagnostic evidence, never a verdict.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

TOOLING_OR_TRANSPORT_MODULES = {
    "distance_oracle",
    "point_relation_oracle",
    "clash_oracle",
    "plane_extreme_oracle",
    "step_import",
    "step_export",
}

SKIP_APIS = {
    "check_sgt": "该接口只读取 .sgt 做检查，复现价值低，请直接用 runner 重跑",
    "step_import": "STEP 导入复现需要原始 .step 文件（showcase 不携带），请人工移植",
    "iges_import": "IGES 导入复现需要原始 .iges 文件（showcase 不携带），请人工移植",
    "step_roundtrip": "STEP 往返复现需要原始 .step 文件（showcase 不携带），请人工移植",
    "iges_roundtrip": "IGES 往返复现需要原始 .iges 文件（showcase 不携带），请人工移植",
    "api_offset2d": "api_offset2d 为 2D 偏移通道，构造差异较大，请人工移植",
}

PRIMITIVE_PARAMS = {
    "solid_cylinder": ("api_make_solid_cylinder", ("radius", "height", "angle", "create_seam_edge")),
    "solid_wedge": ("api_make_solid_wedge", ("length", "width", "height")),
    "solid_sphere": ("api_make_solid_sphere", ("radius", "create_seam_edge")),
    "solid_cone": ("api_make_solid_cone", ("bottom_radius", "top_radius", "height", "angle", "create_seam_edge")),
    "solid_torus": ("api_make_solid_torus", ("long_radius", "short_radius", "angle", "create_seam_edge")),
}

SPEC_DEFAULTS: dict[str, Any] = {
    "kind": "",
    "boolean_type": "SUBTRACTION",
    "radius": 100.0,
    "height": 100.0,
    "angle": 6.283185307179586,
    "create_seam_edge": True,
    "length": 100.0,
    "width": 100.0,
    "bottom_radius": 100.0,
    "top_radius": 50.0,
    "inner_radius": 50.0,
    "outer_radius": 100.0,
    "long_radius": 150.0,
    "short_radius": 30.0,
    "profile_radius": 25.0,
    "path_radius": 150.0,
    "secondary_height": 150.0,
    "secondary_translate_x": 0.0,
    "secondary_translate_y": 0.0,
    "secondary_translate_z": 0.0,
    "min_dist": -10.0,
    "max_dist": 20.0,
    "operation_tol": None,  # None → sggk::Precision::DefModelingTol
    "g1_tol": 0.1,
    "allow_partial_success": True,
    "translate_x": 0.0,
    "translate_y": 0.0,
    "translate_z": 0.0,
    "scale": 1.0,
    "source_file": "",
    "body_index": 0,
}

BOOLEAN_TYPE_ENUM = {
    "UNION": "sggk::BooleanType::UNION",
    "UNITE": "sggk::BooleanType::UNION",
    "INTERSECTION": "sggk::BooleanType::INTERSECTION",
    "INTERSECT": "sggk::BooleanType::INTERSECTION",
    "SUBTRACTION": "sggk::BooleanType::SUBTRACTION",
    "SUBTRACT": "sggk::BooleanType::SUBTRACTION",
}

POINT_RELATION_ENUM = {
    "Unknown": "sggk::BodyPtRelType::Unknown",
    "OnVertex": "sggk::BodyPtRelType::OnVertex",
    "OnEdge": "sggk::BodyPtRelType::OnEdge",
    "OnFace": "sggk::BodyPtRelType::OnFace",
    "Inside": "sggk::BodyPtRelType::Inside",
    "Outside": "sggk::BodyPtRelType::Outside",
}

CLASH_MODE_ENUM = {
    "ClashExistenceOnly": "sggk::ClashMode::ClashExistenceOnly",
    "ClashClassify": "sggk::ClashMode::ClashClassify",
    "ClashClassifySubEntities": "sggk::ClashMode::ClashClassifySubEntities",
}

INCLUDE_BLOCK = """\
#include <gtest/gtest.h>

// SGGK SDK 头文件（与 test_harness/src/sggk_case_runner.cpp 的口径一致；可按需删减）
#include <Boolean/API.h>
#include <GeomBase/BndBox.h>
#include <GeomBase/BndBox2D.h>
#include <GeomBase/Matrix4.h>
#include <GeomBase/Point2D.h>
#include <Geometry/3D/Curve/BoundedCurve3D.h>
#include <Geometry/3D/Curve/Circle3D.h>
#include <Geometry/3D/Surface/BSplineSurface.h>
#include <Geometry/3D/Surface/Surface.h>
#include <ModelAnalysis/API.h>
#include <ModelingBase/API.h>
#include <ModelingPrim/API.h>
#include <Offset/API.h>
#include <Topology/Brep/Body.h>
#include <Topology/Serialize/RapidTopoJsonDeserializer.h>
#include <Topology/Tools/PtBodyRelation.h>
#include <Topology/Tools/TopoBuilder.h>
#include <Foundation/init.h>

#include <memory>
#include <string>
#include <vector>"""


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _fmt(value: float) -> str:
    """Deterministic round-trip literal for a C++ double."""

    result = repr(float(value))
    return result if ("." in result or "e" in result or "inf" in result) else result + ".0"


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _safe_test_name(case_id: str) -> str:
    parts = [part for part in re.split(r"[^0-9A-Za-z]+", case_id) if part]
    name = "".join(part[:1].upper() + part[1:] for part in parts)
    if not name or name[0].isdigit():
        name = "Case" + name
    return name


def _spec_from_recipe(recipe: dict[str, Any], prefix: str) -> dict[str, Any]:
    spec = dict(SPEC_DEFAULTS)
    for key in spec:
        raw = recipe.get(f"{prefix}_{key}")
        if raw is None:
            continue
        if key == "kind":
            spec[key] = _str(raw) or spec[key]
        elif key in {"create_seam_edge", "allow_partial_success"}:
            if isinstance(raw, bool):
                spec[key] = raw
        elif key == "source_file":
            spec[key] = _str(raw)
        elif key == "body_index":
            if isinstance(raw, int) and not isinstance(raw, bool):
                spec[key] = raw
        elif key == "boolean_type":
            spec[key] = _str(raw) or spec[key]
        else:
            number = _num(raw)
            if number is not None:
                spec[key] = number
    return spec


def _emit_transform(lines: list[str], var: str, spec: dict[str, Any]) -> None:
    scale = _num(spec.get("scale")) or 1.0
    if abs(scale - 1.0) > 1e-15:
        lines.append(f"{var}->Transform(sggk::Matrix4::MakeScale({_fmt(scale)}));")
    tx, ty, tz = (_num(spec.get(key)) or 0.0 for key in ("translate_x", "translate_y", "translate_z"))
    if tx or ty or tz:
        lines.append(f"{var}->Transform(sggk::Matrix4::MakeTranslation({_fmt(tx)}, {_fmt(ty)}, {_fmt(tz)}));")


def _emit_primitive(lines: list[str], role: str, kind: str, spec: dict[str, Any], out_var: str) -> None:
    api, params = PRIMITIVE_PARAMS[kind]
    args = ["sggk::Ucs3D()"]
    for param in params:
        value = spec[param]
        args.append(_bool(bool(value)) if isinstance(value, bool) else _fmt(float(value)))
    ret = f"{role}PrimRet"
    lines.append(f"sggk::PrimitivesRetPtr {ret} = sggk::{api}({', '.join(args)});")
    lines.append(f'ASSERT_TRUE({ret} && {ret}->Succeeded()) << "{role}（{kind}）构造失败";')
    lines.append(f"sggk::BodyPtr {out_var} = {ret}->ResultBody();")
    lines.append(f'ASSERT_TRUE(static_cast<bool>({out_var})) << "{role} 构造结果为空体";')
    _emit_transform(lines, out_var, spec)


def _emit_plane_sheet(lines: list[str], role: str, spec: dict[str, Any], out_var: str) -> None:
    half_l = 0.5 * float(spec["length"])
    half_w = 0.5 * float(spec["width"])
    lines.append(
        f"auto {role}Plane = std::make_shared<sggk::Plane>(sggk::Point3D(0.0, 0.0, 0.0), sggk::Dir3D(0.0, 0.0, 1.0));"
    )
    lines.append(
        f"auto {role}Face = sggk::api_create_face({role}Plane, sggk::UVRange("
        f"sggk::Interval({_fmt(-half_l)}, {_fmt(half_l)}), sggk::Interval({_fmt(-half_w)}, {_fmt(half_w)})));"
    )
    lines.append(f"sggk::BodyPtr {out_var} = sggk::api_topo_to_body({role}Face);")
    lines.append(f'ASSERT_TRUE(static_cast<bool>({out_var})) << "{role}（plane_sheet）构造失败";')
    _emit_transform(lines, out_var, spec)


def _emit_extrude_rect(lines: list[str], role: str, spec: dict[str, Any], out_var: str) -> None:
    half_l = 0.5 * float(spec["length"])
    half_w = 0.5 * float(spec["width"])
    height = float(spec["height"])
    lines.append(
        f"const sggk::BndBox2D {role}Box(sggk::Point2D({_fmt(-half_l)}, {_fmt(-half_w)}), "
        f"sggk::Point2D({_fmt(half_l)}, {_fmt(half_w)}));"
    )
    lines.append(f"auto {role}Sheet = sggk::api_create_rect_sheet_body(sggk::Plane(), {role}Box);")
    lines.append(f'ASSERT_TRUE(static_cast<bool>({role}Sheet)) << "{role}（extrude_rect）矩形面片构造失败";')
    lines.append(
        f"auto {role}Ret = sggk::api_extrude_entity({role}Sheet, sggk::Dir3D::UnitZ, 0.0, {_fmt(height)}, true);"
    )
    lines.append(f'ASSERT_TRUE({role}Ret && {role}Ret->Succeeded()) << "{role}（extrude_rect）拉伸失败";')
    lines.append(f'ASSERT_TRUE(!{role}Ret->ResultBodies().empty()) << "{role}（extrude_rect）没有结果体";')
    lines.append(f"sggk::BodyPtr {out_var} = {role}Ret->ResultBodies().front();")
    _emit_transform(lines, out_var, spec)


def _emit_rect_sheet(lines: list[str], role: str, spec: dict[str, Any], sheet_var: str) -> None:
    half_l = 0.5 * float(spec["length"])
    half_w = 0.5 * float(spec["width"])
    lines.append(
        f"const sggk::BndBox2D {role}Box(sggk::Point2D({_fmt(-half_l)}, {_fmt(-half_w)}), "
        f"sggk::Point2D({_fmt(half_l)}, {_fmt(half_w)}));"
    )
    lines.append(f"auto {sheet_var} = sggk::api_create_rect_sheet_body(sggk::Plane(), {role}Box);")
    lines.append(f'ASSERT_TRUE(static_cast<bool>({sheet_var})) << "{role} 矩形面片构造失败";')


def _emit_thicken_rect_sheet(lines: list[str], role: str, spec: dict[str, Any], out_var: str) -> None:
    _emit_rect_sheet(lines, role, spec, f"{role}Sheet")
    op_tol = spec.get("operation_tol")
    lines.append(f"sggk::ThickenOpts {role}ThickenOpts;")
    if op_tol is not None:
        lines.append(f"{role}ThickenOpts.SetModelingTol({_fmt(float(op_tol))});")
    lines.append(f"{role}ThickenOpts.SetCheckValid(true);")
    lines.append(f"{role}ThickenOpts.SetToTopoTrack(false);")
    lines.append(f"{role}ThickenOpts.SetNearTangentAngle({_fmt(float(spec['g1_tol']))});")
    lines.append(f"{role}ThickenOpts.SetAllowPartialSuccess({_bool(bool(spec['allow_partial_success']))});")
    lines.append(
        f"auto {role}Ret = sggk::api_thicken_body({role}Sheet, {_fmt(float(spec['min_dist']))}, "
        f"{_fmt(float(spec['max_dist']))}, {role}ThickenOpts);"
    )
    lines.append(f'ASSERT_TRUE({role}Ret && {role}Ret->Succeeded()) << "{role}（thicken_rect_sheet）加厚失败";')
    lines.append(f'ASSERT_TRUE(!{role}Ret->ResultBodies().empty()) << "{role}（thicken_rect_sheet）没有结果体";')
    lines.append(f"sggk::BodyPtr {out_var} = {role}Ret->ResultBodies().front();")
    _emit_transform(lines, out_var, spec)


def _emit_sweep_circle_line(lines: list[str], role: str, spec: dict[str, Any], out_var: str) -> None:
    radius = float(spec["profile_radius"])
    height = float(spec["height"])
    lines.append(f"sggk::Circle3D {role}Circle(sggk::Ucs3D(), {_fmt(radius)});")
    lines.append(f"auto {role}ProfileEdge = sggk::TopoBuilder::MakeEdge({role}Circle);")
    lines.append(f"auto {role}ProfileCoedge = sggk::TopoBuilder::MakeCoedge({role}ProfileEdge, true);")
    lines.append(
        f"auto {role}ProfileWire = sggk::TopoBuilder::MakeWire({{ {role}ProfileCoedge }}, sggk::WireType::Closed);"
    )
    lines.append(f"auto {role}StartVertex = sggk::TopoBuilder::MakeVertex(sggk::Point3D(0.0, 0.0, 0.0));")
    lines.append(
        f"auto {role}EndVertex = sggk::TopoBuilder::MakeVertex(sggk::Point3D(0.0, 0.0, {_fmt(height)}));"
    )
    lines.append(
        f"auto {role}PathCoedge = sggk::TopoBuilder::MakeCoedge("
        f"sggk::TopoBuilder::MakeLinearEdge({role}StartVertex, {role}EndVertex), true);"
    )
    lines.append(f"auto {role}PathWire = sggk::TopoBuilder::MakeWire({{ {role}PathCoedge }}, sggk::WireType::Open);")
    lines.append(f"sggk::SweepOpts {role}SweepOpts;")
    lines.append(f"{role}SweepOpts.SetSweepMode(sggk::SweepMode::Normal);")
    lines.append(f"{role}SweepOpts.SetG1Tol({_fmt(float(spec['g1_tol']))});")
    if spec.get("operation_tol") is not None:
        lines.append(f"{role}SweepOpts.SetModelingTol({_fmt(float(spec['operation_tol']))});")
    lines.append(f"{role}SweepOpts.SetRelocateProfile(true);")
    lines.append(f"auto {role}Ret = sggk::api_sweep_entity({role}ProfileWire, {role}PathWire, {role}SweepOpts);")
    lines.append(f'ASSERT_TRUE({role}Ret && {role}Ret->Succeeded()) << "{role}（sweep_circle_line）扫掠失败";')
    lines.append(f'ASSERT_TRUE(!{role}Ret->ResultBodies().empty()) << "{role}（sweep_circle_line）没有结果体";')
    lines.append(f"sggk::BodyPtr {out_var} = {role}Ret->ResultBodies().front();")
    _emit_transform(lines, out_var, spec)


def _emit_support_sweep(lines: list[str], role: str, spec: dict[str, Any], out_var: str) -> None:
    """Faithful port of MakeSupportSweepBSplineSurfaceBody."""

    path_radius = float(spec["path_radius"])
    profile_radius = float(spec["profile_radius"])
    height = float(spec["height"])
    xy = f"{_fmt(path_radius)} / 45.53"
    zz = f"{_fmt(height)} / 20.0"
    lines.append(f"const double {role}XyScale = {xy};")
    lines.append(f"const double {role}ZScale = {zz};")
    ctrl = [
        (-45.53, -0.52), (-34.78, 8.52), (-11.68, 21.38), (14.93, -1.33), (32.74, 0.76), (41.49, 2.54),
    ]
    lines.append(f"sggk::Point3DMatrix {role}Ctrl(6);")
    for index, (x, y) in enumerate(ctrl):
        lines.append(
            f"{role}Ctrl[{index}].push_back(sggk::Point3D({_fmt(x)} * {role}XyScale, {_fmt(y)} * {role}XyScale, "
            f"0.0));"
        )
        lines.append(
            f"{role}Ctrl[{index}].push_back(sggk::Point3D({_fmt(x)} * {role}XyScale, {_fmt(y)} * {role}XyScale, "
            f"20.0 * {role}ZScale));"
        )
    lines.append(f"sggk::RealArray {role}KnotU{{0, 0.42, 0.73, 1}};")
    lines.append(f"sggk::UIntArray {role}MultU{{4, 1, 1, 4}};")
    lines.append(f"sggk::RealArray {role}KnotV{{0, 1}};")
    lines.append(f"sggk::UIntArray {role}MultV{{2, 2}};")
    lines.append(
        f"sggk::BSplineSurfacePtr {role}Surface(new sggk::BSplineSurface(3, 1, {role}Ctrl, {role}KnotU, "
        f"{role}KnotV, {role}MultU, {role}MultV));"
    )
    lines.append(
        f"auto {role}SupportFace = sggk::api_create_face({role}Surface, "
        f"sggk::UVRange({role}Surface->DomainU(), {role}Surface->DomainV()));"
    )
    lines.append(f'ASSERT_TRUE(static_cast<bool>({role}SupportFace)) << "{role} 支撑面构造失败";')
    lines.append(f"auto {role}SupportBody = sggk::TopoBuilder::MakeBody({role}SupportFace);")
    lines.append(f'ASSERT_TRUE(static_cast<bool>({role}SupportBody)) << "{role} 支撑体构造失败";')
    lines.append(
        f"const auto {role}PathCurve = std::dynamic_pointer_cast<sggk::BoundedCurve3D>("
        f"{role}Surface->CalcUCurve(0.5));"
    )
    lines.append(f'ASSERT_TRUE(static_cast<bool>({role}PathCurve)) << "{role} 支撑路径构造失败";')
    lines.append(f"auto {role}PathEdge = sggk::TopoBuilder::MakeEdge(*{role}PathCurve);")
    lines.append(f"auto {role}PathCoedge = sggk::TopoBuilder::MakeCoedge({role}PathEdge, true);")
    lines.append(
        f"auto {role}PathWire = sggk::TopoBuilder::MakeWire({{{role}PathCoedge}}, sggk::WireType::Open);"
    )
    lines.append(f"const auto {role}PathStart = {role}PathCurve->CalcStart();")
    lines.append(f"sggk::Dir3D {role}ProfileNormal({role}Ctrl[1][0] - {role}Ctrl[0][0]);")
    lines.append("try")
    lines.append("{")
    lines.append(f"    {role}ProfileNormal = sggk::Dir3D(")
    lines.append(f"        {role}PathCurve->CalcDeriv1({role}PathCurve->Domain().Min()));")
    lines.append("}")
    lines.append("catch (...) {}")
    lines.append(
        f"sggk::Circle3D {role}ProfileCircle(sggk::Ucs3D({role}PathStart, {role}ProfileNormal), "
        f"{_fmt(profile_radius)});"
    )
    lines.append(f"auto {role}ProfileEdge = sggk::TopoBuilder::MakeEdge({role}ProfileCircle);")
    lines.append(f"auto {role}ProfileCoedge = sggk::TopoBuilder::MakeCoedge({role}ProfileEdge, true);")
    lines.append(
        f"auto {role}ProfileWire = sggk::TopoBuilder::MakeWire({{{role}ProfileCoedge}}, sggk::WireType::Closed);"
    )
    lines.append(
        f"sggk::SurfacePtr {role}ProfileSurface = std::make_shared<sggk::Plane>({role}PathStart, "
        f"{role}ProfileNormal);"
    )
    op_tol = spec.get("operation_tol")
    tol_expr = _fmt(float(op_tol)) if op_tol is not None else "sggk::Precision::DefModelingTol"
    lines.append(
        f"auto {role}ProfileFace = sggk::api_create_face({role}ProfileWire, {role}ProfileSurface, "
        f"{tol_expr}, true);"
    )
    lines.append(f'ASSERT_TRUE(static_cast<bool>({role}ProfileFace)) << "{role} 轮廓面构造失败";')
    lines.append(f"sggk::SweepOpts {role}SweepOpts(true);")
    lines.append(f"{role}SweepOpts.SetSweepMode(sggk::SweepMode::SupportFace);")
    lines.append(f"{role}SweepOpts.SetSupportBody({role}SupportBody);")
    lines.append(f"{role}SweepOpts.SetSolid(true);")
    lines.append(f"{role}SweepOpts.SetG1Tol({_fmt(float(spec['g1_tol']))});")
    if op_tol is not None:
        lines.append(f"{role}SweepOpts.SetModelingTol({_fmt(float(op_tol))});")
    lines.append(f"{role}SweepOpts.SetRelocateProfile(true);")
    lines.append(f"auto {role}Ret = sggk::api_sweep_entity({role}ProfileFace, {role}PathWire, {role}SweepOpts);")
    lines.append(
        f'ASSERT_TRUE({role}Ret && {role}Ret->Succeeded()) << "{role}（support_sweep_bspline_surface）扫掠失败";'
    )
    lines.append(f'ASSERT_TRUE(!{role}Ret->ResultBodies().empty()) << "{role} 没有结果体";')
    lines.append(f"sggk::BodyPtr {out_var} = {role}Ret->ResultBodies().front();")
    _emit_transform(lines, out_var, spec)


def _emit_revolve_line(lines: list[str], role: str, spec: dict[str, Any], out_var: str) -> None:
    bottom = float(spec["bottom_radius"])
    top = float(spec["top_radius"])
    height = float(spec["height"])
    angle = float(spec["angle"])
    lines.append(
        f"auto {role}StartVertex = sggk::TopoBuilder::MakeVertex(sggk::Point3D({_fmt(bottom)}, 0.0, "
        f"{_fmt(-0.5 * height)}));"
    )
    lines.append(
        f"auto {role}EndVertex = sggk::TopoBuilder::MakeVertex(sggk::Point3D({_fmt(top)}, 0.0, "
        f"{_fmt(0.5 * height)}));"
    )
    lines.append(
        f"auto {role}ProfileEdge = sggk::TopoBuilder::MakeLinearEdge({role}StartVertex, {role}EndVertex);"
    )
    lines.append(f"sggk::Axis1 {role}Axis(sggk::Point3D(0.0, 0.0, 0.0), sggk::Dir3D::UnitZ);")
    lines.append(f"sggk::RevolveOpts {role}RevolveOpts;")
    if spec.get("operation_tol") is not None:
        lines.append(f"{role}RevolveOpts.SetModelingTol({_fmt(float(spec['operation_tol']))});")
    lines.append(f"{role}RevolveOpts.SetCheckValid(true);")
    lines.append(f"{role}RevolveOpts.SetToTopoTrack(false);")
    lines.append(
        f"auto {role}Ret = sggk::api_revolve_entity({role}ProfileEdge, {role}Axis, {_fmt(angle)}, "
        f"{role}RevolveOpts);"
    )
    lines.append(f'ASSERT_TRUE({role}Ret && {role}Ret->Succeeded()) << "{role}（revolve_line）旋转失败";')
    lines.append(f'ASSERT_TRUE(!{role}Ret->ResultBodies().empty()) << "{role}（revolve_line）没有结果体";')
    lines.append(f"sggk::BodyPtr {out_var} = {role}Ret->ResultBodies().front();")
    _emit_transform(lines, out_var, spec)


def _emit_revolve_rect(lines: list[str], role: str, spec: dict[str, Any], out_var: str) -> None:
    inner = float(spec["inner_radius"])
    outer = float(spec["outer_radius"])
    height = float(spec["height"])
    angle = float(spec["angle"])
    points = [
        ("V0", inner, -0.5 * height),
        ("V1", outer, -0.5 * height),
        ("V2", outer, 0.5 * height),
        ("V3", inner, 0.5 * height),
    ]
    for name, radius, z in points:
        lines.append(
            f"auto {role}{name} = sggk::TopoBuilder::MakeVertex(sggk::Point3D({_fmt(radius)}, 0.0, {_fmt(z)}));"
        )
    edges = [("E0", "V0", "V1"), ("E1", "V1", "V2"), ("E2", "V2", "V3"), ("E3", "V3", "V0")]
    for name, start, end in edges:
        lines.append(f"auto {role}{name} = sggk::TopoBuilder::MakeLinearEdge({role}{start}, {role}{end});")
    coedges = ", ".join(f"sggk::TopoBuilder::MakeCoedge({role}{name}, true)" for name, _, _ in edges)
    lines.append(
        f"auto {role}ProfileWire = sggk::TopoBuilder::MakeWire({{{coedges}}}, sggk::WireType::Closed);"
    )
    lines.append(
        f"sggk::SurfacePtr {role}ProfileSurface = std::make_shared<sggk::Plane>(sggk::Point3D(0.0, 0.0, 0.0), "
        "sggk::Dir3D(0.0, 1.0, 0.0));"
    )
    op_tol = spec.get("operation_tol")
    tol_expr = _fmt(float(op_tol)) if op_tol is not None else "sggk::Precision::DefModelingTol"
    lines.append(
        f"auto {role}ProfileFace = sggk::api_create_face({role}ProfileWire, {role}ProfileSurface, "
        f"{tol_expr}, true);"
    )
    lines.append(f'ASSERT_TRUE(static_cast<bool>({role}ProfileFace)) << "{role}（revolve_rect）轮廓面构造失败";')
    lines.append(f"sggk::Axis1 {role}Axis(sggk::Point3D(0.0, 0.0, 0.0), sggk::Dir3D::UnitZ);")
    lines.append(f"sggk::RevolveOpts {role}RevolveOpts;")
    if op_tol is not None:
        lines.append(f"{role}RevolveOpts.SetModelingTol({_fmt(float(op_tol))});")
    lines.append(f"{role}RevolveOpts.SetCheckValid(true);")
    lines.append(f"{role}RevolveOpts.SetToTopoTrack(false);")
    lines.append(
        f"auto {role}Ret = sggk::api_revolve_entity({role}ProfileFace, {role}Axis, {_fmt(angle)}, "
        f"{role}RevolveOpts);"
    )
    lines.append(f'ASSERT_TRUE({role}Ret && {role}Ret->Succeeded()) << "{role}（revolve_rect）旋转失败";')
    lines.append(f'ASSERT_TRUE(!{role}Ret->ResultBodies().empty()) << "{role}（revolve_rect）没有结果体";')
    lines.append(f"sggk::BodyPtr {out_var} = {role}Ret->ResultBodies().front();")
    _emit_transform(lines, out_var, spec)


def _emit_loaded_sgt(lines: list[str], role: str, spec: dict[str, Any], out_var: str) -> None:
    source = Path(_str(spec.get("source_file"))).name or f"{role}.sgt"
    index = spec.get("body_index") or 0
    lines.append(f"sggk::RapidTopoJsonDeserializer {role}Deserializer;")
    lines.append(f"auto {role}Loaded = {role}Deserializer.DeserializeBodiesFromFile(\"input/{source}\");")
    lines.append(f"if ({role}Loaded.empty())")
    lines.append("{")
    lines.append(
        f"    auto {role}Single = {role}Deserializer.DeserializeBodyFromFile(\"input/{source}\");"
    )
    lines.append(f"    if ({role}Single) {role}Loaded.push_back({role}Single);")
    lines.append("}")
    lines.append(f'ASSERT_TRUE({role}Loaded.size() > {index}u) << "{role} 载入 input/{source} 失败";')
    lines.append(f"sggk::BodyPtr {out_var} = {role}Loaded[{index}];")
    _emit_transform(lines, out_var, spec)


def _emit_pre_boolean(lines: list[str], role: str, spec: dict[str, Any], out_var: str) -> None:
    base = dict(spec)
    base["kind"] = "solid_cylinder"
    lines.append(f"// {role} 预置体：圆柱 minus 楔形（pre_boolean_cylinder_wedge）")
    _emit_primitive(lines, f"{role}PreTarget", "solid_cylinder", base, f"{role}PreTarget")
    cutter = dict(spec)
    cutter["kind"] = "solid_wedge"
    cutter["height"] = spec.get("secondary_height")
    cutter["translate_x"] = spec.get("secondary_translate_x")
    cutter["translate_y"] = spec.get("secondary_translate_y")
    cutter["translate_z"] = spec.get("secondary_translate_z")
    _emit_primitive(lines, f"{role}PreTool", "solid_wedge", cutter, f"{role}PreTool")
    boolean_enum = BOOLEAN_TYPE_ENUM.get(_str(spec.get("boolean_type")).upper(), "sggk::BooleanType::SUBTRACTION")
    lines.append(f"sggk::BooleanOpts {role}BooleanOpts({boolean_enum});")
    if spec.get("operation_tol") is not None:
        lines.append(f"{role}BooleanOpts.SetModelingTol({_fmt(float(spec['operation_tol']))});")
    lines.append(f"{role}BooleanOpts.SetCheckValid(true);")
    lines.append(f"{role}BooleanOpts.SetToTopoTrack(false);")
    lines.append(f"{role}BooleanOpts.SetNonDestructive(true);")
    lines.append(
        f"auto {role}Ret = sggk::api_boolean({role}PreTarget, {role}PreTool, {role}BooleanOpts);"
    )
    lines.append(f'ASSERT_TRUE({role}Ret && {role}Ret->Succeeded()) << "{role} 预置布尔失败";')
    lines.append(f'ASSERT_TRUE(!{role}Ret->ResultBodies().empty()) << "{role} 预置布尔没有结果体";')
    lines.append(f"sggk::BodyPtr {out_var} = {role}Ret->ResultBodies().front();")
    _emit_transform(lines, out_var, spec)


def _emit_body(lines: list[str], role: str, spec: dict[str, Any]) -> None:
    kind = _str(spec.get("kind"))
    lines.append(f"// ---------- {role}（{kind or '未指定'}） ----------")
    out_var = role
    if kind in PRIMITIVE_PARAMS:
        _emit_primitive(lines, role, kind, spec, out_var)
    elif kind == "plane_sheet":
        _emit_plane_sheet(lines, role, spec, out_var)
    elif kind == "extrude_rect":
        _emit_extrude_rect(lines, role, spec, out_var)
    elif kind == "thicken_rect_sheet":
        _emit_thicken_rect_sheet(lines, role, spec, out_var)
    elif kind == "sweep_circle_line":
        _emit_sweep_circle_line(lines, role, spec, out_var)
    elif kind == "support_sweep_bspline_surface":
        _emit_support_sweep(lines, role, spec, out_var)
    elif kind == "revolve_line":
        _emit_revolve_line(lines, role, spec, out_var)
    elif kind == "revolve_rect":
        _emit_revolve_rect(lines, role, spec, out_var)
    elif kind == "pre_boolean_cylinder_wedge":
        _emit_pre_boolean(lines, role, spec, out_var)
    elif kind == "loaded_sgt":
        _emit_loaded_sgt(lines, role, spec, out_var)
    else:
        lines.append(f'GTEST_SKIP() << "{role} 的构造类型 {kind or "（空）"} 暂不支持自动复现，请人工移植";')


def _emit_boolean_opts(lines: list[str], recipe: dict[str, Any], var: str = "opts") -> None:
    boolean_type = _str(recipe.get("boolean_type")).upper()
    enum = BOOLEAN_TYPE_ENUM.get(boolean_type, "sggk::BooleanType::SUBTRACTION")
    lines.append(f"sggk::BooleanOpts {var}({enum});")
    modeling_tol = _num(recipe.get("modeling_tol"))
    if modeling_tol is not None:
        lines.append(f"{var}.SetModelingTol({_fmt(modeling_tol)});")
    check_valid = recipe.get("check_valid")
    if isinstance(check_valid, bool):
        lines.append(f"{var}.SetCheckValid({_bool(check_valid)});")
    non_destructive = recipe.get("non_destructive")
    if isinstance(non_destructive, bool):
        lines.append(f"{var}.SetNonDestructive({_bool(non_destructive)});")
    lines.append(f"{var}.SetToTopoTrack(false);")


def _emit_api_call(lines: list[str], recipe: dict[str, Any], roles: list[str]) -> str:
    """Emit the API-under-test call; returns the result expression kind."""

    api = _str(recipe.get("api"))
    lines.append(f"// 被测接口：{api}（参数与失败 recipe 完全一致）")
    if api in {"api_boolean", "api_boolean_split", "api_boolean_slice"}:
        _emit_boolean_opts(lines, recipe)
        lines.append(f"auto ret = sggk::{api}(target, tool, opts);")
        lines.append('ASSERT_TRUE(ret) << "被测接口返回空指针";')
        return "modeling_ret"
    if api == "api_thicken_body":
        min_dist = _num(recipe.get("min_dist")) or 0.0
        max_dist = _num(recipe.get("max_dist")) or 0.0
        lines.append(f"auto ret = sggk::api_thicken_body(target, {_fmt(min_dist)}, {_fmt(max_dist)});")
        lines.append('ASSERT_TRUE(ret) << "被测接口返回空指针";')
        return "modeling_ret"
    if api == "api_combine_bodies":
        clone = recipe.get("combine_clone")
        clone_expr = _bool(clone) if isinstance(clone, bool) else "true"
        lines.append("sggk::BodyList combineInputs;")
        lines.append("combineInputs.push_back(target);")
        lines.append("combineInputs.push_back(tool);")
        lines.append(f"sggk::BodyPtr result = sggk::api_combine_bodies(combineInputs, {clone_expr});")
        return "body_ptr"
    if api == "api_offset_body":
        distance = _num(recipe.get("offset_distance")) or 0.0
        lines.append("sggk::OffsetOpts opts;")
        modeling_tol = _num(recipe.get("modeling_tol"))
        if modeling_tol is not None:
            lines.append(f"opts.SetModelingTol({_fmt(modeling_tol)});")
        lines.append("opts.SetCheckValid(true);")
        lines.append("opts.SetToTopoTrack(false);")
        lines.append(f"auto ret = sggk::api_offset_body(source, {_fmt(distance)}, opts);")
        lines.append('ASSERT_TRUE(ret) << "被测接口返回空指针";')
        return "modeling_ret"
    if api == "api_topology_section":
        lines.append("sggk::BooleanOpts opts;")
        modeling_tol = _num(recipe.get("modeling_tol"))
        if modeling_tol is not None:
            lines.append(f"opts.SetModelingTol({_fmt(modeling_tol)});")
        lines.append("opts.SetCheckValid(true);")
        lines.append("opts.SetToTopoTrack(false);")
        lines.append("auto ret = sggk::api_topology_section(target, tool, opts);")
        lines.append('ASSERT_TRUE(ret) << "被测接口返回空指针";')
        return "modeling_ret"
    reason = SKIP_APIS.get(api, f"接口 {api} 暂不支持自动复现，请人工移植")
    lines.append(f'GTEST_SKIP() << "{reason}";')
    return "skipped"


def _emit_result_binding(lines: list[str], result_kind: str) -> None:
    if result_kind == "modeling_ret":
        lines.append("std::vector<sggk::BodyPtr> resultBodies;")
        lines.append("for (const auto& item : ret->ResultBodies())")
        lines.append("{")
        lines.append("    resultBodies.push_back(item);")
        lines.append("}")
    elif result_kind == "body_ptr":
        lines.append("std::vector<sggk::BodyPtr> resultBodies;")
        lines.append("if (result)")
        lines.append("{")
        lines.append("    resultBodies.push_back(result);")
        lines.append("}")


def _emit_success_check(lines: list[str], recipe: dict[str, Any], result_kind: str) -> None:
    if result_kind == "modeling_ret":
        lines.append('EXPECT_TRUE(ret->Succeeded()) << "被测接口报告执行失败";')
    elif result_kind == "body_ptr":
        lines.append('EXPECT_TRUE(static_cast<bool>(result)) << "被测接口返回空结果体";')
    expectations = _dict(recipe.get("expectations"))
    result_bodies = _dict(expectations.get("result_bodies"))
    minimum = result_bodies.get("min")
    maximum = result_bodies.get("max")
    if isinstance(minimum, int) and not isinstance(minimum, bool):
        lines.append(f"EXPECT_GE(resultBodies.size(), {minimum}u) << \"结果体数量少于下限 {minimum}\";")
    if isinstance(maximum, int) and not isinstance(maximum, bool):
        lines.append(f"EXPECT_LE(resultBodies.size(), {maximum}u) << \"结果体数量超出上限 {maximum}\";")


def _failed_records(validation: dict[str, Any], family: str) -> list[dict[str, Any]]:
    return [
        record
        for record in _list(validation.get(family))
        if isinstance(record, dict) and record.get("ok") is False
    ]


def _role_expr(role: str, index: int) -> str:
    return f'selectBodies("{role}", {index}u)'


def _emit_oracle_checks(lines: list[str], validation: dict[str, Any]) -> int:
    """Emit EXPECT_* blocks for every failed oracle check; returns the count."""

    count = 0
    families = (
        ("point_relations", "点关系校验"),
        ("face_point_relations", "面点关系校验"),
        ("distance_checks", "距离校验"),
        ("clash_checks", "干涉校验"),
        ("plane_extreme_checks", "平面极值校验"),
    )
    for family, label in families:
        for record in _failed_records(validation, family):
            count += 1
            check_id = _str(record.get("id")) or f"{family}_{count}"
            lines.append(f"// {label} {check_id}（该校验在失败用例中未通过，以下为模型预期复核）")
            lines.append("{")
            if family in {"point_relations", "face_point_relations"}:
                point = record.get("point")
                coords = [float(item) for item in point] if isinstance(point, list) and len(point) == 3 else None
                expected = _str(record.get("expected"))
                enum = POINT_RELATION_ENUM.get(expected)
                tolerance = _num(record.get("tolerance")) or 1e-3
                check_boundary = bool(record.get("check_boundary"))
                role = _str(record.get("role")) or "result"
                index = record.get("body_index") if isinstance(record.get("body_index"), int) else 0
                if coords is None or enum is None:
                    lines.append(f'    GTEST_SKIP() << "{label} {check_id} 的记录不完整，无法自动复现";')
                else:
                    lines.append(
                        f"    sggk::PtBodyRelation evaluator({_role_expr(role, index)});"
                    )
                    lines.append(
                        f"    const auto info = evaluator.Execute(sggk::Point3D({_fmt(coords[0])}, "
                        f"{_fmt(coords[1])}, {_fmt(coords[2])}), sggk::Toler({_fmt(tolerance)}), "
                        f"{_bool(check_boundary)});"
                    )
                    lines.append(
                        f'    EXPECT_EQ(info.relation, {enum}) << "{label} {check_id}";'
                    )
            elif family == "distance_checks":
                role_a = _str(record.get("role_a")) or "target"
                role_b = _str(record.get("role_b")) or "tool"
                index_a = record.get("body_index_a") if isinstance(record.get("body_index_a"), int) else 0
                index_b = record.get("body_index_b") if isinstance(record.get("body_index_b"), int) else 0
                kind = _str(record.get("kind")) or "minimum"
                threshold = _num(record.get("threshold")) or 0.0
                expectation = _dict(record.get("expectation"))
                api = "api_topo_maximum_distance" if kind == "maximum" else "api_topo_minimum_distance"
                args = f"{_role_expr(role_a, index_a)}, {_role_expr(role_b, index_b)}"
                if kind == "minimum" and threshold > 0.0:
                    args += f", {_fmt(threshold)}"
                lines.append(f"    auto distRet = sggk::{api}({args});")
                lines.append('    ASSERT_TRUE(distRet && distRet->IsSuccess()) << "距离计算失败";')
                abs_tol = _num(expectation.get("abs_tol")) or 0.0
                if expectation.get("min_set") and _num(expectation.get("min")) is not None:
                    bound = float(expectation["min"]) - abs_tol
                    lines.append(
                        f'    EXPECT_GE(distRet->Dist(), {_fmt(bound)}) << "{label} {check_id} 低于下限";'
                    )
                if expectation.get("max_set") and _num(expectation.get("max")) is not None:
                    bound = float(expectation["max"]) + abs_tol
                    lines.append(
                        f'    EXPECT_LE(distRet->Dist(), {_fmt(bound)}) << "{label} {check_id} 超出上限";'
                    )
            elif family == "clash_checks":
                role_a = _str(record.get("role_a")) or "target"
                role_b = _str(record.get("role_b")) or "tool"
                index_a = record.get("body_index_a") if isinstance(record.get("body_index_a"), int) else 0
                index_b = record.get("body_index_b") if isinstance(record.get("body_index_b"), int) else 0
                mode = CLASH_MODE_ENUM.get(_str(record.get("mode")), "sggk::ClashMode::ClashClassify")
                tolerance = _num(record.get("tolerance"))
                tol_expr = _fmt(tolerance) if tolerance is not None else "-1.0"
                expected = _str(record.get("expected"))
                lines.append(
                    f"    auto clashRet = sggk::api_body_clash({_role_expr(role_a, index_a)}, "
                    f"{_role_expr(role_b, index_b)}, sggk::ClashOpts({mode}, {tol_expr}));"
                )
                lines.append('    ASSERT_TRUE(clashRet) << "干涉计算返回空指针";')
                if expected == "AnyClash":
                    lines.append(
                        f'    EXPECT_NE(clashRet->GetClashType(), sggk::ClashType::Clash_None) << '
                        f'"{label} {check_id}：期望存在干涉";'
                    )
                else:
                    lines.append(
                        f'    EXPECT_EQ(clashRet->GetClashType(), sggk::ClashType::Clash_None) << '
                        f'"{label} {check_id}：期望无干涉";'
                    )
            elif family == "plane_extreme_checks":
                role = _str(record.get("role")) or "result"
                index = record.get("body_index") if isinstance(record.get("body_index"), int) else 0
                axis = _str(record.get("axis")).lower()
                side = _str(record.get("side")).lower()
                expected = _num(record.get("expected"))
                tolerance = _num(record.get("tolerance")) or 1e-3
                if axis not in {"x", "y", "z"} or side not in {"min", "max"} or expected is None:
                    lines.append(f'    GTEST_SKIP() << "{label} {check_id} 的记录不完整，无法自动复现";')
                else:
                    accessor = "MaxPoint" if side == "max" else "MinPoint"
                    lines.append(
                        f"    const auto checkBox = {_role_expr(role, index)}->CalcBndBox(true);"
                    )
                    lines.append('    ASSERT_FALSE(checkBox.IsEmpty()) << "结果包围盒为空";')
                    lines.append(
                        f'    EXPECT_NEAR(checkBox.{accessor}().{axis.upper()}(), {_fmt(expected)}, '
                        f'{_fmt(tolerance)}) << "{label} {check_id}";'
                    )
            lines.append("}")
    return count


def _emit_sgt_load(lines: list[str], var: str, sgt_rel: str, deserializer: str) -> None:
    lines.append(f"sggk::BodyPtr {var};")
    lines.append("{")
    lines.append(f"    auto loaded = {deserializer}.DeserializeBodiesFromFile(\"{sgt_rel}\");")
    lines.append("    if (loaded.empty())")
    lines.append("    {")
    lines.append(f"        auto single = {deserializer}.DeserializeBodyFromFile(\"{sgt_rel}\");")
    lines.append("        if (single) loaded.push_back(single);")
    lines.append("    }")
    lines.append(f'    ASSERT_FALSE(loaded.empty()) << "载入 {sgt_rel} 失败";')
    lines.append("    " + var + " = loaded.front();")
    lines.append("}")


def generate_repro_cpp(
    showcase_case_dir: Path,
    *,
    source_label: str = "",
    pre_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate the google-test repro for one showcased case capsule.

    ``showcase_case_dir`` must contain ``input/recipe.json`` plus the copied
    ``report/*.json``; the returned dict carries ``cpp`` (full TU text),
    ``file_name``, ``simplified`` and human-readable ``notes``.
    """

    showcase_case_dir = Path(showcase_case_dir)
    recipe = _dict(_load(showcase_case_dir / "input" / "recipe.json"))
    validation = _dict(_load(showcase_case_dir / "report" / "validation.json"))
    topo_check = _dict(_load(showcase_case_dir / "report" / "topo_check.json"))
    case_id = _str(recipe.get("case_id")) or showcase_case_dir.name
    api = _str(recipe.get("api"))
    analysis = pre_analysis if isinstance(pre_analysis, dict) else _dict(
        _load(showcase_case_dir / "pre_analysis.json")
    )
    fault_module = _str(analysis.get("fault_module")) or "unclassified"
    failures = [_str(item) for item in _list(validation.get("failures")) if _str(item)]

    geometry_ok = True
    for key in ("bodies", "topologies"):
        for entry in _list(topo_check.get(key)):
            if isinstance(entry, dict) and entry.get("ok") is False:
                geometry_ok = False
    simplified = fault_module in TOOLING_OR_TRANSPORT_MODULES and geometry_ok

    import oracle_text_zh

    summary = "；".join(oracle_text_zh.translate_oracle_failure(item) for item in failures[:3]) or "（无校验失败记录）"
    module_label = oracle_text_zh.FAULT_MODULE_LABEL_ZH.get(fault_module, fault_module)

    header = [
        "// ======================================================================",
        "// SGGK 失败用例复现（google-test 翻译单元，由测试底座自动生成）",
        f"// 用例：{case_id}",
        f"// 接口：{api}（boolean_type={_str(recipe.get('boolean_type')) or '—'}，"
        f"modeling_tol={recipe.get('modeling_tol') if recipe.get('modeling_tol') is not None else '—'}）",
    ]
    if source_label:
        header.append(f"// 来源：{source_label}")
    header += [
        f"// 失败摘要：{summary}",
        f"// 归因模块（诊断性）：{module_label}（{fault_module}）",
        "// 声明：本文件为诊断性证据，不构成 SDK 缺陷定论；所有数值均取自失败用例的",
        "// recipe 与校验记录，自动生成过程不参考任何模型输出。",
        "// 使用：将本文件放入 SGGK 源码树的 google-test 测试目录（如 tests/），保证",
        "// 头文件搜索路径指向 SGK 的 include/ 根，链接与被测工程一致的 SGGK 库即可。",
        "// 输入 .sgt 的相对路径（input/、output/）与本文件在证据目录中的布局一致；",
        "// 拷出时请连同 input/、output/ 目录一起复制。",
        "// ======================================================================",
        INCLUDE_BLOCK,
        "",
        "",
        "// SDK 进程级初始化（幂等；若宿主工程已有全局初始化可删除此 guard）",
        "struct SggkReproSessionGuard",
        "{",
        "    SggkReproSessionGuard() { sggk::init(nullptr, 16); }",
        "    ~SggkReproSessionGuard() { sggk::fini(); }",
        "};",
        "",
        "static SggkReproSessionGuard& ReproSessionGuard()",
        "{",
        "    static SggkReproSessionGuard guard;",
        "    return guard;",
        "}",
        "",
        f"TEST(SggkFailureRepro, {_safe_test_name(case_id)})",
        "{",
        "    (void)ReproSessionGuard();",
        "",
    ]

    construction: list[str] = []
    construction.append(
        "// ======================= 输入构造（人工复核区，可按分割线整体删减） ======================="
    )
    roles: list[tuple[str, dict[str, Any]]] = []
    if api == "api_offset_body":
        roles.append(("source", _spec_from_recipe(recipe, "source")))
    else:
        roles.append(("target", _spec_from_recipe(recipe, "target")))
        if api not in {"api_thicken_body"}:
            roles.append(("tool", _spec_from_recipe(recipe, "tool")))
    for role, spec in roles:
        _emit_body(construction, role, spec)
    construction.append("")

    body: list[str] = []
    input_dir = showcase_case_dir / "input"
    output_dir = showcase_case_dir / "output"
    input_sgts = sorted(path.name for path in input_dir.glob("*.sgt")) if input_dir.is_dir() else []
    output_sgts = sorted(path.name for path in output_dir.glob("*.sgt")) if output_dir.is_dir() else []
    role_vars = [role for role, _ in roles]

    if simplified:
        # 裁剪版：构造链整段注释掉，输入/输出改从证据目录的 .sgt 直接载入。
        body.append(
            "// ======================= 输入构造（已裁剪，见下方说明） ======================="
        )
        body.append("// （裁剪说明：几何结果与 Parasolid 一致，仅保留可疑工具环节）")
        body.append("// 以下原始构造链整段保留为注释，便于人工核对；如需完整复现，取消注释即可。")
        for line in construction:
            body.append("// " + line if line else "//")
        body.append("")
        body.append("// ---------- 从证据目录载入输入几何（showcase 相对路径） ----------")
        body.append("sggk::RapidTopoJsonDeserializer inputDeserializer;")
        role_sgt = {"target": "input/target.sgt", "tool": "input/tool.sgt", "source": "input/source.sgt"}
        for role in role_vars:
            sgt = role_sgt.get(role)
            if sgt and Path(sgt).name in input_sgts:
                _emit_sgt_load(body, role, sgt, "inputDeserializer")
            elif input_sgts:
                fallback = f"input/{input_sgts[0]}"
                _emit_sgt_load(body, role, fallback, "inputDeserializer")
            else:
                body.append('GTEST_SKIP() << "证据目录缺少输入 .sgt，无法裁剪复现";')
        body.append("")
        body.append(
            "// ======================= 被测接口调用（已裁剪：直接载入当时的输出） ======================="
        )
        body.append("std::vector<sggk::BodyPtr> resultBodies;")
        if output_sgts:
            body.append("sggk::RapidTopoJsonDeserializer outputDeserializer;")
            for name in output_sgts:
                body.append("{")
                body.append(f"    auto loaded = outputDeserializer.DeserializeBodiesFromFile(\"output/{name}\");")
                body.append("    if (loaded.empty())")
                body.append("    {")
                body.append(
                    f"        auto single = outputDeserializer.DeserializeBodyFromFile(\"output/{name}\");"
                )
                body.append("        if (single) loaded.push_back(single);")
                body.append("    }")
                body.append("    for (const auto& item : loaded) resultBodies.push_back(item);")
                body.append("}")
        else:
            body.append('GTEST_SKIP() << "证据目录缺少输出 .sgt，无法复核可疑环节";')
        body.append("")
        result_kind = "loaded"
    else:
        body.extend(construction)
        api_lines: list[str] = []
        api_lines.append(
            "// ======================= 被测接口调用 ======================="
        )
        result_kind = _emit_api_call(api_lines, recipe, role_vars)
        body.extend(api_lines)
        body.append("")
        if result_kind != "skipped":
            binding: list[str] = []
            _emit_result_binding(binding, result_kind)
            body.extend(binding)
            body.append("")

    check_lines: list[str] = []
    check_lines.append(
        "// ======================= 校验（EXPECT_*） ======================="
    )
    if result_kind == "loaded":
        check_lines.append('ASSERT_FALSE(resultBodies.empty()) << "未能从证据目录载入结果体";')
    elif result_kind != "skipped":
        _emit_success_check(check_lines, recipe, result_kind)
    if result_kind != "skipped":
        check_lines.append("// 逐角色取几何体（result 取接口输出/证据输出，其余取输入构造）")
        check_lines.append("auto selectBodies = [&](const std::string& role, size_t index) -> sggk::BodyPtr {")
        if "target" in role_vars or "source" in role_vars:
            first = role_vars[0]
            check_lines.append(f'    if (role == "{first}") return {first};')
        if "tool" in role_vars:
            check_lines.append('    if (role == "tool") return tool;')
        check_lines.append("    if (index < resultBodies.size()) return resultBodies[index];")
        check_lines.append("    return resultBodies.empty() ? nullptr : resultBodies.front();")
        check_lines.append("};")
        emitted = _emit_oracle_checks(check_lines, validation)
        if not emitted:
            check_lines.append("// （本用例没有可自动复核的失败校验项；请结合失败摘要人工核对）")
    body.extend(check_lines)
    body.append("}")

    cpp = "\n".join(header + body) + "\n"
    notes = []
    if simplified:
        notes.append(f"裁剪版：归因模块为 {module_label}，构造链已注释，输入从 .sgt 载入")
    return {
        "case_id": case_id,
        "api": api,
        "file_name": f"{case_id}_repro.cpp",
        "simplified": simplified,
        "fault_module": fault_module,
        "cpp": cpp,
        "notes": notes,
    }


def export_case_repro(
    showcase_case_dir: Path,
    *,
    source_label: str = "",
    pre_analysis: dict[str, Any] | None = None,
) -> str:
    """Write ``<case_id>_repro.cpp`` next to reproduce.ps1; returns the file name."""

    result = generate_repro_cpp(
        showcase_case_dir,
        source_label=source_label,
        pre_analysis=pre_analysis,
    )
    out_path = Path(showcase_case_dir) / str(result["file_name"])
    out_path.write_text(str(result["cpp"]), encoding="utf-8", newline="\n")
    return str(result["file_name"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", required=True, help="Showcase case capsule directory")
    parser.add_argument("--out", default="", help="Optional output path (default: <case_dir>/<case_id>_repro.cpp)")
    parser.add_argument("--source-label", default="", help="Free-text origin label for the header comment")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    case_dir = Path(args.case_dir).expanduser()
    if not case_dir.is_absolute():
        case_dir = (REPO_ROOT / case_dir).resolve()
    if not (case_dir / "input" / "recipe.json").is_file():
        print(f"用例目录缺少 input/recipe.json：{case_dir}")
        return 2
    result = generate_repro_cpp(case_dir, source_label=str(args.source_label))
    out_path = Path(args.out).expanduser() if args.out else case_dir / str(result["file_name"])
    if not out_path.is_absolute():
        out_path = (REPO_ROOT / out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(str(result["cpp"]), encoding="utf-8", newline="\n")
    mode = "裁剪版" if result["simplified"] else "完整版"
    print(f"已生成 google-test 复现源文件（{mode}）：{out_path}")
    for note in result["notes"]:
        print(f"  说明：{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
