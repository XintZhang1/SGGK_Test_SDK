#!/usr/bin/env python3
"""Run SGGK case runner over a local CAD/SGT corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

EXTENSION_TO_API = {
    ".sgt": "check_sgt",
    ".step": "step_import",
    ".stp": "step_import",
    ".iges": "iges_import",
    ".igs": "iges_import",
}
SGT_APIS = {"check_sgt", "step_roundtrip", "iges_roundtrip"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument(
        "--dataset",
        action="append",
        help="Corpus file or directory. Can be passed more than once.",
    )
    parser.add_argument(
        "--dataset-list",
        action="append",
        help="Text file with one corpus path per line, or discover_corpus.py JSON with files[*].path.",
    )
    parser.add_argument("--out", default="artifacts/corpus", help="Artifact root")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-case timeout in seconds")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of files to run; 0 means all")
    parser.add_argument(
        "--preserve-input-order",
        action="store_true",
        help="Preserve explicit file order from --dataset/--dataset-list before applying --limit",
    )
    parser.add_argument(
        "--sgt-api",
        action="append",
        choices=sorted(SGT_APIS),
        help="API to run for .sgt files. Can be passed more than once; default is check_sgt.",
    )
    parser.add_argument("--source-body-index", type=int, default=0, help="Body index used by SGT roundtrip APIs")
    parser.add_argument("--step-app-protocol", choices=["AP203", "AP214", "AP242"], default="AP203")
    parser.add_argument("--step-surface-to-bspline", action="store_true")
    parser.add_argument("--step-curve-to-bspline", action="store_true")
    parser.add_argument("--step-spcurve-in-wire-to-bspline", action="store_true")
    parser.add_argument("--iges-face-only-mode", action="store_true")
    parser.add_argument("--iges-write-sgk-specified-data", action="store_true")
    parser.add_argument("--roundtrip-abs-tol", type=float, default=0.01)
    parser.add_argument("--roundtrip-rel-tol", type=float, default=1e-5)
    parser.add_argument("--fail-fast", action="store_true", help="Stop after first failed case")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel runner processes")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip previously passing source files from this output directory.",
    )
    parser.add_argument(
        "--resume-mode",
        choices=["passed", "completed"],
        default="passed",
        help="With --resume, skip only passed cases or all completed cases.",
    )
    parser.add_argument("--shard-count", type=int, default=1, help="Total number of stable shards")
    parser.add_argument("--shard-index", type=int, default=0, help="Zero-based shard index to run")
    parser.add_argument(
        "--hash-inputs",
        action="store_true",
        help="Store SHA1 and SHA-256 input content digests in corpus_manifest.json.",
    )
    parser.add_argument(
        "--require-input-sha256",
        action="store_true",
        help="Require every selected list entry to declare a matching SHA-256 digest.",
    )
    parser.add_argument(
        "--require-input-count",
        type=int,
        default=0,
        help="Fail unless at least this many input files are selected.",
    )
    parser.add_argument(
        "--triage-out",
        help="Optional output directory for triage_artifacts.py after the corpus run.",
    )
    parser.add_argument(
        "--triage-include-passed",
        action="store_true",
        help="Pass --include-passed to triage_artifacts.py.",
    )
    parser.add_argument(
        "--fail-on-triage-error",
        action="store_true",
        help="Return nonzero when the requested triage command fails.",
    )
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def dedupe_preserving_order(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def iter_inputs(paths: list[str], exclude_roots: list[Path], preserve_order: bool = False) -> list[Path]:
    found: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file() and path.suffix.lower() in EXTENSION_TO_API:
            resolved = path.resolve()
            if not any(is_under(resolved, root) for root in exclude_roots):
                found.append(resolved)
        elif path.is_dir():
            for child in sorted(path.rglob("*"), key=lambda item: str(item).lower()):
                resolved = child.resolve()
                if any(is_under(resolved, root) for root in exclude_roots):
                    continue
                if child.is_file() and child.suffix.lower() in EXTENSION_TO_API:
                    found.append(resolved)
    if preserve_order:
        return dedupe_preserving_order(found)
    return sorted(set(found), key=lambda p: str(p).lower())


def _path_key(path: Path) -> str:
    value = str(path.resolve())
    return value.casefold() if sys.platform == "win32" else value


def _resolve_list_entry(list_path: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (list_path.parent / candidate).resolve()


def load_dataset_list(
    path: Path,
    expected_hashes: dict[str, tuple[str, str]] | None = None,
) -> list[str]:
    path = path.expanduser().resolve()
    if not path.exists():
        raise ValueError(f"dataset list not found: {path}")
    if not path.is_file():
        raise ValueError(f"dataset list must be a file: {path}")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"dataset list JSON root must be object: {path}")
        files = data.get("files")
        if not isinstance(files, list):
            raise ValueError(f"dataset list JSON must contain files array: {path}")
        result: list[str] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            raw = item.get("path") or item.get("source_file")
            if isinstance(raw, str) and raw:
                resolved = _resolve_list_entry(path, raw)
                result.append(str(resolved))
                if expected_hashes is not None:
                    sha256 = str(item.get("sha256") or "").lower()
                    sha1 = str(item.get("sha1") or "").lower()
                    if re.fullmatch(r"[0-9a-f]{64}", sha256):
                        expected_hashes[_path_key(resolved)] = ("sha256", sha256)
                    elif re.fullmatch(r"[0-9a-f]{40}", sha1):
                        expected_hashes[_path_key(resolved)] = ("sha1", sha1)
        return result

    result = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        result.append(str(_resolve_list_entry(path, line)))
    return result


def collect_dataset_paths(
    args: argparse.Namespace,
    expected_hashes: dict[str, tuple[str, str]] | None = None,
) -> list[str]:
    paths = list(args.dataset or [])
    for raw_list in args.dataset_list or []:
        paths.extend(load_dataset_list(Path(raw_list), expected_hashes))
    return paths


def select_shard(inputs: list[Path], shard_count: int, shard_index: int) -> list[Path]:
    if shard_count == 1:
        return inputs
    return [path for index, path in enumerate(inputs) if index % shard_count == shard_index]


def make_case_id(path: Path, api: str | None = None) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem).strip("._")
    if not stem:
        stem = "case"
    if api and api != EXTENSION_TO_API[path.suffix.lower()]:
        stem = f"{stem}_{api}"
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    return f"{stem}_{digest}"


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_hash_issues(
    inputs: list[Path],
    expected_hashes: dict[str, tuple[str, str]],
    *,
    require_sha256: bool,
) -> list[str]:
    issues: list[str] = []
    for input_path in inputs:
        expected = expected_hashes.get(_path_key(input_path))
        if expected is None:
            issues.append(f"missing declared content hash: {input_path}")
            continue
        algorithm, digest = expected
        if require_sha256 and algorithm != "sha256":
            issues.append(f"missing declared SHA-256: {input_path}")
            continue
        actual = file_sha256(input_path) if algorithm == "sha256" else file_sha1(input_path)
        if actual.lower() != digest.lower():
            issues.append(f"content hash mismatch: {input_path}")
    return issues


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None


def write_recipe(path: Path, recipe_dir: Path, api: str, args: argparse.Namespace) -> Path:
    case_id = make_case_id(path, api)
    recipe = {
        "case_id": case_id,
        "api": api,
        "source_file": str(path),
    }
    if path.suffix.lower() == ".sgt":
        recipe["source_body_index"] = args.source_body_index
    if api == "step_roundtrip":
        recipe["step_app_protocol"] = args.step_app_protocol
        recipe["step_surface_to_bspline"] = args.step_surface_to_bspline
        recipe["step_curve_to_bspline"] = args.step_curve_to_bspline
        recipe["step_spcurve_in_wire_to_bspline"] = args.step_spcurve_in_wire_to_bspline
        recipe["roundtrip_abs_tol"] = args.roundtrip_abs_tol
        recipe["roundtrip_rel_tol"] = args.roundtrip_rel_tol
    elif api == "iges_roundtrip":
        recipe["iges_face_only_mode"] = args.iges_face_only_mode
        recipe["iges_write_sgk_specified_data"] = args.iges_write_sgk_specified_data
        recipe["roundtrip_abs_tol"] = args.roundtrip_abs_tol
        recipe["roundtrip_rel_tol"] = args.roundtrip_rel_tol
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe_path = recipe_dir / f"{case_id}.json"
    write_json(recipe_path, recipe)
    return recipe_path


def run_one(runner: Path, recipe_path: Path, out_root: Path, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    cmd = [str(runner), "--recipe", str(recipe_path), "--out", str(out_root)]
    try:
        completed = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - started
        return {
            "recipe": str(recipe_path),
            "returncode": completed.returncode,
            "elapsed_seconds": elapsed,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "skipped": False,
        }
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        return {
            "recipe": str(recipe_path),
            "returncode": 124,
            "elapsed_seconds": elapsed,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
            "skipped": False,
        }


def source_api_key(path: Path, api: str) -> str:
    return f"{path.resolve()}|{api}"


def previous_results(summary_path: Path, resume_mode: str) -> dict[str, dict[str, Any]]:
    summary = read_json(summary_path)
    if not isinstance(summary, dict):
        return {}
    previous: dict[str, dict[str, Any]] = {}
    for item in summary.get("results", []):
        if not isinstance(item, dict):
            continue
        source_file = item.get("source_file")
        if not isinstance(source_file, str) or not source_file:
            continue
        api = item.get("api")
        if not isinstance(api, str) or not api:
            api = EXTENSION_TO_API.get(Path(source_file).suffix.lower(), "check_sgt")
        returncode = item.get("returncode")
        completed = isinstance(returncode, int)
        passed = returncode == 0
        if resume_mode == "completed" and completed:
            previous[source_api_key(Path(source_file), api)] = item
        elif resume_mode == "passed" and passed:
            previous[source_api_key(Path(source_file), api)] = item
    return previous


def skipped_result(
    previous: dict[str, Any],
    recipe_path: Path,
    source_file: Path,
    api: str,
    source_index: int,
) -> dict[str, Any]:
    result = dict(previous)
    result["recipe"] = str(recipe_path)
    result["source_file"] = str(source_file)
    result["api"] = api
    result["source_index"] = source_index
    result["skipped"] = True
    return result


def summarize_results(
    runner: Path,
    out_root: Path,
    started_at: str,
    results: list[dict[str, Any]],
    stopped_early: bool,
    triage: dict[str, Any] | None = None,
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
    return summary


def write_manifest(
    path: Path,
    args: argparse.Namespace,
    runner: Path,
    out_root: Path,
    dataset_paths: list[str],
    scanned_inputs: list[Path],
    selected_inputs: list[Path],
    selected_tasks: list[tuple[Path, str]],
    started_at: str,
) -> None:
    entries: list[dict[str, Any]] = []
    for index, (input_path, api) in enumerate(selected_tasks):
        item: dict[str, Any] = {
            "index": index,
            "source_file": str(input_path),
            "case_id": make_case_id(input_path, api),
            "api": api,
            "size_bytes": input_path.stat().st_size,
        }
        if args.hash_inputs:
            item["sha1"] = file_sha1(input_path)
            item["sha256"] = file_sha256(input_path)
        entries.append(item)

    manifest = {
        "started_at": started_at,
        "runner": str(runner),
        "out_root": str(out_root),
        "datasets": dataset_paths,
        "dataset_lists": args.dataset_list or [],
        "timeout": args.timeout,
        "limit": args.limit,
        "jobs": args.jobs,
        "resume": args.resume,
        "resume_mode": args.resume_mode,
        "sgt_apis": args.sgt_api or ["check_sgt"],
        "source_body_index": args.source_body_index,
        "step_app_protocol": args.step_app_protocol,
        "step_surface_to_bspline": args.step_surface_to_bspline,
        "step_curve_to_bspline": args.step_curve_to_bspline,
        "step_spcurve_in_wire_to_bspline": args.step_spcurve_in_wire_to_bspline,
        "iges_face_only_mode": args.iges_face_only_mode,
        "iges_write_sgk_specified_data": args.iges_write_sgk_specified_data,
        "roundtrip_abs_tol": args.roundtrip_abs_tol,
        "roundtrip_rel_tol": args.roundtrip_rel_tol,
        "shard_count": args.shard_count,
        "shard_index": args.shard_index,
        "hash_inputs": args.hash_inputs,
        "scanned_count": len(scanned_inputs),
        "selected_count": len(selected_inputs),
        "selected_task_count": len(selected_tasks),
        "inputs": entries,
    }
    write_json(path, manifest)


def validate_args(args: argparse.Namespace) -> None:
    if not args.dataset and not args.dataset_list:
        raise ValueError("pass at least one --dataset or --dataset-list")
    if args.jobs <= 0:
        raise ValueError("--jobs must be >= 1")
    if args.timeout <= 0:
        raise ValueError("--timeout must be > 0")
    if args.limit < 0:
        raise ValueError("--limit must be >= 0")
    if args.source_body_index < 0:
        raise ValueError("--source-body-index must be >= 0")
    if args.roundtrip_abs_tol <= 0:
        raise ValueError("--roundtrip-abs-tol must be > 0")
    if args.roundtrip_rel_tol <= 0:
        raise ValueError("--roundtrip-rel-tol must be > 0")
    if args.shard_count <= 0:
        raise ValueError("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise ValueError("--shard-index must satisfy 0 <= index < shard-count")
    if args.require_input_count < 0:
        raise ValueError("--require-input-count must be >= 0")
    if args.fail_fast and args.jobs > 1:
        print("--fail-fast with --jobs > 1 cancels pending cases after the first failure", file=sys.stderr)


def expand_tasks(inputs: list[Path], sgt_apis: list[str]) -> list[tuple[Path, str]]:
    tasks: list[tuple[Path, str]] = []
    for input_path in inputs:
        if input_path.suffix.lower() == ".sgt":
            for api in sgt_apis:
                tasks.append((input_path, api))
        else:
            tasks.append((input_path, EXTENSION_TO_API[input_path.suffix.lower()]))
    return tasks


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
        capture_output=True,
    )
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "out": triage_out,
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

    started_at = now_iso_like()
    out_root = Path(args.out).resolve()
    recipe_dir = out_root / "_recipes"
    summary_path = out_root / "corpus_summary.json"
    manifest_path = out_root / "corpus_manifest.json"
    out_root.mkdir(parents=True, exist_ok=True)

    expected_hashes: dict[str, tuple[str, str]] = {}
    dataset_paths = collect_dataset_paths(args, expected_hashes)
    scanned_inputs = iter_inputs(dataset_paths, [out_root], preserve_order=args.preserve_input_order)
    if not scanned_inputs:
        print("no supported corpus files found", file=sys.stderr)
        return 1
    selected_inputs = select_shard(scanned_inputs, args.shard_count, args.shard_index)
    if args.limit > 0:
        selected_inputs = selected_inputs[: args.limit]
    if len(selected_inputs) < args.require_input_count:
        print(
            f"selected corpus input count {len(selected_inputs)} is below required {args.require_input_count}",
            file=sys.stderr,
        )
        return 1
    if args.require_input_sha256:
        hash_issues = input_hash_issues(
            selected_inputs,
            expected_hashes,
            require_sha256=True,
        )
        if hash_issues:
            print("; ".join(hash_issues[:8]), file=sys.stderr)
            return 1
    sgt_apis = args.sgt_api or ["check_sgt"]
    selected_tasks = expand_tasks(selected_inputs, sgt_apis)

    write_manifest(
        manifest_path,
        args,
        runner,
        out_root,
        dataset_paths,
        scanned_inputs,
        selected_inputs,
        selected_tasks,
        started_at,
    )
    if not selected_inputs:
        final_summary = summarize_results(runner, out_root, started_at, [], False)
        final_summary["empty_shard"] = True
        write_json(summary_path, final_summary)
        print(f"summary={summary_path}")
        print("selected_inputs=0")
        return 0
    previous = previous_results(summary_path, args.resume_mode) if args.resume else {}

    results: list[dict[str, Any]] = []
    stopped_early = False

    def record_result(result: dict[str, Any]) -> None:
        results.append(result)
        summary = summarize_results(runner, out_root, started_at, results, stopped_early)
        write_json(summary_path, summary)

    def make_task(input_path: Path, api: str) -> tuple[Path, str, Path]:
        recipe_path = write_recipe(input_path, recipe_dir, api, args)
        return input_path, api, recipe_path

    if args.jobs == 1:
        for index, (input_path, api) in enumerate(selected_tasks, start=1):
            input_key = source_api_key(input_path, api)
            recipe_path = write_recipe(input_path, recipe_dir, api, args)
            if input_key in previous:
                print(f"[{index}/{len(selected_tasks)}] skip {api} {input_path}")
                record_result(skipped_result(previous[input_key], recipe_path, input_path, api, index - 1))
                continue
            print(f"[{index}/{len(selected_tasks)}] {api} {input_path}")
            result = run_one(runner, recipe_path, out_root, args.timeout)
            result["source_file"] = str(input_path)
            result["api"] = api
            result["source_index"] = index - 1
            record_result(result)
            if result["returncode"] != 0 and args.fail_fast:
                stopped_early = True
                break
    else:
        pending: set[Future[dict[str, Any]]] = set()
        next_index = 0

        def submit_until_full(executor: ThreadPoolExecutor) -> None:
            nonlocal next_index
            while next_index < len(selected_tasks) and len(pending) < args.jobs and not stopped_early:
                input_path, api = selected_tasks[next_index]
                index = next_index + 1
                next_index += 1
                input_key = source_api_key(input_path, api)
                recipe_path = write_recipe(input_path, recipe_dir, api, args)
                if input_key in previous:
                    print(f"[{index}/{len(selected_tasks)}] skip {api} {input_path}")
                    record_result(skipped_result(previous[input_key], recipe_path, input_path, api, index - 1))
                    continue
                print(f"[{index}/{len(selected_tasks)}] {api} {input_path}")
                future = executor.submit(run_one, runner, recipe_path, out_root, args.timeout)
                future.input_path = input_path  # type: ignore[attr-defined]
                future.api = api  # type: ignore[attr-defined]
                future.source_index = index - 1  # type: ignore[attr-defined]
                pending.add(future)

        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            submit_until_full(executor)
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.remove(future)
                    input_path = future.input_path  # type: ignore[attr-defined]
                    result = future.result()
                    result["source_file"] = str(input_path)
                    result["api"] = future.api  # type: ignore[attr-defined]
                    result["source_index"] = future.source_index  # type: ignore[attr-defined]
                    record_result(result)
                    if result["returncode"] != 0 and args.fail_fast:
                        stopped_early = True
                if stopped_early:
                    for future in pending:
                        future.cancel()
                    pending = {future for future in pending if not future.cancelled()}
                submit_until_full(executor)

    triage = run_triage(out_root, args.triage_out, args.triage_include_passed)
    final_summary = summarize_results(runner, out_root, started_at, results, stopped_early, triage)
    write_json(summary_path, final_summary)
    print(f"summary={summary_path}")
    if triage:
        print(f"triage_out={args.triage_out}")
    triage_failed = bool(
        args.fail_on_triage_error
        and isinstance(triage, dict)
        and int(triage.get("returncode") or 0) != 0
    )
    return 0 if final_summary["failed"] == 0 and not triage_failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
