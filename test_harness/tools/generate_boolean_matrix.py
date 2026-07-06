#!/usr/bin/env python3
"""Generate OCC-style flat boolean attack recipes for the SGGK harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from validate_recipe import validate_recipe


TOPO_TOL = 1e-2
GEOM_TOL = 1e-5
MAX_MODEL_SIZE = 5e5
TAU = math.tau


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Directory for generated flat recipe JSON files")
    parser.add_argument(
        "--preset",
        choices=["smoke", "standard", "stress"],
        default="smoke",
        help="Generation breadth",
    )
    parser.add_argument("--case-prefix", default="matrix", help="Prefix for generated case_id values")
    parser.add_argument("--limit", type=int, default=0, help="Maximum recipes to write; 0 means all")
    parser.add_argument("--topo-track", action="store_true", help="Enable SDK topo tracking in generated recipes")
    parser.add_argument("--no-validate", action="store_true", help="Skip recipe validation before writing")
    parser.add_argument("--manifest", default="", help="Optional manifest path; defaults to a sibling <out>_manifest.json")
    return parser.parse_args()


def sanitize_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_").lower()
    return text or "case"


def body(kind: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": kind}
    for key, value in fields.items():
        if value is not None:
            result[key] = value
    return result


def cylinder(radius: float, height: float, *, x: float = 0.0, y: float = 0.0, z: float = 0.0, angle: float = TAU) -> dict[str, Any]:
    return body("solid_cylinder", radius=radius, height=height, angle=angle, translate_x=x, translate_y=y, translate_z=z)


def wedge(length: float, width: float, height: float, *, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict[str, Any]:
    return body("solid_wedge", length=length, width=width, height=height, translate_x=x, translate_y=y, translate_z=z)


def sphere(radius: float, *, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict[str, Any]:
    return body("solid_sphere", radius=radius, translate_x=x, translate_y=y, translate_z=z)


def cone(
    bottom_radius: float,
    top_radius: float,
    height: float,
    *,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    angle: float = TAU,
) -> dict[str, Any]:
    return body(
        "solid_cone",
        bottom_radius=bottom_radius,
        top_radius=top_radius,
        height=height,
        angle=angle,
        translate_x=x,
        translate_y=y,
        translate_z=z,
    )


def torus(long_radius: float, short_radius: float, *, x: float = 0.0, y: float = 0.0, z: float = 0.0, angle: float = TAU) -> dict[str, Any]:
    return body(
        "solid_torus",
        long_radius=long_radius,
        short_radius=short_radius,
        angle=angle,
        translate_x=x,
        translate_y=y,
        translate_z=z,
    )


def extrude_rect(length: float, width: float, height: float, *, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict[str, Any]:
    return body("extrude_rect", length=length, width=width, height=height, operation_tol=TOPO_TOL, translate_x=x, translate_y=y, translate_z=z)


def sweep_circle_line(profile_radius: float, height: float, *, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict[str, Any]:
    return body(
        "sweep_circle_line",
        profile_radius=profile_radius,
        height=height,
        operation_tol=TOPO_TOL,
        g1_tol=0.1,
        translate_x=x,
        translate_y=y,
        translate_z=z,
    )


def revolve_line(
    bottom_radius: float,
    top_radius: float,
    height: float,
    *,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    angle: float = TAU,
) -> dict[str, Any]:
    return body(
        "revolve_line",
        bottom_radius=bottom_radius,
        top_radius=top_radius,
        height=height,
        angle=angle,
        operation_tol=TOPO_TOL,
        translate_x=x,
        translate_y=y,
        translate_z=z,
    )


def revolve_rect(
    inner_radius: float,
    outer_radius: float,
    height: float,
    *,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    angle: float = TAU,
) -> dict[str, Any]:
    return body(
        "revolve_rect",
        inner_radius=inner_radius,
        outer_radius=outer_radius,
        height=height,
        angle=angle,
        operation_tol=TOPO_TOL,
        translate_x=x,
        translate_y=y,
        translate_z=z,
    )


def preboolean_cylinder_wedge(*, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict[str, Any]:
    return body(
        "pre_boolean_cylinder_wedge",
        boolean_type="SUBTRACTION",
        radius=180.0,
        height=360.0,
        angle=TAU,
        length=140.0,
        width=240.0,
        secondary_height=200.0,
        secondary_translate_x=40.0,
        secondary_translate_y=0.0,
        secondary_translate_z=0.0,
        operation_tol=TOPO_TOL,
        translate_x=x,
        translate_y=y,
        translate_z=z,
    )


def flatten_body(prefix: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{key}": value for key, value in spec.items()}


def base_expectations(allow_empty: bool = False) -> dict[str, Any]:
    return {
        "result_bodies": {"min": 0 if allow_empty else 1},
        "require_property_calculations": True,
        "require_finite_properties": True,
        "require_nonnegative_length_area": True,
        "require_nonnegative_volume": False,
        "boolean_volume_relation": True,
        "boolean_bbox_relation": True,
        "volume_relation_abs_tol": TOPO_TOL,
        "volume_relation_rel_tol": 1e-8,
    }


def make_recipe(
    *,
    case_id: str,
    boolean_type: str,
    target: dict[str, Any],
    tool: dict[str, Any],
    hypothesis: str,
    family: str,
    variant: str,
    topo_track: bool,
    allow_empty: bool = False,
) -> dict[str, Any]:
    recipe: dict[str, Any] = {
        "case_id": case_id,
        "api": "api_boolean",
        "hypothesis": hypothesis,
        "family": family,
        "variant": variant,
        "boolean_type": boolean_type,
        "modeling_tol": TOPO_TOL,
        "check_valid": True,
        "topo_track": topo_track,
        "non_destructive": True,
        "expectations": base_expectations(allow_empty=allow_empty),
    }
    recipe.update(flatten_body("target", target))
    recipe.update(flatten_body("tool", tool))
    return recipe


def offset_values() -> list[tuple[str, float]]:
    return [
        ("overlap_topo_tol", -TOPO_TOL),
        ("overlap_geom_tol", -GEOM_TOL),
        ("exact", 0.0),
        ("gap_geom_tol", GEOM_TOL),
        ("gap_topo_tol", TOPO_TOL),
    ]


def boolean_types(preset: str) -> list[str]:
    if preset == "smoke":
        return ["SUBTRACTION"]
    return ["SUBTRACTION", "INTERSECTION", "UNION"]


def add_tolerance_cylinder_cylinder(cases: list[dict[str, Any]], prefix: str, preset: str, topo_track: bool) -> None:
    target = cylinder(250.0, 700.0)
    for bool_type in boolean_types(preset):
        for suffix, delta in offset_values():
            tool = cylinder(75.0, 760.0, x=325.0 + delta, z=-30.0)
            variant = f"{bool_type.lower()}_{suffix}"
            cases.append(
                make_recipe(
                    case_id=sanitize_id(f"{prefix}_cyl_cyl_tangent_{variant}"),
                    boolean_type=bool_type,
                    target=target,
                    tool=tool,
                    family="cylinder_cylinder_tangent",
                    variant=variant,
                    hypothesis="Parallel cylinders near exact tangency sweep exact contact plus +/- geometry and topology tolerances.",
                    topo_track=topo_track,
                    allow_empty=bool_type == "INTERSECTION" and delta > -TOPO_TOL,
                )
            )


def add_extrude_side_cases(cases: list[dict[str, Any]], prefix: str, preset: str, topo_track: bool) -> None:
    target = extrude_rect(500.0, 260.0, 220.0)
    bools = ["SUBTRACTION"] if preset == "smoke" else ["SUBTRACTION", "INTERSECTION"]
    for bool_type in bools:
        for suffix, delta in offset_values():
            tool = cylinder(40.0, 300.0, x=290.0 + delta, z=-40.0)
            variant = f"{bool_type.lower()}_{suffix}"
            cases.append(
                make_recipe(
                    case_id=sanitize_id(f"{prefix}_extrude_side_cutter_{variant}"),
                    boolean_type=bool_type,
                    target=target,
                    tool=tool,
                    family="extrude_side_cutter",
                    variant=variant,
                    hypothesis="Generated extrusion side face is attacked by a cylindrical cutter across +/- 1e-5 and +/- 1e-2.",
                    topo_track=topo_track,
                    allow_empty=bool_type == "INTERSECTION" and delta > -TOPO_TOL,
                )
            )


def add_preboolean_recut_cases(cases: list[dict[str, Any]], prefix: str, preset: str, topo_track: bool) -> None:
    target = preboolean_cylinder_wedge()
    bools = ["SUBTRACTION"] if preset == "smoke" else ["SUBTRACTION", "INTERSECTION"]
    for bool_type in bools:
        for suffix, delta in offset_values():
            tool = cylinder(45.0, 420.0, x=70.0 + delta, z=-30.0)
            variant = f"{bool_type.lower()}_{suffix}"
            cases.append(
                make_recipe(
                    case_id=sanitize_id(f"{prefix}_preboolean_recut_{variant}"),
                    boolean_type=bool_type,
                    target=target,
                    tool=tool,
                    family="preboolean_recut",
                    variant=variant,
                    hypothesis="A body produced by an earlier boolean is cut again near the original generated split region.",
                    topo_track=topo_track,
                    allow_empty=bool_type == "INTERSECTION",
                )
            )


def add_large_coordinate_cases(cases: list[dict[str, Any]], prefix: str, preset: str, topo_track: bool) -> None:
    target = cylinder(30000.0, 70000.0, x=300000.0, y=-100000.0)
    bools = ["SUBTRACTION"] if preset == "smoke" else ["SUBTRACTION", "INTERSECTION"]
    for bool_type in bools:
        for suffix, delta in offset_values():
            tool = cylinder(5000.0, 76000.0, x=335000.0 + delta, y=-100000.0, z=-3000.0)
            variant = f"{bool_type.lower()}_{suffix}"
            cases.append(
                make_recipe(
                    case_id=sanitize_id(f"{prefix}_large_cyl_cyl_{variant}"),
                    boolean_type=bool_type,
                    target=target,
                    tool=tool,
                    family="large_coordinate_cylinder_cylinder",
                    variant=variant,
                    hypothesis=f"Near-tangent cylinders at large coordinates below max modeling size {MAX_MODEL_SIZE:g}.",
                    topo_track=topo_track,
                    allow_empty=bool_type == "INTERSECTION" and delta > -TOPO_TOL,
                )
            )


def add_revolve_side_cases(cases: list[dict[str, Any]], prefix: str, preset: str, topo_track: bool) -> None:
    target = revolve_line(90.0, 70.0, 260.0)
    bools = ["SUBTRACTION"] if preset == "smoke" else ["SUBTRACTION", "INTERSECTION"]
    for bool_type in bools:
        for suffix, delta in offset_values():
            tool = cylinder(20.0, 320.0, x=110.0 + delta, z=-160.0)
            variant = f"{bool_type.lower()}_{suffix}"
            cases.append(
                make_recipe(
                    case_id=sanitize_id(f"{prefix}_revolve_side_cutter_{variant}"),
                    boolean_type=bool_type,
                    target=target,
                    tool=tool,
                    family="revolve_side_cutter",
                    variant=variant,
                    hypothesis="Generated revolve side topology is attacked by a cylindrical cutter across +/- geometry and topology tolerances.",
                    topo_track=topo_track,
                    allow_empty=bool_type == "INTERSECTION" and delta > -TOPO_TOL,
                )
            )


def add_revolve_rect_side_cases(cases: list[dict[str, Any]], prefix: str, preset: str, topo_track: bool) -> None:
    target = revolve_rect(60.0, 90.0, 120.0)
    bools = ["SUBTRACTION"] if preset == "smoke" else ["SUBTRACTION", "INTERSECTION"]
    for bool_type in bools:
        for suffix, delta in offset_values():
            tool = cylinder(15.0, 160.0, x=105.0 + delta, z=-80.0)
            variant = f"{bool_type.lower()}_{suffix}"
            cases.append(
                make_recipe(
                    case_id=sanitize_id(f"{prefix}_revolve_rect_side_cutter_{variant}"),
                    boolean_type=bool_type,
                    target=target,
                    tool=tool,
                    family="revolve_rect_side_cutter",
                    variant=variant,
                    hypothesis="Closed-profile revolved solid side topology is attacked by a cylindrical cutter across +/- geometry and topology tolerances.",
                    topo_track=topo_track,
                    allow_empty=bool_type == "INTERSECTION" and delta > -TOPO_TOL,
                )
            )


def add_oscillating_near_tangent_cases(cases: list[dict[str, Any]], prefix: str, preset: str, topo_track: bool) -> None:
    if preset == "smoke":
        phases = [0.0]
        bools = ["SUBTRACTION", "INTERSECTION"]
    elif preset == "standard":
        phases = [0.0, math.pi / 6.0, math.pi / 3.0]
        bools = ["SUBTRACTION", "INTERSECTION"]
    else:
        phases = [0.0, math.pi / 12.0, math.pi / 6.0, math.pi / 4.0, math.pi / 3.0]
        bools = ["SUBTRACTION", "INTERSECTION", "UNION"]

    target_radius = 90.0
    tool_radius = 35.0
    target = sweep_circle_line(target_radius, 360.0)
    for phase_index, phase in enumerate(phases):
        for bool_type in bools:
            for suffix, delta in offset_values():
                center_distance = target_radius + tool_radius + delta
                tool = sweep_circle_line(
                    tool_radius,
                    420.0,
                    x=center_distance * math.cos(phase),
                    y=center_distance * math.sin(phase),
                    z=-30.0,
                )
                variant = f"phase_{phase_index:02d}_{bool_type.lower()}_{suffix}"
                cases.append(
                    make_recipe(
                        case_id=sanitize_id(f"{prefix}_sweep_sweep_oscillating_tangent_{variant}"),
                        boolean_type=bool_type,
                        target=target,
                        tool=tool,
                        family="sweep_sweep_oscillating_tangent",
                        variant=variant,
                        hypothesis=(
                            "Two sweep-generated cylindrical bodies approach tangency from multiple XY phases, "
                            "crossing +/- geometry tolerance and +/- topology tolerance bands."
                        ),
                        topo_track=topo_track,
                        allow_empty=bool_type == "INTERSECTION" and delta > -TOPO_TOL,
                    )
                )


def add_primitive_pair_cases(cases: list[dict[str, Any]], prefix: str, preset: str, topo_track: bool) -> None:
    if preset == "smoke":
        return
    pairs = [
        ("sphere_cylinder_overlap", sphere(160.0), cylinder(70.0, 360.0, x=90.0, z=-80.0)),
        ("cone_cylinder_slant", cone(150.0, 30.0, 320.0), cylinder(55.0, 360.0, x=85.0, z=-20.0)),
        ("torus_cylinder_tube", torus(170.0, 35.0), cylinder(40.0, 240.0, x=170.0, z=-120.0)),
        ("wedge_sweep_generated", wedge(300.0, 220.0, 260.0), sweep_circle_line(35.0, 320.0, x=70.0, y=20.0, z=-30.0)),
    ]
    for name, target, tool in pairs:
        for bool_type in boolean_types(preset):
            cases.append(
                make_recipe(
                    case_id=sanitize_id(f"{prefix}_{name}_{bool_type.lower()}"),
                    boolean_type=bool_type,
                    target=target,
                    tool=tool,
                    family=name,
                    variant=bool_type.lower(),
                    hypothesis="Primitive and generated-body boolean matrix case for broad regression coverage.",
                    topo_track=topo_track,
                    allow_empty=bool_type == "INTERSECTION",
                )
            )


def add_stress_scale_cases(cases: list[dict[str, Any]], prefix: str, preset: str, topo_track: bool) -> None:
    if preset != "stress":
        return
    scales = [0.1, 1.0, 10.0]
    for scale in scales:
        target = cylinder(120.0 * scale, 500.0 * scale)
        tool = wedge(140.0 * scale, 210.0 * scale, 160.0 * scale, x=40.0 * scale, z=20.0 * scale)
        for bool_type in boolean_types(preset):
            cases.append(
                make_recipe(
                    case_id=sanitize_id(f"{prefix}_scale_{scale:g}_cyl_wedge_{bool_type.lower()}"),
                    boolean_type=bool_type,
                    target=target,
                    tool=tool,
                    family="scale_cylinder_wedge",
                    variant=f"scale_{scale:g}_{bool_type.lower()}",
                    hypothesis="Scaled primitive boolean checks tolerance behavior across object-size bands.",
                    topo_track=topo_track,
                    allow_empty=bool_type == "INTERSECTION",
                )
            )


def generate_cases(prefix: str, preset: str, topo_track: bool) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    add_tolerance_cylinder_cylinder(cases, prefix, preset, topo_track)
    add_extrude_side_cases(cases, prefix, preset, topo_track)
    add_preboolean_recut_cases(cases, prefix, preset, topo_track)
    add_large_coordinate_cases(cases, prefix, preset, topo_track)
    add_revolve_side_cases(cases, prefix, preset, topo_track)
    add_revolve_rect_side_cases(cases, prefix, preset, topo_track)
    add_oscillating_near_tangent_cases(cases, prefix, preset, topo_track)
    add_primitive_pair_cases(cases, prefix, preset, topo_track)
    add_stress_scale_cases(cases, prefix, preset, topo_track)
    return cases


def write_recipe(path: Path, recipe: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recipe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def recipe_digest(recipe: dict[str, Any]) -> str:
    payload = json.dumps(recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = generate_cases(sanitize_id(args.case_prefix), args.preset, args.topo_track)
    if args.limit > 0:
        cases = cases[: args.limit]

    failures = 0
    written: list[dict[str, Any]] = []
    for index, recipe in enumerate(cases):
        if not args.no_validate:
            errors = validate_recipe(recipe)
            if errors:
                failures += 1
                print(f"FAIL {recipe.get('case_id', '<unknown>')}")
                for error in errors:
                    print(f"  - {error}")
                continue
        path = out_dir / f"{recipe['case_id']}.json"
        write_recipe(path, recipe)
        written.append(
            {
                "index": index,
                "case_id": recipe["case_id"],
                "family": recipe.get("family"),
                "variant": recipe.get("variant"),
                "boolean_type": recipe.get("boolean_type"),
                "path": str(path),
                "sha1": recipe_digest(recipe),
            }
        )
        print(f"OK {path}")

    manifest_path = Path(args.manifest) if args.manifest else out_dir.with_name(f"{out_dir.name}_manifest.json")
    manifest = {
        "preset": args.preset,
        "case_prefix": sanitize_id(args.case_prefix),
        "topo_tol": TOPO_TOL,
        "geom_tol": GEOM_TOL,
        "max_model_size": MAX_MODEL_SIZE,
        "topo_track": args.topo_track,
        "generated": len(cases),
        "written": len(written),
        "validation_failures": failures,
        "recipes": written,
    }
    write_recipe(manifest_path, manifest)
    print(f"manifest={manifest_path}")
    print(f"generated={len(cases)} written={len(written)} failures={failures}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
