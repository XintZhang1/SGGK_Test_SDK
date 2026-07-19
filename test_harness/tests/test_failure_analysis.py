from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from test_harness.authoring_gateway.client import HttpResponse, OpenAICompatibleMessageClient
from test_harness.authoring_gateway.config import load_gateway_config
from test_harness.authoring_gateway.contracts import validate_candidate
from test_harness.authoring_gateway.gateway import AuthoringGateway
from test_harness.orchestration import workflow as workflow_module
from test_harness.orchestration.workflow import HarnessWorkflow
from test_harness.tools import analyze_failure_cases, render_case_preview, run_visual_review
from test_harness.ui.state import failure_analysis, read_artifact, session_snapshot

REPO_ROOT = Path(__file__).resolve().parents[2]
API_KEY = "test-api-key-never-persist"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def bbox(mn: tuple[float, float, float], mx: tuple[float, float, float]) -> dict[str, Any]:
    return {"empty": False, "min": list(mn), "max": list(mx)}


def input_props(
    target: tuple = ((0, 0, 0), (10, 10, 10)),
    tool: tuple = ((0, 0, 0), (10, 10, 10)),
) -> dict[str, Any]:
    return {
        "target": [{"index": 0, "bbox": bbox(*target)}],
        "tool": [{"index": 0, "bbox": bbox(*tool)}],
    }


def result_props(box: tuple = ((0, 0, 0), (10, 10, 10))) -> dict[str, Any]:
    return {"bodies": [{"index": 0, "bbox": bbox(*box)}]}


def make_case_capsule(
    cases_root: Path,
    case_id: str,
    *,
    validation: dict[str, Any] | None = None,
    topo_check: dict[str, Any] | None = None,
    input_properties: dict[str, Any] | None = None,
    properties: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
    recipe: dict[str, Any] | None = None,
    returncode: int = 2,
    timed_out: bool = False,
    runner_path: str = "",
    with_files: bool = True,
) -> Path:
    case_dir = cases_root / case_id
    write_json(case_dir / "manifest.json", {"case_id": case_id, "api": "api_boolean"})
    write_json(
        case_dir / "run_state.json",
        {
            "case_id": case_id,
            "api": "api_boolean",
            "phase": "completed",
            "last_phase": "oracle",
            "returncode": returncode,
            "timed_out": timed_out,
            "runner_path": runner_path,
        },
    )
    write_json(
        case_dir / "report" / "status.json",
        {
            "succeeded": returncode == 0,
            "error_code": 0 if returncode == 0 else 3,
            "error_message": "" if returncode == 0 else "boom",
        },
    )
    write_json(
        case_dir / "report" / "validation.json",
        validation if validation is not None else {"ok": returncode == 0, "failures": []},
    )
    write_json(
        case_dir / "report" / "topo_check.json",
        topo_check if topo_check is not None else {"bodies": [], "topologies": []},
    )
    if input_properties is not None:
        write_json(case_dir / "report" / "input_properties.json", input_properties)
    if properties is not None:
        write_json(case_dir / "report" / "properties.json", properties)
    if with_files:
        write_json(
            case_dir / "input" / "recipe.json",
            recipe if recipe is not None else {"api": "api_boolean", "case_id": case_id, "expectations": {}},
        )
        (case_dir / "input").mkdir(parents=True, exist_ok=True)
        (case_dir / "input" / "target.sgt").write_bytes(b"sgt-target-bytes")
        (case_dir / "input" / "tool.sgt").write_bytes(b"sgt-tool-bytes")
        (case_dir / "output").mkdir(parents=True, exist_ok=True)
        (case_dir / "output" / "result.sgt").write_bytes(b"sgt-result-bytes")
    if comparison is not None:
        write_json(case_dir / "comparison" / "comparison.json", comparison)
    return case_dir


def analyze_one(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    case_dir = make_case_capsule(tmp_path / "cases", "case_x", **kwargs)
    return analyze_failure_cases.analyze_case(case_dir)


# ---------------------------------------------------------------------------
# Deterministic fault-domain rules
# ---------------------------------------------------------------------------


def test_point_relation_outside_bbox_is_test_expectation(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        validation={
            "ok": False,
            "failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
            "point_relations": [
                {
                    "id": "p1",
                    "role": "result",
                    "body_index": 0,
                    "point": [500, 500, 500],
                    "expected": "Inside",
                    "actual": "OnFace",
                    "ok": False,
                }
            ],
        },
        input_properties=input_props(),
        properties=result_props(),
    )

    assert result["fault_domain"] == "test_expectation_suspect"
    assert result["confidence"] == 0.9
    assert any("rule=point_relation_outside_bbox" in item for item in result["evidence"])
    assert "不构成 SDK 缺陷定论" in result["notes"]


def test_point_relation_inside_bbox_falls_through(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        validation={
            "ok": False,
            "failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
            "point_relations": [
                {"id": "p1", "point": [5, 5, 5], "expected": "Inside", "actual": "OnFace", "ok": False}
            ],
        },
        input_properties=input_props(),
        properties=result_props(),
    )

    assert result["fault_domain"] == "geometry_result_suspect"


def test_distance_contact_vs_bbox_gap_is_test_expectation(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        validation={
            "ok": False,
            "failures": ["distance_check_d1_above_max actual=90 max=0"],
            "distance_checks": [
                {
                    "id": "d1",
                    "role_a": "target",
                    "role_b": "tool",
                    "kind": "minimum",
                    "distance": {"min_set": True, "min": 0.0},
                    "ok": False,
                }
            ],
        },
        input_properties=input_props(target=((0, 0, 0), (10, 10, 10)), tool=((100, 100, 100), (110, 110, 110))),
    )

    assert result["fault_domain"] == "test_expectation_suspect"
    assert any("rule=distance_contact_vs_bbox_gap" in item for item in result["evidence"])


def test_distance_separation_vs_bbox_overlap_is_test_expectation(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        validation={
            "ok": False,
            "failures": ["distance_check_d1_below_min actual=0 min=5"],
            "distance_checks": [
                {
                    "id": "d1",
                    "role_a": "target",
                    "role_b": "tool",
                    "kind": "minimum",
                    "distance": {"min_set": True, "min": 5.0},
                    "ok": False,
                }
            ],
        },
        input_properties=input_props(),
    )

    assert result["fault_domain"] == "test_expectation_suspect"
    assert any("rule=distance_separation_vs_bbox_overlap" in item for item in result["evidence"])


def test_disjoint_union_body_count_is_test_expectation(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        recipe={
            "api": "api_boolean",
            "case_id": "case_x",
            "boolean_type": "UNITE",
            "expectations": {"result_bodies": {"max": 1}},
        },
        validation={
            "ok": False,
            "failures": ["result_body_count_above_max actual=2 max=1"],
        },
        input_properties=input_props(target=((0, 0, 0), (10, 10, 10)), tool=((100, 0, 0), (110, 10, 10))),
    )

    assert result["fault_domain"] == "test_expectation_suspect"
    assert result["confidence"] == 0.95
    assert any("rule=disjoint_union_body_count" in item for item in result["evidence"])


def test_union_with_overlapping_bodies_not_flagged(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        recipe={
            "api": "api_boolean",
            "case_id": "case_x",
            "boolean_type": "UNITE",
            "expectations": {"result_bodies": {"max": 1}},
        },
        validation={"ok": False, "failures": ["result_body_count_above_max actual=2 max=1"]},
        input_properties=input_props(),
    )

    assert result["fault_domain"] == "geometry_result_suspect"


def test_transport_export_suspect_cause_class(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        validation={"ok": False, "failures": ["total_volume_above_max actual=1 max=0"]},
        comparison={
            "verdict": "sggk_correct",
            "signals": {"parasolid_available": True, "sggk_result_measurable": False},
            "sggk": {"api_ok": True, "result_body_count": 1},
            "parasolid": {"import_ok": True, "boolean_ok": True, "measurement_ok": True},
            "sggk_result_nx": {"available": False, "import_ok": False},
            "checks": {},
        },
    )

    assert result["fault_domain"] == "transport_suspect"
    assert any("rule=transport_export_suspect" in item for item in result["evidence"])


def test_transport_self_vs_nx_measurement_drift(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        validation={"ok": False, "failures": ["total_volume_above_max actual=1 max=0"]},
        comparison={
            "verdict": "both_correct",
            "signals": {"parasolid_available": True, "sggk_result_measurable": True},
            "sggk": {
                "api_ok": True,
                "self_measurement_ok": True,
                "self_total_area": 100.0,
                "self_total_abs_volume": 1000.0,
            },
            "parasolid": {"import_ok": True, "measurement_ok": True, "body_count": 1, "all_solid_closed": True},
            "sggk_result_nx": {
                "available": True,
                "import_ok": True,
                "measurement_ok": True,
                "all_solid_closed": True,
                "total_area": 150.0,
                "total_abs_volume": 1000.0,
                "body_count": 1,
            },
            "tolerances": {"abs_tol": 0.01, "rel_tol": 1e-05},
            "checks": {
                "volume_agree": {"ok": True},
                "area_agree": {"ok": True},
                "body_count_agree": {"ok": True},
            },
        },
    )

    assert result["fault_domain"] == "transport_suspect"
    assert any("rule=self_vs_nx_measurement_drift" in item for item in result["evidence"])


def test_oracle_contradictory_actuals_is_tooling_suspect(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        validation={
            "ok": False,
            "failures": ["distance_check_d1_above_max actual=1.5 max=0"],
            "distance_checks": [
                {"id": "d1", "actual": 1.5, "ok": False},
                {"id": "d1", "actual": 2.5, "ok": False},
            ],
        },
    )

    assert result["fault_domain"] == "oracle_tooling_suspect"
    assert any("rule=oracle_contradictory_actuals" in item for item in result["evidence"])


def test_oracle_non_finite_actual_is_tooling_suspect(tmp_path: Path) -> None:
    case_dir = make_case_capsule(
        tmp_path / "cases",
        "case_x",
        validation={"ok": False, "failures": ["distance_check_d1_above_max"]},
    )
    (case_dir / "report" / "validation.json").write_text(
        '{"ok": false, "failures": ["distance_check_d1_above_max"],'
        ' "distance_checks": [{"id": "d1", "actual": NaN, "ok": false}]}',
        encoding="utf-8",
    )

    result = analyze_failure_cases.analyze_case(case_dir)

    assert result["fault_domain"] == "oracle_tooling_suspect"
    assert any("rule=oracle_non_finite_actual" in item for item in result["evidence"])


def test_topo_check_failure_is_geometry_suspect(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        topo_check={"bodies": [{"ok": False, "error_string": "self-intersecting body"}], "topologies": []},
        returncode=0,
    )

    assert result["fault_domain"] == "geometry_result_suspect"
    assert result["confidence"] == 0.55
    assert any("rule=topo_check_failed" in item for item in result["evidence"])


def test_generic_oracle_failure_is_geometry_suspect(tmp_path: Path) -> None:
    result = analyze_one(
        tmp_path,
        validation={"ok": False, "failures": ["total_volume_above_max actual=13.59 max=0"]},
    )

    assert result["fault_domain"] == "geometry_result_suspect"
    assert result["confidence"] == 0.4
    assert any("oracle_failure=" in item for item in result["evidence"])


def test_inconclusive_when_no_rule_matches(tmp_path: Path) -> None:
    result = analyze_one(tmp_path, validation={"ok": True, "failures": []})

    assert result["fault_domain"] == "inconclusive"
    assert result["confidence"] == 0.2


def test_corrupt_inputs_degrade_to_inconclusive(tmp_path: Path) -> None:
    case_dir = tmp_path / "cases" / "case_corrupt"
    (case_dir / "report").mkdir(parents=True)
    (case_dir / "manifest.json").write_text("{ not json", encoding="utf-8")
    (case_dir / "report" / "validation.json").write_text("{ also not json", encoding="utf-8")

    result = analyze_failure_cases.analyze_case(case_dir)

    assert result["fault_domain"] == "inconclusive"
    missing = analyze_failure_cases.analyze_case(tmp_path / "does_not_exist")
    assert missing["fault_domain"] == "inconclusive"


# ---------------------------------------------------------------------------
# Overlay rendering
# ---------------------------------------------------------------------------


def test_overlay_marks_failed_oracle_focus(tmp_path: Path) -> None:
    case_dir = make_case_capsule(
        tmp_path / "cases",
        "case_marked",
        validation={
            "ok": False,
            "failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
            "point_relations": [
                {"id": "p1", "point": [5, 5, 5], "expected": "Inside", "actual": "OnFace", "ok": False}
            ],
        },
        input_properties=input_props(),
        properties=result_props(),
    )

    overlay = tmp_path / "out" / "case_marked_analysis.png"
    plain = tmp_path / "out" / "plain.png"
    result = render_case_preview.case_analysis_overlay(case_dir, overlay)
    render_case_preview.case_preview(case_dir, plain, 80)

    assert result["marker_count"] >= 1
    assert overlay.read_bytes() != plain.read_bytes()


def test_overlay_degrades_to_plain_preview_without_coordinates(tmp_path: Path) -> None:
    case_dir = make_case_capsule(
        tmp_path / "cases",
        "case_plain",
        validation={"ok": False, "failures": ["total_volume_above_max actual=1 max=0"]},
    )

    overlay = tmp_path / "out" / "case_plain_analysis.png"
    plain = tmp_path / "out" / "plain.png"
    result = render_case_preview.case_analysis_overlay(case_dir, overlay)
    render_case_preview.case_preview(case_dir, plain, 80)

    assert result["marker_count"] == 0
    assert overlay.read_bytes() == plain.read_bytes()


# ---------------------------------------------------------------------------
# Advisory visual fault hints
# ---------------------------------------------------------------------------


def test_merge_visual_hint_never_overrides_deterministic_domain() -> None:
    base = {
        "schema_version": 1,
        "case_id": "case_a",
        "fault_domain": "geometry_result_suspect",
        "confidence": 0.55,
        "evidence": [],
        "notes": "",
    }

    disagree = analyze_failure_cases.merge_visual_hint(base, "test_expectation", "预期点似乎落在体外")
    assert disagree["fault_domain"] == "geometry_result_suspect"
    assert disagree["visual_fault_hint"] == "test_expectation"
    assert disagree["visual_disagrees"] is True
    assert "预期点" in disagree["visual_notes"]

    agree = analyze_failure_cases.merge_visual_hint(base, "geometry", "一致")
    assert agree["visual_disagrees"] is False
    assert "visual_fault_hint" not in base


def visual_contract() -> dict[str, Any]:
    return {"type": "json_object", "kind_field": "kind", "allowed_kinds": ["visual_review_report"]}


def visual_candidate(fault_hint: str | None = None) -> dict[str, Any]:
    review: dict[str, Any] = {
        "case_id": "case_a",
        "geometry_plausibility": "suspect",
        "view_consistency": "consistent",
        "misuse_flags": [],
        "confidence": 0.5,
        "notes_zh_cn": "几何存疑。",
    }
    if fault_hint is not None:
        review["fault_hint"] = fault_hint
    return {
        "kind": "visual_review_report",
        "schema_version": 1,
        "case_reviews": [review],
        "overall_notes_zh_cn": "总体说明。",
    }


def test_fault_hint_is_optional_but_enum_checked() -> None:
    assert validate_candidate(visual_candidate(), visual_contract()).ok
    assert validate_candidate(visual_candidate("transport"), visual_contract()).ok
    rejected = validate_candidate(visual_candidate("nonsense"), visual_contract())
    assert not rejected.ok
    assert "VISUAL_REVIEW_FAULT_HINT_INVALID" in {item.error_code for item in rejected.diagnostics}

    schema = json.loads(
        (REPO_ROOT / "test_harness/schemas/visual_review_report.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(visual_candidate("geometry"))) == []
    assert list(validator.iter_errors(visual_candidate("nonsense")))


class QueueTransport:
    def __init__(self, *items: HttpResponse) -> None:
        self.items = list(items)
        self.requests: list[dict[str, Any]] = []

    def post(self, **kwargs: Any) -> HttpResponse:
        self.requests.append(dict(kwargs))
        if not self.items:
            raise AssertionError("mock transport queue is empty")
        return self.items.pop(0)


def provider_response(content: str) -> HttpResponse:
    payload = {
        "id": "mock-completion",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    return HttpResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode())


def make_vision_gateway(repo: Path, transport: QueueTransport) -> AuthoringGateway:
    config = load_gateway_config("siliconflow_vision", environ={"SILICONFLOW_API_KEY": API_KEY})
    client = OpenAICompatibleMessageClient(
        config,
        transport=transport,
        sleeper=lambda _delay: None,
        random_source=lambda: 0.0,
    )
    return AuthoringGateway(config, repo_root=repo, client=client)


def make_png(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), (200, 30, 30)).save(path, format="PNG")
    return path


def test_run_fault_hint_review_collects_hints(tmp_path: Path) -> None:
    image = make_png(tmp_path / "artifacts" / "analysis" / "case_a_analysis.png")
    transport = QueueTransport(provider_response(json.dumps(visual_candidate("transport"))))
    gateway = make_vision_gateway(tmp_path, transport)

    result = run_visual_review.run_fault_hint_review(
        [("case_a", image)],
        tmp_path / "artifacts" / "visual",
        gateway=gateway,
    )

    assert result["ran"] is True and result["ok"] is True
    assert result["hints"] == {"case_a": {"fault_hint": "transport", "notes": "几何存疑。"}}
    prompt_text = transport.requests[0]["body"].decode("utf-8")
    assert "fault_hint" in prompt_text
    assert "transport" in prompt_text


def test_run_fault_hint_review_degrades_on_model_failure(tmp_path: Path) -> None:
    image = make_png(tmp_path / "artifacts" / "analysis" / "case_a_analysis.png")
    transport = QueueTransport(provider_response("not-json"), provider_response("still-not-json"))
    gateway = make_vision_gateway(tmp_path, transport)

    result = run_visual_review.run_fault_hint_review(
        [("case_a", image)],
        tmp_path / "artifacts" / "visual",
        gateway=gateway,
    )

    assert result["ok"] is False
    assert result["hints"] == {}
    assert result["note"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_writes_pre_analysis_and_summary(tmp_path: Path, capsys: Any) -> None:
    cases_root = tmp_path / "cases"
    make_case_capsule(
        cases_root,
        "case_fail",
        validation={
            "ok": False,
            "failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
            "point_relations": [
                {"id": "p1", "point": [500, 500, 500], "expected": "Inside", "actual": "OnFace", "ok": False}
            ],
        },
        input_properties=input_props(),
        properties=result_props(),
    )
    make_case_capsule(cases_root, "case_pass", returncode=0)
    out_dir = tmp_path / "pre"

    rc = analyze_failure_cases.main(["--cases-root", str(cases_root), "--out", str(out_dir)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "预分析完成" in out and "不构成 SDK 缺陷定论" in out
    summary = json.loads((out_dir / "pre_analysis_summary.json").read_text(encoding="utf-8"))
    assert summary["total_cases"] == 1
    assert summary["fault_domain_counts"] == {"test_expectation_suspect": 1}
    assert summary["cases"][0]["case_id"] == "case_fail"
    pre = json.loads((out_dir / "case_fail" / "pre_analysis.json").read_text(encoding="utf-8"))
    assert pre["fault_domain"] == "test_expectation_suspect"
    assert (out_dir / "case_fail" / "case_fail_analysis.png").is_file()


def test_cli_returns_two_for_missing_cases_root(tmp_path: Path, capsys: Any) -> None:
    rc = analyze_failure_cases.main(
        ["--cases-root", str(tmp_path / "missing"), "--out", str(tmp_path / "pre")]
    )
    assert rc == 2


def test_cli_with_visual_degrades_without_api_key(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    cases_root = tmp_path / "cases"
    make_case_capsule(
        cases_root,
        "case_fail",
        validation={"ok": False, "failures": ["total_volume_above_max actual=1 max=0"]},
    )
    out_dir = tmp_path / "pre"

    rc = analyze_failure_cases.main(
        ["--cases-root", str(cases_root), "--out", str(out_dir), "--with-visual"]
    )

    assert rc == 0
    summary = json.loads((out_dir / "pre_analysis_summary.json").read_text(encoding="utf-8"))
    assert summary["total_cases"] == 1
    assert summary["visual"]["requested"] is True
    assert summary["visual"]["note"]
    pre = json.loads((out_dir / "case_fail" / "pre_analysis.json").read_text(encoding="utf-8"))
    assert pre["fault_domain"] == "geometry_result_suspect"
    assert "visual_fault_hint" not in pre


# ---------------------------------------------------------------------------
# Workflow failure-showcase hook
# ---------------------------------------------------------------------------

SESSION_ID = "20260718T173157Z_api_boolean_af23e34c"


def make_hook_self(tmp_path: Path) -> HarnessWorkflow:
    runner = tmp_path / "build" / "test_harness" / "Release" / "sggk_case_runner.exe"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_bytes(b"fake-runner")
    # Uninitialized instance: the hook must not require __init__ side effects.
    self_ns = object.__new__(HarnessWorkflow)
    self_ns.repo_root = tmp_path
    self_ns.runner_path = runner
    self_ns.profile_category = "intranet"
    self_ns.runtime = SimpleNamespace(config=SimpleNamespace(api_key=""))
    self_ns.capabilities = {"apis": {"api_boolean": {}}}
    return self_ns


def make_execution_layout(
    tmp_path: Path,
    *,
    with_triage: bool = True,
    returncode: int = 2,
) -> tuple[Path, dict[str, str], dict[str, Any], Path]:
    execution_root = (
        tmp_path / "artifacts" / "harness_sessions" / SESSION_ID / "execution" / "round_0001" / "attempt_0001"
    )
    cases_root = execution_root / "pipeline" / "run_x" / "task_y" / "execution" / "cases"
    case_dir = make_case_capsule(
        cases_root,
        "case_fail",
        validation={
            "ok": False,
            "failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
            "point_relations": [
                {"id": "p1", "point": [500, 500, 500], "expected": "Inside", "actual": "OnFace", "ok": False}
            ],
        },
        input_properties=input_props(),
        properties=result_props(),
        comparison={
            "verdict": "both_correct",
            "signals": {"parasolid_available": True, "sggk_result_measurable": True},
            "sggk": {"api_ok": True},
            "parasolid": {"import_ok": True},
            "sggk_result_nx": {"available": True, "import_ok": True},
            "checks": {},
        },
        returncode=returncode,
    )
    write_json(
        cases_root / "recipe_summary.json",
        {
            "total": 1,
            "passed": 0 if returncode else 1,
            "failed": 1 if returncode else 0,
            "results": [
                {
                    "case_id": "case_fail",
                    "artifact_dir": str(case_dir),
                    "returncode": returncode,
                    "timed_out": False,
                }
            ],
        },
    )
    prefix = f"artifacts/harness_sessions/{SESSION_ID}/execution/round_0001/attempt_0001"
    artifacts = {"cases": f"{prefix}/pipeline/run_x/task_y/execution/cases"}
    if with_triage:
        triage_root = execution_root / "pipeline" / "run_x" / "task_y" / "triage"
        failures = (
            [
                {
                    "case_id": "case_fail",
                    "case_dir": str(case_dir),
                    "api": "api_boolean",
                    "reasons": ["validation_failed"],
                    "validation_failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
                    "failure_signature": {
                        "kind": "oracle_failure",
                        "phase": "oracle",
                        "sdk_error_code": 3,
                    },
                    "parasolid": {"verdict": "both_correct", "cause_class": "consistent"},
                }
            ]
            if returncode
            else []
        )
        write_json(
            triage_root / "triage_summary.json",
            {
                "total_cases": 1,
                "failed_cases": 1 if returncode else 0,
                "failures": failures,
                "failure_groups": [],
            },
        )
        artifacts["triage"] = f"{prefix}/pipeline/run_x/task_y/triage"
    session = {
        "session_id": SESSION_ID,
        "public_function": "api_boolean",
        "data_classification": "public_interface",
        "approved_round": 1,
        "current_round": 1,
        "state": "execution_failed" if returncode else "completed",
        "visual_review": {"ran": False},
    }
    return execution_root, artifacts, session, case_dir


def run_hook(self_ns: HarnessWorkflow, session: dict, execution_root: Path, artifacts: dict, passed: bool):
    return HarnessWorkflow._run_failure_showcase(self_ns, session, execution_root, artifacts, passed)


def test_showcase_copies_capsule_and_writes_reproduction(tmp_path: Path) -> None:
    execution_root, artifacts, session, case_dir = make_execution_layout(tmp_path)
    self_ns = make_hook_self(tmp_path)

    result = run_hook(self_ns, session, execution_root, artifacts, False)

    assert result["ran"] is True and result["ok"] is True
    assert result["root"] == "artifacts/api_boolean/round_0001_20260718T173157Z"
    assert result["db"] == "artifacts/failure_analysis_db.json"
    assert len(result["cases"]) == 1
    row = result["cases"][0]
    showcase_dir = tmp_path / row["dir"]
    assert row["dir"] == "artifacts/api_boolean/round_0001_20260718T173157Z/case_fail"
    # Capsule subset copied.
    assert (showcase_dir / "input" / "recipe.json").is_file()
    assert (showcase_dir / "input" / "target.sgt").is_file()
    assert (showcase_dir / "output" / "result.sgt").is_file()
    assert (showcase_dir / "report" / "validation.json").is_file()
    assert (showcase_dir / "run_state.json").is_file()
    assert (showcase_dir / "manifest.json").is_file()
    assert (showcase_dir / "comparison" / "comparison.json").is_file()
    # Fixed-content reproduce script uses the session runner and the copied recipe.
    reproduce = (showcase_dir / "reproduce.ps1").read_text(encoding="utf-8")
    assert str(self_ns.runner_path) in reproduce
    assert str(showcase_dir / "input" / "recipe.json") in reproduce
    assert "--out" in reproduce and "repro" in reproduce
    # Chinese analysis carries the diagnostic framing.
    analysis_md = (showcase_dir / "analysis.md").read_text(encoding="utf-8")
    assert "诊断性证据，不构成 SDK 缺陷定论" in analysis_md
    assert "确定性预分析" in analysis_md
    assert "oracle_failure" in analysis_md
    # Pre-analysis JSON (showcase copy + session mirror) is augmented for the UI.
    pre = json.loads((showcase_dir / "pre_analysis.json").read_text(encoding="utf-8"))
    assert pre["fault_domain"] == "test_expectation_suspect"
    assert pre["signature"] == {"kind": "oracle_failure", "phase": "oracle", "sdk_error_code": 3}
    assert pre["triage_reasons"] == ["validation_failed", "runner_nonzero_exit"]
    assert pre["oracle_failures"] == ["point_relation_p1_mismatch expected=Inside actual=OnFace"]
    assert pre["parasolid"] == {"verdict": "both_correct", "cause_class": "consistent"}
    mirror_pre = execution_root / "failure_analysis" / "case_fail_pre_analysis.json"
    assert json.loads(mirror_pre.read_text(encoding="utf-8")) == pre
    assert (execution_root / "failure_analysis" / "case_fail_analysis.png").is_file()
    assert row["analysis_png"].startswith("artifacts/harness_sessions/")
    # Durable db record.
    db = json.loads((tmp_path / "artifacts" / "failure_analysis_db.json").read_text(encoding="utf-8"))
    assert db["schema_version"] == 1
    assert len(db["records"]) == 1
    record = db["records"][0]
    assert record["case_id"] == "case_fail"
    assert record["fault_domain"] == "test_expectation_suspect"
    assert record["signature"]["kind"] == "oracle_failure"
    assert record["showcase_dir"] == row["dir"]
    assert record["reproduce"].endswith("reproduce.ps1")


def test_showcase_never_copies_step_and_skips_oversize_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_root, artifacts, session, case_dir = make_execution_layout(tmp_path)
    (case_dir / "output" / "result.step").write_bytes(b"step-transport")
    (case_dir / "input" / "source.stp").write_bytes(b"step-transport")
    self_ns = make_hook_self(tmp_path)
    monkeypatch.setattr(workflow_module, "FAILURE_SHOWCASE_MAX_FILE_BYTES", 40)

    result = run_hook(self_ns, session, execution_root, artifacts, False)

    showcase_dir = tmp_path / result["cases"][0]["dir"]
    copied = [path.name for path in showcase_dir.rglob("*") if path.is_file()]
    assert not any(name.endswith((".step", ".stp")) for name in copied)
    assert "target.sgt" in copied  # 16 bytes < cap
    assert "validation.json" not in copied  # larger than the patched 40-byte cap
    assert "大小限制" in result["note"]


def test_showcase_recipe_summary_fallback(tmp_path: Path) -> None:
    execution_root, artifacts, session, _case_dir = make_execution_layout(tmp_path, with_triage=False)
    self_ns = make_hook_self(tmp_path)

    result = run_hook(self_ns, session, execution_root, artifacts, False)

    assert result["ok"] is True and len(result["cases"]) == 1
    pre = json.loads((tmp_path / result["cases"][0]["dir"] / "pre_analysis.json").read_text(encoding="utf-8"))
    assert "runner_nonzero_exit" in pre["triage_reasons"]


def test_showcase_no_failures_is_note_only(tmp_path: Path) -> None:
    execution_root, artifacts, session, _case_dir = make_execution_layout(tmp_path, returncode=0)
    self_ns = make_hook_self(tmp_path)

    result = run_hook(self_ns, session, execution_root, artifacts, True)

    assert result["ran"] is True and result["ok"] is True
    assert result["cases"] == []
    assert "没有失败用例" in result["note"]


def test_showcase_never_raises_on_internal_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    execution_root, artifacts, session, _case_dir = make_execution_layout(tmp_path)
    self_ns = make_hook_self(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(workflow_module, "_copy_showcase_capsule", boom)
    result = run_hook(self_ns, session, execution_root, artifacts, False)

    assert result["ran"] is True
    assert result["ok"] is False
    assert "失败用例 showcase 生成失败" in result["note"]


def test_showcase_db_is_capped(tmp_path: Path) -> None:
    execution_root, artifacts, session, _case_dir = make_execution_layout(tmp_path)
    db_path = tmp_path / "artifacts" / "failure_analysis_db.json"
    write_json(
        db_path,
        {
            "schema_version": 1,
            "kind": "failure_analysis_db",
            "records": [{"recorded_at": "old", "case_id": f"old_{index}"} for index in range(500)],
        },
    )
    self_ns = make_hook_self(tmp_path)

    result = run_hook(self_ns, session, execution_root, artifacts, False)

    assert result["ok"] is True
    db = json.loads(db_path.read_text(encoding="utf-8"))
    assert len(db["records"]) == 500
    assert db["records"][-1]["case_id"] == "case_fail"
    assert db["records"][0]["case_id"] == "old_1"  # oldest record dropped


# ---------------------------------------------------------------------------
# Full workflow integration: hook runs on completed and execution_failed
# ---------------------------------------------------------------------------


class ShowcaseRuntime:
    def __init__(self, repo_root: Path, *, passed: bool) -> None:
        self.repo_root = repo_root
        self.campaign_dataset = ""
        self.passed = passed

    def generate(self, *, manifest_path: Path, run_id: str, staging_root: Path) -> dict[str, Any]:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        task = manifest["tasks"][0]
        output = self.repo_root / task["expected_output_path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        candidate = {
            "kind": "attack_dsl",
            "dsl": {"version": 1, "cases": [{"case_id": "round_1_nominal", "api": "api_boolean"}]},
            "notes": [],
        }
        output.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        provenance = output.with_name(f"{output.stem}.provenance.json")
        provenance.write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
        review_root = staging_root / run_id / task["task_id"] / "review"
        review_root.mkdir(parents=True, exist_ok=True)
        (review_root / "review_packet.json").write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
        (review_root / "review_report.zh-CN.md").write_text("# 固定审查报告\n", encoding="utf-8")

        def rel(path: Path) -> str:
            return path.relative_to(self.repo_root).as_posix()

        return {
            "ok": True,
            "run_id": run_id,
            "results": [
                {
                    "task_id": task["task_id"],
                    "run_id": run_id,
                    "authoring_accepted": True,
                    "review_packet_path": rel(review_root / "review_packet.json"),
                    "review_report_path": rel(review_root / "review_report.zh-CN.md"),
                }
            ],
        }

    def interpret_comment(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "decision": {
                "decision": "approve",
                "summary_zh_cn": "用户同意当前轮次。",
                "requested_changes": [],
                "constraints": [],
            },
        }

    def execute(
        self,
        *,
        manifest_path: Path,
        run_id: str,
        staging_root: Path,
        runner_path: Path | None,
    ) -> dict[str, Any]:
        del manifest_path, run_id
        assert runner_path is None
        staging_root.mkdir(parents=True, exist_ok=True)
        cases_root = staging_root / "fake_execution" / "cases"
        returncode = 0 if self.passed else 2
        case_dir = make_case_capsule(
            cases_root,
            "case_fail" if not self.passed else "case_pass",
            validation=(
                {"ok": True, "failures": []}
                if self.passed
                else {
                    "ok": False,
                    "failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
                    "point_relations": [
                        {"id": "p1", "point": [500, 500, 500], "expected": "Inside", "actual": "OnFace", "ok": False}
                    ],
                }
            ),
            input_properties=input_props(),
            properties=result_props(),
            returncode=returncode,
            runner_path=str(self.repo_root / "build" / "test_harness" / "Release" / "sggk_case_runner.exe"),
        )
        write_json(
            cases_root / "recipe_summary.json",
            {
                "total": 1,
                "passed": 1 if self.passed else 0,
                "failed": 0 if self.passed else 1,
                "results": [
                    {"case_id": case_dir.name, "artifact_dir": str(case_dir), "returncode": returncode}
                ],
            },
        )
        artifacts = {"cases": cases_root.relative_to(self.repo_root).as_posix()}
        if not self.passed:
            triage_root = staging_root / "fake_execution" / "triage"
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
                        }
                    ],
                    "failure_groups": [],
                },
            )
            artifacts["triage"] = triage_root.relative_to(self.repo_root).as_posix()
        return {
            "ok": self.passed,
            "staging_path": staging_root.relative_to(self.repo_root).as_posix(),
            "results": [
                {
                    "authoring_accepted": True,
                    "error": "" if self.passed else "simulated SDK execution failure",
                    "execution": {
                        "requested": True,
                        "ok": self.passed,
                        "status": "passed" if self.passed else "failed",
                        "error": "" if self.passed else "simulated SDK execution failure",
                        "candidate_cause": "",
                        "commands": [],
                        "artifacts": artifacts,
                    },
                }
            ],
        }


def make_showcase_workflow(tmp_path: Path, *, passed: bool) -> HarnessWorkflow:
    capabilities = tmp_path / "test_harness" / "interface_capabilities.json"
    capabilities.parent.mkdir(parents=True)
    capabilities.write_bytes((REPO_ROOT / "test_harness/interface_capabilities.json").read_bytes())
    (tmp_path / "artifacts").mkdir()
    runtime = ShowcaseRuntime(tmp_path, passed=passed)
    return HarnessWorkflow(runtime, repo_root=tmp_path, profile="intranet")


def _run_session(workflow: HarnessWorkflow) -> dict[str, Any]:
    workflow.start("api_boolean")
    return workflow.comment("我明确同意当前方案，可以开始执行真实测试。")


def test_workflow_showcase_hook_runs_on_execution_failed(tmp_path: Path) -> None:
    workflow = make_showcase_workflow(tmp_path, passed=False)

    result = _run_session(workflow)

    assert result["state"] == "execution_failed"
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    session = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    showcase = session["failure_showcase"]
    assert showcase["ran"] is True and showcase["ok"] is True
    assert len(showcase["cases"]) == 1
    row = showcase["cases"][0]
    assert row["case_id"] == "case_fail"
    assert (tmp_path / row["dir"] / "reproduce.ps1").is_file()
    assert (tmp_path / row["analysis"]).is_file()
    assert (tmp_path / row["pre_analysis"]).is_file()
    assert (tmp_path / "artifacts/failure_analysis_db.json").is_file()


def test_workflow_showcase_hook_runs_on_completed(tmp_path: Path) -> None:
    workflow = make_showcase_workflow(tmp_path, passed=True)

    result = _run_session(workflow)

    assert result["state"] == "completed"
    session_root = next((tmp_path / "artifacts/harness_sessions").glob("*_api_boolean_*"))
    session = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    showcase = session["failure_showcase"]
    assert showcase["ran"] is True and showcase["ok"] is True
    assert showcase["cases"] == []
    assert "没有失败用例" in showcase["note"]


# ---------------------------------------------------------------------------
# UI projection and artifact image endpoint data path
# ---------------------------------------------------------------------------


def make_ui_session(tmp_path: Path, *, with_mirror: bool = True) -> Path:
    session_root = tmp_path / "artifacts" / "harness_sessions" / SESSION_ID
    write_json(tmp_path / "artifacts/harness_sessions/active.json", {"session_id": SESSION_ID})
    mirror = "execution/round_0001/attempt_0001/failure_analysis"
    session = {
        "session_id": SESSION_ID,
        "public_function": "api_boolean",
        "state": "execution_failed",
        "current_round": 1,
        "failure_showcase": {
            "ran": True,
            "ok": True,
            "root": "artifacts/api_boolean/round_0001_20260718T173157Z",
            "db": "artifacts/failure_analysis_db.json",
            "note": "",
            "cases": [
                {
                    "case_id": "case_fail",
                    "dir": "artifacts/api_boolean/round_0001_20260718T173157Z/case_fail",
                    "reproduce": "artifacts/api_boolean/round_0001_20260718T173157Z/case_fail/reproduce.ps1",
                    "analysis": "artifacts/api_boolean/round_0001_20260718T173157Z/case_fail/analysis.md",
                    "pre_analysis": f"{mirror}/case_fail_pre_analysis.json",
                    "analysis_png": f"{mirror}/case_fail_analysis.png",
                }
            ],
        },
    }
    write_json(session_root / "session.json", session)
    if with_mirror:
        write_json(
            session_root / mirror / "case_fail_pre_analysis.json",
            {
                "schema_version": 1,
                "case_id": "case_fail",
                "fault_domain": "test_expectation_suspect",
                "confidence": 0.9,
                "evidence": ["rule=point_relation_outside_bbox check=p1"],
                "notes": "确定性证据指向测试预期本身可疑（诊断性证据，不构成 SDK 缺陷定论）",
                "outcome": "failed",
                "signature": {"kind": "oracle_failure", "phase": "oracle", "sdk_error_code": 3},
                "triage_reasons": ["validation_failed"],
                "oracle_failures": ["point_relation_p1_mismatch expected=Inside actual=OnFace"],
                "parasolid": {"verdict": "inconclusive", "cause_class": "volume_drift"},
                "visual_fault_hint": "geometry",
                "visual_disagrees": True,
            },
        )
        make_png(session_root / mirror / "case_fail_analysis.png")
    return session_root


def test_failure_analysis_projection(tmp_path: Path) -> None:
    make_ui_session(tmp_path)

    snapshot = session_snapshot(tmp_path)
    data = snapshot["failure_analysis"]

    assert data["available"] is True
    assert data["root"] == "artifacts/api_boolean/round_0001_20260718T173157Z"
    assert data["db"] == "artifacts/failure_analysis_db.json"
    assert len(data["cases"]) == 1
    row = data["cases"][0]
    assert row["case_id"] == "case_fail"
    assert row["signature"] == {"kind": "oracle_failure", "phase": "oracle", "sdk_error_code": 3}
    assert row["triage_reasons"] == ["validation_failed"]
    assert row["oracle_failures"] == ["point_relation_p1_mismatch expected=Inside actual=OnFace"]
    assert row["parasolid"] == {"verdict": "inconclusive", "cause_class": "volume_drift"}
    assert row["fault_domain"] == "test_expectation_suspect"
    assert row["confidence"] == 0.9
    assert row["visual_fault_hint"] == "geometry"
    assert row["visual_disagrees"] is True
    assert row["analysis_png"].endswith("case_fail_analysis.png")
    assert row["showcase_dir"].endswith("case_fail")
    assert row["reproduction_note"]


def test_failure_analysis_degrades_without_showcase_or_mirrors(tmp_path: Path) -> None:
    session_root = tmp_path / "artifacts" / "harness_sessions" / "s-plain"
    write_json(tmp_path / "artifacts/harness_sessions/active.json", {"session_id": "s-plain"})
    write_json(session_root / "session.json", {"session_id": "s-plain", "state": "awaiting_comment"})

    assert session_snapshot(tmp_path)["failure_analysis"]["available"] is False

    make_ui_session(tmp_path, with_mirror=False)
    data = failure_analysis(
        tmp_path,
        tmp_path / "artifacts" / "harness_sessions" / SESSION_ID,
        json.loads(
            (tmp_path / "artifacts/harness_sessions" / SESSION_ID / "session.json").read_text(encoding="utf-8")
        ),
    )
    assert data["available"] is True
    row = data["cases"][0]
    assert row["fault_domain"] == ""
    assert row["analysis_png"] == ""
    assert row["reproduction_note"]


def test_read_artifact_png_returns_base64_image(tmp_path: Path) -> None:
    root = tmp_path / "session"
    png = make_png(root / "analysis.png")
    expected = png.read_bytes()

    result = read_artifact(root, "analysis.png")

    assert result["kind"] == "image"
    assert result["mime"] == "image/png"
    assert result["bytes"] == len(expected)
    assert base64.b64decode(result["content_base64"]) == expected


def test_read_artifact_image_guards(tmp_path: Path) -> None:
    root = tmp_path / "session"
    root.mkdir()
    (root / "blob.sgt").write_bytes(b"sgt")
    (root / "note.txt").write_text("hello", encoding="utf-8")
    big = root / "big.png"
    big.write_bytes(b"\x89PNG" + b"0" * (4 * 1024 * 1024 + 1))

    with pytest.raises(ValueError, match="escapes"):
        read_artifact(root, "../escape.png")
    with pytest.raises(FileNotFoundError):
        read_artifact(root, "blob.sgt")
    with pytest.raises(ValueError, match="too large"):
        read_artifact(root, "big.png")
    text = read_artifact(root, "note.txt")
    assert text["content"] == "hello"
    assert "kind" not in text


def test_static_ui_contains_failure_tab_scroll_css_and_csp() -> None:
    static = REPO_ROOT / "test_harness" / "ui" / "static"
    index = (static / "index.html").read_text(encoding="utf-8")
    app = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "styles.css").read_text(encoding="utf-8")
    server = (REPO_ROOT / "test_harness" / "ui" / "server.py").read_text(encoding="utf-8")

    assert 'id="failureTab"' in index
    assert "失败分析" in index
    assert "renderFailureAnalysis" in app
    assert "content_base64" in app
    assert "overflow-x: auto" in css
    assert "word-break: break-all" in css
    assert "img-src 'self' data:" in server
