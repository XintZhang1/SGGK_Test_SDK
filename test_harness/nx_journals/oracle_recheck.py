"""Re-measure failed SGGK oracle checks against Parasolid in NX (data-only).

This reviewed journal accepts up to three STEP inputs (target, tool, and a
directory of exported result STEPs), a JSON check spec, and an output JSON
path.  It imports the STEPs with the exact importer configuration used by
``boolean_measure.py``, re-measures each requested oracle check with NXOpen
measurement APIs, and writes one ``sggk_nx_oracle_recheck`` report.  It does
not execute code from any input path and removes the temporary NX part and
translator logs afterwards.

Check kinds (arguments via the spec JSON):

- ``distance``: minimum distance between two bodies
  (``MeasureManager.NewDistance``).
- ``point_relation``: point vs body classification.  NX measures the distance
  to the body surface; a point within ``tolerance`` of the surface is
  classified OnEdge/OnVertex/OnFace by its nearest topology, otherwise a
  tiny probe sphere (radius < distance-to-surface) is intersected with the
  body: full intersection volume means Inside, an empty intersection means
  Outside.  Both primitives are real Parasolid measurements; an ambiguous
  ratio is reported as an error, never guessed.
- ``clash``: Parasolid Intersect boolean between the two bodies; a positive
  intersection volume means AnyClash, NX's "completely outside" rejection or
  a negligible volume means Clash_None.  This rechecks overlap interference
  only; exact tangency semantics may differ from the SGGK clash oracle.
- ``plane_extreme``: extreme point of a body along a direction
  (``MeasureManager.NewRectangularExtreme`` with a body dumb rule).
- ``import_integrity``: per-role solid/sheet counts and the seam-aware
  free-edge counter copied from ``boolean_measure.py``.

Per-check records carry ``status`` = ``measured`` | ``unsupported`` |
``error``; nothing is ever fabricated when an API is missing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RESULT_KIND = "sggk_nx_oracle_recheck"
REQUEST_KIND = "sggk_nx_oracle_recheck_request"
RESULT_PREFIX = "SGGK_NX_ORACLE_RECHECK_JSON="
MAX_ERROR_CHARS = 2000
MAX_CHECKS = 64
CONTAINMENT_FULL_RATIO = 0.9
CONTAINMENT_EMPTY_RATIO = 0.1
FAR_DATUM_DISTANCE = 1.0e6
CHECK_KINDS = ("distance", "point_relation", "clash", "plane_extreme", "import_integrity")
ROLES = ("target", "tool", "result")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _diagnostic(code: str, severity: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "message": str(message)[:MAX_ERROR_CHARS],
    }


def _input_record(path: Path | None) -> dict[str, Any]:
    name = path.name if path is not None else ""
    sha256 = ""
    size = 0
    if path is not None and path.is_file():
        sha256 = _sha256_file(path)
        size = path.stat().st_size
    return {"name": name, "sha256": sha256, "size_bytes": size}


def _check_record(check_id: str, kind: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "kind": kind,
        "status": "error",
        "ok": False,
        "actual": None,
        "detail": "",
    }


def _empty_report() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "ok": False,
        "status": "invalid_request",
        "input": {
            "target": _input_record(None),
            "tool": _input_record(None),
            "result_files": [],
        },
        "nx": {"version": "", "full_version": "", "session_type": ""},
        "import": {},
        "checks": [],
        "diagnostics": [],
    }


def _nx_version(session: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    getter = getattr(session, "GetEnvironmentVariableValue", None)
    if callable(getter):
        for key, output_key in (("UGII_VERSION", "version"), ("UGII_FULL_VERSION", "full_version")):
            try:
                value = getter(key)
            except Exception:
                value = ""
            values[output_key] = str(value or "")
    return {
        "version": values.get("version", ""),
        "full_version": values.get("full_version", ""),
        "session_type": type(session).__name__,
    }


def _enum_value(owner: Any, *names: str) -> Any:
    for name in names:
        if hasattr(owner, name):
            return getattr(owner, name)
    raise AttributeError(f"none of the NX enum members exist: {', '.join(names)}")


def _dispose(value: Any) -> None:
    for name in ("Dispose", "Destroy"):
        method = getattr(value, name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
            return


def _find_unit(part: Any, *names: str) -> Any:
    last_error: Exception | None = None
    for name in names:
        try:
            return part.UnitCollection.FindObject(name)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"NX unit is unavailable: {'/'.join(names)}") from last_error


def _mass_units(part: Any) -> list[Any]:
    return [
        _find_unit(part, "SquareMilliMeter"),
        _find_unit(part, "CubicMilliMeter"),
        _find_unit(part, "Kilogram"),
        _find_unit(part, "MilliMeter"),
        _find_unit(part, "Newton", "MilliNewton"),
    ]


def _body_solid_flag(body: Any) -> bool | None:
    value = getattr(body, "IsSolidBody", None)
    if callable(value):
        try:
            return bool(value())
        except Exception:
            return None
    if isinstance(value, bool):
        return value
    return None


def _tag_value(body: Any) -> int:
    try:
        return int(getattr(body, "Tag"))
    except (TypeError, ValueError):
        return 0


def _free_edge_count(body: Any) -> tuple[int, int]:
    """Return (edge_count, free_edge_count).  Copied from boolean_measure.py.

    An edge shared by fewer than two faces is a boundary *candidate*.  Periodic
    analytic seams (sphere/cylinder/cone/torus) also report a single face, but
    they are isolated loops: no other candidate edge shares one of their
    vertices (a closed loop may even report none).  Only candidates connected
    to at least one other candidate through a vertex — a genuine boundary
    chain — are counted as free edges.  Edges with no adjacent face, or whose
    vertices cannot be probed, stay conservatively counted.
    """

    edge_count = 0
    candidates: list[set[str] | None] = []
    try:
        edges = list(body.GetEdges())
    except Exception:
        return 0, 0
    for edge in edges:
        edge_count += 1
        try:
            face_count = len(list(edge.GetFaces()))
        except Exception:
            face_count = 0
        if face_count >= 2:
            continue
        if face_count == 0:
            candidates.append(None)
            continue
        try:
            vertex_keys = {str(vertex) for vertex in edge.GetVertices()}
        except Exception:
            vertex_keys = None
        candidates.append(vertex_keys if vertex_keys else set())
    vertex_uses: dict[str, list[int]] = {}
    for index, vertex_keys in enumerate(candidates):
        if vertex_keys:
            for key in vertex_keys:
                vertex_uses.setdefault(key, []).append(index)
    free_edges = 0
    for index, vertex_keys in enumerate(candidates):
        if vertex_keys is None:
            free_edges += 1
            continue
        if not vertex_keys:
            continue
        connected = any(
            any(other != index for other in vertex_uses.get(key, [])) for key in vertex_keys
        )
        if connected:
            free_edges += 1
    return edge_count, free_edges


def _configure_importer(nxopen: Any, session: Any, importer: Any, work_part: Any, input_path: Path) -> None:
    """Exact mirror of boolean_measure._configure_importer."""

    importer.SimplifyGeometry = True
    importer.LayerDefault = 1
    object_types = importer.ObjectTypes
    object_types.Curves = True
    object_types.Surfaces = True
    object_types.Solids = True
    object_types.PmiData = True
    importer.InputFile = str(input_path)
    importer.OutputFile = str(work_part.FullPath)
    importer.FileOpenFlag = False
    importer.ImportToTeamcenter = False
    importer.FlattenAssembly = True
    importer.ImportTo = _enum_value(
        nxopen.Step214Importer.ImportToOption,
        "WorkPart",
        "ImportToOptionWorkPart",
    )
    if hasattr(importer, "ProcessHoldFlag"):
        importer.ProcessHoldFlag = True
    settings_root = ""
    getter = getattr(session, "GetEnvironmentVariableValue", None)
    if callable(getter):
        try:
            settings_root = str(getter("STEP214UG_DIR") or "")
        except Exception:
            settings_root = ""
    settings_path = Path(settings_root) / "step214ug.def" if settings_root else None
    if settings_path is not None and settings_path.is_file() and hasattr(importer, "SettingsFile"):
        importer.SettingsFile = str(settings_path)


def _import_step(nxopen: Any, session: Any, work_part: Any, input_path: Path) -> list[Any]:
    """Import one STEP into the work part and return the newly added bodies."""

    before = {_tag_value(body) for body in list(work_part.Bodies)}
    importer = session.DexManager.CreateStep214Importer()
    try:
        _configure_importer(nxopen, session, importer, work_part, input_path)
        importer.Commit()
    finally:
        _dispose(importer)
    return [body for body in list(work_part.Bodies) if _tag_value(body) not in before]


def _measure_distance(work_part: Any, length_unit: Any, obj_a: Any, obj_b: Any) -> float:
    measure = None
    try:
        measure = work_part.MeasureManager.NewDistance(length_unit, obj_a, obj_b)
        value = float(measure.Value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"NX returned an invalid distance: {value}")
        return value
    finally:
        if measure is not None:
            _dispose(measure)


def _body_volume(work_part: Any, mass_units: list[Any], body: Any) -> float:
    measure = None
    try:
        measure = work_part.MeasureManager.NewMassProperties(mass_units, 0.999, [body])
        volume = abs(float(measure.Volume))
        if not math.isfinite(volume):
            raise ValueError("NX returned a non-finite volume")
        return volume
    finally:
        if measure is not None:
            _dispose(measure)


def _make_sphere(work_part: Any, center: tuple[float, float, float], radius: float) -> Any:
    nxopen = sys.modules["NXOpen"]
    builder = work_part.Features.CreateSphereBuilder(nxopen.Features.Sphere.Null)
    try:
        builder.CenterPoint = work_part.Points.CreatePoint(nxopen.Point3d(*center))
        builder.Diameter.RightHandSide = repr(2.0 * radius)
        builder.Commit()
    finally:
        _dispose(builder)
    bodies = list(work_part.Bodies)
    if not bodies:
        raise RuntimeError("probe sphere creation produced no body")
    return bodies[-1]


def _intersection_volume(
    work_part: Any,
    mass_units: list[Any],
    target: Any,
    tool: Any,
) -> float:
    """Intersection volume via a retained Parasolid Intersect boolean.

    NX raises an NXException carrying "completely outside" when the tool does
    not touch the target at all; that case maps to a zero intersection.
    """

    nxopen = sys.modules["NXOpen"]
    before = {_tag_value(body) for body in list(work_part.Bodies)}
    builder = None
    try:
        builder = work_part.Features.CreateBooleanBuilder(None)
        builder.Operation = _enum_value(nxopen.Features.Feature.BooleanType, "Intersect")
        builder.Target = target
        builder.Tool = tool
        builder.RetainTarget = True
        builder.RetainTool = True
        builder.Commit()
    except Exception as exc:
        if "completely outside" in str(exc):
            return 0.0
        raise
    finally:
        if builder is not None:
            _dispose(builder)
    added = [body for body in list(work_part.Bodies) if _tag_value(body) not in before]
    return sum(_body_volume(work_part, mass_units, body) for body in added)


def _containment_ratio(
    work_part: Any,
    mass_units: list[Any],
    body: Any,
    point: tuple[float, float, float],
    radius: float,
) -> float:
    """Fraction of a small probe sphere around ``point`` lying inside ``body``."""

    probe = _make_sphere(work_part, point, radius)
    probe_volume = _body_volume(work_part, mass_units, probe)
    if probe_volume <= 0:
        raise RuntimeError("probe sphere has zero volume")
    intersection = _intersection_volume(work_part, mass_units, body, probe)
    return intersection / probe_volume


def _vertex_distance(work_part: Any, length_unit: Any, point_obj: Any, body: Any) -> float | None:
    best: float | None = None
    vertices: list[Any] = []
    getter = getattr(body, "GetVertices", None)
    if callable(getter):
        try:
            vertices = list(getter())
        except Exception:
            vertices = []
    if not vertices:
        try:
            edges = list(body.GetEdges())
        except Exception:
            edges = []
        for edge in edges:
            try:
                vertices.extend(list(edge.GetVertices()))
            except Exception:
                continue
    for vertex in vertices:
        try:
            value = _measure_distance(work_part, length_unit, point_obj, vertex)
        except Exception:
            continue
        if best is None or value < best:
            best = value
    return best


def _classify_point(
    work_part: Any,
    length_unit: Any,
    mass_units: list[Any],
    body: Any,
    point: tuple[float, float, float],
    tolerance: float,
) -> str:
    nxopen = sys.modules["NXOpen"]
    tol = max(float(tolerance), 1e-6)
    point_obj = work_part.Points.CreatePoint(nxopen.Point3d(*point))
    surface_distance = _measure_distance(work_part, length_unit, point_obj, body)
    if surface_distance > tol:
        radius = min(tol, 0.5 * surface_distance)
        ratio = _containment_ratio(work_part, mass_units, body, point, radius)
        if ratio >= CONTAINMENT_FULL_RATIO:
            return "Inside"
        if ratio <= CONTAINMENT_EMPTY_RATIO:
            return "Outside"
        raise RuntimeError(f"containment probe is ambiguous (ratio={ratio:g})")
    vertex_distance = _vertex_distance(work_part, length_unit, point_obj, body)
    if vertex_distance is not None and vertex_distance <= tol:
        return "OnVertex"
    edge_distance: float | None = None
    try:
        edges = list(body.GetEdges())
    except Exception:
        edges = []
    for edge in edges:
        try:
            value = _measure_distance(work_part, length_unit, point_obj, edge)
        except Exception:
            continue
        if edge_distance is None or value < edge_distance:
            edge_distance = value
    if edge_distance is not None and edge_distance <= tol:
        return "OnEdge"
    return "OnFace"


def _plane_extreme(work_part: Any, length_unit: Any, body: Any, axis: str, side: str) -> float:
    nxopen = sys.modules["NXOpen"]
    axis_index = {"x": 0, "y": 1, "z": 2}[axis]
    primary = [0.0, 0.0, 0.0]
    primary[axis_index] = 1.0 if side == "max" else -1.0
    secondary = [0.0, 0.0, 0.0]
    secondary[(axis_index + 1) % 3] = 1.0
    tertiary = [0.0, 0.0, 0.0]
    tertiary[(axis_index + 2) % 3] = 1.0
    origin = work_part.Points.CreatePoint(nxopen.Point3d(0.0, 0.0, 0.0))
    directions = [
        work_part.Directions.CreateDirection(origin, nxopen.Vector3d(*vector))
        for vector in (primary, secondary, tertiary)
    ]
    collector = work_part.ScCollectors.CreateCollector()
    rule = work_part.ScRuleFactory.CreateRuleBodyDumb([body])
    collector.ReplaceRules([rule], False)
    extreme = None
    try:
        extreme = work_part.MeasureManager.NewRectangularExtreme(
            length_unit, directions[0], directions[1], directions[2], collector, False
        )
        point = getattr(extreme, "Point", None)
        if point is None:
            raise RuntimeError("NX extreme measurement returned no point")
        coordinate = getattr(point, "XYZ"[axis_index], None)
        if coordinate is None:
            raise RuntimeError("NX extreme point exposes no coordinate components")
        value = float(coordinate)
        if not math.isfinite(value):
            raise RuntimeError(f"NX extreme coordinate is not finite: {value}")
        return value
    finally:
        if extreme is not None:
            _dispose(extreme)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _point3(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, list | tuple) or len(value) != 3:
        return None
    coords = [_num(item) for item in value]
    if any(coord is None for coord in coords):
        return None
    return (float(coords[0]), float(coords[1]), float(coords[2]))


def _role_body(
    role_bodies: dict[str, list[Any]],
    role: Any,
    body_index: Any,
) -> Any:
    bodies = role_bodies.get(str(role or ""), [])
    index = body_index if isinstance(body_index, int) and not isinstance(body_index, bool) else 0
    if index < 0 or index >= len(bodies):
        raise IndexError(f"role {role} has {len(bodies)} bodies; index {index} is out of range")
    return bodies[index]


def _run_check(
    work_part: Any,
    length_unit: Any,
    mass_units: list[Any],
    role_bodies: dict[str, list[Any]],
    spec: dict[str, Any],
) -> dict[str, Any]:
    kind = str(spec.get("kind") or "")
    record = _check_record(str(spec.get("id") or ""), kind)
    try:
        if kind == "distance":
            body_a = _role_body(role_bodies, spec.get("role_a"), spec.get("body_index_a"))
            body_b = _role_body(role_bodies, spec.get("role_b"), spec.get("body_index_b"))
            record["actual"] = _measure_distance(work_part, length_unit, body_a, body_b)
        elif kind == "point_relation":
            body = _role_body(role_bodies, spec.get("role"), spec.get("body_index"))
            point = _point3(spec.get("point"))
            if point is None:
                raise ValueError("point_relation check requires a 3D point")
            tolerance = _num(spec.get("tolerance")) or 1e-3
            record["actual"] = _classify_point(work_part, length_unit, mass_units, body, point, tolerance)
        elif kind == "clash":
            body_a = _role_body(role_bodies, spec.get("role_a"), spec.get("body_index_a"))
            body_b = _role_body(role_bodies, spec.get("role_b"), spec.get("body_index_b"))
            tolerance = _num(spec.get("tolerance")) or 1e-3
            volume_tol = max(tolerance**3, 1e-9)
            volume = _intersection_volume(work_part, mass_units, body_a, body_b)
            record["actual"] = "AnyClash" if volume > volume_tol else "Clash_None"
            record["detail"] = f"intersection_volume={volume:g} volume_tol={volume_tol:g}"
        elif kind == "plane_extreme":
            body = _role_body(role_bodies, spec.get("role"), spec.get("body_index"))
            axis = str(spec.get("axis") or "").lower()
            side = str(spec.get("side") or "").lower()
            if axis not in {"x", "y", "z"} or side not in {"min", "max"}:
                raise ValueError("plane_extreme check requires axis in x/y/z and side in min/max")
            record["actual"] = _plane_extreme(work_part, length_unit, body, axis, side)
        elif kind == "import_integrity":
            bodies = role_bodies.get(str(spec.get("role") or ""), [])
            solids = 0
            sheets = 0
            edges = 0
            free_edges = 0
            for body in bodies:
                flag = _body_solid_flag(body)
                if flag is True:
                    solids += 1
                elif flag is False:
                    sheets += 1
                edge_count, free_count = _free_edge_count(body)
                edges += edge_count
                free_edges += free_count
            record["actual"] = {
                "body_count": len(bodies),
                "solid_count": solids,
                "sheet_count": sheets,
                "edge_count": edges,
                "free_edge_count": free_edges,
                "closed": bool(bodies) and solids == len(bodies) and free_edges == 0,
            }
        else:
            record["status"] = "unsupported"
            record["detail"] = f"unsupported check kind: {kind}"
            return record
    except Exception as exc:
        record["status"] = "error"
        record["detail"] = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        return record
    record["status"] = "measured"
    record["ok"] = True
    return record


def _close_temporary_part(nxopen: Any, part: Any) -> str:
    if part is None:
        return ""
    try:
        whole_tree = _enum_value(nxopen.BasePart.CloseWholeTree, "TrueValue", "True")
        close_modified = _enum_value(
            nxopen.BasePart.CloseModified,
            "CloseModified",
            "DontCloseModified",
        )
        part.Close(whole_tree, close_modified, None)
        return ""
    except Exception as exc:
        return str(exc)[:MAX_ERROR_CHARS]


def _remove_temporary_tree(path: Path) -> str:
    try:
        shutil.rmtree(path)
        return ""
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return str(exc)[:MAX_ERROR_CHARS]


def _load_spec(path: Path) -> dict[str, Any]:
    spec = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(spec, dict):
        raise ValueError("check spec must be a JSON object")
    checks = spec.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("check spec requires a nonempty checks array")
    if len(checks) > MAX_CHECKS:
        raise ValueError(f"check spec is bounded to {MAX_CHECKS} checks")
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("each check must be a JSON object")
    return spec


def collect_oracle_recheck(
    target_path: Path | None,
    tool_path: Path | None,
    result_dir: Path | None,
    spec_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    spec_path = spec_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    report = _empty_report()
    if output_path.suffix.casefold() != ".json":
        raise ValueError("output must be a .json file")
    spec = _load_spec(spec_path)
    for label, path in (("target", target_path), ("tool", tool_path)):
        if path is not None:
            resolved = path.expanduser().resolve()
            if resolved.suffix.casefold() not in {".step", ".stp"} or not resolved.is_file():
                raise ValueError(f"{label} must be an existing .step or .stp file")
            report["input"][label] = _input_record(resolved)
    result_files: list[Path] = []
    if result_dir is not None:
        resolved_dir = result_dir.expanduser().resolve()
        if not resolved_dir.is_dir():
            raise ValueError("result directory does not exist")
        result_files = sorted(
            (
                path
                for path in resolved_dir.iterdir()
                if path.is_file()
                and path.suffix.casefold() in {".step", ".stp"}
                and path.stem.casefold().startswith("result")
            ),
            key=lambda path: path.name.casefold(),
        )
        report["input"]["result_files"] = [_input_record(path) for path in result_files]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    import NXOpen  # type: ignore[import-not-found]

    session = NXOpen.Session.GetSession()
    if session is None:
        raise RuntimeError("NXOpen.Session.GetSession() returned no session")
    report["nx"] = _nx_version(session)
    temporary_dir = output_path.parent / f".sggk-nx-recheck-{os.getpid()}-{secrets.token_hex(6)}"
    temporary_dir.mkdir()
    temporary_part = temporary_dir / "recheck.prt"
    work_part = None
    try:
        work_part = session.Parts.NewDisplay(str(temporary_part), NXOpen.Part.Units.Millimeters)
        length_unit = _find_unit(work_part, "MilliMeter")
        mass_units = _mass_units(work_part)

        role_bodies: dict[str, list[Any]] = {"target": [], "tool": [], "result": []}
        import_records: dict[str, Any] = {}
        for role, path in (("target", target_path), ("tool", tool_path)):
            if path is None:
                import_records[role] = {"provided": False, "body_count": 0}
                continue
            try:
                bodies = _import_step(NXOpen, session, work_part, path.expanduser().resolve())
            except Exception as exc:
                report["status"] = "import_failed"
                import_records[role] = {
                    "provided": True,
                    "body_count": 0,
                    "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS],
                }
                report["diagnostics"].append(
                    _diagnostic("NX_ORACLE_RECHECK_IMPORT_FAILED", "error", f"{role}: {exc}")
                )
                continue
            role_bodies[role] = bodies
            import_records[role] = {"provided": True, "body_count": len(bodies)}
        for result_file in result_files:
            try:
                role_bodies["result"].extend(_import_step(NXOpen, session, work_part, result_file))
            except Exception as exc:
                report["diagnostics"].append(
                    _diagnostic("NX_ORACLE_RECHECK_IMPORT_FAILED", "error", f"{result_file.name}: {exc}")
                )
        import_records["result"] = {
            "provided": result_dir is not None,
            "file_count": len(result_files),
            "body_count": len(role_bodies["result"]),
        }
        report["import"] = import_records

        for check in spec["checks"]:
            record = _run_check(work_part, length_unit, mass_units, role_bodies, check)
            report["checks"].append(record)

        if report["status"] == "invalid_request":
            report["status"] = "completed"
        report["ok"] = True
        report["diagnostics"].append(
            _diagnostic("NX_ORACLE_RECHECK_COMPLETED", "info", "NX oracle recheck completed.")
        )
        return report
    except Exception as exc:
        if report["status"] == "invalid_request":
            report["status"] = "failed"
        report["diagnostics"].append(
            _diagnostic("NX_ORACLE_RECHECK_FAILED", "error", f"{type(exc).__name__}: {exc}")
        )
        return report
    finally:
        close_error = _close_temporary_part(NXOpen, work_part)
        if close_error:
            report["diagnostics"].append(
                _diagnostic("NX_TEMPORARY_PART_CLOSE_FAILED", "warning", close_error)
            )
        delete_error = _remove_temporary_tree(temporary_dir)
        if delete_error:
            report["diagnostics"].append(
                _diagnostic("NX_TEMPORARY_FILES_DELETE_FAILED", "warning", delete_error)
            )


def _write_result(path: Path, report: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _optional_path(value: str) -> Path | None:
    return None if value.strip() in {"", "-"} else Path(value)


def main(arguments: list[str] | None = None) -> None:
    values = list(sys.argv[1:] if arguments is None else arguments)
    target_path = _optional_path(values[0]) if values else None
    tool_path = _optional_path(values[1]) if len(values) > 1 else None
    result_dir = _optional_path(values[2]) if len(values) > 2 else None
    spec_path = Path(values[3]) if len(values) > 3 else None
    output_path = Path(values[4]) if len(values) > 4 else None
    try:
        if len(values) != 5 or spec_path is None or output_path is None:
            raise ValueError(
                "expected arguments: TARGET.step|- TOOL.step|- RESULT_DIR|- SPEC.json OUTPUT.json"
            )
        report = collect_oracle_recheck(target_path, tool_path, result_dir, spec_path, output_path)
    except Exception as exc:
        report = _empty_report()
        report["diagnostics"].append(
            _diagnostic("NX_ORACLE_RECHECK_REQUEST_INVALID", "error", f"{type(exc).__name__}: {exc}")
        )
    if output_path is not None:
        _write_result(output_path, report)
    print(RESULT_PREFIX + json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    if not report["ok"]:
        detail = report["diagnostics"][0]["message"] if report["diagnostics"] else report["status"]
        raise RuntimeError(f"NX oracle recheck failed: {detail}")


if __name__ == "__main__":
    main()
