from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from run_nx_sggk_step_compare import (  # noqa: E402
    PipelineConfig,
    PipelineInputError,
    load_selection,
    run_pipeline,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


class StubToolchain:
    def __init__(self, *, comparison_returncode: int = 0) -> None:
        self.comparison_returncode = comparison_returncode
        self.calls: list[list[str]] = []

    def __call__(self, raw_command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        command = list(raw_command)  # type: ignore[arg-type]
        self.calls.append(command)
        tool = Path(command[1]).name
        if tool == "run_corpus.py":
            out = Path(_option(command, "--out"))
            selected = json.loads(Path(_option(command, "--dataset-list")).read_text(encoding="utf-8"))
            source = Path(selected["files"][0]["path"]).resolve()
            sha256 = selected["files"][0]["sha256"]
            case_id = "stub_step_import_case"
            (out / case_id).mkdir(parents=True)
            _write_json(
                out / "corpus_manifest.json",
                {
                    "inputs": [
                        {
                            "api": "step_import",
                            "source_file": str(source),
                            "sha256": sha256,
                            "case_id": case_id,
                        }
                    ]
                },
            )
            _write_json(out / "corpus_summary.json", {"passed": 1, "failed": 0})
            return subprocess.CompletedProcess(command, 0, "sggk ok", "")
        if tool == "nx_runtime.py":
            runtime_out = Path(_option(command, "--out"))
            measurement_out = Path(_option(command, "--measurement-out"))
            source = Path(_option(command, "--step"))
            sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            _write_json(runtime_out, {"ok": True, "operation": "run"})
            _write_json(measurement_out, {"input": {"sha256": sha256}})
            return subprocess.CompletedProcess(command, 0, "nx ok", "")
        if tool == "compare_nx_sggk_step.py":
            out = Path(_option(command, "--out"))
            measurement = json.loads(Path(_option(command, "--nx-measurement")).read_text(encoding="utf-8"))
            comparison_ok = self.comparison_returncode == 0
            _write_json(
                out / "comparison.json",
                {"ok": comparison_ok, "input": {"sha256": measurement["input"]["sha256"]}},
            )
            (out / "comparison.zh-CN.md").write_text("# stub comparison\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, self.comparison_returncode, "compare done", "")
        raise AssertionError(f"unexpected tool: {tool}")


def _fixture(tmp_path: Path, *, count: int = 2, index: int = 0) -> tuple[PipelineConfig, list[Path]]:
    files: list[Path] = []
    entries: list[dict[str, object]] = []
    for number in range(count):
        step = tmp_path / "dataset" / f"case-{number}.step"
        step.parent.mkdir(parents=True, exist_ok=True)
        step.write_bytes(f"ISO-10303-21; stub {number}".encode())
        files.append(step.resolve())
        entries.append(
            {
                "path": str(step.resolve()),
                "sha256": hashlib.sha256(step.read_bytes()).hexdigest(),
                "size_bytes": step.stat().st_size,
            }
        )
    dataset_index = tmp_path / "dataset_index.json"
    _write_json(dataset_index, {"files": entries})
    runner = tmp_path / "sggk_case_runner.exe"
    runner.write_bytes(b"stub")
    nx_root = tmp_path / "NX2512"
    nx_root.mkdir()
    return (
        PipelineConfig(
            dataset_index=dataset_index,
            index=index,
            runner=runner,
            nx_root=nx_root,
            out=tmp_path / "out",
        ),
        files,
    )


def test_fixed_pipeline_records_sha_bound_commands_and_artifacts(tmp_path: Path) -> None:
    config, files = _fixture(tmp_path, index=1)
    stub = StubToolchain()

    summary = run_pipeline(config, command_runner=stub)

    assert summary["ok"] is True
    assert summary["outcome"] == "comparison_passed"
    assert summary["comparison_ok"] is True
    assert summary["selection"]["source"] == str(files[1])
    assert [Path(call[1]).name for call in stub.calls] == [
        "run_corpus.py",
        "nx_runtime.py",
        "compare_nx_sggk_step.py",
    ]
    sggk_command, nx_command, _comparison_command = stub.calls
    assert "--require-input-sha256" in sggk_command
    assert "--preserve-input-order" in sggk_command
    assert "measure-step" in nx_command
    assert "--journal" not in nx_command
    assert Path(summary["paths"]["summary_json"]).is_file()
    assert Path(summary["paths"]["summary_markdown"]).is_file()
    bound = json.loads(Path(summary["paths"]["selected_dataset_index"]).read_text(encoding="utf-8"))
    assert len(bound["files"]) == 1
    assert bound["files"][0]["sha256"] == summary["selection"]["sha256"]


def test_comparator_returncode_two_is_completed_mismatch(tmp_path: Path) -> None:
    config, _files = _fixture(tmp_path)

    summary = run_pipeline(config, command_runner=StubToolchain(comparison_returncode=2))

    assert summary["ok"] is True
    assert summary["outcome"] == "comparison_mismatch"
    assert summary["comparison_ok"] is False
    assert summary["steps"]["comparison"]["returncode"] == 2
    assert summary["steps"]["comparison"]["status"] == "completed_with_mismatch"


def test_selected_dataset_entry_requires_matching_sha256(tmp_path: Path) -> None:
    config, files = _fixture(tmp_path)
    files[0].write_bytes(b"content changed after index approval")

    with pytest.raises(PipelineInputError, match="SHA-256 mismatch"):
        load_selection(config.dataset_index, 0)


def test_owned_output_can_be_safely_repeated_and_records_cleanup(tmp_path: Path) -> None:
    config, _files = _fixture(tmp_path)
    run_pipeline(config, command_runner=StubToolchain())
    stale = config.out / "comparison" / "stale.txt"
    stale.write_text("stale", encoding="utf-8")

    summary = run_pipeline(config, command_runner=StubToolchain())

    assert summary["ok"] is True
    assert summary["cleanup"]["owned_output_reused"] is True
    assert set(summary["cleanup"]["removed"]) >= {"binding", "sggk", "nx", "comparison"}
    assert not stale.exists()
