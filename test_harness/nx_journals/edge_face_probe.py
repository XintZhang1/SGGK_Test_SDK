"""Reviewed diagnostic journal: report edge/face incidence facts for seam analysis.

Data-only probe used to calibrate free-edge detection on periodic analytic
surfaces (sphere/cylinder/cone/torus seams).  Prints one JSON object.
"""

import json
import os
import sys
from pathlib import Path

import NXOpen


def _tag(obj):
    try:
        return int(getattr(obj, "Tag"))
    except (TypeError, ValueError):
        return 0


def main() -> None:
    step_path = sys.argv[1]
    session = NXOpen.Session.GetSession()
    work_part = session.Parts.NewDisplay(
        str(Path(step_path).with_suffix(f".edge_face_probe.{os.getpid()}.prt")),
        NXOpen.Part.Units.Millimeters,
    )
    before = {_tag(body) for body in list(work_part.Bodies)}
    importer = session.DexManager.CreateStep214Importer()
    try:
        importer.SimplifyGeometry = True
        importer.LayerDefault = 1
        object_types = importer.ObjectTypes
        object_types.Curves = True
        object_types.Surfaces = True
        object_types.Solids = True
        object_types.PmiData = True
        importer.InputFile = str(step_path)
        importer.OutputFile = str(work_part.FullPath)
        importer.FileOpenFlag = False
        importer.ImportToTeamcenter = False
        importer.FlattenAssembly = True
        importer.ImportTo = NXOpen.Step214Importer.ImportToOption.WorkPart
        if hasattr(importer, "ProcessHoldFlag"):
            importer.ProcessHoldFlag = True
        getter = getattr(session, "GetEnvironmentVariableValue", None)
        settings_root = ""
        if callable(getter):
            try:
                settings_root = str(getter("STEP214UG_DIR") or "")
            except Exception:
                settings_root = ""
        if settings_root and hasattr(importer, "SettingsFile"):
            settings_path = Path(settings_root) / "step214ug.def"
            if settings_path.is_file():
                importer.SettingsFile = str(settings_path)
        importer.Commit()
    finally:
        for name in ("Dispose", "Destroy"):
            method = getattr(importer, name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
                break
    bodies = [body for body in list(work_part.Bodies) if _tag(body) not in before]
    report = {"kind": "sggk_nx_edge_face_probe", "input": str(step_path), "bodies": []}
    for body in bodies:
        body_info = {
            "tag": _tag(body),
            "is_solid": bool(getattr(body, "IsSolid", False)),
            "edges": [],
        }
        for edge in list(body.GetEdges()):
            edge_info = {"tag": _tag(edge)}
            try:
                faces = list(edge.GetFaces())
            except Exception as exc:
                faces = []
                edge_info["get_faces_error"] = str(exc)[:200]
            edge_info["face_count"] = len(faces)
            edge_info["face_tags"] = [_tag(face) for face in faces]
            is_seam = getattr(edge, "IsSeam", None)
            edge_info["is_seam_attr_type"] = type(is_seam).__name__
            if isinstance(is_seam, bool):
                edge_info["is_seam"] = is_seam
            if faces:
                try:
                    occurrences = sum(
                        1 for face_edge in list(faces[0].GetEdges()) if _tag(face_edge) == _tag(edge)
                    )
                    edge_info["occurrences_in_first_face"] = occurrences
                except Exception as exc:
                    edge_info["face_edges_error"] = str(exc)[:200]
                face_type = getattr(faces[0], "SolidFaceType", None)
                edge_info["first_face_type"] = str(face_type)[:80]
            try:
                vertices = edge.GetVertices()
                edge_info["vertices"] = [str(v)[:60] for v in list(vertices)]
            except Exception as exc:
                edge_info["get_vertices_error"] = str(exc)[:200]
            body_info["edges"].append(edge_info)
        report["bodies"].append(body_info)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
