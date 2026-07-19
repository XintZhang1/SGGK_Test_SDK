from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import classify_parasolid_divergence  # noqa: E402

from test_harness.orchestration.workflow import HarnessWorkflow  # noqa: E402
from test_harness.tests.test_harness_orchestration import make_workflow  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _make_cases(root: Path) -> Path:
    cases = root / "pipeline" / "run_x" / "task_y" / "execution" / "cases"
    for name in ("case_a", "case_b"):
        (cases / name / "input").mkdir(parents=True, exist_ok=True)
        (cases / name / "input" / "target.sgt").write_bytes(b"sgt")
        (cases / name / "input" / "tool.sgt").write_bytes(b"sgt")
    return cases


def test_parasolid_comparison_skipped_without_nx_root(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    workflow.nx_root = None
    result = workflow._run_parasolid_comparison(tmp_path, {"cases": "pipeline/cases"})
    assert result["ran"] is False


def test_parasolid_comparison_skipped_without_cases(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    workflow.nx_root = tmp_path / "nx"
    result = workflow._run_parasolid_comparison(tmp_path, {})
    assert result["ran"] is False


def test_parasolid_comparison_summarizes_batch(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    nx_root = tmp_path / "nx"
    nx_root.mkdir()
    workflow.nx_root = nx_root
    workflow.runner_path = tmp_path / "runner.exe"
    execution_root = tmp_path / "execution"
    cases = _make_cases(execution_root)
    cases_ref = cases.relative_to(tmp_path).as_posix()

    batch = {
        "total_cases": 2,
        "verdict_counts": {"both_correct": 1, "both_wrong": 1},
        "cases": [
            {"case_id": "case_a", "verdict": "both_correct"},
            {"case_id": "case_b", "verdict": "both_wrong"},
        ],
    }

    def fake_run(command, **kwargs):  # noqa: ARG001
        out_root = Path(command[command.index("--out") + 1])
        _write_json(out_root / "batch_summary.json", batch)
        (out_root / "parasolid_comparison.zh-CN.md").write_text("# report\n", encoding="utf-8")

        class Completed:
            returncode = 0
            stderr = ""

        return Completed()

    with patch("test_harness.orchestration.workflow.subprocess.run", side_effect=fake_run):
        result = workflow._run_parasolid_comparison(execution_root, {"cases": cases_ref})

    assert result["ran"] is True
    assert result["ok"] is True
    assert result["consistent"] == 1
    assert result["attention"] == 1
    assert result["report_path"].endswith("parasolid_comparison.zh-CN.md")


def _comparison_doc(verdict: str, *, agree: bool, volume_ok: bool = True) -> dict:
    return {
        "schema_version": 1,
        "kind": "nx_sggk_boolean_comparison",
        "verdict": verdict,
        "reasons": ["fixture"],
        "tolerances": {"abs_tol": 0.01, "rel_tol": 1e-5, "formula": "abs(a-b)"},
        "sggk": {"api_ok": True, "result_body_count": 1},
        "parasolid": {
            "import_ok": True,
            "boolean_ok": True,
            "measurement_ok": True,
            "body_count": 1,
            "total_area": 120.0,
            "total_abs_volume": 40.0,
            "all_solid_closed": True,
        },
        "sggk_result_nx": {
            "available": True,
            "import_ok": True,
            "measurement_ok": True,
            "body_count": 1,
            "total_area": 120.0,
            "total_abs_volume": 40.0,
            "all_solid_closed": True,
            "bodies": [],
        },
        "signals": {
            "sggk_valid": True,
            "parasolid_available": True,
            "parasolid_valid": True,
            "sggk_result_measurable": True,
            "measurements_agree": agree,
        },
        "checks": {}
        if agree
        else {
            "volume_agree": {"ok": volume_ok, "a": 40.0, "b": 42.0, "abs_delta": 2.0, "tolerance": 0.01},
        },
    }


def test_parasolid_comparison_enriches_attention_and_copies_evidence(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    nx_root = tmp_path / "nx"
    nx_root.mkdir()
    workflow.nx_root = nx_root
    workflow.runner_path = tmp_path / "runner.exe"
    execution_root = tmp_path / "execution"
    cases = _make_cases(execution_root)
    cases_ref = cases.relative_to(tmp_path).as_posix()

    batch = {
        "total_cases": 2,
        "verdict_counts": {"both_correct": 1, "inconclusive": 1},
        "cases": [
            {"case_id": "case_a", "verdict": "both_correct"},
            {"case_id": "case_b", "verdict": "inconclusive"},
        ],
    }

    def fake_run(command, **kwargs):  # noqa: ARG001
        tool = Path(command[1]).name
        out_root = Path(command[command.index("--out") + 1])
        if tool == "run_nx_sggk_boolean_compare.py":
            _write_json(
                out_root / "case_a" / "comparison" / "comparison.json",
                _comparison_doc("both_correct", agree=True),
            )
            (out_root / "case_a" / "comparison" / "comparison.zh-CN.md").write_text("# a\n", encoding="utf-8")
            _write_json(
                out_root / "case_b" / "comparison" / "comparison.json",
                _comparison_doc("inconclusive", agree=False, volume_ok=False),
            )
            _write_json(out_root / "batch_summary.json", batch)
            (out_root / "parasolid_comparison.zh-CN.md").write_text("# report\n", encoding="utf-8")
        elif tool == "classify_parasolid_divergence.py":
            classify_parasolid_divergence.main(["--compare-root", str(out_root), "--out", str(out_root)])

        class Completed:
            returncode = 0
            stderr = ""

        return Completed()

    with patch("test_harness.orchestration.workflow.subprocess.run", side_effect=fake_run):
        result = workflow._run_parasolid_comparison(execution_root, {"cases": cases_ref})

    assert result["ran"] is True
    assert result["ok"] is True
    assert result["analysis_path"].endswith("parasolid_analysis.json")
    attention = result["attention_cases"]
    assert len(attention) == 1
    assert attention[0]["case_id"] == "case_b"
    assert attention[0]["verdict"] == "inconclusive"
    assert attention[0]["cause_class"] == "volume_drift"
    assert 1 <= len(attention[0]["reasons"]) <= 4
    assert all(len(reason) <= 120 for reason in attention[0]["reasons"])
    # comparison evidence copied into the executed case capsules
    assert (cases / "case_a" / "comparison" / "comparison.json").is_file()
    assert (cases / "case_a" / "comparison" / "comparison.zh-CN.md").is_file()
    assert (cases / "case_b" / "comparison" / "comparison.json").is_file()


def test_parasolid_analysis_failure_is_best_effort(tmp_path: Path) -> None:
    workflow, _runtime = make_workflow(tmp_path)
    nx_root = tmp_path / "nx"
    nx_root.mkdir()
    workflow.nx_root = nx_root
    workflow.runner_path = tmp_path / "runner.exe"
    execution_root = tmp_path / "execution"
    cases = _make_cases(execution_root)
    cases_ref = cases.relative_to(tmp_path).as_posix()

    batch = {
        "total_cases": 1,
        "verdict_counts": {"both_correct": 1},
        "cases": [{"case_id": "case_a", "verdict": "both_correct"}],
    }

    def fake_run(command, **kwargs):  # noqa: ARG001
        tool = Path(command[1]).name
        out_root = Path(command[command.index("--out") + 1])
        if tool == "run_nx_sggk_boolean_compare.py":
            _write_json(out_root / "batch_summary.json", batch)
        elif tool == "classify_parasolid_divergence.py":
            raise OSError("classifier unavailable")

        class Completed:
            returncode = 0
            stderr = ""

        return Completed()

    with patch("test_harness.orchestration.workflow.subprocess.run", side_effect=fake_run):
        result = workflow._run_parasolid_comparison(execution_root, {"cases": cases_ref})

    assert result["ran"] is True
    assert result["ok"] is True
    assert result["consistent"] == 1
    assert result["attention_cases"] == []
    assert result["analysis_path"] == ""
    assert "note" in result


def test_final_report_renders_attention_cases(tmp_path: Path) -> None:
    report = tmp_path / "final_report.zh-CN.md"
    parasolid = {
        "ran": True,
        "ok": True,
        "total": 2,
        "consistent": 1,
        "attention": 1,
        "verdict_counts": {"both_correct": 1, "inconclusive": 1},
        "report_path": "artifacts/x/parasolid_compare/parasolid_comparison.zh-CN.md",
        "analysis_path": "artifacts/x/parasolid_compare/parasolid_analysis.json",
        "attention_cases": [
            {
                "case_id": "case_b",
                "verdict": "inconclusive",
                "cause_class": "volume_drift",
                "reasons": ["体积差异超差：abs_delta=2.0 tolerance=0.01"],
            }
        ],
    }
    HarnessWorkflow._write_final_report(
        report,
        session={"public_function": "api_combine_bodies"},
        round_record={"round_number": 1},
        result={},
        task_result={"execution": {}},
        passed=True,
        parasolid=parasolid,
    )
    text = report.read_text(encoding="utf-8")
    assert "需关注用例" in text
    assert "case_b" in text
    assert "volume_drift" in text
    assert "体积差异超差" in text
    assert "差异分析" in text
    assert "与 Parasolid 一致（不用管）：`1`" in text
