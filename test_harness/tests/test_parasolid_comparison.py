from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
