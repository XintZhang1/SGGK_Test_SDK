#!/usr/bin/env python3
"""Classify a bug-registry replay lane as still failing, fixed, or changed."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, help="bug_registry.json or registry output directory")
    parser.add_argument("--recipe-summary", required=True, help="recipe_summary.json produced by run_recipes.py")
    parser.add_argument("--out", required=True, help="Output directory for registry_regression.json/md")
    parser.add_argument("--fail-on-fixed", action="store_true", help="Return 2 when a registered bug no longer reproduces")
    parser.add_argument("--fail-on-changed", action="store_true", help="Return 2 when a registered bug fails differently")
    parser.add_argument("--fail-on-unavailable", action="store_true", help="Return 2 when a registered replay recipe was not run")
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


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


def normalize_registry_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        return path / "bug_registry.json"
    return path


def normalized_path_key(raw: Any) -> str:
    text = as_str(raw)
    if not text:
        return ""
    return str(Path(text).resolve()).lower()


def validation_failures_from_artifact(artifact_dir: Any) -> list[str]:
    path_text = as_str(artifact_dir)
    if not path_text:
        return []
    validation = load_json(Path(path_text) / "report" / "validation.json")
    if isinstance(validation, dict) and isinstance(validation.get("failures"), list):
        return [as_str(item) for item in validation["failures"] if as_str(item)]
    return []


def roundtrip_failures_from_artifact(artifact_dir: Any) -> list[str]:
    path_text = as_str(artifact_dir)
    if not path_text:
        return []
    roundtrip = load_json(Path(path_text) / "report" / "roundtrip_comparison.json")
    if isinstance(roundtrip, dict):
        failures = roundtrip.get("failures")
        if isinstance(failures, list):
            return [as_str(item) for item in failures if as_str(item)]
        if roundtrip.get("ok") is False:
            return ["roundtrip_comparison_failed"]
    return []


def topo_track_diagnostic_from_artifact(artifact_dir: Any) -> dict[str, Any]:
    path_text = as_str(artifact_dir)
    if not path_text:
        return {"status": "unavailable", "reason": "artifact_dir unavailable"}
    summary = load_json(Path(path_text) / "report" / "topo_track_summary.json")
    if summary is None:
        return {"status": "missing", "reason": "topo_track_summary.json not found"}
    if not isinstance(summary, dict):
        return {"status": "invalid", "reason": "topo_track_summary root is not an object"}
    if summary.get("_json_error"):
        return {"status": "invalid", "reason": summary.get("_json_error")}
    diagnostic = {
        "status": "ok",
        "reason": "",
        "skipped": bool(summary.get("skipped", False)),
        "skip_reason": as_str(summary.get("reason")),
        "item_count": as_int(summary.get("item_count")),
        "ancestor_count": as_int(summary.get("ancestor_count")),
        "resolved_ancestor_count": as_int(summary.get("resolved_ancestor_count")),
        "unresolved_ancestor_count": as_int(summary.get("unresolved_ancestor_count")),
        "ambiguous_ancestor_count": as_int(summary.get("ambiguous_ancestor_count")),
        "ancestor_input_role_counts": summary.get("ancestor_input_role_counts", {}),
    }
    if diagnostic["skipped"]:
        diagnostic["status"] = "skipped"
        diagnostic["reason"] = diagnostic["skip_reason"]
    elif diagnostic["unresolved_ancestor_count"] or diagnostic["ambiguous_ancestor_count"]:
        diagnostic["status"] = "incomplete"
        diagnostic["reason"] = "topo-track has unresolved or ambiguous ancestors"
    elif diagnostic["item_count"] == 0 and diagnostic["ancestor_count"] == 0:
        diagnostic["status"] = "empty"
        diagnostic["reason"] = "topo-track produced no items"
    return diagnostic


def topo_track_note(policy: str, diagnostic: dict[str, Any], actual_failures: list[str], returncode: Any) -> str:
    status = as_str(diagnostic.get("status"))
    if status in {"ok"}:
        return ""
    if policy == "diagnostic_when_modeling_fails" and (actual_failures or returncode not in (0, None)):
        return f"modeling failure reproduced; topo-track diagnostic is {status}"
    if policy == "ignore":
        return ""
    return f"topo-track diagnostic is {status}"


def result_by_recipe(summary: Any) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    if not isinstance(summary, dict):
        return lookup
    for item in as_list(summary.get("results")):
        if not isinstance(item, dict):
            continue
        key = normalized_path_key(item.get("recipe"))
        if key:
            lookup[key] = item
    return lookup


def expected_recipe_path(bug: dict[str, Any]) -> str:
    paths = bug.get("paths") if isinstance(bug.get("paths"), dict) else {}
    return as_str(paths.get("replay_recipe")) or as_str(paths.get("original_recipe"))


def classify_bug(bug: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    expected_failures = [as_str(item) for item in as_list(bug.get("validation_failures")) if as_str(item)]
    expected_roundtrip_failures = [as_str(item) for item in as_list(bug.get("roundtrip_failures")) if as_str(item)]
    expected = bug.get("expected") if isinstance(bug.get("expected"), dict) else {}
    expected_returncode = expected.get("returncode") if isinstance(expected.get("returncode"), int) else None
    expected_timeout = bool(expected.get("runner_timeout", False))
    expected_status = as_str(bug.get("replay_status"))
    topo_policy = as_str(bug.get("topo_track_policy")) or "diagnostic_when_modeling_fails"
    recipe = expected_recipe_path(bug)
    base = {
        "fingerprint": bug.get("fingerprint"),
        "bug_id": bug.get("bug_id"),
        "representative_case_id": bug.get("representative_case_id"),
        "expected_replay_status": expected_status,
        "expected_returncode": expected_returncode,
        "expected_runner_timeout": expected_timeout,
        "expected_validation_failures": expected_failures,
        "expected_roundtrip_failures": expected_roundtrip_failures,
        "topo_track_policy": topo_policy,
        "topo_track_required": bool(bug.get("topo_track_required", False)),
        "recipe": recipe,
    }
    if result is None:
        return {**base, "status": "unavailable", "reason": "replay recipe not found in recipe_summary results"}

    returncode = result.get("returncode")
    artifact_dir = result.get("artifact_dir")
    actual_failures = validation_failures_from_artifact(artifact_dir)
    actual_roundtrip_failures = roundtrip_failures_from_artifact(artifact_dir)
    actual_all_failures = actual_failures + actual_roundtrip_failures
    topo_diagnostic = topo_track_diagnostic_from_artifact(artifact_dir)
    result_patch = {
        "result_case_id": result.get("case_id"),
        "artifact_dir": artifact_dir,
        "returncode": returncode,
        "timed_out": result.get("timed_out", False),
        "actual_validation_failures": actual_failures,
        "actual_roundtrip_failures": actual_roundtrip_failures,
        "topo_track_diagnostic": topo_diagnostic,
        "topo_track_note": topo_track_note(topo_policy, topo_diagnostic, actual_all_failures, returncode),
    }
    if result.get("timed_out"):
        if expected_timeout:
            return {**base, **result_patch, "status": "still_failing", "reason": "expected runner timeout reproduced"}
        return {**base, **result_patch, "status": "changed_failure", "reason": "runner timed out"}
    if returncode == 0:
        return {**base, **result_patch, "status": "fixed_or_not_reproduced", "reason": "runner returned success"}
    if expected_timeout:
        return {**base, **result_patch, "status": "changed_failure", "reason": "expected runner timeout did not reproduce"}
    if expected_returncode is not None and returncode != expected_returncode:
        return {
            **base,
            **result_patch,
            "status": "changed_failure",
            "reason": "runner returncode changed",
        }
    if expected_failures:
        missing = [item for item in expected_failures if item not in actual_failures]
        if missing:
            return {
                **base,
                **result_patch,
                "status": "changed_failure",
                "reason": "expected validation failures missing",
                "missing_expected_failures": missing,
            }
    if expected_roundtrip_failures:
        missing = [item for item in expected_roundtrip_failures if item not in actual_roundtrip_failures]
        if missing:
            return {
                **base,
                **result_patch,
                "status": "changed_failure",
                "reason": "expected roundtrip failures missing",
                "missing_expected_roundtrip_failures": missing,
            }
    return {**base, **result_patch, "status": "still_failing", "reason": "expected failure reproduced"}


def build_report(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Bug Registry Regression")
    lines.append("")
    lines.append(f"- Generated: `{summary.get('generated_at')}`")
    lines.append(f"- Registry: `{summary.get('registry')}`")
    lines.append(f"- Recipe summary: `{summary.get('recipe_summary')}`")
    lines.append(f"- Total: `{summary.get('total')}`")
    lines.append(f"- Status counts: `{summary.get('status_counts')}`")
    lines.append("")
    lines.append("| fingerprint | status | case | returncode | expected failures | actual failures | topo-track |")
    lines.append("| --- | --- | --- | ---: | --- | --- | --- |")
    for item in summary.get("results", []):
        if not isinstance(item, dict):
            continue
        topo = item.get("topo_track_diagnostic") if isinstance(item.get("topo_track_diagnostic"), dict) else {}
        lines.append(
            "| `{fp}` | `{status}` | `{case}` | {returncode} | {expected} | {actual} | `{topo}` |".format(
                fp=item.get("fingerprint"),
                status=item.get("status"),
                case=item.get("representative_case_id"),
                returncode=item.get("returncode", ""),
                expected=", ".join(as_list(item.get("expected_validation_failures")) + as_list(item.get("expected_roundtrip_failures"))),
                actual=", ".join(as_list(item.get("actual_validation_failures")) + as_list(item.get("actual_roundtrip_failures"))),
                topo=topo.get("status", ""),
            )
        )
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    for item in summary.get("results", []):
        if not isinstance(item, dict):
            continue
        lines.append(f"### {item.get('fingerprint')}")
        lines.append("")
        lines.append(f"- Status: `{item.get('status')}`")
        if item.get("reason"):
            lines.append(f"- Reason: `{item.get('reason')}`")
        if item.get("artifact_dir"):
            lines.append(f"- Artifact: `{item.get('artifact_dir')}`")
        if item.get("recipe"):
            lines.append(f"- Recipe: `{item.get('recipe')}`")
        topo = item.get("topo_track_diagnostic") if isinstance(item.get("topo_track_diagnostic"), dict) else {}
        if topo:
            lines.append(f"- Topo-track: `{topo.get('status')}` reason=`{topo.get('reason', '')}`")
        if item.get("topo_track_note"):
            lines.append(f"- Topo-track note: {item.get('topo_track_note')}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    registry_path = normalize_registry_path(args.registry).resolve()
    recipe_summary_path = Path(args.recipe_summary).resolve()
    registry = load_json(registry_path)
    recipe_summary = load_json(recipe_summary_path)
    if not isinstance(registry, dict):
        print(f"invalid registry: {registry_path}")
        return 1
    if not isinstance(recipe_summary, dict):
        print(f"invalid recipe summary: {recipe_summary_path}")
        return 1

    results_by_recipe = result_by_recipe(recipe_summary)
    results = []
    for bug in as_list(registry.get("bugs")):
        if not isinstance(bug, dict):
            continue
        recipe = expected_recipe_path(bug)
        results.append(classify_bug(bug, results_by_recipe.get(normalized_path_key(recipe))))
    status_counts = Counter(as_str(item.get("status")) for item in results)
    summary = {
        "generated_at": now_iso_like(),
        "registry": str(registry_path),
        "recipe_summary": str(recipe_summary_path),
        "total": len(results),
        "status_counts": dict(sorted(status_counts.items())),
        "results": results,
    }
    out_dir = Path(args.out).resolve()
    write_json(out_dir / "registry_regression.json", summary)
    (out_dir / "registry_regression.md").write_text(build_report(summary), encoding="utf-8")
    print(f"summary={out_dir / 'registry_regression.json'}")
    print(f"report={out_dir / 'registry_regression.md'}")
    print(f"total={summary['total']} status_counts={summary['status_counts']}")

    if args.fail_on_fixed and status_counts.get("fixed_or_not_reproduced", 0):
        return 2
    if args.fail_on_changed and status_counts.get("changed_failure", 0):
        return 2
    if args.fail_on_unavailable and status_counts.get("unavailable", 0):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
