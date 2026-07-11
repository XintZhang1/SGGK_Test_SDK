from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import run_model_output_preflight as preflight


def write_summary(
    path: Path,
    counts: dict[str, int],
    *,
    stage_counts: dict[str, int] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "stage_counts": stage_counts or {},
                "model_output_matrix": {
                    "counts": {
                        "saved_outputs": 1,
                        "missing_outputs": 0,
                        "normalized_failed": 0,
                        "gate_failed": 0,
                        **counts,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("failure_count", [{"gate_failed": 1}, {"normalized_failed": 1}])
def test_saved_output_failure_blocks_preflight_even_when_child_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_count: dict[str, int],
) -> None:
    report_path = tmp_path / "interface_distillation_summary.json"
    write_summary(report_path, failure_count)
    monkeypatch.setattr(
        preflight,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = preflight.run_gate("saved_output_matrix", ["fixed-command"], report_path=report_path)

    assert result["ok"] is False
    assert result["report_ok"] is False
    assert "saved_output_matrix_failures" in result["report_policy_error"]


def test_missing_saved_outputs_remain_gateway_pending_not_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "interface_distillation_summary.json"
    write_summary(report_path, {"saved_outputs": 0, "missing_outputs": 14})
    monkeypatch.setattr(
        preflight,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = preflight.run_gate("saved_output_matrix", ["fixed-command"], report_path=report_path)

    assert result["ok"] is True
    assert result["report_ok"] is True
    assert result["report_policy_error"] == ""


def test_saved_output_check_failed_stage_cannot_be_hidden_as_skipped_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "interface_distillation_summary.json"
    write_summary(
        report_path,
        {"gate_failed": 0},
        stage_counts={"model_output_check_failed": 1},
    )
    monkeypatch.setattr(
        preflight,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = preflight.run_gate("saved_output_matrix", ["fixed-command"], report_path=report_path)

    assert result["ok"] is False
    assert "model_output_check_failed=1" in result["report_policy_error"]


@pytest.mark.parametrize("field", ["provenance_found", "provenance_source_known"])
def test_saved_output_requires_registered_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    report_path = tmp_path / "interface_distillation_summary.json"
    summary = {
        "stage_counts": {},
        "model_output_matrix": {
            "counts": {"saved_outputs": 1, "normalized_failed": 0, "gate_failed": 0},
            "rows": [
                {
                    "saved_output": True,
                    "provenance_found": True,
                    "provenance_source_known": True,
                    field: False,
                }
            ],
        },
    }
    report_path.write_text(json.dumps(summary), encoding="utf-8")
    monkeypatch.setattr(
        preflight,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = preflight.run_gate("saved_output_matrix", ["fixed-command"], report_path=report_path)

    assert result["ok"] is False
    assert "saved_output_provenance_" in result["report_policy_error"]


def test_saved_output_gate_is_strict_and_preflight_is_message_api_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["run_model_output_preflight.py"])
    args = preflight.parse_args()
    commands = preflight.build_commands(args, tmp_path)
    saved_output_command = next(command for label, command, _ in commands if label == "saved_output_matrix")

    assert "--fail-on-failures" in saved_output_command
    assert not any("authoring_gateway" in part for command in commands for part in command[1])


def test_gateway_sidecars_are_included_in_provenance_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "model_outputs"
    provenance_root = tmp_path / "separate_provenance"
    output_root.mkdir()
    provenance_root.mkdir()
    sidecar = output_root / "api_boolean.provenance.json"
    sidecar.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_model_output_preflight.py",
            "--model-output-root",
            str(output_root),
            "--model-output-provenance-root",
            str(provenance_root),
        ],
    )

    args = preflight.parse_args()
    commands = preflight.build_commands(args, tmp_path / "reports")
    provenance_command = next(command for label, command, _ in commands if label == "provenance")

    assert ["--path", str(sidecar)] == provenance_command[-2:]
