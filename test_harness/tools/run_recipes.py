#!/usr/bin/env python3
"""Run flat SGGK recipes in process-isolated large-scale lanes."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from validate_recipe import validate_file


CASE_ID_RE = re.compile(r"^case_id=(?P<case_id>.+)$", re.MULTILINE)
ARTIFACT_DIR_RE = re.compile(r"^artifact_dir=(?P<artifact_dir>.+)$", re.MULTILINE)


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


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    expanded_paths = list(paths)
    for raw_list in recipe_lists:
        expanded_paths.extend(read_recipe_list(Path(raw_list)))
    for raw in expanded_paths:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() == ".json":
            files.append(path.resolve())
        elif path.is_dir():
            files.extend(child.resolve() for child in path.rglob("*.json") if child.is_file())
        else:
            files.append(path.resolve())
    return sorted(set(files), key=lambda item: str(item).lower())


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


def previous_results(summary_path: Path, resume_mode: str) -> dict[str, dict[str, Any]]:
    summary = read_json(summary_path)
    if not isinstance(summary, dict):
        return {}
    previous: dict[str, dict[str, Any]] = {}
    for item in summary.get("results", []):
        if not isinstance(item, dict):
            continue
        recipe = item.get("recipe")
        if not isinstance(recipe, str) or not recipe:
            continue
        returncode = item.get("returncode")
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


def run_one(runner: Path, recipe_path: Path, out_root: Path, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    cmd = [str(runner), "--recipe", str(recipe_path), "--out", str(out_root)]
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
        artifact_dir = parse_stdout_field(stdout, ARTIFACT_DIR_RE, "artifact_dir") or infer_artifact_dir(out_root, case_id)
        return {
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
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        case_id = parse_stdout_field(stdout, CASE_ID_RE, "case_id") or recipe_case_id(recipe_path)
        artifact_dir = parse_stdout_field(stdout, ARTIFACT_DIR_RE, "artifact_dir") or infer_artifact_dir(out_root, case_id)
        return {
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
        }


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
    entries: list[dict[str, Any]] = []
    for index, recipe_path in enumerate(selected_recipes):
        item: dict[str, Any] = {
            "index": index,
            "recipe": str(recipe_path),
            "case_id": recipe_case_id(recipe_path),
            "size_bytes": recipe_path.stat().st_size if recipe_path.exists() else 0,
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
        "resume": args.resume,
        "resume_mode": args.resume_mode,
        "shard_count": args.shard_count,
        "shard_index": args.shard_index,
        "hash_recipes": args.hash_recipes,
        "validate": not args.no_validate,
        "scanned_count": len(scanned_recipes),
        "selected_count": len(selected_recipes),
        "inputs": entries,
    }
    write_json(path, manifest)


def validate_args(args: argparse.Namespace) -> None:
    if args.jobs <= 0:
        raise ValueError("--jobs must be >= 1")
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

    previous = previous_results(summary_path, args.resume_mode) if args.resume else {}
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
            result = run_one(runner, recipe_path, out_root, args.timeout)
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
                future = executor.submit(run_one, runner, recipe_path, out_root, args.timeout)
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
