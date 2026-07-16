#!/usr/bin/env python3
"""Run flat SGGK recipes in process-isolated large-scale lanes."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from validate_recipe import validate_file


CASE_ID_RE = re.compile(r"^case_id=(?P<case_id>.+)$", re.MULTILINE)
ARTIFACT_DIR_RE = re.compile(r"^artifact_dir=(?P<artifact_dir>.+)$", re.MULTILINE)
SAFE_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _windows_extended_path(value: str) -> str:
    """Return an absolute Windows path in the explicit long-path namespace."""

    if value.startswith("\\\\?\\") or value.startswith("\\\\.\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return "\\\\?\\" + value.replace("/", "\\")
    return value


def native_path_argument(path: Path) -> str:
    """Render a subprocess path without relying on Windows LongPathsEnabled."""

    resolved = path.resolve()
    value = str(resolved)
    if os.name != "nt" or not resolved.is_absolute():
        return value
    return _windows_extended_path(value)


def display_path(value: str) -> str:
    """Remove the Windows device prefix from paths recorded for people/tools."""

    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument(
        "--recipe",
        action="append",
        default=[],
        help="Flat recipe JSON file or directory. Can be passed more than once.",
    )
    parser.add_argument(
        "--recipe-list",
        action="append",
        default=[],
        help="Text file containing one recipe JSON path per line. Can be passed more than once.",
    )
    parser.add_argument("--out", default="artifacts/recipe_lane", help="Artifact root")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-recipe timeout in seconds")
    parser.add_argument("--limit", type=int, default=0, help="Maximum recipes to run; 0 means all")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel runner processes")
    parser.add_argument("--sdk-threads", type=int, default=1, help="SDK threads per isolated runner process")
    parser.add_argument(
        "--capture-flat-topotrack",
        action="store_true",
        help="Enable crash-prone flat-recipe TopoTrack querying inside each isolated runner process",
    )
    parser.add_argument("--fail-fast", action="store_true", help="Stop after first failed recipe")
    parser.add_argument("--resume", action="store_true", help="Skip previous passing recipes")
    parser.add_argument(
        "--resume-mode",
        choices=["passed", "completed"],
        default="passed",
        help="With --resume, skip only passed recipes or all completed recipes.",
    )
    parser.add_argument("--shard-count", type=int, default=1, help="Total number of stable shards")
    parser.add_argument("--shard-index", type=int, default=0, help="Zero-based shard index to run")
    parser.add_argument("--hash-recipes", action="store_true", help="Store SHA1 recipe digests in the manifest")
    parser.add_argument("--no-validate", action="store_true", help="Skip validating recipe JSON before running")
    parser.add_argument(
        "--triage-out",
        help="Optional output directory for triage_artifacts.py after the lane run.",
    )
    parser.add_argument(
        "--triage-include-passed",
        action="store_true",
        help="Pass --include-passed to triage_artifacts.py.",
    )
    parser.add_argument("--preview-out", help="Optional directory for rendered previews after the lane run")
    parser.add_argument("--contact-sheet", help="Optional preview contact-sheet PNG path")
    parser.add_argument("--preview-limit", type=int, default=0, help="Maximum previews to render; 0 means all")
    parser.add_argument("--preview-max-edges", type=int, default=80, help="Maximum input edges drawn per role")
    parser.add_argument("--geometry-audit-out", help="Optional output directory for audit_case_geometry.py after the lane run")
    parser.add_argument("--geometry-audit-round-digits", type=int, default=9, help="Digits used when hashing geometry audit bboxes")
    parser.add_argument("--geometry-audit-fail-on-duplicates", action="store_true", help="Fail when same-boolean duplicate input geometry is found")
    parser.add_argument("--geometry-audit-fail-on-tolerance-mismatch", action="store_true", help="Fail when inferred tolerance offsets mismatch")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def recipe_review_record(recipe_path: Path, index: int) -> dict[str, Any]:
    recipe = json.loads(recipe_path.read_text(encoding="utf-8-sig"))
    if not isinstance(recipe, dict):
        raise ValueError(f"recipe root must be object: {recipe_path}")
    api = str(recipe.get("api") or "unknown")
    target_kind = str(recipe.get("target_kind") or "")
    tool_kind = str(recipe.get("tool_kind") or "")
    source_file = str(recipe.get("source_file") or recipe.get("target_source_file") or "")
    inputs = [f"API={api}"]
    if target_kind:
        inputs.append(f"target={target_kind}")
    if tool_kind:
        inputs.append(f"tool={tool_kind}")
    if source_file:
        inputs.append(f"输入资产={source_file}")
    expectations = recipe.get("expectations") if isinstance(recipe.get("expectations"), dict) else {}
    oracle_names = sorted(str(key) for key in expectations)
    source_review = recipe.get("source_review") if isinstance(recipe.get("source_review"), dict) else {}
    risk_summary = str(
        source_review.get("summary")
        or recipe.get("hypothesis")
        or recipe.get("notes")
        or "重点审查复杂输入、容差边界、拓扑有效性和 Oracle 完整性。"
    )
    return {
        "schema_version": 1,
        "language": "zh-CN",
        "index": index,
        "case_id": recipe_case_id(recipe_path),
        "api": api,
        "recipe_path": str(recipe_path.resolve()),
        "recipe_sha256": file_sha256(recipe_path),
        "purpose_zh_cn": f"验证 {api} 在当前几何与参数组合下的行为。",
        "input_summary_zh_cn": "；".join(inputs),
        "expected_behavior_zh_cn": (
            "验证确定性 Oracle：" + "、".join(oracle_names)
            if oracle_names
            else "用例未声明 expectations；用户复核时必须确认其固定执行判据。"
        ),
        "risk_summary_zh_cn": risk_summary,
        "oracles": oracle_names,
        "source_evidence": {
            "source_ref": str(recipe.get("source_ref") or ""),
            "source_task_id": str(recipe.get("source_task_id") or ""),
            "source_risk_id": str(recipe.get("source_risk_id") or ""),
            "source_review": source_review,
        },
        "generator": {
            "dsl_source": str(recipe.get("dsl_source") or ""),
            "dsl_case_id": str(recipe.get("dsl_case_id") or ""),
            "dsl_variant": str(recipe.get("dsl_variant") or ""),
        },
        "machine_validation": {"recipe_schema": "passed"},
        "review_workflow": {
            "status": "awaiting_natural_language_comment",
            "managed_by": "harness_session_orchestrator",
            "user_editable": False,
        },
    }


def write_recipe_review_index(out_root: Path, selected_recipes: list[Path]) -> dict[str, Any]:
    out_root.mkdir(parents=True, exist_ok=True)
    index_path = out_root / "recipe_review_index.jsonl"
    report_path = out_root / "recipe_review_report.zh-CN.md"
    state_path = out_root / "recipe_review_state.internal.json"
    by_api: dict[str, int] = {}
    by_oracle: dict[str, int] = {}
    preview: list[dict[str, Any]] = []
    with index_path.open("w", encoding="utf-8", newline="\n") as handle:
        for index, recipe_path in enumerate(selected_recipes):
            record = recipe_review_record(recipe_path, index)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            api = str(record["api"])
            by_api[api] = by_api.get(api, 0) + 1
            for oracle in record["oracles"]:
                by_oracle[str(oracle)] = by_oracle.get(str(oracle), 0) + 1
            if len(preview) < 50:
                preview.append(record)
    index_sha256 = file_sha256(index_path)
    state = {
        "schema_version": 1,
        "review_index": str(index_path),
        "review_index_sha256": index_sha256,
        "status": "awaiting_natural_language_comment",
        "managed_by": "harness_session_orchestrator",
        "user_editable": False,
        "instructions_zh_cn": (
            "用户只通过 Harness 提交自然语言 comment；轮次、状态、ID、索引哈希和批准证明"
            "均由宿主管理。请勿编辑本内部状态文件。"
        ),
    }
    write_json(state_path, state)
    lines = [
        "# SGGK 测试用例中文审查索引",
        "",
        "> 本报告由固定宿主生成。每个 recipe 在 JSONL 中恰好对应一条哈希记录；"
        "机器校验通过不会自动触发 SDK 执行。用户不需要编辑任何审批 JSON。",
        "",
        f"- 用例总数：`{len(selected_recipes)}`",
        f"- JSONL 索引：`{index_path}`",
        f"- 索引 SHA-256：`{index_sha256}`",
        f"- Harness 内部状态：`{state_path}`（只读）",
        "- 当前用户复核状态：`awaiting_comment`",
        "",
        "## API 分布",
        "",
    ]
    lines.extend(f"- `{key}`：`{value}`" for key, value in sorted(by_api.items()))
    lines.extend(["", "## Oracle 覆盖", ""])
    lines.extend(f"- `{key}`：`{value}`" for key, value in sorted(by_oracle.items()))
    lines.extend(
        [
            "",
            "## 前 50 条审查预览",
            "",
            "| case_id | API | 输入 | 预期/Oracle | 风险 | recipe SHA-256 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for record in preview:
        lines.append(
            f"| `{record['case_id']}` | `{record['api']}` | {record['input_summary_zh_cn']} | "
            f"{record['expected_behavior_zh_cn']} | {record['risk_summary_zh_cn']} | "
            f"`{record['recipe_sha256']}` |"
        )
    lines.extend(
        [
            "",
            "## 用户复核要点",
            "",
            "- [ ] 输入资产、target/tool 构造与 API 目标一致。",
            "- [ ] 复杂几何链、容差带和变体没有退化成重复基本体。",
            "- [ ] 每条用例至少有一个可观测判据，且预期值不过度拟合单次运行。",
            "- [ ] 源码风险摘要、source_ref 和生成的参数变化能够对应。",
            "- [ ] 对失败用例检查 triage、稳定重放、TopoTrack 和 reduction 证据。",
            "- [ ] 如需调整，只提交自然语言 comment；任何调整都会产生新的不可变审查轮次。",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "schema_version": 1,
        "language": "zh-CN",
        "record_count": len(selected_recipes),
        "index_path": str(index_path),
        "index_sha256": index_sha256,
        "report_path": str(report_path),
        "report_sha256": file_sha256(report_path),
        "review_state_path": str(state_path),
        "review_state_sha256": file_sha256(state_path),
        "review_status": "awaiting_natural_language_comment",
        "by_api": dict(sorted(by_api.items())),
        "by_oracle": dict(sorted(by_oracle.items())),
    }


def read_recipe_list(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return [str(path)]
    result: list[str] = []
    base = path.parent
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        child = Path(line)
        result.append(str(child if child.is_absolute() else (base / child)))
    return result


def iter_recipe_files(paths: list[str], recipe_lists: list[str]) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    expanded_paths = list(paths)
    for raw_list in recipe_lists:
        expanded_paths.extend(read_recipe_list(Path(raw_list)))
    for raw in expanded_paths:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() == ".json":
            candidates = [path.resolve()]
        elif path.is_dir():
            candidates = sorted(
                (child.resolve() for child in path.rglob("*.json") if child.is_file()),
                key=lambda item: str(item).lower(),
            )
        else:
            candidates = [path.resolve()]
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            files.append(candidate)
    return files


def select_shard(items: list[Path], shard_count: int, shard_index: int) -> list[Path]:
    if shard_count == 1:
        return items
    return [path for index, path in enumerate(items) if index % shard_count == shard_index]


def recipe_key(path: Path) -> str:
    return str(path.resolve())


def recipe_case_id(path: Path) -> str:
    value = read_json(path)
    if isinstance(value, dict) and isinstance(value.get("case_id"), str) and value["case_id"]:
        return value["case_id"]
    return path.stem


def recipe_metadata(path: Path) -> dict[str, Any]:
    value = read_json(path)
    recipe = value if isinstance(value, dict) else {}
    return {
        "case_id": recipe_case_id(path),
        "api": recipe.get("api") if isinstance(recipe.get("api"), str) else "",
        "recipe_sha1": file_sha1(path) if path.is_file() else "",
    }


def initialize_run_state(runner: Path, recipe_path: Path, out_root: Path) -> tuple[Path, dict[str, Any]]:
    metadata = recipe_metadata(recipe_path)
    case_id = str(metadata["case_id"])
    if not SAFE_CASE_ID_RE.fullmatch(case_id):
        raise ValueError(f"unsafe case_id for artifact path: {case_id!r}")
    root = out_root.resolve()
    case_dir = (root / case_id).resolve()
    if case_dir.parent != root:
        raise ValueError(f"case output escaped --out root: {case_id!r}")
    state_path = case_dir / "run_state.json"
    state = {
        "schema_version": 1,
        "case_id": metadata["case_id"],
        "api": metadata["api"],
        "phase": "launching",
        "last_phase": "launching",
        "started_at": now_iso_like(),
        "updated_at": now_iso_like(),
        "recipe_path": str(recipe_path),
        "recipe_sha1": metadata["recipe_sha1"],
        "runner_path": str(runner),
        "runner_sha1": file_sha1(runner),
        "completed": False,
    }
    write_json_atomic(state_path, state)
    frozen_recipe = case_dir / "input" / "recipe.json"
    frozen_recipe.parent.mkdir(parents=True, exist_ok=True)
    frozen_recipe.write_bytes(recipe_path.read_bytes())
    return state_path, state


def finalize_run_state(
    state_path: Path,
    state: dict[str, Any],
    *,
    returncode: int,
    timed_out: bool,
    stderr: str,
) -> None:
    last_phase, phase_evidence = infer_execution_phase(state_path.parent)
    state.update(
        {
            "phase": "timed_out" if timed_out else "completed",
            "last_phase": last_phase,
            "phase_evidence": phase_evidence,
            "updated_at": now_iso_like(),
            "completed": True,
            "returncode": returncode,
            "timed_out": timed_out,
            "stderr_tail": (stderr or "")[-2000:],
        }
    )
    write_json_atomic(state_path, state)


def infer_execution_phase(case_dir: Path) -> tuple[str, str]:
    """Infer the last reached runner phase from monotonic artifact markers."""

    report_dir = case_dir / "report"
    phase_markers = (
        ("oracle", (report_dir / "validation.json", report_dir / "roundtrip_comparison.json")),
        ("topocheck", (report_dir / "topo_check.json",)),
        ("serialize_result", (report_dir / "status.json",)),
    )
    for phase, markers in phase_markers:
        for marker in markers:
            if marker.is_file():
                return phase, str(marker)

    output_dir = case_dir / "output"
    if output_dir.is_dir() and any(path.is_file() for path in output_dir.rglob("*")):
        return "serialize_result", str(output_dir)

    input_dir = case_dir / "input"
    if input_dir.is_dir() and any(
        path.is_file() and path.name != "recipe.json" for path in input_dir.rglob("*")
    ):
        return "invoke_api", str(input_dir)
    manifest = case_dir / "manifest.json"
    if manifest.is_file():
        return "build_inputs", str(manifest)
    return "parse", "launcher_run_state_only"


def parse_stdout_field(stdout: str, regex: re.Pattern[str], group: str) -> str:
    match = regex.search(stdout or "")
    return match.group(group).strip() if match else ""


def infer_artifact_dir(out_root: Path, case_id: str) -> str:
    if not case_id:
        return ""
    candidate = out_root / case_id
    return str(candidate) if candidate.is_dir() else ""


def validate_recipes(paths: list[Path], skip_validation: bool) -> int:
    if skip_validation:
        return 0
    failures = 0
    for path in paths:
        errors = validate_file(path)
        if errors:
            failures += 1
            print(f"FAIL {path}", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
    return failures


def validate_unique_case_ids(paths: list[Path]) -> int:
    owners: dict[str, Path] = {}
    duplicates = 0
    for path in paths:
        case_id = recipe_case_id(path)
        previous = owners.get(case_id)
        if previous is None:
            owners[case_id] = path
            continue
        duplicates += 1
        print(f"duplicate case_id {case_id!r}: {previous} and {path}", file=sys.stderr)
    return duplicates


def previous_results(summary_path: Path, resume_mode: str, runner: Path) -> dict[str, dict[str, Any]]:
    summary = read_json(summary_path)
    if not isinstance(summary, dict):
        return {}
    previous: dict[str, dict[str, Any]] = {}
    runner_sha1 = file_sha1(runner)
    for item in summary.get("results", []):
        if not isinstance(item, dict):
            continue
        recipe = item.get("recipe")
        if not isinstance(recipe, str) or not recipe:
            continue
        returncode = item.get("returncode")
        recipe_path = Path(recipe)
        if not recipe_path.is_file():
            continue
        if item.get("recipe_sha1") != file_sha1(recipe_path) or item.get("runner_sha1") != runner_sha1:
            continue
        completed = isinstance(returncode, int)
        passed = returncode == 0
        key = str(Path(recipe).resolve())
        if resume_mode == "completed" and completed:
            previous[key] = item
        elif resume_mode == "passed" and passed:
            previous[key] = item
    return previous


def skipped_result(previous: dict[str, Any], recipe_path: Path, recipe_index: int) -> dict[str, Any]:
    result = dict(previous)
    result["recipe"] = str(recipe_path)
    result["case_id"] = recipe_case_id(recipe_path)
    result["recipe_index"] = recipe_index
    result["skipped"] = True
    return result


def run_one(
    runner: Path,
    recipe_path: Path,
    out_root: Path,
    timeout: float,
    sdk_threads: int = 1,
    capture_flat_topotrack: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    state_path, run_state = initialize_run_state(runner, recipe_path, out_root)
    cmd = [
        native_path_argument(runner),
        "--recipe",
        native_path_argument(recipe_path),
        "--out",
        native_path_argument(out_root),
        "--sdk-threads",
        str(sdk_threads),
    ]
    if capture_flat_topotrack:
        cmd.append("--capture-flat-topotrack")
    try:
        completed = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        stdout = completed.stdout or ""
        case_id = parse_stdout_field(stdout, CASE_ID_RE, "case_id") or recipe_case_id(recipe_path)
        artifact_dir = display_path(
            parse_stdout_field(stdout, ARTIFACT_DIR_RE, "artifact_dir")
        ) or infer_artifact_dir(out_root, case_id)
        result = {
            "recipe": str(recipe_path),
            "case_id": case_id,
            "artifact_dir": artifact_dir,
            "command": cmd,
            "returncode": completed.returncode,
            "elapsed_seconds": time.perf_counter() - started,
            "stdout": stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "skipped": False,
            "recipe_sha1": run_state["recipe_sha1"],
            "runner_sha1": run_state["runner_sha1"],
            "run_state": str(state_path),
        }
        finalize_run_state(
            state_path,
            run_state,
            returncode=completed.returncode,
            timed_out=False,
            stderr=completed.stderr or "",
        )
        return result
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        case_id = parse_stdout_field(stdout, CASE_ID_RE, "case_id") or recipe_case_id(recipe_path)
        artifact_dir = display_path(
            parse_stdout_field(stdout, ARTIFACT_DIR_RE, "artifact_dir")
        ) or infer_artifact_dir(out_root, case_id)
        result = {
            "recipe": str(recipe_path),
            "case_id": case_id,
            "artifact_dir": artifact_dir,
            "command": cmd,
            "returncode": 124,
            "elapsed_seconds": time.perf_counter() - started,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "skipped": False,
            "recipe_sha1": run_state["recipe_sha1"],
            "runner_sha1": run_state["runner_sha1"],
            "run_state": str(state_path),
        }
        finalize_run_state(
            state_path,
            run_state,
            returncode=124,
            timed_out=True,
            stderr=stderr,
        )
        return result


def summarize_results(
    runner: Path,
    out_root: Path,
    started_at: str,
    results: list[dict[str, Any]],
    stopped_early: bool,
    triage: dict[str, Any] | None = None,
    preview: dict[str, Any] | None = None,
    geometry_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    executed = [item for item in results if not item.get("skipped")]
    skipped = [item for item in results if item.get("skipped")]
    summary: dict[str, Any] = {
        "runner": str(runner),
        "out_root": str(out_root),
        "started_at": started_at,
        "updated_at": now_iso_like(),
        "total": len(results),
        "executed": len(executed),
        "skipped": len(skipped),
        "passed": sum(1 for item in results if item.get("returncode") == 0),
        "failed": sum(1 for item in results if item.get("returncode") != 0),
        "timed_out": sum(1 for item in results if item.get("timed_out")),
        "stopped_early": stopped_early,
        "results": results,
    }
    if triage is not None:
        summary["triage"] = triage
    if preview is not None:
        summary["preview"] = preview
    if geometry_audit is not None:
        summary["geometry_audit"] = geometry_audit
    return summary


def write_manifest(
    path: Path,
    args: argparse.Namespace,
    runner: Path,
    out_root: Path,
    scanned_recipes: list[Path],
    selected_recipes: list[Path],
    started_at: str,
) -> None:
    review = write_recipe_review_index(out_root, selected_recipes)
    entries: list[dict[str, Any]] = []
    for index, recipe_path in enumerate(selected_recipes):
        item: dict[str, Any] = {
            "index": index,
            "recipe": str(recipe_path),
            "case_id": recipe_case_id(recipe_path),
            "size_bytes": recipe_path.stat().st_size if recipe_path.exists() else 0,
            "sha256": file_sha256(recipe_path) if recipe_path.exists() else "",
            "review_status": "awaiting_natural_language_comment",
        }
        if args.hash_recipes and recipe_path.exists():
            item["sha1"] = file_sha1(recipe_path)
        entries.append(item)

    manifest = {
        "started_at": started_at,
        "runner": str(runner),
        "out_root": str(out_root),
        "recipes": args.recipe,
        "recipe_lists": args.recipe_list,
        "timeout": args.timeout,
        "limit": args.limit,
        "jobs": args.jobs,
        "sdk_threads": args.sdk_threads,
        "capture_flat_topotrack": args.capture_flat_topotrack,
        "resume": args.resume,
        "resume_mode": args.resume_mode,
        "shard_count": args.shard_count,
        "shard_index": args.shard_index,
        "hash_recipes": args.hash_recipes,
        "validate": not args.no_validate,
        "scanned_count": len(scanned_recipes),
        "selected_count": len(selected_recipes),
        "inputs": entries,
        "generated_artifact_review": review,
    }
    write_json(path, manifest)


def validate_args(args: argparse.Namespace) -> None:
    if args.jobs <= 0:
        raise ValueError("--jobs must be >= 1")
    if args.sdk_threads <= 0 or args.sdk_threads > 64:
        raise ValueError("--sdk-threads must be in [1, 64]")
    if not args.recipe and not args.recipe_list:
        raise ValueError("at least one --recipe or --recipe-list is required")
    if args.timeout <= 0:
        raise ValueError("--timeout must be > 0")
    if args.limit < 0:
        raise ValueError("--limit must be >= 0")
    if args.shard_count <= 0:
        raise ValueError("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise ValueError("--shard-index must satisfy 0 <= index < shard-count")
    if args.preview_limit < 0:
        raise ValueError("--preview-limit must be >= 0")
    if args.preview_max_edges < 0:
        raise ValueError("--preview-max-edges must be >= 0")
    if args.geometry_audit_round_digits < 0:
        raise ValueError("--geometry-audit-round-digits must be >= 0")
    if args.fail_fast and args.jobs > 1:
        print("--fail-fast with --jobs > 1 cancels pending recipes after the first failure", file=sys.stderr)


def run_triage(out_root: Path, triage_out: str | None, include_passed: bool) -> dict[str, Any] | None:
    if not triage_out:
        return None
    triage_script = Path(__file__).resolve().with_name("triage_artifacts.py")
    cmd = [sys.executable, str(triage_script), str(out_root), "--out", triage_out]
    if include_passed:
        cmd.append("--include-passed")
    completed = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "out": triage_out,
    }


def run_preview(
    out_root: Path,
    preview_out: str | None,
    contact_sheet: str | None,
    limit: int,
    max_edges: int,
) -> dict[str, Any] | None:
    if not preview_out and not contact_sheet:
        return None
    preview_script = Path(__file__).resolve().with_name("render_case_preview.py")
    cmd = [sys.executable, str(preview_script), str(out_root)]
    if preview_out:
        cmd.extend(["--out-dir", preview_out])
    if contact_sheet:
        cmd.extend(["--contact-sheet", contact_sheet])
    elif preview_out:
        cmd.extend(["--contact-sheet", str(Path(preview_out) / "contact.png")])
    if limit > 0:
        cmd.extend(["--limit", str(limit)])
    cmd.extend(["--max-edges", str(max_edges)])
    completed = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "out": preview_out,
        "contact_sheet": contact_sheet or (str(Path(preview_out) / "contact.png") if preview_out else ""),
    }


def run_geometry_audit(
    out_root: Path,
    audit_out: str | None,
    round_digits: int,
    fail_on_duplicates: bool,
    fail_on_tolerance_mismatch: bool,
    runner: Path,
) -> dict[str, Any] | None:
    if not audit_out:
        return None
    audit_script = Path(__file__).resolve().with_name("audit_case_geometry.py")
    cmd = [
        sys.executable,
        str(audit_script),
        str(out_root),
        "--out",
        audit_out,
        "--round-digits",
        str(round_digits),
        "--exact-bbox-runner",
        str(runner),
    ]
    if fail_on_duplicates:
        cmd.append("--fail-on-duplicates")
    if fail_on_tolerance_mismatch:
        cmd.append("--fail-on-tolerance-mismatch")
    completed = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "out": audit_out,
    }


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    runner = Path(args.runner).resolve()
    if not runner.exists():
        print(f"runner not found: {runner}", file=sys.stderr)
        return 1

    scanned_recipes = iter_recipe_files(args.recipe, args.recipe_list)
    if not scanned_recipes:
        print("no recipe JSON files found", file=sys.stderr)
        return 1
    selected_recipes = select_shard(scanned_recipes, args.shard_count, args.shard_index)
    if args.limit > 0:
        selected_recipes = selected_recipes[: args.limit]

    validation_failures = validate_recipes(selected_recipes, args.no_validate)
    validation_failures += validate_unique_case_ids(selected_recipes)
    if validation_failures:
        print(f"recipe validation failed for {validation_failures} file(s)", file=sys.stderr)
        return 2

    started_at = now_iso_like()
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    summary_path = out_root / "recipe_summary.json"
    manifest_path = out_root / "recipe_manifest.json"
    write_manifest(manifest_path, args, runner, out_root, scanned_recipes, selected_recipes, started_at)

    if not selected_recipes:
        final_summary = summarize_results(runner, out_root, started_at, [], False)
        final_summary["empty_shard"] = True
        write_json(summary_path, final_summary)
        print(f"summary={summary_path}")
        print("selected_recipes=0")
        return 0

    previous = previous_results(summary_path, args.resume_mode, runner) if args.resume else {}
    results: list[dict[str, Any]] = []
    stopped_early = False

    def record_result(result: dict[str, Any]) -> None:
        results.append(result)
        summary = summarize_results(runner, out_root, started_at, results, stopped_early)
        write_json(summary_path, summary)

    if args.jobs == 1:
        for index, recipe_path in enumerate(selected_recipes, start=1):
            key = recipe_key(recipe_path)
            if key in previous:
                print(f"[{index}/{len(selected_recipes)}] skip {recipe_path}")
                record_result(skipped_result(previous[key], recipe_path, index - 1))
                continue
            print(f"[{index}/{len(selected_recipes)}] {recipe_path}")
            result = run_one(
                runner,
                recipe_path,
                out_root,
                args.timeout,
                args.sdk_threads,
                args.capture_flat_topotrack,
            )
            result["recipe_index"] = index - 1
            record_result(result)
            if result["returncode"] != 0 and args.fail_fast:
                stopped_early = True
                break
    else:
        pending: set[Future[dict[str, Any]]] = set()
        next_index = 0

        def submit_until_full(executor: ThreadPoolExecutor) -> None:
            nonlocal next_index
            while next_index < len(selected_recipes) and len(pending) < args.jobs and not stopped_early:
                recipe_path = selected_recipes[next_index]
                index = next_index + 1
                next_index += 1
                key = recipe_key(recipe_path)
                if key in previous:
                    print(f"[{index}/{len(selected_recipes)}] skip {recipe_path}")
                    record_result(skipped_result(previous[key], recipe_path, index - 1))
                    continue
                print(f"[{index}/{len(selected_recipes)}] {recipe_path}")
                future = executor.submit(
                    run_one,
                    runner,
                    recipe_path,
                    out_root,
                    args.timeout,
                    args.sdk_threads,
                    args.capture_flat_topotrack,
                )
                future.recipe_index = index - 1  # type: ignore[attr-defined]
                pending.add(future)

        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            submit_until_full(executor)
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.remove(future)
                    result = future.result()
                    result["recipe_index"] = future.recipe_index  # type: ignore[attr-defined]
                    record_result(result)
                    if result["returncode"] != 0 and args.fail_fast:
                        stopped_early = True
                if stopped_early:
                    for future in pending:
                        future.cancel()
                    pending = {future for future in pending if not future.cancelled()}
                submit_until_full(executor)

    triage = run_triage(out_root, args.triage_out, args.triage_include_passed)
    preview = run_preview(out_root, args.preview_out, args.contact_sheet, args.preview_limit, args.preview_max_edges)
    geometry_audit = run_geometry_audit(
        out_root,
        args.geometry_audit_out,
        args.geometry_audit_round_digits,
        args.geometry_audit_fail_on_duplicates,
        args.geometry_audit_fail_on_tolerance_mismatch,
        runner,
    )
    final_summary = summarize_results(runner, out_root, started_at, results, stopped_early, triage, preview, geometry_audit)
    write_json(summary_path, final_summary)

    print(f"summary={summary_path}")
    if triage:
        print(f"triage_out={args.triage_out}")
    if preview:
        print(f"preview_out={args.preview_out or ''}")
        if preview.get("contact_sheet"):
            print(f"contact_sheet={preview['contact_sheet']}")
    if geometry_audit:
        print(f"geometry_audit_out={args.geometry_audit_out}")
    if triage and triage["returncode"] != 0:
        return 2
    if preview and preview["returncode"] != 0:
        return 2
    if geometry_audit and geometry_audit["returncode"] != 0:
        return 2
    return 0 if final_summary["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
