#!/usr/bin/env python3
"""Probe whether failed topo-track-enabled recipes pass with topo_track disabled."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument("--summary", required=True, help="recipe_summary.json produced by run_recipes.py")
    parser.add_argument("--out", required=True, help="Output directory for probe recipes, runs, and reports")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-recipe timeout for probe runs")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel probe runner processes")
    parser.add_argument("--limit", type=int, default=0, help="Maximum selected failures to probe; 0 means all")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"_json_error": f"{exc.msg} at line {exc.lineno}, column {exc.colno}"}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def is_process_crash_returncode(value: Any) -> bool:
    code = as_int(value, 0)
    return code < 0 or code >= 128


def artifact_has(path: Path, relative: str) -> bool:
    return (path / relative).is_file()


def selection_reason(result: dict[str, Any]) -> str:
    returncode = result.get("returncode")
    if returncode in (0, None):
        return "runner_did_not_fail"
    if as_bool(result.get("timed_out"), False):
        return "runner_timed_out"

    recipe_path = Path(as_str(result.get("recipe")))
    recipe = load_json(recipe_path)
    if not isinstance(recipe, dict):
        return "recipe_missing_or_invalid"
    if not as_bool(recipe.get("topo_track"), True):
        return "recipe_topo_track_not_enabled"

    artifact_text = as_str(result.get("artifact_dir"))
    if not artifact_text:
        return "artifact_dir_missing"
    artifact_dir = Path(artifact_text)
    if not artifact_has(artifact_dir, "report/status.json"):
        return "status_report_missing"
    if not artifact_has(artifact_dir, "report/topo_check.json"):
        return "topo_check_report_missing"
    if artifact_has(artifact_dir, "report/validation.json"):
        return "validation_report_exists"
    return "selected"


def selected_results(summary: dict[str, Any], limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for result in summary.get("results", []):
        if not isinstance(result, dict):
            continue
        reason = selection_reason(result)
        if reason == "selected":
            if limit <= 0 or len(selected) < limit:
                selected.append(result)
            else:
                skipped.append({"case_id": as_str(result.get("case_id")), "reason": "limit_reached"})
        else:
            skipped.append({"case_id": as_str(result.get("case_id")), "reason": reason})
    return selected, skipped


def probe_recipe_name(result: dict[str, Any]) -> str:
    case_id = as_str(result.get("case_id")) or Path(as_str(result.get("recipe"))).stem
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in case_id).strip("._")
    return (safe or "case") + ".json"


def write_probe_recipes(selected: list[dict[str, Any]], recipe_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for result in selected:
        source_path = Path(as_str(result.get("recipe")))
        recipe = load_json(source_path)
        if not isinstance(recipe, dict):
            records.append({
                "case_id": as_str(result.get("case_id")),
                "source_recipe": str(source_path),
                "status": "unavailable",
                "reason": "source recipe missing or invalid",
            })
            continue
        probe = deepcopy(recipe)
        probe["topo_track"] = False
        target_path = recipe_dir / probe_recipe_name(result)
        write_json(target_path, probe)
        records.append({
            "case_id": as_str(result.get("case_id")),
            "source_recipe": str(source_path),
            "source_artifact_dir": as_str(result.get("artifact_dir")),
            "source_returncode": result.get("returncode"),
            "probe_recipe": str(target_path),
            "status": "written",
        })
    return records


def run_probe_recipes(args: argparse.Namespace, recipe_dir: Path, out_dir: Path) -> dict[str, Any]:
    run_out = out_dir / "runs"
    triage_out = out_dir / "triage"
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("run_recipes.py")),
        "--runner",
        str(Path(args.runner)),
        "--recipe",
        str(recipe_dir),
        "--out",
        str(run_out),
        "--timeout",
        str(args.timeout),
        "--jobs",
        str(args.jobs),
        "--triage-out",
        str(triage_out),
    ]
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
        "run_summary": str(run_out / "recipe_summary.json"),
        "triage": str(triage_out),
    }


def result_by_case_id(summary: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(summary, dict):
        return {}
    results: dict[str, dict[str, Any]] = {}
    for item in summary.get("results", []):
        if isinstance(item, dict):
            case_id = as_str(item.get("case_id"))
            if case_id:
                results[case_id] = item
    return results


def validation_status(artifact_dir: Any) -> dict[str, Any]:
    path_text = as_str(artifact_dir)
    if not path_text:
        return {"available": False, "ok": False, "reason": "artifact_dir_missing"}
    validation = load_json(Path(path_text) / "report" / "validation.json")
    if not isinstance(validation, dict):
        return {"available": False, "ok": False, "reason": "validation_missing_or_invalid"}
    if validation.get("_json_error"):
        return {"available": False, "ok": False, "reason": as_str(validation.get("_json_error"))}
    return {
        "available": True,
        "ok": validation.get("ok") is True,
        "failures": validation.get("failures") if isinstance(validation.get("failures"), list) else [],
    }


def classify_probe(record: dict[str, Any], probe_result: dict[str, Any] | None) -> dict[str, Any]:
    if record.get("status") != "written":
        return {**record, "classification": "unavailable"}
    if not probe_result:
        return {**record, "classification": "unavailable", "reason": "probe result missing"}

    validation = validation_status(probe_result.get("artifact_dir"))
    returncode = probe_result.get("returncode")
    if returncode == 0 and validation.get("ok") is True:
        classification = "topotrack_only_modeling_ok"
    elif is_process_crash_returncode(returncode) and not validation.get("available"):
        classification = "still_crashes_without_topotrack"
    else:
        classification = "modeling_or_validation_failure_after_topotrack_disabled"

    return {
        **record,
        "classification": classification,
        "probe_returncode": returncode,
        "probe_artifact_dir": probe_result.get("artifact_dir"),
        "probe_validation": validation,
        "probe_timed_out": probe_result.get("timed_out"),
    }


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Topo-track Crash Probe",
        "",
        f"- Started: `{summary.get('started_at')}`",
        f"- Source summary: `{summary.get('source_summary')}`",
        f"- Selected: `{summary.get('selected_count')}`",
        f"- Status counts: `{summary.get('classification_counts')}`",
        "",
    ]
    run = summary.get("probe_run")
    if isinstance(run, dict):
        lines.extend([
            "## Probe Run",
            "",
            f"- Return code: `{run.get('returncode')}`",
            f"- Run summary: `{run.get('run_summary')}`",
            f"- Triage: `{run.get('triage')}`",
            "",
        ])
    lines.extend([
        "## Cases",
        "",
        "| case | classification | original rc | probe rc | validation |",
        "| --- | --- | --- | --- | --- |",
    ])
    for item in summary.get("results", []):
        validation = item.get("probe_validation") if isinstance(item.get("probe_validation"), dict) else {}
        lines.append(
            "| `{case}` | `{classification}` | `{source_rc}` | `{probe_rc}` | `{validation}` |".format(
                case=item.get("case_id", ""),
                classification=item.get("classification", item.get("status", "")),
                source_rc=item.get("source_returncode", ""),
                probe_rc=item.get("probe_returncode", ""),
                validation="ok" if validation.get("ok") is True else validation.get("reason", ""),
            )
        )
    lines.append("")
    write_text(path, "\n".join(lines))


def main() -> int:
    args = parse_args()
    if args.timeout <= 0.0:
        raise SystemExit("--timeout must be > 0")
    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")
    if args.limit < 0:
        raise SystemExit("--limit must be >= 0")

    started_at = now_iso_like()
    out_dir = Path(args.out)
    recipe_dir = out_dir / "recipes"
    summary_path = Path(args.summary)
    source_summary = load_json(summary_path)
    if not isinstance(source_summary, dict):
        raise SystemExit(f"could not read recipe summary: {summary_path}")

    selected, skipped = selected_results(source_summary, args.limit)
    records = write_probe_recipes(selected, recipe_dir)
    write_text(out_dir / "probe_recipes.txt", "\n".join(record["probe_recipe"] for record in records if record.get("probe_recipe")) + "\n")

    probe_run: dict[str, Any] | None = None
    probe_results_by_case: dict[str, dict[str, Any]] = {}
    if any(record.get("status") == "written" for record in records):
        probe_run = run_probe_recipes(args, recipe_dir, out_dir)
        probe_summary = load_json(Path(as_str(probe_run.get("run_summary"))))
        probe_results_by_case = result_by_case_id(probe_summary)

    classified = [classify_probe(record, probe_results_by_case.get(as_str(record.get("case_id")))) for record in records]
    counts = Counter(as_str(item.get("classification")) or as_str(item.get("status")) for item in classified)
    summary = {
        "started_at": started_at,
        "source_summary": str(summary_path),
        "out": str(out_dir),
        "selected_count": len(selected),
        "skipped_count": len(skipped),
        "classification_counts": dict(counts),
        "probe_run": probe_run or {},
        "results": classified,
        "skipped": skipped,
    }
    write_json(out_dir / "topotrack_probe_summary.json", summary)
    write_report(out_dir / "topotrack_probe_report.md", summary)
    print(f"summary={out_dir / 'topotrack_probe_summary.json'}")
    print(f"report={out_dir / 'topotrack_probe_report.md'}")
    print(f"selected={len(selected)} classifications={dict(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
