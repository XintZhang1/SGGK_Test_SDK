#!/usr/bin/env python3
"""Run one SHA-bound ABC STEP through SGGK, NX, and the fixed comparator."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RESULT_KIND = "nx_sggk_step_compare_run"
OWNER_KIND = "nx_sggk_step_compare_output"
MAX_CAPTURE_CHARS = 64 * 1024
REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = Path(__file__).resolve().parent
RUN_CORPUS = TOOLS_ROOT / "run_corpus.py"
NX_RUNTIME = TOOLS_ROOT / "nx_runtime.py"
COMPARATOR = TOOLS_ROOT / "compare_nx_sggk_step.py"
OWNER_MARKER = ".nx_sggk_step_compare_output.json"
SUMMARY_JSON = "run_summary.json"
SUMMARY_MARKDOWN = "run_summary.zh-CN.md"
OWNED_CHILDREN = ("binding", "sggk", "nx", "comparison")
OWNED_FILES = (SUMMARY_JSON, SUMMARY_MARKDOWN)


class PipelineInputError(ValueError):
    """Raised before an external process is launched when an input is unsafe or invalid."""


@dataclass(frozen=True)
class PipelineConfig:
    dataset_index: Path
    index: int
    runner: Path
    nx_root: Path
    out: Path
    sggk_timeout: float = 180.0
    nx_timeout: float = 300.0
    abs_tol: float = 0.01
    rel_tol: float = 1e-5


@dataclass(frozen=True)
class Selection:
    dataset_index: Path
    index: int
    source: Path
    sha256: str
    size_bytes: int


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise PipelineInputError(f"{label} is unavailable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineInputError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineInputError(f"{label} JSON root must be an object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise PipelineInputError(f"selected STEP is unavailable: {exc}") from exc
    return digest.hexdigest()


def _resolve_index_entry(index_path: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (index_path.parent / candidate).resolve()


def load_selection(dataset_index: Path, index: int) -> Selection:
    """Resolve and cryptographically verify one stable entry from ``files[index]``."""

    dataset_index = dataset_index.expanduser().resolve()
    if not dataset_index.is_file() or dataset_index.suffix.casefold() != ".json":
        raise PipelineInputError("--dataset-index must name an existing JSON file")
    if index < 0:
        raise PipelineInputError("--index must be nonnegative")
    payload = _load_object(dataset_index, "dataset index")
    files = payload.get("files")
    if not isinstance(files, list):
        raise PipelineInputError("dataset index must contain a files array")
    if index >= len(files):
        raise PipelineInputError(f"--index {index} is outside files[0:{len(files)}]")
    entry = files[index]
    if not isinstance(entry, dict):
        raise PipelineInputError(f"dataset index files[{index}] must be an object")
    raw_path = entry.get("path") or entry.get("source_file")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PipelineInputError(f"dataset index files[{index}] must declare path or source_file")
    source = _resolve_index_entry(dataset_index, raw_path)
    if not source.is_file() or source.suffix.casefold() not in {".step", ".stp"}:
        raise PipelineInputError(f"dataset index files[{index}] must resolve to an existing STEP file")
    declared_sha256 = str(entry.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", declared_sha256):
        raise PipelineInputError(f"dataset index files[{index}] must declare a 64-character SHA-256")
    actual_sha256 = _sha256_file(source)
    if actual_sha256 != declared_sha256:
        raise PipelineInputError(
            f"dataset index files[{index}] SHA-256 mismatch: declared content no longer matches the file"
        )
    return Selection(
        dataset_index=dataset_index,
        index=index,
        source=source,
        sha256=actual_sha256,
        size_bytes=source.stat().st_size,
    )


def _validate_config(config: PipelineConfig) -> PipelineConfig:
    runner = config.runner.expanduser().resolve()
    nx_root = config.nx_root.expanduser().resolve()
    out = config.out.expanduser().resolve()
    if not runner.is_file():
        raise PipelineInputError("--runner must name an existing file")
    if not nx_root.is_dir():
        raise PipelineInputError("--nx-root must name an existing directory")
    if out == Path(out.anchor):
        raise PipelineInputError("--out may not be a filesystem root")
    if config.index < 0:
        raise PipelineInputError("--index must be nonnegative")
    for label, value in (
        ("--sggk-timeout", config.sggk_timeout),
        ("--nx-timeout", config.nx_timeout),
    ):
        if not math.isfinite(value) or value <= 0:
            raise PipelineInputError(f"{label} must be finite and positive")
    for label, value in (("--abs-tol", config.abs_tol), ("--rel-tol", config.rel_tol)):
        if not math.isfinite(value) or value < 0:
            raise PipelineInputError(f"{label} must be finite and nonnegative")
    return PipelineConfig(
        dataset_index=config.dataset_index.expanduser().resolve(),
        index=config.index,
        runner=runner,
        nx_root=nx_root,
        out=out,
        sggk_timeout=config.sggk_timeout,
        nx_timeout=config.nx_timeout,
        abs_tol=config.abs_tol,
        rel_tol=config.rel_tol,
    )


def _owned_marker_value() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "kind": OWNER_KIND}


def _remove_owned_path(path: Path, out: Path) -> None:
    if path.parent != out:
        raise PipelineInputError(f"refusing to clean path outside the output root: {path}")
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def prepare_output(out: Path) -> dict[str, Any]:
    """Create an owned output root or safely reset a root created by this CLI."""

    if out.exists() and not out.is_dir():
        raise PipelineInputError("--out must be a directory")
    out.mkdir(parents=True, exist_ok=True)
    marker = out / OWNER_MARKER
    existing_items = list(out.iterdir())
    reused = marker.exists()
    if reused:
        if _load_object(marker, "output ownership marker") != _owned_marker_value():
            raise PipelineInputError("--out has an unrecognized ownership marker; refusing cleanup")
    elif existing_items:
        raise PipelineInputError("--out is nonempty and is not owned by this fixed pipeline")
    else:
        _write_json(marker, _owned_marker_value())

    removed: list[str] = []
    if reused:
        for name in (*OWNED_CHILDREN, *OWNED_FILES):
            candidate = out / name
            if candidate.exists() or candidate.is_symlink():
                _remove_owned_path(candidate, out)
                removed.append(name)
    return {
        "owned_output_reused": reused,
        "performed": bool(removed),
        "removed": removed,
        "temporary_paths": [],
        "temporary_cleanup_ok": True,
        "note": "No orchestration temporary files are retained; the selected binding index is audit evidence.",
    }


def _default_command_runner(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _tail(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[-MAX_CAPTURE_CHARS:]


def _not_run(command: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "status": "not_run",
        "command": list(command),
        "returncode": None,
        "timed_out": False,
        "duration_seconds": 0.0,
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _run(command: Sequence[str], timeout: float, runner: CommandRunner) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = runner(list(command), timeout)
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = exc.stdout
        stderr = exc.stderr
        timed_out = True
    except OSError as exc:
        returncode = 126
        stdout = ""
        stderr = str(exc)
        timed_out = False
    return {
        "status": "completed" if returncode == 0 else "failed",
        "command": list(command),
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.perf_counter() - started, 6),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }


def _relative_or_absolute(path: str, base: Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()


def _verify_sggk_artifacts(sggk_root: Path, selection: Selection) -> Path:
    manifest_path = sggk_root / "corpus_manifest.json"
    summary_path = sggk_root / "corpus_summary.json"
    manifest = _load_object(manifest_path, "SGGK corpus manifest")
    summary = _load_object(summary_path, "SGGK corpus summary")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], dict):
        raise PipelineInputError("SGGK corpus manifest must contain exactly one input")
    entry = inputs[0]
    if entry.get("api") != "step_import":
        raise PipelineInputError("SGGK corpus manifest input must use step_import")
    if str(entry.get("sha256") or "").lower() != selection.sha256:
        raise PipelineInputError("SGGK corpus manifest SHA-256 does not match the selected input")
    raw_source = entry.get("source_file")
    if not isinstance(raw_source, str) or _relative_or_absolute(raw_source, sggk_root) != selection.source:
        raise PipelineInputError("SGGK corpus manifest source does not match the selected input")
    if summary.get("passed") != 1 or summary.get("failed") != 0:
        raise PipelineInputError("SGGK corpus summary does not prove one passing case")
    case_id = entry.get("case_id")
    if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", case_id):
        raise PipelineInputError("SGGK corpus manifest has an unsafe or missing case_id")
    case_dir = (sggk_root / case_id).resolve()
    if case_dir.parent != sggk_root or not case_dir.is_dir():
        raise PipelineInputError("SGGK case artifact directory is missing or outside the SGGK output root")
    return case_dir


def _verify_nx_artifacts(runtime_report: Path, measurement_path: Path, selection: Selection) -> None:
    runtime = _load_object(runtime_report, "NX runtime report")
    measurement = _load_object(measurement_path, "NX measurement")
    if runtime.get("ok") is not True:
        raise PipelineInputError("NX runtime report does not prove successful journal completion")
    input_record = measurement.get("input")
    if not isinstance(input_record, dict) or str(input_record.get("sha256") or "").lower() != selection.sha256:
        raise PipelineInputError("NX measurement SHA-256 does not match the selected input")


def _verify_comparison(path: Path, markdown: Path, selection: Selection, returncode: int) -> bool:
    comparison = _load_object(path, "NX/SGGK comparison")
    if not markdown.is_file():
        raise PipelineInputError("NX/SGGK Chinese comparison report is missing")
    input_record = comparison.get("input")
    if not isinstance(input_record, dict) or str(input_record.get("sha256") or "").lower() != selection.sha256:
        raise PipelineInputError("NX/SGGK comparison SHA-256 does not match the selected input")
    comparison_ok = comparison.get("ok")
    if not isinstance(comparison_ok, bool):
        raise PipelineInputError("NX/SGGK comparison must contain a boolean ok value")
    if (returncode == 0) != comparison_ok:
        raise PipelineInputError("comparator return code and comparison ok value are inconsistent")
    return comparison_ok


def _command_text(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def render_markdown(summary: Mapping[str, Any]) -> str:
    outcome = str(summary.get("outcome") or "")
    comparison_label = {
        "comparison_passed": "通过",
        "comparison_mismatch": "存在差异（编排已完成）",
    }.get(outcome, "未完成")
    steps = summary.get("steps") if isinstance(summary.get("steps"), dict) else {}
    lines = [
        "# ABC STEP：NX / SGGK 固定编排摘要",
        "",
        f"- 编排状态：{'完成' if summary.get('ok') else '失败'}",
        f"- 对比结论：{comparison_label}",
        f"- 输入索引：`files[{summary['selection']['index']}]`",
        f"- 输入 SHA-256：`{summary['selection']['sha256']}`",
        f"- STEP：`{summary['selection']['source']}`",
        "",
        "## 阶段结果",
        "",
        "| 阶段 | 状态 | 返回码 | 耗时（秒） |",
        "|---|---|---:|---:|",
    ]
    for name, label in (("sggk", "SGGK step_import"), ("nx", "NX fixed measure-step"), ("comparison", "比较器")):
        step = steps.get(name, {}) if isinstance(steps, dict) else {}
        returncode = step.get("returncode")
        lines.append(
            f"| {label} | {step.get('status', 'not_run')} | "
            f"{returncode if returncode is not None else '-'} | {step.get('duration_seconds', 0)} |"
        )
    lines.extend(["", "## 产物", ""])
    for label, key in (
        ("SGGK case", "sggk_case"),
        ("NX measurement", "nx_measurement"),
        ("comparison JSON", "comparison_json"),
        ("comparison 中文报告", "comparison_markdown"),
    ):
        value = summary["paths"].get(key) or "未生成"
        lines.append(f"- {label}：`{value}`")
    cleanup = summary.get("cleanup") if isinstance(summary.get("cleanup"), dict) else {}
    lines.extend(
        [
            "",
            "## 清理记录",
            "",
            f"- 复用本工具拥有的输出目录：{'是' if cleanup.get('owned_output_reused') else '否'}",
            f"- 已移除旧产物：{', '.join(cleanup.get('removed', [])) or '无'}",
            f"- 临时路径清理成功：{'是' if cleanup.get('temporary_cleanup_ok') else '否'}",
        ]
    )
    diagnostics = summary.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        lines.extend(["", "## 诊断", ""])
        for item in diagnostics:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('code', 'PIPELINE_ERROR')}`：{item.get('message', '')}")
    lines.extend(["", "## 命令证据", ""])
    for name, label in (("sggk", "SGGK"), ("nx", "NX"), ("comparison", "比较器")):
        step = steps.get(name, {}) if isinstance(steps, dict) else {}
        command = step.get("command")
        if isinstance(command, list) and command:
            lines.extend([f"### {label}", "", "```text", _command_text(command), "```", ""])
    return "\n".join(lines)


def _finish(summary: dict[str, Any], started: float, out: Path) -> dict[str, Any]:
    summary["finished_at"] = _utc_now()
    summary["duration_seconds"] = round(time.perf_counter() - started, 6)
    _write_json(out / SUMMARY_JSON, summary)
    (out / SUMMARY_MARKDOWN).write_text(render_markdown(summary), encoding="utf-8")
    return summary


def _fail(
    summary: dict[str, Any],
    *,
    outcome: str,
    code: str,
    message: str,
    started: float,
    out: Path,
) -> dict[str, Any]:
    summary["ok"] = False
    summary["outcome"] = outcome
    summary["comparison_ok"] = None
    summary["diagnostics"].append({"code": code, "severity": "error", "message": message})
    return _finish(summary, started, out)


def run_pipeline(
    config: PipelineConfig,
    *,
    command_runner: CommandRunner = _default_command_runner,
) -> dict[str, Any]:
    """Execute the fixed pipeline; comparison return codes 0 and 2 both complete it."""

    started = time.perf_counter()
    config = _validate_config(config)
    selection = load_selection(config.dataset_index, config.index)
    cleanup = prepare_output(config.out)

    binding_dir = config.out / "binding"
    sggk_root = config.out / "sggk"
    nx_dir = config.out / "nx"
    comparison_dir = config.out / "comparison"
    selected_index = binding_dir / "selected_dataset_index.json"
    nx_runtime_report = nx_dir / "runtime.json"
    nx_measurement = nx_dir / "measurement.json"
    comparison_json = comparison_dir / "comparison.json"
    comparison_markdown = comparison_dir / "comparison.zh-CN.md"
    _write_json(
        selected_index,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "nx_sggk_selected_dataset_binding",
            "source_dataset_index": str(selection.dataset_index),
            "selected_index": selection.index,
            "files": [
                {
                    "path": str(selection.source),
                    "sha256": selection.sha256,
                    "size_bytes": selection.size_bytes,
                }
            ],
        },
    )

    sggk_command = [
        sys.executable,
        str(RUN_CORPUS),
        "--runner",
        str(config.runner),
        "--dataset-list",
        str(selected_index),
        "--out",
        str(sggk_root),
        "--limit",
        "1",
        "--preserve-input-order",
        "--require-input-sha256",
        "--require-input-count",
        "1",
        "--hash-inputs",
        "--jobs",
        "1",
        "--fail-fast",
        "--timeout",
        str(config.sggk_timeout),
    ]
    nx_command = [
        sys.executable,
        str(NX_RUNTIME),
        "--out",
        str(nx_runtime_report),
        "measure-step",
        "--nx-root",
        str(config.nx_root),
        "--step",
        str(selection.source),
        "--measurement-out",
        str(nx_measurement),
        "--timeout",
        str(config.nx_timeout),
    ]
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "ok": False,
        "outcome": "running",
        "comparison_ok": None,
        "started_at": _utc_now(),
        "finished_at": "",
        "duration_seconds": 0.0,
        "selection": {
            "dataset_index": str(selection.dataset_index),
            "index": selection.index,
            "source": str(selection.source),
            "sha256": selection.sha256,
            "size_bytes": selection.size_bytes,
            "verified": True,
        },
        "paths": {
            "output_root": str(config.out),
            "selected_dataset_index": str(selected_index),
            "sggk_root": str(sggk_root),
            "sggk_manifest": str(sggk_root / "corpus_manifest.json"),
            "sggk_summary": str(sggk_root / "corpus_summary.json"),
            "sggk_case": "",
            "nx_runtime_report": str(nx_runtime_report),
            "nx_measurement": str(nx_measurement),
            "comparison_json": str(comparison_json),
            "comparison_markdown": str(comparison_markdown),
            "summary_json": str(config.out / SUMMARY_JSON),
            "summary_markdown": str(config.out / SUMMARY_MARKDOWN),
        },
        "steps": {
            "sggk": _not_run(sggk_command),
            "nx": _not_run(nx_command),
            "comparison": _not_run(),
        },
        "cleanup": cleanup,
        "diagnostics": [],
    }

    summary["steps"]["sggk"] = _run(sggk_command, config.sggk_timeout + 60.0, command_runner)
    if summary["steps"]["sggk"]["returncode"] != 0:
        return _fail(
            summary,
            outcome="sggk_failed",
            code="NX_SGGK_ORCHESTRATION_SGGK_FAILED",
            message="SGGK step_import did not complete successfully.",
            started=started,
            out=config.out,
        )
    try:
        sggk_case = _verify_sggk_artifacts(sggk_root, selection)
    except PipelineInputError as exc:
        return _fail(
            summary,
            outcome="sggk_artifact_invalid",
            code="NX_SGGK_ORCHESTRATION_SGGK_ARTIFACT_INVALID",
            message=str(exc),
            started=started,
            out=config.out,
        )
    summary["paths"]["sggk_case"] = str(sggk_case)

    summary["steps"]["nx"] = _run(nx_command, config.nx_timeout + 60.0, command_runner)
    if summary["steps"]["nx"]["returncode"] != 0:
        return _fail(
            summary,
            outcome="nx_failed",
            code="NX_SGGK_ORCHESTRATION_NX_FAILED",
            message="NX fixed measure-step did not complete successfully.",
            started=started,
            out=config.out,
        )
    try:
        _verify_nx_artifacts(nx_runtime_report, nx_measurement, selection)
    except PipelineInputError as exc:
        return _fail(
            summary,
            outcome="nx_artifact_invalid",
            code="NX_SGGK_ORCHESTRATION_NX_ARTIFACT_INVALID",
            message=str(exc),
            started=started,
            out=config.out,
        )

    comparison_command = [
        sys.executable,
        str(COMPARATOR),
        "--nx-measurement",
        str(nx_measurement),
        "--sggk-case",
        str(sggk_case),
        "--out",
        str(comparison_dir),
        "--abs-tol",
        str(config.abs_tol),
        "--rel-tol",
        str(config.rel_tol),
    ]
    summary["steps"]["comparison"] = _run(comparison_command, 120.0, command_runner)
    comparison_returncode = summary["steps"]["comparison"]["returncode"]
    if comparison_returncode == 2:
        summary["steps"]["comparison"]["status"] = "completed_with_mismatch"
    if comparison_returncode not in {0, 2}:
        return _fail(
            summary,
            outcome="comparison_error",
            code="NX_SGGK_ORCHESTRATION_COMPARATOR_ERROR",
            message=f"fixed comparator returned unexpected code {comparison_returncode}.",
            started=started,
            out=config.out,
        )
    try:
        comparison_ok = _verify_comparison(
            comparison_json,
            comparison_markdown,
            selection,
            comparison_returncode,
        )
    except PipelineInputError as exc:
        return _fail(
            summary,
            outcome="comparison_artifact_invalid",
            code="NX_SGGK_ORCHESTRATION_COMPARISON_ARTIFACT_INVALID",
            message=str(exc),
            started=started,
            out=config.out,
        )

    summary["ok"] = True
    summary["comparison_ok"] = comparison_ok
    summary["outcome"] = "comparison_passed" if comparison_ok else "comparison_mismatch"
    if not comparison_ok:
        summary["diagnostics"].append(
            {
                "code": "NX_SGGK_COMPARISON_MISMATCH",
                "severity": "warning",
                "message": "The fixed pipeline completed, but one or more NX/SGGK comparison checks differ.",
            }
        )
    return _finish(summary, started, config.out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-index", required=True, type=Path)
    parser.add_argument("--index", type=int, default=0, help="Stable zero-based files[] index")
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--nx-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--sggk-timeout", type=float, default=180.0)
    parser.add_argument("--nx-timeout", type=float, default=300.0)
    parser.add_argument("--abs-tol", type=float, default=0.01)
    parser.add_argument("--rel-tol", type=float, default=1e-5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = PipelineConfig(
        dataset_index=args.dataset_index,
        index=args.index,
        runner=args.runner,
        nx_root=args.nx_root,
        out=args.out,
        sggk_timeout=args.sggk_timeout,
        nx_timeout=args.nx_timeout,
        abs_tol=args.abs_tol,
        rel_tol=args.rel_tol,
    )
    try:
        summary = run_pipeline(config)
    except PipelineInputError as exc:
        print(f"pipeline input error: {exc}", file=sys.stderr)
        return 1
    print(f"summary_json={config.out.expanduser().resolve() / SUMMARY_JSON}")
    print(f"summary_markdown={config.out.expanduser().resolve() / SUMMARY_MARKDOWN}")
    print(f"outcome={summary['outcome']}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
