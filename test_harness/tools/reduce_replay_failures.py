#!/usr/bin/env python3
"""Reduce stable replay failures with the repository-owned fixed reducer."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from failure_predicate import build_failure_signature, signatures_match

REPO_ROOT = Path(__file__).resolve().parents[2]
REDUCER_SCRIPT = Path(__file__).resolve().with_name("reduce_failure_recipe.py")
STABLE_STATUSES = frozenset({"stable_same_failure"})
MIN_STABLE_ATTEMPTS = 3


class ReductionInputError(ValueError):
    """Raised when a caller supplies an unsafe or malformed batch input."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument(
        "--replay",
        required=True,
        help="replay_summary.json or the directory containing it",
    )
    parser.add_argument("--out", required=True, help="Reduction batch output directory")
    parser.add_argument("--limit", type=int, default=3, help="Maximum stable failures to reduce; 0 means all")
    parser.add_argument("--max-trials", type=int, default=60, help="Maximum reducer trials per recipe")
    parser.add_argument("--timeout", type=float, default=120.0, help="Runner timeout passed to each reducer")
    parser.add_argument(
        "--min-dimension",
        type=float,
        default=0.01,
        help="Minimum positive geometry dimension passed to the reducer",
    )
    return parser.parse_args(argv)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReductionInputError(f"invalid JSON file {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_inside_repo(raw: str | Path, *, label: str) -> Path:
    root = REPO_ROOT.resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReductionInputError(f"{label} must stay inside repository: {resolved}") from exc
    return resolved


def resolve_inputs(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    if args.limit < 0:
        raise ReductionInputError("--limit must be >= 0")
    if args.max_trials <= 0:
        raise ReductionInputError("--max-trials must be > 0")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ReductionInputError("--timeout must be > 0")
    if not math.isfinite(args.min_dimension) or args.min_dimension <= 0:
        raise ReductionInputError("--min-dimension must be > 0")

    runner = resolve_inside_repo(args.runner, label="runner")
    if not runner.is_file():
        raise ReductionInputError(f"runner not found: {runner}")

    replay_input = resolve_inside_repo(args.replay, label="replay")
    replay_summary = replay_input / "replay_summary.json" if replay_input.is_dir() else replay_input
    if replay_summary.name != "replay_summary.json" or not replay_summary.is_file():
        raise ReductionInputError(f"replay_summary.json not found: {replay_summary}")

    out_root = resolve_inside_repo(args.out, label="out")
    if out_root.exists() and not out_root.is_dir():
        raise ReductionInputError(f"out must be a directory: {out_root}")

    reducer = resolve_inside_repo(REDUCER_SCRIPT, label="fixed reducer")
    if not reducer.is_file():
        raise ReductionInputError(f"fixed reducer not found: {reducer}")
    return runner, replay_summary, out_root, reducer


def first_existing_recipe(seed: dict[str, Any]) -> Path | None:
    raw_paths = seed.get("recipe_paths")
    if not isinstance(raw_paths, list):
        return None
    for index, raw in enumerate(raw_paths):
        if not isinstance(raw, str) or not raw.strip():
            continue
        recipe = resolve_inside_repo(raw, label=f"seed recipe_paths[{index}]")
        if recipe.is_file():
            return recipe
    return None


def stable_candidates(replay_summary: dict[str, Any]) -> list[dict[str, Any]]:
    results = replay_summary.get("results")
    if not isinstance(results, list):
        raise ReductionInputError("replay summary results must be an array")

    candidates: list[dict[str, Any]] = []
    for result_index, result in enumerate(results):
        if not isinstance(result, dict) or result.get("status") not in STABLE_STATUSES:
            continue
        seed = result.get("seed")
        if not isinstance(seed, dict):
            continue
        recipe = first_existing_recipe(seed)
        if recipe is None:
            continue
        expected_signature = result.get("expected_failure_signature")
        attempts = result.get("attempts")
        trusted_signature = seed.get("failure_signature")
        declared_attempt_count = result.get("attempt_count")
        if (
            not isinstance(expected_signature, dict)
            or not expected_signature.get("kind")
            or not isinstance(trusted_signature, dict)
            or expected_signature != trusted_signature
            or not isinstance(attempts, list)
            or len(attempts) < MIN_STABLE_ATTEMPTS
            or not isinstance(declared_attempt_count, int)
            or isinstance(declared_attempt_count, bool)
            or declared_attempt_count != len(attempts)
        ):
            continue
        attempts_verified = True
        for attempt in attempts:
            if not isinstance(attempt, dict) or attempt.get("matches_expected") is not True:
                attempts_verified = False
                break
            observed = attempt.get("failure_signature")
            if not isinstance(observed, dict) or not signatures_match(
                expected_signature,
                observed,
            )[0]:
                attempts_verified = False
                break
        if not attempts_verified:
            continue
        fingerprint = str(result.get("fingerprint") or seed.get("fingerprint") or f"seed_{result_index}")
        case_id = str(
            result.get("representative_case_id")
            or seed.get("representative_case_id")
            or fingerprint
        )
        candidates.append(
            {
                "fingerprint": fingerprint,
                "representative_case_id": case_id,
                "replay_status": result["status"],
                "recipe": recipe,
                "expected_failure_signature": expected_signature,
                "stable_attempts": len(attempts),
            }
        )
    return candidates


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "failure"


def _inside(root: Path, path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ReductionInputError(f"{label} escaped fixed reducer workspace: {resolved}") from exc
    return resolved


def redact_ephemeral_paths(value: Any, workspace: Path, durable_recipe: Path) -> Any:
    if isinstance(value, list):
        return [redact_ephemeral_paths(item, workspace, durable_recipe) for item in value]
    if isinstance(value, dict):
        return {
            str(key): redact_ephemeral_paths(item, workspace, durable_recipe)
            for key, item in value.items()
        }
    if not isinstance(value, str):
        return value
    try:
        relative = Path(value).resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return value
    if relative.name == "reduced_recipe.json":
        return str(durable_recipe)
    return f"<isolated_reducer_workspace>/{relative.as_posix()}"


def _signature_from_observation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    returncode = value.get("returncode")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        return {}
    return build_failure_signature(
        returncode=returncode,
        timed_out=value.get("timed_out") is True,
        stderr=str(value.get("stderr") or ""),
        status=value.get("status") if isinstance(value.get("status"), dict) else {},
        validation=value.get("validation") if isinstance(value.get("validation"), dict) else {},
        topo_check=value.get("topo_check") if isinstance(value.get("topo_check"), dict) else {},
        run_state=value.get("run_state") if isinstance(value.get("run_state"), dict) else {},
    )


def preserved_from_summary(
    summary: dict[str, Any],
    trusted_replay_signature: dict[str, Any],
) -> tuple[bool, str]:
    if summary.get("error"):
        return False, f"reducer_summary_error:{summary['error']}"
    predicate = summary.get("predicate")
    baseline = summary.get("baseline_observation")
    final = summary.get("final_observation")
    if not isinstance(predicate, dict) or not isinstance(baseline, dict) or not isinstance(final, dict):
        return False, "incomplete_reduction_summary"
    expected = predicate.get("failure_signature")
    if not isinstance(expected, dict):
        return False, "missing_failure_signature"
    if not signatures_match(trusted_replay_signature, expected)[0]:
        return False, "reducer_predicate_does_not_match_trusted_replay_signature"
    baseline_signature = _signature_from_observation(baseline)
    if not baseline_signature or not signatures_match(
        trusted_replay_signature,
        baseline_signature,
    )[0]:
        return False, "reducer_baseline_does_not_match_trusted_replay_signature"
    final_signature = _signature_from_observation(final)
    if not final_signature:
        return False, "invalid_final_observation"
    return signatures_match(trusted_replay_signature, final_signature)


def reduction_entry(
    *,
    candidate: dict[str, Any],
    index: int,
    runner: Path,
    reducer: Path,
    out_root: Path,
    timeout: float,
    max_trials: int,
    min_dimension: float,
) -> dict[str, Any]:
    fingerprint = str(candidate["fingerprint"])
    reduce_out = out_root / f"{index:03d}_{safe_name(fingerprint)}"
    base: dict[str, Any] = {
        "fingerprint": fingerprint,
        "representative_case_id": candidate["representative_case_id"],
        "replay_status": candidate["replay_status"],
        "input_recipe": str(candidate["recipe"]),
        "out": str(reduce_out),
        "summary_path": str(reduce_out / "reduction_summary.json"),
        "report_path": str(reduce_out / "reduction_report.md"),
        "reduced_recipe": "",
        "reduced_recipe_sha256": "",
        "reduction_summary_sha256": "",
        "signature_verified": False,
        "trusted_replay_signature": candidate["expected_failure_signature"],
        "stable_attempts": candidate["stable_attempts"],
    }
    with tempfile.TemporaryDirectory(prefix="sggk_reduce_") as temporary:
        workspace = Path(temporary)
        short_out = workspace / "r"
        command = [
            sys.executable,
            str(reducer),
            "--runner",
            str(runner),
            "--recipe",
            str(candidate["recipe"]),
            "--out",
            str(short_out),
            "--timeout",
            str(timeout),
            "--max-trials",
            str(max_trials),
            "--min-dimension",
            str(min_dimension),
        ]
        try:
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout * max_trials + 60.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                **base,
                "returncode": None,
                "ok": False,
                "status": "launch_failed",
                "reason": str(exc),
            }

        base["returncode"] = completed.returncode
        if completed.returncode not in {0, 2}:
            return {
                **base,
                "ok": False,
                "status": "reducer_failed",
                "reason": f"unexpected_reducer_returncode:{completed.returncode}",
            }

        ephemeral_summary = short_out / "reduction_summary.json"
        if not ephemeral_summary.is_file():
            return {
                **base,
                "ok": False,
                "status": "missing_summary",
                "reason": "reduction_summary.json missing",
            }
        try:
            summary = read_json(ephemeral_summary)
        except ReductionInputError as exc:
            return {**base, "ok": False, "status": "invalid_summary", "reason": str(exc)}
        if not isinstance(summary, dict):
            return {
                **base,
                "ok": False,
                "status": "invalid_summary",
                "reason": "summary root must be an object",
            }

        preserved, reason = preserved_from_summary(
            summary,
            candidate["expected_failure_signature"],
        )
        reduced_recipe = summary.get("reduced_recipe")
        if not isinstance(reduced_recipe, str) or not reduced_recipe:
            return {
                **base,
                "ok": False,
                "status": "invalid_summary",
                "reason": "reduced recipe missing",
            }
        try:
            ephemeral_reduced = _inside(
                short_out,
                Path(reduced_recipe),
                label="reduced recipe",
            )
        except ReductionInputError as exc:
            return {**base, "ok": False, "status": "invalid_summary", "reason": str(exc)}
        if not ephemeral_reduced.is_file():
            return {
                **base,
                "ok": False,
                "status": "invalid_summary",
                "reason": f"reduced recipe not found: {ephemeral_reduced}",
            }

        durable_reduced = reduce_out / "reduced_recipe.json"
        reduce_out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ephemeral_reduced, durable_reduced)
        durable_summary = redact_ephemeral_paths(summary, workspace, durable_reduced)
        durable_summary["isolated_workspace_paths_redacted"] = True
        write_json(reduce_out / "reduction_summary.json", durable_summary)
        ephemeral_report = short_out / "reduction_report.md"
        if ephemeral_report.is_file():
            report_text = ephemeral_report.read_text(encoding="utf-8", errors="replace")
            write_text(
                reduce_out / "reduction_report.md",
                report_text.replace(str(workspace), "<isolated_reducer_workspace>"),
            )
        if preserved:
            # Only a signature-preserving result may become the canonical
            # bundled reproducer.
            base["reduced_recipe"] = str(durable_reduced)
            base["reduced_recipe_sha256"] = sha256_file(durable_reduced)
            base["reduction_summary_sha256"] = sha256_file(
                reduce_out / "reduction_summary.json"
            )
            base["signature_verified"] = True

    trials = summary.get("trials")
    accepted = summary.get("accepted_reductions")
    base.update(
        {
            "ok": preserved,
            "status": "preserved" if preserved else "not_preserved",
            "reason": reason,
            "trials": trials if isinstance(trials, int) and not isinstance(trials, bool) else None,
            "accepted_reductions": accepted
            if isinstance(accepted, int) and not isinstance(accepted, bool)
            else 0,
        }
    )
    return base


def run_batch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    runner, replay_path, out_root, reducer = resolve_inputs(args)
    replay_summary = read_json(replay_path)
    if not isinstance(replay_summary, dict):
        raise ReductionInputError("replay summary root must be an object")

    candidates = stable_candidates(replay_summary)
    selected = candidates if args.limit == 0 else candidates[: args.limit]
    out_root.mkdir(parents=True, exist_ok=True)
    reductions = [
        reduction_entry(
            candidate=candidate,
            index=index,
            runner=runner,
            reducer=reducer,
            out_root=out_root,
            timeout=args.timeout,
            max_trials=args.max_trials,
            min_dimension=args.min_dimension,
        )
        for index, candidate in enumerate(selected)
    ]
    index_payload = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "runner": str(runner),
        "replay_summary": str(replay_path),
        "out_root": str(out_root),
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "completed_count": sum(item.get("status") in {"preserved", "not_preserved"} for item in reductions),
        "preserved_count": sum(item.get("status") == "preserved" for item in reductions),
        "accepted_reduction_count": sum(int(item.get("accepted_reductions") or 0) for item in reductions),
        "reduction_limit": args.limit,
        "reduction_max_trials": args.max_trials,
        "reduction_timeout": args.timeout,
        "reduction_min_dimension": args.min_dimension,
        "reductions": reductions,
    }
    write_json(out_root / "reduction_index.json", index_payload)
    operational_failure = any(
        item.get("status") not in {"preserved", "not_preserved"} for item in reductions
    )
    semantic_failure = any(item.get("status") == "not_preserved" for item in reductions)
    return index_payload, 1 if operational_failure else (2 if semantic_failure else 0)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        index, returncode = run_batch(args)
    except ReductionInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"index={Path(index['out_root']) / 'reduction_index.json'}")
    print(
        f"candidates={index['candidate_count']} selected={index['selected_count']} "
        f"completed={index['completed_count']} preserved={index['preserved_count']}"
    )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
