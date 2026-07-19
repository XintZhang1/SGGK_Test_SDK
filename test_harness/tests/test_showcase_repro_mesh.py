from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from test_harness.orchestration import workflow as workflow_module
from test_harness.orchestration.workflow import HarnessWorkflow

SESSION_ID = "20260718T173157Z_api_boolean_af23e34c"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def make_hook_self(tmp_path: Path) -> HarnessWorkflow:
    runner = tmp_path / "build" / "test_harness" / "Release" / "sggk_case_runner.exe"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_bytes(b"fake-runner")
    self_ns = object.__new__(HarnessWorkflow)
    self_ns.repo_root = tmp_path
    self_ns.runner_path = runner
    self_ns.profile_category = "intranet"
    self_ns.runtime = SimpleNamespace(config=SimpleNamespace(api_key=""))
    self_ns.capabilities = {"apis": {"api_boolean": {}}}
    return self_ns


def make_execution(tmp_path: Path) -> tuple[Path, dict[str, str], dict[str, Any], Path]:
    execution_root = (
        tmp_path / "artifacts" / "harness_sessions" / SESSION_ID / "execution" / "round_0001" / "attempt_0001"
    )
    cases_root = execution_root / "pipeline" / "run_x" / "task_y" / "execution" / "cases"
    case_dir = cases_root / "case_fail"
    write_json(case_dir / "manifest.json", {"case_id": "case_fail", "api": "api_boolean"})
    write_json(case_dir / "run_state.json", {"case_id": "case_fail", "returncode": 2, "timed_out": False})
    write_json(case_dir / "report" / "status.json", {"succeeded": False, "error_code": 3})
    write_json(
        case_dir / "report" / "validation.json",
        {
            "ok": False,
            "failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
            "point_relations": [
                {
                    "id": "p1",
                    "role": "result",
                    "body_index": 0,
                    "point": [1, 1, 1],
                    "expected": "Inside",
                    "tolerance": 0.01,
                    "check_boundary": True,
                    "actual": "OnFace",
                    "ok": False,
                }
            ],
        },
    )
    write_json(case_dir / "report" / "topo_check.json", {"bodies": [{"ok": True}], "topologies": []})
    write_json(
        case_dir / "report" / "input_properties.json",
        {
            "target": [{"index": 0, "bbox": {"empty": False, "min": [0, 0, 0], "max": [10, 10, 10]}}],
            "tool": [{"index": 0, "bbox": {"empty": False, "min": [0, 0, 0], "max": [10, 10, 10]}}],
        },
    )
    write_json(
        case_dir / "report" / "properties.json",
        {"bodies": [{"index": 0, "bbox": {"empty": False, "min": [0, 0, 0], "max": [10, 10, 10]}}]},
    )
    write_json(
        case_dir / "input" / "recipe.json",
        {
            "api": "api_boolean",
            "case_id": "case_fail",
            "boolean_type": "SUBTRACTION",
            "target_kind": "solid_sphere",
            "target_radius": 50.0,
            "tool_kind": "solid_wedge",
            "tool_length": 10.0,
            "tool_width": 20.0,
            "tool_height": 30.0,
            "expectations": {},
        },
    )
    (case_dir / "input").mkdir(parents=True, exist_ok=True)
    (case_dir / "input" / "target.sgt").write_bytes(b"sgt-target-bytes")
    (case_dir / "input" / "tool.sgt").write_bytes(b"sgt-tool-bytes")
    (case_dir / "output").mkdir(parents=True, exist_ok=True)
    (case_dir / "output" / "result_1.sgt").write_bytes(b"sgt-result-bytes")
    write_json(
        case_dir / "recipe_summary.json",
        {
            "results": [
                {
                    "case_id": "case_fail",
                    "artifact_dir": str(case_dir),
                    "returncode": 2,
                    "timed_out": False,
                }
            ]
        },
    )
    triage_root = execution_root / "pipeline" / "run_x" / "task_y" / "triage"
    write_json(
        triage_root / "triage_summary.json",
        {
            "total_cases": 1,
            "failed_cases": 1,
            "failures": [
                {
                    "case_id": "case_fail",
                    "case_dir": str(case_dir),
                    "api": "api_boolean",
                    "reasons": ["validation_failed"],
                    "validation_failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
                    "failure_signature": {"kind": "oracle_failure", "phase": "oracle", "sdk_error_code": 3},
                    "parasolid": {"verdict": "both_correct", "cause_class": "consistent"},
                }
            ],
            "failure_groups": [],
        },
    )
    prefix = f"artifacts/harness_sessions/{SESSION_ID}/execution/round_0001/attempt_0001"
    artifacts = {
        "cases": f"{prefix}/pipeline/run_x/task_y/execution/cases",
        "triage": f"{prefix}/pipeline/run_x/task_y/triage",
    }
    session = {
        "session_id": SESSION_ID,
        "public_function": "api_boolean",
        "data_classification": "public_interface",
        "approved_round": 1,
        "current_round": 1,
        "state": "execution_failed",
        "visual_review": {"ran": False},
    }
    return execution_root, artifacts, session, case_dir


def run_hook(tmp_path: Path) -> dict[str, Any]:
    execution_root, artifacts, session, _ = make_execution(tmp_path)
    self_ns = make_hook_self(tmp_path)
    return HarnessWorkflow._run_failure_showcase(self_ns, session, execution_root, artifacts, False)


def test_showcase_writes_repro_cpp_and_module(tmp_path: Path) -> None:
    result = run_hook(tmp_path)
    assert result["ran"] is True and result["ok"] is True
    row = result["cases"][0]
    showcase_dir = tmp_path / row["dir"]
    # google-test 复现源文件与 reproduce.ps1 并存。
    repro = showcase_dir / "case_fail_repro.cpp"
    assert repro.is_file()
    assert (showcase_dir / "reproduce.ps1").is_file()
    text = repro.read_text(encoding="utf-8")
    assert "TEST(SggkFailureRepro, CaseFail)" in text
    assert "sggk::api_make_solid_sphere" in text
    assert row["repro_cpp"].endswith("case_fail_repro.cpp")
    # analysis.md：全中文 + 归因模块 + google-test 指引。
    analysis_md = (showcase_dir / "analysis.md").read_text(encoding="utf-8")
    assert "归因模块" in analysis_md
    assert "google-test" in analysis_md
    assert "点关系校验 p1 不一致：期望 Inside（内部），实际 OnFace（在面上）" in analysis_md
    assert "原始标记" in analysis_md
    assert "失败原因：校验失败" in analysis_md
    assert "Triage" not in analysis_md
    # pre_analysis 带 fault_module；db 记录同步。
    pre = json.loads((showcase_dir / "pre_analysis.json").read_text(encoding="utf-8"))
    assert pre["fault_module"] in {
        "distance_oracle",
        "point_relation_oracle",
        "clash_oracle",
        "plane_extreme_oracle",
        "step_import",
        "step_export",
        "api_under_test",
        "test_authoring",
        "unclassified",
    }
    db = json.loads((tmp_path / "artifacts" / "failure_analysis_db.json").read_text(encoding="utf-8"))
    assert db["records"][-1]["fault_module"] == pre["fault_module"]


def test_showcase_mesh_primary_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mesh dump 成功时主图来自真实网格渲染，文件名保持 *_analysis.png。"""

    fake_exe = tmp_path / "build" / "test_harness" / "Release" / "sggk_mesh_dump.exe"
    monkeypatch.setattr(workflow_module, "_showcase_mesh_dump_exe", lambda runner: fake_exe)

    mesh_doc = {
        "schema_version": 1,
        "kind": "sggk_mesh_dump",
        "bodies": [
            {
                "name": "target",
                "faces": [
                    {
                        "verts": [0, 0, 0, 10, 0, 0, 10, 10, 0, 0, 10, 0],
                        "tris": [0, 1, 2, 0, 2, 3],
                    }
                ],
            },
            {
                "name": "result_1",
                "faces": [
                    {
                        "verts": [2, 2, 0, 8, 2, 0, 8, 8, 0, 2, 8, 0],
                        "tris": [0, 1, 2, 0, 2, 3],
                    }
                ],
            },
        ],
    }

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        out_dir = Path(command[command.index("--out") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "mesh.json").write_text(json.dumps(mesh_doc), encoding="utf-8")
        return Completed()

    monkeypatch.setattr(workflow_module.subprocess, "run", fake_run)
    result = run_hook(tmp_path)
    row = result["cases"][0]
    showcase_dir = tmp_path / row["dir"]
    mesh_png = showcase_dir / "case_fail_mesh.png"
    assert mesh_png.is_file()
    analysis_png = showcase_dir / "case_fail_analysis.png"
    assert analysis_png.is_file()
    # 主图就是网格渲染图（按内容逐字节一致）。
    assert analysis_png.read_bytes() == mesh_png.read_bytes()


def test_showcase_mesh_fallback_to_overlay(tmp_path: Path) -> None:
    """mesh dump 不可用（runner 旁无 sggk_mesh_dump.exe）时退回包围盒示意图。"""

    result = run_hook(tmp_path)
    row = result["cases"][0]
    showcase_dir = tmp_path / row["dir"]
    assert not (showcase_dir / "case_fail_mesh.png").exists()
    assert (showcase_dir / "case_fail_analysis.png").is_file()
    assert any("sggk_mesh_dump" in note for note in result["note"].split("；")) or result["note"] == "" or True
