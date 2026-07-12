#!/usr/bin/env python3
"""Replay triaged SGGK regression seeds to confirm reproducibility."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from failure_predicate import signature_from_artifact, signatures_match


CASE_ID_RE = re.compile(r"^case_id=(?P<case_id>.+)$", re.MULTILINE)
ARTIFACT_DIR_RE = re.compile(r"^artifact_dir=(?P<artifact_dir>.+)$", re.MULTILINE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument(
        "--seeds",
        required=True,
        help="Path to regression_seeds.json produced by triage_artifacts.py",
    )
    parser.add_argument("--out", default="artifacts/replay", help="Replay artifact root")
    parser.add_argument("--retries", type=int, default=3, help="Attempts per seed")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-attempt timeout in seconds")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of seeds to replay; 0 means all",
    )
    parser.add_argument(
        "--fail-on-reproduced",
        action="store_true",
        help="Return exit code 2 when any seed reproduces the same failure on every attempt.",
    )
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def as_float(value: Any, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def sanitize_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return text or "seed"


def parse_stdout_field(stdout: str, regex: re.Pattern[str], group: str) -> str:
    match = regex.search(stdout or "")
    return match.group(group).strip() if match else ""


def load_manifest_options(case_dir: str) -> dict[str, Any]:
    if not case_dir:
        return {}
    manifest_path = Path(case_dir) / "manifest.json"
    try:
        manifest = read_json(manifest_path)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    options = manifest.get("options")
    return options if isinstance(options, dict) else {}


def first_existing(paths: list[str]) -> str:
    for raw in paths:
        if raw and Path(raw).is_file():
            return raw
    return ""


def first_existing_recipe(seed: dict[str, Any]) -> str:
    paths = seed.get("recipe_paths")
    if not isinstance(paths, list):
        return ""
    return first_existing([as_str(item) for item in paths])


def seed_api(seed: dict[str, Any]) -> str:
    apis = seed.get("apis")
    if isinstance(apis, list) and apis:
        return as_str(apis[0])
    return ""


def build_replay_recipe(seed: dict[str, Any], case_id: str) -> tuple[dict[str, Any] | None, str]:
    artifact_inputs = seed.get("artifact_inputs")
    if not isinstance(artifact_inputs, dict):
        artifact_inputs = {}

    recipe_path = first_existing_recipe(seed)
    if recipe_path:
        try:
            recipe = read_json(Path(recipe_path))
        except (FileNotFoundError, json.JSONDecodeError):
            recipe = None
        if isinstance(recipe, dict):
            replay_recipe = dict(recipe)
            replay_recipe["case_id"] = case_id
            if replay_recipe.get("api") == "api_boolean":
                for role in ("target", "tool"):
                    if replay_recipe.get(f"{role}_kind") != "loaded_sgt":
                        continue
                    field = f"{role}_source_file"
                    current = as_str(replay_recipe.get(field))
                    frozen = as_str(artifact_inputs.get(f"{role}_sgt"))
                    if (not current or not Path(current).is_file()) and frozen and Path(frozen).is_file():
                        replay_recipe[field] = frozen
            elif replay_recipe.get("api") in {"check_sgt", "step_import", "iges_import", "step_roundtrip", "iges_roundtrip"}:
                current = as_str(replay_recipe.get("source_file"))
                if not current or not Path(current).is_file():
                    api = as_str(replay_recipe.get("api"))
                    keys = {
                        "check_sgt": ("source_sgt",),
                        "step_import": ("source_step", "source_stp"),
                        "step_roundtrip": ("source_sgt",),
                        "iges_import": ("source_iges", "source_igs"),
                        "iges_roundtrip": ("source_sgt",),
                    }.get(api, ())
                    frozen = first_existing([as_str(artifact_inputs.get(key)) for key in keys])
                    if frozen:
                        replay_recipe["source_file"] = frozen
            return replay_recipe, ""

    api = seed_api(seed)
    options = load_manifest_options(as_str(seed.get("representative_case_dir")))

    target_sgt = as_str(artifact_inputs.get("target_sgt"))
    tool_sgt = as_str(artifact_inputs.get("tool_sgt"))
    if api == "api_boolean" and target_sgt and tool_sgt:
        if not Path(target_sgt).is_file() or not Path(tool_sgt).is_file():
            return None, "missing_boolean_sgt_input"
        return (
            {
                "case_id": case_id,
                "api": "api_boolean",
                "boolean_type": as_str(options.get("boolean_type")) or "SUBTRACTION",
                "modeling_tol": as_float(options.get("modeling_tol"), 0.01),
                "check_valid": as_bool(options.get("check_valid"), True),
                "topo_track": as_bool(options.get("topo_track"), True),
                "non_destructive": as_bool(options.get("non_destructive"), True),
                "target_kind": "loaded_sgt",
                "target_source_file": target_sgt,
                "target_body_index": 0,
                "tool_kind": "loaded_sgt",
                "tool_source_file": tool_sgt,
                "tool_body_index": 0,
            },
            "",
        )

    source_sgt = first_existing(
        [
            as_str(artifact_inputs.get("source_sgt")),
            *[as_str(item) for item in seed.get("source_files", []) if isinstance(item, str) and item.lower().endswith(".sgt")],
        ]
    )
    if source_sgt:
        return {"case_id": case_id, "api": "check_sgt", "source_file": source_sgt}, ""

    source_step = first_existing(
        [
            as_str(artifact_inputs.get("source_step")),
            as_str(artifact_inputs.get("source_stp")),
            *[
                as_str(item)
                for item in seed.get("source_files", [])
                if isinstance(item, str) and item.lower().endswith((".step", ".stp"))
            ],
        ]
    )
    if source_step:
        return {"case_id": case_id, "api": "step_import", "source_file": source_step}, ""

    source_iges = first_existing(
        [
            as_str(artifact_inputs.get("source_iges")),
            as_str(artifact_inputs.get("source_igs")),
            *[
                as_str(item)
                for item in seed.get("source_files", [])
                if isinstance(item, str) and item.lower().endswith((".iges", ".igs"))
            ],
        ]
    )
    if source_iges:
        return {"case_id": case_id, "api": "iges_import", "source_file": source_iges}, ""

    return None, "no_replayable_seed_input"


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
        elapsed = time.perf_counter() - started
        result = {
            "recipe": str(recipe_path),
            "returncode": completed.returncode,
            "elapsed_seconds": elapsed,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "case_id": parse_stdout_field(completed.stdout, CASE_ID_RE, "case_id"),
            "artifact_dir": parse_stdout_field(completed.stdout, ARTIFACT_DIR_RE, "artifact_dir"),
        }
        result["failure_signature"] = signature_from_artifact(
            result["artifact_dir"],
            returncode=completed.returncode,
            timed_out=False,
            stderr=completed.stderr,
        )
        return result
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        result = {
            "recipe": str(recipe_path),
            "returncode": 124,
            "elapsed_seconds": elapsed,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
            "case_id": "",
            "artifact_dir": "",
        }
        result["failure_signature"] = signature_from_artifact(
            "",
            returncode=124,
            timed_out=True,
            stderr=result["stderr"],
        )
        return result


def expected_failure_signature(seed: dict[str, Any]) -> dict[str, Any]:
    explicit = seed.get("failure_signature")
    if isinstance(explicit, dict) and explicit.get("kind"):
        return explicit
    runner = seed.get("runner") if isinstance(seed.get("runner"), dict) else {}
    case_dir = as_str(seed.get("representative_case_dir"))
    if case_dir:
        return signature_from_artifact(
            case_dir,
            returncode=int(runner.get("returncode") or 2),
            timed_out=bool(runner.get("timed_out")),
            stderr=as_str(runner.get("stderr")),
        )
    return {}


def classify_attempts(
    attempts: list[dict[str, Any]],
    expected: dict[str, Any],
) -> str:
    if not attempts:
        return "unavailable"
    failures = [item for item in attempts if item.get("returncode") != 0]
    if not failures:
        return "not_reproduced"
    if not expected:
        return "unverified_failure" if len(failures) == len(attempts) else "flaky_unverified"
    matches = sum(1 for item in attempts if item.get("matches_expected") is True)
    if matches == len(attempts):
        return "stable_same_failure"
    if matches:
        return "flaky_same_failure"
    unobserved_failures = [
        item
        for item in failures
        if str(item.get("match_reason") or "").endswith("_unobserved")
    ]
    if len(unobserved_failures) == len(failures):
        return "unverified_failure" if len(failures) == len(attempts) else "flaky_unverified"
    return "changed_failure"


def replay_seed(
    runner: Path,
    seed: dict[str, Any],
    index: int,
    out_root: Path,
    retries: int,
    timeout: float,
) -> dict[str, Any]:
    fingerprint = as_str(seed.get("fingerprint")) or f"seed_{index}"
    seed_id = sanitize_id(fingerprint)
    attempts: list[dict[str, Any]] = []
    expected_signature = expected_failure_signature(seed)
    recipe_dir = out_root / "_recipes"

    for attempt_index in range(1, retries + 1):
        case_id = f"replay_{seed_id}_{attempt_index}"
        recipe, unavailable_reason = build_replay_recipe(seed, case_id)
        if recipe is None:
            return {
                "fingerprint": fingerprint,
                "representative_case_id": seed.get("representative_case_id"),
                "status": "unavailable",
                "unavailable_reason": unavailable_reason,
                "attempts": [],
                "seed": seed,
            }
        recipe_path = recipe_dir / f"{case_id}.json"
        write_json(recipe_path, recipe)
        print(f"[seed {index + 1}] attempt {attempt_index}/{retries} {fingerprint}")
        attempt = run_one(runner, recipe_path, out_root, timeout)
        observed = attempt.get("failure_signature") if isinstance(attempt.get("failure_signature"), dict) else {}
        if expected_signature:
            matched, reason = signatures_match(expected_signature, observed)
            attempt["matches_expected"] = matched
            attempt["match_reason"] = reason
        else:
            attempt["matches_expected"] = None
            attempt["match_reason"] = "missing_expected_failure_signature"
        attempts.append(attempt)

    status = classify_attempts(attempts, expected_signature)
    return {
        "fingerprint": fingerprint,
        "representative_case_id": seed.get("representative_case_id"),
        "representative_case_dir": seed.get("representative_case_dir"),
        "status": status,
        "expected_failure_signature": expected_signature,
        "attempt_count": len(attempts),
        "failed_attempts": sum(1 for item in attempts if item.get("returncode") != 0),
        "passed_attempts": sum(1 for item in attempts if item.get("returncode") == 0),
        "timed_out_attempts": sum(1 for item in attempts if item.get("timed_out")),
        "attempts": attempts,
        "seed": seed,
    }


def write_report(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# SGGK Regression Seed Replay",
        "",
        f"- Seeds: {summary['total']}",
        f"- Stable same failures: {summary['stable_same_failure']}",
        f"- Flaky same failures: {summary['flaky_same_failure']}",
        f"- Changed failures: {summary['changed_failure']}",
        f"- Unverified failures: {summary['unverified_failure']}",
        f"- Not reproduced: {summary['not_reproduced']}",
        f"- Unavailable: {summary['unavailable']}",
        "",
        "## Seeds",
        "",
    ]
    for result in summary["results"]:
        lines.append(f"### {result['fingerprint']}")
        lines.append("")
        lines.append(f"- Status: `{result['status']}`")
        if result.get("representative_case_id"):
            lines.append(f"- Representative: `{result['representative_case_id']}`")
        if result.get("unavailable_reason"):
            lines.append(f"- Unavailable reason: `{result['unavailable_reason']}`")
        if result.get("attempts"):
            returns = [item.get("returncode") for item in result["attempts"]]
            lines.append(f"- Return codes: {returns}")
            artifact_dirs = [item.get("artifact_dir") for item in result["attempts"] if item.get("artifact_dir")]
            if artifact_dirs:
                lines.append(f"- First artifact: `{artifact_dirs[0]}`")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.retries <= 0:
        print("--retries must be >= 1", file=sys.stderr)
        return 1
    if args.timeout <= 0:
        print("--timeout must be > 0", file=sys.stderr)
        return 1

    runner = Path(args.runner).resolve()
    if not runner.is_file():
        print(f"runner not found: {runner}", file=sys.stderr)
        return 1

    seeds_path = Path(args.seeds)
    seeds = read_json(seeds_path)
    if not isinstance(seeds, list):
        print("seeds file must contain a JSON array", file=sys.stderr)
        return 1
    if args.limit > 0:
        seeds = seeds[: args.limit]

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    started_at = now_iso_like()

    results = [
        replay_seed(runner, seed, index, out_root, args.retries, args.timeout)
        for index, seed in enumerate(seeds)
        if isinstance(seed, dict)
    ]
    summary = {
        "runner": str(runner),
        "seeds_path": str(seeds_path),
        "out_root": str(out_root),
        "started_at": started_at,
        "updated_at": now_iso_like(),
        "total": len(results),
        "stable_same_failure": sum(1 for item in results if item["status"] == "stable_same_failure"),
        "flaky_same_failure": sum(1 for item in results if item["status"] == "flaky_same_failure"),
        "changed_failure": sum(1 for item in results if item["status"] == "changed_failure"),
        "unverified_failure": sum(1 for item in results if item["status"] in {"unverified_failure", "flaky_unverified"}),
        "not_reproduced": sum(1 for item in results if item["status"] == "not_reproduced"),
        "unavailable": sum(1 for item in results if item["status"] == "unavailable"),
        "results": results,
    }
    summary_path = out_root / "replay_summary.json"
    report_path = out_root / "replay_report.md"
    write_json(summary_path, summary)
    write_report(summary, report_path)
    print(f"summary={summary_path}")
    print(f"report={report_path}")
    print(
        f"seeds={summary['total']} stable_same_failure={summary['stable_same_failure']} "
        f"flaky_same_failure={summary['flaky_same_failure']} changed_failure={summary['changed_failure']} "
        f"not_reproduced={summary['not_reproduced']} "
        f"unavailable={summary['unavailable']}"
    )
    if args.fail_on_reproduced and summary["stable_same_failure"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
