from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from test_harness.nx_journals.abc_step_measure import (
    RESULT_KIND as NX_RESULT_KIND,
    _collect_body_occurrences,
    _configure_importer,
    _remove_temporary_tree,
)
from test_harness.tools.compare_nx_sggk_step import compare, main, render_markdown
import test_harness.tools.nx_runtime as nx_runtime_cli


HARNESS_ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_sggk_case(tmp_path: Path, *, content: bytes = b"ISO-10303-21;", area: float = 100.0) -> Path:
    case_dir = tmp_path / "sggk_case"
    source = case_dir / "input" / "source.step"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    write_json(
        case_dir / "manifest.json",
        {
            "case_id": "abc_step_0001",
            "api": "step_import",
            "sggk_version": "1.4.test",
        },
    )
    write_json(
        case_dir / "report" / "status.json",
        {
            "succeeded": True,
            "error_code": 0,
            "error_message": "",
            "error_entity_count": 0,
            "result_body_count": 2,
            "result_topology_count": 2,
        },
    )
    write_json(
        case_dir / "report" / "data_exchange.json",
        {
            "failed_item_count": 0,
            "invalid_topology_count": 0,
            "length_scale": 1.0,
        },
    )
    write_json(
        case_dir / "report" / "properties.json",
        {
            "bodies": [
                {
                    "index": 0,
                    "body_id": 1,
                    "summary": {},
                    "bbox": {},
                    "property_ok": True,
                    "length": 30.0,
                    "area": area * 0.6,
                    "volume": 30.0,
                },
                {
                    "index": 1,
                    "body_id": 2,
                    "summary": {},
                    "bbox": {},
                    "property_ok": True,
                    "length": 20.0,
                    "area": area * 0.4,
                    "volume": -20.0,
                },
            ]
        },
    )
    return case_dir


def nx_measurement_payload(
    digest: str,
    *,
    areas: tuple[float, ...] = (60.0007, 39.9998),
    volumes: tuple[float, ...] = (30.0, 20.0003),
    import_ok: bool = True,
) -> dict[str, object]:
    bodies = [
        {
            "index": index,
            "tag": 100 + index,
            "body_type": "solid",
            "measurement_ok": True,
            "area": area,
            "abs_volume": volumes[index],
            "error": "",
        }
        for index, area in enumerate(areas)
    ]
    count = len(bodies)
    measurement_ok = all(item["measurement_ok"] for item in bodies)
    return {
        "schema_version": 1,
        "kind": NX_RESULT_KIND,
        "ok": import_ok and measurement_ok,
        "status": "completed" if import_ok and measurement_ok else "import_failed",
        "input": {
            "name": "source.step",
            "sha256": digest,
            "size_bytes": 13,
        },
        "nx": {
            "version": "2512",
            "full_version": "2512.1000",
            "session_type": "Session",
        },
        "units": {
            "length": "millimeter",
            "area": "square_millimeter",
            "volume": "cubic_millimeter",
        },
        "import": {
            "ok": import_ok,
            "protocol": "STEP AP214",
            "flatten_assembly": True,
            "body_count": count,
            "solid_body_count": count,
            "sheet_body_count": 0,
            "unknown_body_count": 0,
        },
        "measurement": {
            "ok": measurement_ok,
            "accuracy": 0.999,
            "body_count": count,
            "measured_body_count": count,
            "total_area": sum(areas),
            "total_abs_volume": sum(volumes),
            "bodies": bodies,
        },
        "diagnostics": [
            {
                "code": "NX_STEP_MEASUREMENT_COMPLETED",
                "severity": "info",
                "message": "completed",
            }
        ],
    }


def test_measurement_and_comparison_schemas_are_valid_and_accept_pass_fixture(tmp_path: Path) -> None:
    content = b"ISO-10303-21;"
    digest = hashlib.sha256(content).hexdigest()
    nx_payload = nx_measurement_payload(digest)
    nx_schema = json.loads((HARNESS_ROOT / "schemas" / "nx_step_measurement.schema.json").read_text(encoding="utf-8"))
    comparison_schema = json.loads(
        (HARNESS_ROOT / "schemas" / "nx_sggk_step_comparison.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(nx_schema)
    Draft202012Validator.check_schema(comparison_schema)
    Draft202012Validator(nx_schema).validate(nx_payload)

    nx_path = tmp_path / "nx.json"
    write_json(nx_path, nx_payload)
    result = compare(nx_path, make_sggk_case(tmp_path, content=content), abs_tol=0.001, rel_tol=1e-5)

    assert result["ok"] is True
    assert result["checks"]["total_area"]["abs_delta"] > 0
    assert result["checks"]["total_area"]["abs_delta"] <= result["checks"]["total_area"]["tolerance"]
    assert result["checks"]["total_abs_volume"]["ok"] is True
    Draft202012Validator(comparison_schema).validate(result)


def test_sha_mismatch_fails_even_when_all_geometry_metrics_match(tmp_path: Path) -> None:
    content = b"ISO-10303-21;"
    nx_path = tmp_path / "nx.json"
    write_json(nx_path, nx_measurement_payload("a" * 64, areas=(60.0, 40.0), volumes=(30.0, 20.0)))

    result = compare(nx_path, make_sggk_case(tmp_path, content=content))

    assert result["ok"] is False
    assert result["input"]["same_input"] is False
    assert result["input"]["sha256"] == ""
    assert result["failures"] == ["input_sha256_failed"]


def test_metric_outside_abs_rel_tolerance_fails_and_markdown_names_failure(tmp_path: Path) -> None:
    content = b"ISO-10303-21;"
    digest = hashlib.sha256(content).hexdigest()
    nx_path = tmp_path / "nx.json"
    write_json(nx_path, nx_measurement_payload(digest, areas=(90.0, 40.0), volumes=(30.0, 20.0)))

    result = compare(nx_path, make_sggk_case(tmp_path, content=content), abs_tol=0.01, rel_tol=1e-5)
    markdown = render_markdown(result)

    assert result["ok"] is False
    assert result["checks"]["total_area"]["ok"] is False
    assert result["checks"]["total_abs_volume"]["ok"] is True
    assert result["failures"] == ["total_area_failed"]
    assert "总面积 (mm²)" in markdown
    assert "`total_area_failed`" in markdown


def test_import_and_exact_body_count_are_hard_checks(tmp_path: Path) -> None:
    content = b"ISO-10303-21;"
    digest = hashlib.sha256(content).hexdigest()
    nx_path = tmp_path / "nx.json"
    write_json(
        nx_path,
        nx_measurement_payload(digest, areas=(100.0,), volumes=(50.0,), import_ok=False),
    )

    result = compare(nx_path, make_sggk_case(tmp_path, content=content))

    assert result["ok"] is False
    assert result["checks"]["import"]["ok"] is False
    assert result["checks"]["body_count"]["ok"] is False
    assert "import_failed" in result["failures"]
    assert "body_count_failed" in result["failures"]


def test_compound_body_shell_aggregation_is_explained_without_hiding_strict_failure(
    tmp_path: Path,
) -> None:
    content = b"ISO-10303-21;"
    digest = hashlib.sha256(content).hexdigest()
    nx_path = tmp_path / "nx.json"
    write_json(
        nx_path,
        nx_measurement_payload(
            digest,
            areas=(10.0, 20.0, 15.0, 25.0, 12.0, 18.0),
            volumes=(5.0, 10.0, 7.5, 12.5, 6.0, 9.0),
        ),
    )
    case_dir = make_sggk_case(tmp_path, content=content)
    write_json(
        case_dir / "report" / "status.json",
        {
            "succeeded": True,
            "error_code": 0,
            "error_message": "",
            "error_entity_count": 0,
            "result_body_count": 1,
            "result_topology_count": 6,
        },
    )
    write_json(
        case_dir / "report" / "properties.json",
        {
            "bodies": [
                {
                    "index": 0,
                    "body_id": 1,
                    "summary": {"shells": 6},
                    "bbox": {},
                    "property_ok": True,
                    "length": 100.0,
                    "area": 100.0,
                    "volume": 50.0,
                }
            ]
        },
    )

    result = compare(nx_path, case_dir)
    markdown = render_markdown(result)

    assert result["ok"] is False
    assert result["failures"] == ["body_count_failed"]
    assert result["sggk"]["shell_count"] == 6
    assert result["checks"]["total_area"]["ok"] is True
    assert result["checks"]["total_abs_volume"]["ok"] is True
    assert result["diagnostics"] == [
        {
            "code": "NX_SGGK_COMPOUND_BODY_SHELL_AGGREGATION",
            "severity": "info",
            "classification": "cross_kernel_representation_difference",
            "nx_body_count": 6,
            "sggk_body_count": 1,
            "sggk_shell_count": 6,
            "geometry_bug_confirmed": False,
            "message": result["diagnostics"][0]["message"],
        }
    ]
    assert "NX 实体数与 SGGK shell 数一致" in markdown
    assert "不能据此自动确认几何 bug" in markdown
    assert (
        main(
            [
                "--nx-measurement",
                str(nx_path),
                "--sggk-case",
                str(case_dir),
                "--out",
                str(tmp_path / "representation_comparison"),
            ]
        )
        == 2
    )


def test_cli_writes_json_and_chinese_markdown(tmp_path: Path) -> None:
    content = b"ISO-10303-21;"
    digest = hashlib.sha256(content).hexdigest()
    nx_path = tmp_path / "nx.json"
    out_dir = tmp_path / "comparison"
    write_json(nx_path, nx_measurement_payload(digest))

    returncode = main(
        [
            "--nx-measurement",
            str(nx_path),
            "--sggk-case",
            str(make_sggk_case(tmp_path, content=content)),
            "--out",
            str(out_dir),
            "--abs-tol",
            "0.001",
            "--rel-tol",
            "1e-5",
        ]
    )

    assert returncode == 0
    assert json.loads((out_dir / "comparison.json").read_text(encoding="utf-8"))["ok"] is True
    markdown = (out_dir / "comparison.zh-CN.md").read_text(encoding="utf-8")
    assert "结论：通过" in markdown
    assert "输入 SHA-256" in markdown


def test_measure_step_cli_maps_only_to_fixed_reviewed_journal(tmp_path: Path, monkeypatch) -> None:
    step = tmp_path / "one.step"
    step.write_bytes(b"ISO-10303-21;")
    measurement = tmp_path / "nx" / "measurement.json"
    captured: dict[str, object] = {}

    def fake_execute(journal_path, **kwargs):
        captured["journal_path"] = journal_path
        captured.update(kwargs)
        return {"ok": True, "status": "completed"}

    monkeypatch.setattr(nx_runtime_cli, "execute_nx_journal", fake_execute)
    args = nx_runtime_cli.build_parser().parse_args(
        [
            "measure-step",
            "--step",
            str(step),
            "--measurement-out",
            str(measurement),
            "--timeout",
            "45",
        ]
    )

    assert nx_runtime_cli.execute(args)["ok"] is True
    journal = Path(captured["journal_path"])
    assert journal == HARNESS_ROOT / "nx_journals" / "abc_step_measure.py"
    assert captured["allowed_roots"] == [journal.parent]
    assert captured["arguments"] == [str(step.resolve()), str(measurement.resolve())]
    assert captured["timeout_seconds"] == 45.0


def test_nx_importer_uses_step_to_ug_definition_not_export_definition(tmp_path: Path) -> None:
    settings_dir = tmp_path / "step214ug"
    settings_dir.mkdir()
    import_settings = settings_dir / "step214ug.def"
    import_settings.write_text("CHOOSE_DIRECTION = STEP to UG\n", encoding="utf-8")
    (settings_dir / "ugstep214.def").write_text("CHOOSE_DIRECTION = UG to STEP\n", encoding="utf-8")

    class ImportToOption:
        WorkPart = "work_part"

    class NxOpen:
        pass

    class Step214Importer:
        pass

    Step214Importer.ImportToOption = ImportToOption
    NxOpen.Step214Importer = Step214Importer

    class Session:
        @staticmethod
        def GetEnvironmentVariableValue(name: str) -> str:
            return str(settings_dir) if name == "STEP214UG_DIR" else ""

    class Importer:
        ProcessHoldFlag = False
        SettingsFile = ""

        class ObjectTypesValue:
            Curves = False
            Surfaces = False
            Solids = False
            PmiData = False

        ObjectTypes = ObjectTypesValue()

    importer = Importer()
    step = tmp_path / "input.step"
    work_part = type("WorkPart", (), {"FullPath": str(tmp_path / "temporary.prt")})()
    _configure_importer(NxOpen, Session(), importer, work_part, step)

    assert importer.InputFile == str(step)
    assert importer.OutputFile == work_part.FullPath
    assert importer.ImportTo == "work_part"
    assert importer.ProcessHoldFlag is True
    assert importer.SettingsFile == str(import_settings)
    assert Path(importer.SettingsFile).name != "ugstep214.def"
    assert importer.SimplifyGeometry is True
    assert importer.LayerDefault == 1
    assert importer.ObjectTypes.Curves is True
    assert importer.ObjectTypes.Surfaces is True
    assert importer.ObjectTypes.Solids is True
    assert importer.ObjectTypes.PmiData is True


def test_nx_assembly_bodies_are_collected_per_occurrence_after_full_load() -> None:
    class LoadStatus:
        def __init__(self) -> None:
            self.disposed = False

        def Dispose(self) -> None:
            self.disposed = True

    class Part:
        def __init__(self, tag: int, bodies: list[str]) -> None:
            self.Tag = tag
            self.Bodies: list[str] = []
            self._full_bodies = bodies
            self.load_calls = 0
            self.load_statuses: list[LoadStatus] = []

        def LoadThisPartFully(self) -> LoadStatus:
            self.load_calls += 1
            self.Bodies = list(self._full_bodies)
            status = LoadStatus()
            self.load_statuses.append(status)
            return status

    class Component:
        def __init__(self, tag: int, prototype=None, children=None) -> None:
            self.Tag = tag
            self.Prototype = prototype
            self._children = list(children or [])

        def GetChildren(self):
            return list(self._children)

    shared = Part(11, ["shared"])
    subassembly = Part(12, ["subassembly"])
    nested = Part(13, ["nested"])
    nested_occurrence = Component(104, nested)
    root_component = Component(
        100,
        children=[
            Component(101, shared),
            Component(102, shared),
            Component(103, subassembly, [nested_occurrence]),
        ],
    )

    class WorkPart:
        Tag = 10
        Bodies = ["root"]
        load_calls = 0
        load_status = LoadStatus()

        def LoadFully(self):
            type(self).load_calls += 1
            self.ComponentAssembly.RootComponent = root_component
            return self.load_status

    work_part = WorkPart()
    work_part.ComponentAssembly = type("Assembly", (), {"RootComponent": None})()
    session = type("Session", (), {"Parts": [work_part, shared, subassembly, nested]})()

    occurrences = _collect_body_occurrences(session, work_part, {("tag", 10)})

    assert [body for _, body in occurrences] == [
        "root",
        "shared",
        "shared",
        "subassembly",
        "nested",
    ]
    assert WorkPart.load_calls == 1
    assert WorkPart.load_status.disposed is True
    assert shared.load_calls == 1
    assert subassembly.load_calls == 1
    assert nested.load_calls == 1
    assert all(
        status.disposed
        for part in (shared, subassembly, nested)
        for status in part.load_statuses
    )


def test_nx_assembly_falls_back_to_new_session_parts_and_temp_tree_is_removed(
    tmp_path: Path,
) -> None:
    class Part:
        def __init__(self, tag: int, bodies: list[str]) -> None:
            self.Tag = tag
            self.Bodies = bodies
            self.ComponentAssembly = type("Assembly", (), {"RootComponent": None})()
            self.load_calls = 0

        def LoadFully(self) -> None:
            return None

        def LoadThisPartFully(self) -> None:
            self.load_calls += 1
            return None

    root = Part(1, [])
    preexisting = Part(2, ["unrelated"])
    translated_child = Part(3, ["translated"])
    session = type("Session", (), {"Parts": [root, preexisting, translated_child]})()

    occurrences = _collect_body_occurrences(
        session,
        root,
        {("tag", 1), ("tag", 2)},
    )

    assert [body for _, body in occurrences] == ["translated"]
    assert translated_child.load_calls == 1
    assert preexisting.load_calls == 0

    temporary_tree = tmp_path / ".sggk-nx-step-test"
    temporary_tree.mkdir()
    (temporary_tree / "Cap.prt").write_bytes(b"part")
    (temporary_tree / "step214ug.log").write_text("translator log", encoding="utf-8")
    assert _remove_temporary_tree(temporary_tree) == ""
    assert not temporary_tree.exists()
