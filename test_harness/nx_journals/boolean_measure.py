"""Import a target and tool STEP in NX, run one Parasolid boolean, and measure the result.

This reviewed journal is intentionally data-only.  It accepts a target
``.step``/``.stp`` file, a tool ``.step``/``.stp`` file, one boolean operation
(``unite``/``subtract``/``intersect``), an output ``.json`` path, and an
optional result ``.step`` export path.  It does not execute code from any of
those paths and removes the temporary NX part and translator logs afterwards.

The measurement records per-body Parasolid mass properties plus a free-edge
closedness probe (edges shared by fewer than two faces), so a boolean that
returns a non-closed "solid" is visible as evidence rather than inferred.
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
RESULT_KIND = "sggk_nx_boolean_measurement"
RESULT_PREFIX = "SGGK_NX_BOOLEAN_MEASUREMENT_JSON="
MEASUREMENT_ACCURACY = 0.999
MAX_ERROR_CHARS = 2000
OPERATIONS = {
    "unite": "Unite",
    "subtract": "Subtract",
    "intersect": "Intersect",
}


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


def _empty_report(target_path: Path | None = None, tool_path: Path | None = None, operation: str = "") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "ok": False,
        "status": "invalid_request",
        "input": {
            "target": _input_record(target_path),
            "tool": _input_record(tool_path),
            "operation": operation,
        },
        "nx": {"version": "", "full_version": "", "session_type": ""},
        "units": {
            "length": "millimeter",
            "area": "square_millimeter",
            "volume": "cubic_millimeter",
        },
        "import": {
            "target": {"ok": False, "body_count": 0, "solid_body_count": 0},
            "tool": {"ok": False, "body_count": 0, "solid_body_count": 0},
        },
        "boolean": {
            "ok": False,
            "operation": operation,
            "error_code": 0,
            "error_message": "",
            "result_body_count": 0,
        },
        "pre_measurement": {
            "target": {"ok": False, "total_area": 0.0, "total_abs_volume": 0.0},
            "tool": {"ok": False, "total_area": 0.0, "total_abs_volume": 0.0},
        },
        "measurement": {
            "ok": False,
            "accuracy": MEASUREMENT_ACCURACY,
            "body_count": 0,
            "measured_body_count": 0,
            "total_area": 0.0,
            "total_abs_volume": 0.0,
            "all_solid_closed": False,
            "bodies": [],
        },
        "result_export": {"ok": False, "path": "", "sha256": "", "error": ""},
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


def _new_mass_properties(part: Any, units: list[Any], body: Any) -> Any:
    try:
        return part.MeasureManager.NewMassProperties(units, MEASUREMENT_ACCURACY, [body])
    except TypeError:
        return part.MeasureManager.NewMassProperties(units, MEASUREMENT_ACCURACY, False, [body])


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
    """Return (edge_count, free_edge_count); free edges have fewer than two faces."""

    edge_count = 0
    free_edges = 0
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
        if face_count < 2:
            free_edges += 1
    return edge_count, free_edges


def _measure_body(part: Any, body: Any, index: int, units: list[Any]) -> tuple[dict[str, Any], str]:
    edge_count, free_edges = _free_edge_count(body)
    solid_flag = _body_solid_flag(body)
    mass_measure = None
    try:
        mass_measure = _new_mass_properties(part, units, body)
        area = float(mass_measure.Area)
        volume = abs(float(mass_measure.Volume))
        if not math.isfinite(area) or not math.isfinite(volume) or area < 0:
            raise ValueError("NX returned non-finite or negative mass properties")
        return {
            "index": index,
            "tag": _tag_value(body),
            "body_type": "solid" if solid_flag else ("sheet" if solid_flag is False else "unknown"),
            "is_solid": bool(solid_flag),
            "edge_count": edge_count,
            "free_edge_count": free_edges,
            "closed": bool(solid_flag) and free_edges == 0,
            "measurement_ok": True,
            "area": area,
            "abs_volume": volume,
            "error": "",
        }, ""
    except Exception as mass_error:
        message = f"mass properties failed: {mass_error}"
        return {
            "index": index,
            "tag": _tag_value(body),
            "body_type": "solid" if solid_flag else ("sheet" if solid_flag is False else "unknown"),
            "is_solid": bool(solid_flag),
            "edge_count": edge_count,
            "free_edge_count": free_edges,
            "closed": False,
            "measurement_ok": False,
            "area": 0.0,
            "abs_volume": 0.0,
            "error": message[:MAX_ERROR_CHARS],
        }, message
    finally:
        if mass_measure is not None:
            _dispose(mass_measure)


def _configure_importer(nxopen: Any, session: Any, importer: Any, work_part: Any, input_path: Path) -> None:
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
    """Import one STEP into the work part and return the newly added solid bodies."""

    before = {_tag_value(body) for body in list(work_part.Bodies)}
    importer = session.DexManager.CreateStep214Importer()
    try:
        _configure_importer(nxopen, session, importer, work_part, input_path)
        importer.Commit()
    finally:
        _dispose(importer)
    added = [body for body in list(work_part.Bodies) if _tag_value(body) not in before]
    solids = [body for body in added if _body_solid_flag(body) is not False]
    return solids if solids else added


def _measure_group(part: Any, bodies: list[Any], units: list[Any]) -> dict[str, Any]:
    total_area = 0.0
    total_volume = 0.0
    ok = True
    for index, body in enumerate(bodies):
        item, error = _measure_body(part, body, index, units)
        if error:
            ok = False
        total_area += float(item["area"])
        total_volume += float(item["abs_volume"])
    return {"ok": ok, "total_area": total_area, "total_abs_volume": total_volume}


def _export_result_step(nxopen: Any, session: Any, work_part: Any, export_path: Path) -> dict[str, Any]:
    record = {"ok": False, "path": str(export_path), "sha256": "", "error": ""}
    creator = None
    try:
        creator = session.DexManager.CreateStepCreator()
        creator.ExportFrom = _enum_value(
            nxopen.StepCreator.ExportFromOption,
            "DisplayPart",
            "ExistingPart",
        )
        object_types = creator.ObjectTypes
        object_types.Solids = True
        object_types.Surfaces = True
        creator.OutputFile = str(export_path)
        settings_root = ""
        getter = getattr(session, "GetEnvironmentVariableValue", None)
        if callable(getter):
            try:
                settings_root = str(getter("STEP214UG_DIR") or "")
            except Exception:
                settings_root = ""
        settings_path = Path(settings_root) / "ugstep214.def" if settings_root else None
        if settings_path is not None and settings_path.is_file() and hasattr(creator, "SettingsFile"):
            creator.SettingsFile = str(settings_path)
        creator.Commit()
        if export_path.is_file() and export_path.stat().st_size > 0:
            record["ok"] = True
            record["sha256"] = _sha256_file(export_path)
        else:
            record["error"] = "STEP export produced no file"
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
    finally:
        if creator is not None:
            _dispose(creator)
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


def collect_boolean_measurement(
    target_path: Path,
    tool_path: Path,
    operation: str,
    output_path: Path,
    result_export_path: Path | None = None,
) -> dict[str, Any]:
    target_path = target_path.expanduser().resolve()
    tool_path = tool_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    report = _empty_report(target_path, tool_path, operation)
    for label, path in (("target", target_path), ("tool", tool_path)):
        if path.suffix.casefold() not in {".step", ".stp"}:
            raise ValueError(f"{label} must be a .step or .stp file")
        if not path.is_file():
            raise ValueError(f"{label} STEP file does not exist")
    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {sorted(OPERATIONS)}")
    if output_path.suffix.casefold() != ".json":
        raise ValueError("output must be a .json file")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    import NXOpen  # type: ignore[import-not-found]

    session = NXOpen.Session.GetSession()
    if session is None:
        raise RuntimeError("NXOpen.Session.GetSession() returned no session")
    report["nx"] = _nx_version(session)
    temporary_dir = output_path.parent / f".sggk-nx-boolean-{os.getpid()}-{secrets.token_hex(6)}"
    temporary_dir.mkdir()
    temporary_part = temporary_dir / "boolean.prt"
    work_part = None
    try:
        work_part = session.Parts.NewDisplay(str(temporary_part), NXOpen.Part.Units.Millimeters)
        units = _mass_units(work_part)

        target_bodies = _import_step(NXOpen, session, work_part, target_path)
        report["import"]["target"] = {
            "ok": bool(target_bodies),
            "body_count": len(target_bodies),
            "solid_body_count": sum(1 for body in target_bodies if _body_solid_flag(body)),
        }
        tool_bodies = _import_step(NXOpen, session, work_part, tool_path)
        report["import"]["tool"] = {
            "ok": bool(tool_bodies),
            "body_count": len(tool_bodies),
            "solid_body_count": sum(1 for body in tool_bodies if _body_solid_flag(body)),
        }
        if not target_bodies or not tool_bodies:
            report["status"] = "import_failed"
            report["diagnostics"].append(
                _diagnostic(
                    "NX_BOOLEAN_IMPORT_EMPTY",
                    "error",
                    f"NX imported {len(target_bodies)} target and {len(tool_bodies)} tool bodies; both must be nonempty.",
                )
            )
            return report
        if len(target_bodies) != 1 or len(tool_bodies) != 1:
            report["status"] = "import_failed"
            report["diagnostics"].append(
                _diagnostic(
                    "NX_BOOLEAN_MULTI_BODY_UNSUPPORTED",
                    "error",
                    f"boolean journal requires exactly one target and one tool body; "
                    f"got {len(target_bodies)} target and {len(tool_bodies)} tool.",
                )
            )
            return report

        report["pre_measurement"] = {
            "target": _measure_group(work_part, target_bodies, units),
            "tool": _measure_group(work_part, tool_bodies, units),
        }

        boolean_builder = None
        try:
            boolean_builder = work_part.Features.CreateBooleanBuilder(None)
            boolean_builder.Operation = _enum_value(
                NXOpen.Features.Feature.BooleanType,
                OPERATIONS[operation],
            )
            boolean_builder.Target = target_bodies[0]
            boolean_builder.Tool = tool_bodies[0]
            boolean_builder.RetainTarget = False
            boolean_builder.RetainTool = False
            boolean_builder.Commit()
        except Exception as bool_error:
            report["status"] = "boolean_failed"
            report["boolean"]["error_message"] = f"{type(bool_error).__name__}: {bool_error}"[:MAX_ERROR_CHARS]
            report["diagnostics"].append(
                _diagnostic("NX_BOOLEAN_OPERATION_FAILED", "error", report["boolean"]["error_message"])
            )
            return report
        finally:
            if boolean_builder is not None:
                _dispose(boolean_builder)

        result_bodies = [body for body in list(work_part.Bodies) if _body_solid_flag(body) is not False]
        if not result_bodies:
            result_bodies = list(work_part.Bodies)
        report["boolean"]["ok"] = True
        report["boolean"]["result_body_count"] = len(result_bodies)

        measured: list[dict[str, Any]] = []
        measurement_errors: list[str] = []
        for index, body in enumerate(result_bodies):
            item, error = _measure_body(work_part, body, index, units)
            measured.append(item)
            if error:
                measurement_errors.append(error)
        measurement_ok = not measurement_errors and len(measured) == len(result_bodies)
        report["measurement"] = {
            "ok": measurement_ok,
            "accuracy": MEASUREMENT_ACCURACY,
            "body_count": len(result_bodies),
            "measured_body_count": sum(1 for item in measured if item["measurement_ok"]),
            "total_area": sum(float(item["area"]) for item in measured),
            "total_abs_volume": sum(float(item["abs_volume"]) for item in measured),
            "all_solid_closed": bool(measured) and all(item["closed"] for item in measured),
            "bodies": measured,
        }
        if measurement_errors:
            report["status"] = "measurement_failed"
            report["diagnostics"].append(
                _diagnostic(
                    "NX_BOOLEAN_MEASUREMENT_FAILED",
                    "error",
                    f"{len(measurement_errors)} result measurement(s) failed; first: {measurement_errors[0]}",
                )
            )
            return report

        if result_export_path is not None:
            report["result_export"] = _export_result_step(
                NXOpen,
                session,
                work_part,
                result_export_path.expanduser().resolve(),
            )
            if not report["result_export"]["ok"]:
                report["diagnostics"].append(
                    _diagnostic(
                        "NX_BOOLEAN_RESULT_EXPORT_FAILED",
                        "warning",
                        report["result_export"]["error"] or "result STEP export failed",
                    )
                )

        report["ok"] = True
        report["status"] = "completed"
        report["diagnostics"].append(
            _diagnostic("NX_BOOLEAN_MEASUREMENT_COMPLETED", "info", "NX boolean and measurement completed.")
        )
        return report
    except Exception as exc:
        if report["status"] in {"invalid_request", "boolean_failed"}:
            report["status"] = "import_failed"
        report["diagnostics"].append(
            _diagnostic("NX_BOOLEAN_FAILED", "error", f"{type(exc).__name__}: {exc}")
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


def main(arguments: list[str] | None = None) -> None:
    values = list(sys.argv[1:] if arguments is None else arguments)
    target_path = Path(values[0]) if values else None
    tool_path = Path(values[1]) if len(values) > 1 else None
    operation = str(values[2]).strip().lower() if len(values) > 2 else ""
    output_path = Path(values[3]) if len(values) > 3 else None
    result_export_path = Path(values[4]) if len(values) > 4 else None
    try:
        if len(values) not in {4, 5} or target_path is None or tool_path is None or output_path is None:
            raise ValueError(
                "expected arguments: TARGET.step TOOL.step {unite|subtract|intersect} OUTPUT.json [RESULT.step]"
            )
        report = collect_boolean_measurement(
            target_path,
            tool_path,
            operation,
            output_path,
            result_export_path,
        )
    except Exception as exc:
        report = _empty_report(target_path, tool_path, operation)
        report["diagnostics"].append(
            _diagnostic("NX_BOOLEAN_REQUEST_INVALID", "error", f"{type(exc).__name__}: {exc}")
        )
    if output_path is not None:
        _write_result(output_path, report)
    print(RESULT_PREFIX + json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    if not report["ok"]:
        detail = report["diagnostics"][0]["message"] if report["diagnostics"] else report["status"]
        raise RuntimeError(f"NX boolean measurement failed: {detail}")


if __name__ == "__main__":
    main()
