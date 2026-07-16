"""Import one STEP file in NX and emit a fixed, machine-readable measurement.

This reviewed journal is intentionally data-only.  It accepts exactly two
arguments: an input ``.step``/``.stp`` file and an output ``.json`` path.  It
does not execute code from either path and removes the temporary NX assembly,
component parts, and translator logs after measurement.
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
RESULT_KIND = "sggk_nx_step_measurement"
RESULT_PREFIX = "SGGK_NX_STEP_MEASUREMENT_JSON="
MEASUREMENT_ACCURACY = 0.999
MAX_ERROR_CHARS = 2000


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


def _empty_report(input_path: Path | None = None) -> dict[str, Any]:
    input_name = input_path.name if input_path is not None else ""
    input_sha256 = ""
    input_size = 0
    if input_path is not None and input_path.is_file():
        input_sha256 = _sha256_file(input_path)
        input_size = input_path.stat().st_size
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "ok": False,
        "status": "invalid_request",
        "input": {
            "name": input_name,
            "sha256": input_sha256,
            "size_bytes": input_size,
        },
        "nx": {
            "version": "",
            "full_version": "",
            "session_type": "",
        },
        "units": {
            "length": "millimeter",
            "area": "square_millimeter",
            "volume": "cubic_millimeter",
        },
        "import": {
            "ok": False,
            "protocol": "STEP AP214",
            "flatten_assembly": True,
            "body_count": 0,
            "solid_body_count": 0,
            "sheet_body_count": 0,
            "unknown_body_count": 0,
        },
        "measurement": {
            "ok": False,
            "accuracy": MEASUREMENT_ACCURACY,
            "body_count": 0,
            "measured_body_count": 0,
            "total_area": 0.0,
            "total_abs_volume": 0.0,
            "bodies": [],
        },
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
        # Some generated Python wrappers retain the legacy create-geometry
        # boolean even though the list overload does not expose it elsewhere.
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


def _part_key(part: Any) -> tuple[str, Any]:
    """Return a stable key for caching loads, never for occurrence de-duplication."""

    try:
        tag = int(getattr(part, "Tag"))
    except (AttributeError, TypeError, ValueError):
        tag = 0
    if tag:
        return ("tag", tag)
    full_path = str(getattr(part, "FullPath", "") or "")
    if full_path:
        return ("path", os.path.normcase(os.path.abspath(full_path)))
    return ("object", id(part))


def _load_part_fully(part: Any, method_name: str = "LoadThisPartFully") -> None:
    loader = getattr(part, method_name, None)
    if not callable(loader):
        return
    load_status = loader()
    if load_status is not None:
        _dispose(load_status)


def _root_component(work_part: Any) -> Any:
    component_assembly = getattr(work_part, "ComponentAssembly", None)
    return getattr(component_assembly, "RootComponent", None)


def _session_parts(session: Any) -> list[Any]:
    try:
        return list(session.Parts)
    except (AttributeError, TypeError):
        return []


def _collect_body_occurrences(
    session: Any,
    work_part: Any,
    parts_before_import: set[tuple[str, Any]],
) -> list[tuple[Any, Any]]:
    """Collect root and component bodies, preserving every assembly occurrence.

    STEP assemblies commonly have no bodies in their root part.  Measuring only
    ``work_part.Bodies`` therefore reports a false empty import.  Component
    prototypes are loaded fully before their bodies are read because NX batch
    sessions can default to lightweight partial loading.  A prototype may occur
    more than once: loading is cached, but body occurrences are deliberately not
    de-duplicated so total area and volume match the assembly.
    """

    loaded_parts: set[tuple[str, Any]] = set()

    root = _root_component(work_part)
    if root is None:
        # The translator can leave the root assembly partially loaded.  A full
        # load materializes RootComponent and its occurrence tree.
        _load_part_fully(work_part, "LoadFully")
        root = _root_component(work_part)

    occurrences: list[tuple[Any, Any]] = [
        (work_part, body) for body in list(work_part.Bodies)
    ]
    component_occurrences: list[tuple[Any, Any]] = []

    def visit(component: Any, ancestor_components: set[tuple[str, Any]]) -> None:
        component_key = _part_key(component)
        if component_key in ancestor_components:
            raise RuntimeError("NX assembly contains a recursive component occurrence")
        next_ancestors = ancestor_components | {component_key}

        prototype = getattr(component, "Prototype", None)
        if prototype is not None and hasattr(prototype, "Bodies"):
            prototype_key = _part_key(prototype)
            if prototype_key not in loaded_parts:
                _load_part_fully(prototype)
                loaded_parts.add(prototype_key)
            component_occurrences.extend(
                (prototype, body) for body in list(prototype.Bodies)
            )

        get_children = getattr(component, "GetChildren", None)
        children = list(get_children()) if callable(get_children) else []
        for child in children:
            visit(child, next_ancestors)

    if root is not None:
        get_children = getattr(root, "GetChildren", None)
        children = list(get_children()) if callable(get_children) else []
        for child in children:
            visit(child, set())

    if not component_occurrences:
        # Compatibility fallback for translators/NX releases which load child
        # parts but do not expose a RootComponent tree.  This cannot represent
        # repeated instances, so it is used only when occurrence traversal found
        # no component bodies at all.
        root_key = _part_key(work_part)
        for part in _session_parts(session):
            part_key = _part_key(part)
            if part_key == root_key or part_key in parts_before_import:
                continue
            if part_key not in loaded_parts:
                _load_part_fully(part)
                loaded_parts.add(part_key)
            component_occurrences.extend((part, body) for body in list(part.Bodies))

    occurrences.extend(component_occurrences)
    return occurrences


def _measure_body(part: Any, body: Any, index: int, units: list[Any]) -> tuple[dict[str, Any], str]:
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
            "body_type": "solid",
            "measurement_ok": True,
            "area": area,
            "abs_volume": volume,
            "error": "",
        }, ""
    except Exception as mass_error:
        solid_flag = _body_solid_flag(body)
        if solid_flag is not False:
            body_type = "solid" if solid_flag else "unknown"
            message = f"mass properties failed for {body_type} body: {mass_error}"
            return {
                "index": index,
                "tag": _tag_value(body),
                "body_type": body_type,
                "measurement_ok": False,
                "area": 0.0,
                "abs_volume": 0.0,
                "error": message[:MAX_ERROR_CHARS],
            }, message

        face_measure = None
        try:
            faces = list(body.GetFaces())
            if not faces:
                raise ValueError("sheet body contains no faces")
            face_measure = part.MeasureManager.NewFaceProperties(
                units[0],
                units[3],
                MEASUREMENT_ACCURACY,
                faces,
            )
            area = float(face_measure.Area)
            if not math.isfinite(area) or area < 0:
                raise ValueError("NX returned non-finite or negative face area")
            return {
                "index": index,
                "tag": _tag_value(body),
                "body_type": "sheet",
                "measurement_ok": True,
                "area": area,
                "abs_volume": 0.0,
                "error": "",
            }, ""
        except Exception as face_error:
            message = f"sheet face properties failed: {face_error}"
            return {
                "index": index,
                "tag": _tag_value(body),
                "body_type": "sheet",
                "measurement_ok": False,
                "area": 0.0,
                "abs_volume": 0.0,
                "error": message[:MAX_ERROR_CHARS],
            }, message
        finally:
            if face_measure is not None:
                _dispose(face_measure)
    finally:
        if mass_measure is not None:
            _dispose(mass_measure)


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


def _configure_importer(
    nxopen: Any,
    session: Any,
    importer: Any,
    work_part: Any,
    input_path: Path,
) -> None:
    # Match the NX2512 installed SDK sample
    # UGOPEN/SampleNXOpenApplications/DotNet/CAMSetupImport/GeometryImporter.cs.
    # In particular, WorkPart imports still require OutputFile to name the
    # work part; omitting it causes the external translator to return error 1.
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
    # NX ships two similarly named files with opposite directions:
    #   step214ug.def = STEP -> NX (import)
    #   ugstep214.def = NX -> STEP (export)
    # Using the export definition with Step214Importer launches the wrong
    # translator and NX2512 reports "Unable to import selected file".
    settings_path = Path(settings_root) / "step214ug.def" if settings_root else None
    if settings_path is not None and settings_path.is_file() and hasattr(importer, "SettingsFile"):
        importer.SettingsFile = str(settings_path)


def collect_measurement(input_path: Path, output_path: Path) -> dict[str, Any]:
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    report = _empty_report(input_path)
    if input_path.suffix.casefold() not in {".step", ".stp"}:
        raise ValueError("input must be a .step or .stp file")
    if not input_path.is_file():
        raise ValueError("input STEP file does not exist")
    if output_path.suffix.casefold() != ".json":
        raise ValueError("output must be a .json file")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    import NXOpen  # type: ignore[import-not-found]

    session = NXOpen.Session.GetSession()
    if session is None:
        raise RuntimeError("NXOpen.Session.GetSession() returned no session")
    report["nx"] = _nx_version(session)
    temporary_dir = output_path.parent / (
        f".sggk-nx-step-{os.getpid()}-{secrets.token_hex(6)}"
    )
    temporary_dir.mkdir()
    temporary_part = temporary_dir / "assembly.prt"
    work_part = None
    importer = None
    try:
        work_part = session.Parts.NewDisplay(
            str(temporary_part),
            NXOpen.Part.Units.Millimeters,
        )
        parts_before_import = {_part_key(part) for part in _session_parts(session)}
        importer = session.DexManager.CreateStep214Importer()
        _configure_importer(NXOpen, session, importer, work_part, input_path)
        importer.Commit()
        body_occurrences = _collect_body_occurrences(
            session,
            work_part,
            parts_before_import,
        )
        report["import"]["body_count"] = len(body_occurrences)
        report["import"]["unknown_body_count"] = len(body_occurrences)
        report["import"]["ok"] = bool(body_occurrences)
        if not body_occurrences:
            report["status"] = "import_failed"
            report["diagnostics"].append(
                _diagnostic("NX_STEP_IMPORT_EMPTY", "error", "NX imported no bodies from the STEP file.")
            )
            return report

        measured: list[dict[str, Any]] = []
        measurement_errors: list[str] = []
        units_by_part: dict[tuple[str, Any], list[Any]] = {}
        for index, (owning_part, body) in enumerate(body_occurrences):
            part_key = _part_key(owning_part)
            try:
                if part_key not in units_by_part:
                    units_by_part[part_key] = _mass_units(owning_part)
                units = units_by_part[part_key]
            except Exception as exc:
                report["status"] = "measurement_failed"
                report["diagnostics"].append(
                    _diagnostic(
                        "NX_STEP_MEASUREMENT_SETUP_FAILED",
                        "error",
                        f"{type(exc).__name__}: {exc}",
                    )
                )
                return report
            item, error = _measure_body(owning_part, body, index, units)
            measured.append(item)
            if error:
                measurement_errors.append(error)
        type_counts = {
            kind: sum(1 for item in measured if item["body_type"] == kind)
            for kind in ("solid", "sheet", "unknown")
        }
        report["import"].update(
            {
                "solid_body_count": type_counts["solid"],
                "sheet_body_count": type_counts["sheet"],
                "unknown_body_count": type_counts["unknown"],
            }
        )
        measurement_ok = not measurement_errors and len(measured) == len(body_occurrences)
        report["measurement"] = {
            "ok": measurement_ok,
            "accuracy": MEASUREMENT_ACCURACY,
            "body_count": len(body_occurrences),
            "measured_body_count": sum(1 for item in measured if item["measurement_ok"]),
            "total_area": sum(float(item["area"]) for item in measured),
            "total_abs_volume": sum(float(item["abs_volume"]) for item in measured),
            "bodies": measured,
        }
        if measurement_errors:
            report["status"] = "measurement_failed"
            report["diagnostics"].append(
                _diagnostic(
                    "NX_STEP_MEASUREMENT_FAILED",
                    "error",
                    f"{len(measurement_errors)} body measurement(s) failed; first: {measurement_errors[0]}",
                )
            )
            return report
        report["ok"] = True
        report["status"] = "completed"
        report["diagnostics"].append(
            _diagnostic("NX_STEP_MEASUREMENT_COMPLETED", "info", "NX STEP import and measurement completed.")
        )
        return report
    except Exception as exc:
        report["status"] = "import_failed"
        report["diagnostics"].append(
            _diagnostic("NX_STEP_IMPORT_FAILED", "error", f"{type(exc).__name__}: {exc}")
        )
        return report
    finally:
        if importer is not None:
            _dispose(importer)
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
    input_path = Path(values[0]) if values else None
    output_path = Path(values[1]) if len(values) > 1 else None
    try:
        if len(values) != 2 or input_path is None or output_path is None:
            raise ValueError("expected exactly two arguments: INPUT.step OUTPUT.json")
        report = collect_measurement(input_path, output_path)
    except Exception as exc:
        report = _empty_report(input_path)
        report["diagnostics"].append(
            _diagnostic("NX_STEP_MEASUREMENT_REQUEST_INVALID", "error", f"{type(exc).__name__}: {exc}")
        )
    if output_path is not None:
        _write_result(output_path, report)
    print(RESULT_PREFIX + json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    if not report["ok"]:
        detail = report["diagnostics"][0]["message"] if report["diagnostics"] else report["status"]
        raise RuntimeError(f"NX STEP measurement failed: {detail}")


if __name__ == "__main__":
    main()
