from __future__ import annotations

import json
from pathlib import Path

from classify_parasolid_divergence import (
    analyze_compare_root,
    attention_case_entries,
    classify_comparison,
)
from classify_parasolid_divergence import main as classify_main
from export_failure_bundles import copy_bundle_files
from triage_artifacts import classify_case

from test_harness.investigation.tool_registry import InvestigationToolRegistry
from test_harness.tests.test_bug_investigation import make_bundle


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _sggk(**overrides):
    base = {
        "api_ok": True,
        "result_body_count": 1,
        "self_measurement_ok": True,
        "self_total_area": 120.0,
        "self_total_abs_volume": 40.0,
        "validation_ok": True,
    }
    base.update(overrides)
    return base


def _parasolid(**overrides):
    base = {
        "status": "ok",
        "import_ok": True,
        "boolean_ok": True,
        "operation": "SUBTRACTION",
        "error_message": "",
        "measurement_ok": True,
        "body_count": 1,
        "total_area": 120.0,
        "total_abs_volume": 40.0,
        "all_solid_closed": True,
        "bodies": [],
    }
    base.update(overrides)
    return base


def _sggk_result(**overrides):
    base = {
        "available": True,
        "import_ok": True,
        "measurement_ok": True,
        "body_count": 1,
        "total_area": 120.0,
        "total_abs_volume": 40.0,
        "all_solid_closed": True,
        "bodies": [
            {
                "is_solid": True,
                "closed": True,
                "free_edge_count": 0,
                "measurement_ok": True,
                "area": 120.0,
                "abs_volume": 40.0,
            }
        ],
    }
    base.update(overrides)
    return base


def _signals(**overrides):
    base = {
        "sggk_valid": True,
        "parasolid_available": True,
        "parasolid_valid": True,
        "sggk_result_measurable": True,
        "measurements_agree": True,
    }
    base.update(overrides)
    return base


def _comparison(verdict: str, *, signals=None, sggk=None, parasolid=None, sggk_result=None, checks=None) -> dict:
    return {
        "schema_version": 1,
        "kind": "nx_sggk_boolean_comparison",
        "verdict": verdict,
        "reasons": ["fixture reason"],
        "tolerances": {
            "abs_tol": 0.01,
            "rel_tol": 1e-5,
            "formula": "abs(a-b) <= abs_tol + rel_tol * max(abs(a), abs(b))",
        },
        "sggk": _sggk(**(sggk or {})),
        "parasolid": _parasolid(**(parasolid or {})),
        "sggk_result_nx": _sggk_result(**(sggk_result or {})),
        "signals": _signals(**(signals or {})),
        "checks": checks or {},
    }


def _write_comparison(case_dir: Path, comparison: dict) -> None:
    _write_json(case_dir / "comparison" / "comparison.json", comparison)


def test_classify_both_correct_is_consistent() -> None:
    entry = classify_comparison("case_a", _comparison("both_correct"))
    assert entry["cause_class"] == "consistent"
    assert entry["verdict"] == "both_correct"
    assert entry["evidence"]["sggk_api_ok"] is True


def test_classify_parasolid_import_limited() -> None:
    comparison = _comparison(
        "inconclusive",
        signals={
            "parasolid_available": False,
            "sggk_valid": False,
            "parasolid_valid": False,
            "sggk_result_measurable": False,
            "measurements_agree": False,
        },
        parasolid={"import_ok": False, "boolean_ok": False},
    )
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "parasolid_import_limited"
    assert entry["evidence"]["parasolid_import_ok"] is False


def test_classify_transport_export_suspect() -> None:
    comparison = _comparison(
        "inconclusive",
        signals={"sggk_result_measurable": False, "sggk_valid": False, "measurements_agree": False},
        sggk_result={"available": True, "import_ok": False},
    )
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "transport_export_suspect"


def test_classify_non_closed_result_via_free_edges() -> None:
    comparison = _comparison(
        "parasolid_correct",
        signals={"sggk_valid": False, "measurements_agree": False},
        sggk_result={
            "all_solid_closed": False,
            "bodies": [
                {
                    "is_solid": True,
                    "closed": False,
                    "free_edge_count": 3,
                    "measurement_ok": True,
                    "area": 100.0,
                    "abs_volume": 30.0,
                }
            ],
        },
    )
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "non_closed_result"
    assert entry["evidence"]["sggk_result_nx_free_edges"] == 3


def test_classify_non_closed_result_via_parasolid_side() -> None:
    comparison = _comparison(
        "sggk_correct",
        signals={"parasolid_valid": False, "measurements_agree": False},
        parasolid={"boolean_ok": True, "all_solid_closed": False},
    )
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "non_closed_result"
    assert entry["evidence"]["parasolid_all_solid_closed"] is False


def test_classify_body_count_mismatch() -> None:
    comparison = _comparison(
        "inconclusive",
        signals={"measurements_agree": False},
        sggk_result={"body_count": 2},
        checks={
            "body_count_agree": {"ok": False, "sggk": 2, "parasolid": 1},
            "volume_agree": {"ok": True, "a": 40.0, "b": 40.0, "abs_delta": 0.0, "tolerance": 0.01},
            "area_agree": {"ok": True, "a": 120.0, "b": 120.0, "abs_delta": 0.0, "tolerance": 0.01},
        },
    )
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "body_count_mismatch"
    assert entry["evidence"]["body_count_sggk"] == 2
    assert entry["evidence"]["body_count_parasolid"] == 1


def test_classify_volume_drift() -> None:
    comparison = _comparison(
        "inconclusive",
        signals={"measurements_agree": False},
        checks={
            "volume_agree": {"ok": False, "a": 40.0, "b": 43.5, "abs_delta": 3.5, "tolerance": 0.01},
        },
    )
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "volume_drift"
    assert entry["evidence"]["volume_abs_delta"] == 3.5


def test_classify_area_drift() -> None:
    comparison = _comparison(
        "inconclusive",
        signals={"measurements_agree": False},
        checks={
            "volume_agree": {"ok": True, "a": 40.0, "b": 40.0, "abs_delta": 0.0, "tolerance": 0.01},
            "area_agree": {"ok": False, "a": 120.0, "b": 125.0, "abs_delta": 5.0, "tolerance": 0.01},
        },
    )
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "area_drift"
    assert entry["evidence"]["area_abs_delta"] == 5.0


def test_classify_divergent_closed_geometry() -> None:
    comparison = _comparison("inconclusive", signals={"measurements_agree": False})
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "divergent_closed_geometry"


def test_classify_measurement_unavailable_for_missing_or_unknown_verdict() -> None:
    assert classify_comparison("case_a", {})["cause_class"] == "measurement_unavailable"
    comparison = _comparison("surprising_verdict")
    assert classify_comparison("case_a", comparison)["cause_class"] == "measurement_unavailable"


def test_classify_measurement_unavailable_for_failed_measurement() -> None:
    comparison = _comparison(
        "inconclusive",
        signals={"parasolid_valid": False, "measurements_agree": False},
        parasolid={"measurement_ok": False},
    )
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "measurement_unavailable"


def test_classify_unclassified_divergence() -> None:
    comparison = _comparison(
        "both_wrong",
        signals={
            "sggk_valid": False,
            "parasolid_valid": False,
            "sggk_result_measurable": False,
            "measurements_agree": False,
        },
        sggk={"api_ok": False, "result_body_count": 0},
        parasolid={
            "boolean_ok": False,
            "body_count": 0,
            "all_solid_closed": False,
            "total_area": 0.0,
            "total_abs_volume": 0.0,
        },
        sggk_result={"available": False, "import_ok": False, "measurement_ok": False, "body_count": 0, "bodies": []},
    )
    entry = classify_comparison("case_a", comparison)
    assert entry["cause_class"] == "unclassified_divergence"


def test_cli_writes_analysis_and_tolerates_missing_and_corrupt(tmp_path: Path, capsys) -> None:
    compare_root = tmp_path / "compare"
    out_root = tmp_path / "out"
    _write_comparison(compare_root / "case_ok", _comparison("both_correct"))
    _write_comparison(
        compare_root / "case_drift",
        _comparison(
            "inconclusive",
            signals={"measurements_agree": False},
            checks={"volume_agree": {"ok": False, "a": 40.0, "b": 43.5, "abs_delta": 3.5, "tolerance": 0.01}},
        ),
    )
    (compare_root / "case_missing" / "comparison").mkdir(parents=True)
    corrupt_dir = compare_root / "case_corrupt" / "comparison"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "comparison.json").write_text("{ not json", encoding="utf-8")

    returncode = classify_main(["--compare-root", str(compare_root), "--out", str(out_root)])

    assert returncode == 0
    stdout = capsys.readouterr().out
    assert "cases=4 attention=3" in stdout
    analysis = json.loads((out_root / "parasolid_analysis.json").read_text(encoding="utf-8"))
    assert analysis["schema_version"] == 1
    assert analysis["case_count"] == 4
    by_id = {entry["case_id"]: entry for entry in analysis["cases"]}
    assert by_id["case_ok"]["cause_class"] == "consistent"
    assert by_id["case_drift"]["cause_class"] == "volume_drift"
    assert by_id["case_missing"]["cause_class"] == "measurement_unavailable"
    assert by_id["case_corrupt"]["cause_class"] == "measurement_unavailable"
    assert analysis["verdict_counts"]["both_correct"] == 1
    assert analysis["cause_class_counts"]["volume_drift"] == 1
    assert analysis["cause_class_counts"]["measurement_unavailable"] == 2
    attention_ids = {entry["case_id"] for entry in analysis["attention_cases"]}
    assert attention_ids == {"case_drift", "case_missing", "case_corrupt"}
    for entry in analysis["cases"]:
        assert len(entry["reasons"]) <= 4
        assert all(len(reason) <= 120 for reason in entry["reasons"])


def test_cli_handles_empty_compare_root(tmp_path: Path) -> None:
    out_root = tmp_path / "out"
    returncode = classify_main(["--compare-root", str(tmp_path / "missing"), "--out", str(out_root)])
    assert returncode == 0
    analysis = json.loads((out_root / "parasolid_analysis.json").read_text(encoding="utf-8"))
    assert analysis["case_count"] == 0
    assert analysis["attention_cases"] == []


def test_attention_case_entries_filters_consistent(tmp_path: Path) -> None:
    compare_root = tmp_path / "compare"
    _write_comparison(compare_root / "case_ok", _comparison("both_correct"))
    _write_comparison(
        compare_root / "case_area",
        _comparison(
            "inconclusive",
            signals={"measurements_agree": False},
            checks={"area_agree": {"ok": False, "a": 120.0, "b": 130.0, "abs_delta": 10.0, "tolerance": 0.01}},
        ),
    )
    analysis = analyze_compare_root(compare_root)
    assert analysis["case_count"] == 2
    entries = attention_case_entries(compare_root)
    assert [entry["case_id"] for entry in entries] == ["case_area"]
    assert entries[0]["cause_class"] == "area_drift"


def _make_triage_case(case_dir: Path, comparison: dict | None) -> None:
    report = case_dir / "report"
    report.mkdir(parents=True)
    _write_json(report / "status.json", {"succeeded": True, "error_code": 0})
    _write_json(report / "validation.json", {"ok": True})
    if comparison is not None:
        _write_comparison(case_dir, comparison)


def test_triage_enriches_case_with_parasolid_metadata(tmp_path: Path) -> None:
    case_dir = tmp_path / "case_a"
    _make_triage_case(case_dir, _comparison("both_correct"))
    entry = classify_case(case_dir, None, 1, 1)
    assert entry["parasolid"] == {"verdict": "both_correct", "cause_class": "consistent"}
    assert entry["failed"] is False
    assert "failure_signature" not in entry


def test_triage_enriches_divergence_without_touching_failure_semantics(tmp_path: Path) -> None:
    case_dir = tmp_path / "case_b"
    _make_triage_case(
        case_dir,
        _comparison(
            "inconclusive",
            signals={"measurements_agree": False},
            checks={"volume_agree": {"ok": False, "a": 40.0, "b": 43.5, "abs_delta": 3.5, "tolerance": 0.01}},
        ),
    )
    entry = classify_case(case_dir, None, 1, 1)
    assert entry["parasolid"]["cause_class"] == "volume_drift"
    assert entry["failed"] is False
    assert entry["reasons"] == []


def test_triage_omits_parasolid_when_no_comparison(tmp_path: Path) -> None:
    case_dir = tmp_path / "case_c"
    _make_triage_case(case_dir, None)
    entry = classify_case(case_dir, None, 1, 1)
    assert "parasolid" not in entry


def test_failure_bundle_copies_comparison_evidence(tmp_path: Path) -> None:
    case_dir = tmp_path / "case_x"
    (case_dir / "report").mkdir(parents=True)
    _write_json(case_dir / "report" / "status.json", {"succeeded": False, "error_code": 7})
    _write_comparison(case_dir, _comparison("both_correct"))
    comparison_dir = case_dir / "comparison"
    _write_json(comparison_dir / "nx_boolean_measurement.json", {"measurement": {"ok": True}})
    (comparison_dir / "huge.json").write_bytes(b" " * (1024 * 1024 + 1))
    (comparison_dir / "notes.txt").write_text("not json evidence", encoding="utf-8")
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()

    copied = copy_bundle_files(
        bundle_dir,
        {"representative_case_id": "case_x", "representative_case_dir": str(case_dir)},
        {"case_id": "case_x", "case_dir": str(case_dir)},
        {},
        {},
        {},
        {},
        [],
        False,
    )

    comparison_copies = copied.get("comparison", {})
    assert set(comparison_copies) == {"comparison.json", "nx_boolean_measurement.json"}
    assert (bundle_dir / "comparison" / "comparison.json").is_file()
    assert not (bundle_dir / "comparison" / "huge.json").exists()
    assert not (bundle_dir / "comparison" / "notes.txt").exists()


def _bundle_with_comparison(tmp_path: Path) -> dict:
    bundle = make_bundle(tmp_path)
    bundle_dir = Path(bundle["bundle_dir"])
    _write_comparison(
        bundle_dir,
        _comparison(
            "inconclusive",
            signals={"measurements_agree": False},
            checks={"volume_agree": {"ok": False, "a": 40.0, "b": 42.0, "abs_delta": 2.0, "tolerance": 0.01}},
        ),
    )
    manifest_path = Path(bundle["bundle_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["copied"]["comparison"] = {"comparison.json": str(bundle_dir / "comparison" / "comparison.json")}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_investigation_tool_reads_copied_comparison(tmp_path: Path) -> None:
    bundle = _bundle_with_comparison(tmp_path)
    registry = InvestigationToolRegistry(bundle_record=bundle, source_roots=[], allow_source_content=False)
    assert "comparison.get_verdict" in registry.tool_ids

    result = registry.execute("comparison.get_verdict", {})

    assert result["ok"]
    payload = result["result"]
    assert payload["available"] is True
    assert payload["verdict"] == "inconclusive"
    assert payload["cause_class"] == "volume_drift"
    assert payload["signals"]["measurements_agree"] is False
    assert payload["tolerances"]["abs_tol"] == 0.01
    assert payload["sggk"]["api_ok"] is True
    assert payload["parasolid"]["body_count"] == 1
    assert payload["sggk_result_nx"]["free_edge_total"] == 0
    reports = registry.execute("artifact.list_reports", {})
    assert "comparison_comparison_json" in reports["result"]["report_ids"]


def test_investigation_tool_reports_unavailable_without_comparison(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    registry = InvestigationToolRegistry(bundle_record=bundle, source_roots=[], allow_source_content=False)

    result = registry.execute("comparison.get_verdict", {})

    assert result["ok"]
    assert result["result"]["available"] is False
