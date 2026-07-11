#!/usr/bin/env python3
"""Qualify triaged failures before creating SDK bug candidates.

The qualification gate intentionally proves only obvious exclusions. Unknown
or ambiguous cases remain investigation candidates; they are never labelled as
confirmed SDK bugs by this tool.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import time
from typing import Any


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _resolve(raw: Any, bases: list[Path]) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if path.is_absolute() and path.exists():
        return path.resolve()
    for base in bases:
        candidate = (base / path).resolve()
        if candidate.exists():
            return candidate
    return None


def _first_bbox(input_properties: dict[str, Any], role: str) -> dict[str, Any]:
    values = input_properties.get(role)
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        return {}
    return _dict(values[0].get("bbox"))


def _bbox_gap(a: dict[str, Any], b: dict[str, Any]) -> tuple[list[float], float] | None:
    a_min, a_max = a.get("min"), a.get("max")
    b_min, b_max = b.get("min"), b.get("max")
    if not all(
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
        for value in (a_min, a_max, b_min, b_max)
    ):
        return None
    gaps = [
        max(0.0, float(b_min[index]) - float(a_max[index]), float(a_min[index]) - float(b_max[index]))
        for index in range(3)
    ]
    return gaps, sum(gap * gap for gap in gaps) ** 0.5


def _intersection_nonempty_contradiction(
    recipe: dict[str, Any],
    case_dir: Path | None,
) -> dict[str, Any] | None:
    if recipe.get("api") != "api_boolean" or recipe.get("boolean_type") != "INTERSECTION":
        return None
    expectations = _dict(recipe.get("expectations"))
    result_bodies = _dict(expectations.get("result_bodies"))
    minimum = result_bodies.get("min")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum <= 0 or case_dir is None:
        return None
    properties_path = case_dir / "report" / "input_properties.json"
    if not properties_path.is_file():
        return None
    input_properties = _dict(_read(properties_path))
    gap = _bbox_gap(_first_bbox(input_properties, "target"), _first_bbox(input_properties, "tool"))
    if gap is None:
        return None
    axis_gaps, distance = gap
    tolerance = max(0.0, _number(recipe.get("modeling_tol"), 0.0))
    if distance <= tolerance:
        return None
    return {
        "rule_id": "boolean_intersection_nonempty_vs_disjoint_bbox_v1",
        "assertion": (
            "The recipe requires a non-empty Boolean intersection while the serialized input "
            "bounding boxes are separated by more than modeling_tol."
        ),
        "axis_gaps": axis_gaps,
        "bbox_distance": distance,
        "modeling_tol": tolerance,
        "expected_min_result_bodies": minimum,
        "evidence_quality": "deterministic_necessary_condition",
    }


def qualify_group(group: dict[str, Any], triage_root: Path) -> dict[str, Any]:
    bases = [triage_root, triage_root.parent, Path.cwd()]
    case_dir = _resolve(group.get("representative_case_dir"), bases)
    recipe_paths = _list(group.get("recipe_paths"))
    recipe_path = _resolve(recipe_paths[0], bases) if recipe_paths else None
    recipe = _dict(_read(recipe_path)) if recipe_path and recipe_path.is_file() else {}
    failure_signature = _dict(group.get("representative_failure_signature"))
    validation_failures = [str(item) for item in _list(group.get("representative_validation_failures"))]
    reasons = [str(item) for item in _list(group.get("reasons"))]
    exclusion = _intersection_nonempty_contradiction(recipe, case_dir)
    evidence: list[dict[str, Any]] = []
    if exclusion:
        evidence.append({"evidence_id": "qualification_bbox_necessary_condition", **exclusion})
        classification = "test_generation_defect"
        eligible = False
        confidence = 0.99
    elif failure_signature.get("kind") in {"timeout", "crash", "sdk_status"} or any(
        reason in {"api_error", "api_failed", "runner_timeout"} for reason in reasons
    ):
        classification = "sdk_or_runtime_investigation_candidate"
        eligible = True
        confidence = 0.75
    elif validation_failures:
        classification = "oracle_test_or_sdk_investigation_candidate"
        eligible = True
        confidence = 0.45
    elif any(reason in {"runner_nonzero_exit", "missing_artifact", "invalid_json"} for reason in reasons):
        classification = "harness_or_infrastructure_investigation_candidate"
        eligible = True
        confidence = 0.55
    else:
        classification = "inconclusive_investigation_candidate"
        eligible = True
        confidence = 0.25
    return {
        "fingerprint": str(group.get("fingerprint") or ""),
        "representative_case_id": str(group.get("representative_case_id") or ""),
        "classification": classification,
        "eligible_for_bug_investigation": eligible,
        "qualification_confidence": confidence,
        "assessment_status": "candidate_only" if eligible else "excluded_from_sdk_bug_candidates",
        "failure_signature": failure_signature,
        "reasons": reasons,
        "validation_failures": validation_failures,
        "evidence": evidence,
        "source_paths": {
            "case_dir": str(case_dir) if case_dir else "",
            "recipe": str(recipe_path) if recipe_path else "",
        },
    }


def qualify(triage_root: Path, out: Path) -> dict[str, Any]:
    summary_path = triage_root / "triage_summary.json"
    seeds_path = triage_root / "regression_seeds.json"
    summary = _dict(_read(summary_path))
    groups = [item for item in _list(summary.get("failure_groups")) if isinstance(item, dict)]
    records = [qualify_group(group, triage_root) for group in groups]
    eligible_fingerprints = {
        item["fingerprint"] for item in records if item["eligible_for_bug_investigation"]
    }
    seeds = _read(seeds_path) if seeds_path.is_file() else []
    eligible_seeds = [
        item
        for item in seeds if isinstance(seeds, list) and isinstance(item, dict)
        if str(item.get("fingerprint") or "") in eligible_fingerprints
    ]
    eligible_groups = [
        group for group in groups if str(group.get("fingerprint") or "") in eligible_fingerprints
    ]
    eligible_failures = [
        item
        for item in _list(summary.get("failures"))
        if isinstance(item, dict) and str(item.get("fingerprint") or "") in eligible_fingerprints
    ]
    filtered = copy.deepcopy(summary)
    filtered["failure_groups"] = eligible_groups
    filtered["failures"] = eligible_failures
    filtered["regression_seeds"] = eligible_seeds
    filtered["failure_group_count"] = len(eligible_groups)
    filtered["failed_cases"] = len(eligible_failures)
    filtered["qualification"] = {
        "source": str(out / "qualification_summary.json"),
        "candidate_only": True,
        "eligible_fingerprints": sorted(eligible_fingerprints),
    }
    eligible_root = out / "eligible_triage"
    _write(eligible_root / "triage_summary.json", filtered)
    _write(eligible_root / "regression_seeds.json", eligible_seeds)
    counts: dict[str, int] = {}
    for record in records:
        classification = record["classification"]
        counts[classification] = counts.get(classification, 0) + 1
    report = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "assessment_status": "qualification_only",
        "triage": str(triage_root),
        "group_count": len(records),
        "eligible_group_count": len(eligible_groups),
        "excluded_group_count": len(records) - len(eligible_groups),
        "classification_counts": counts,
        "eligible_triage": str(eligible_root),
        "records": records,
    }
    _write(out / "qualification_summary.json", report)
    lines = [
        "# Failure Qualification",
        "",
        "This report excludes only deterministically contradicted test/oracle cases. Remaining entries are investigation candidates, not confirmed SDK bugs.",
        "",
        f"- Groups: `{len(records)}`",
        f"- Eligible for investigation: `{len(eligible_groups)}`",
        f"- Excluded: `{len(records) - len(eligible_groups)}`",
        "",
        "| case | classification | eligible | confidence |",
        "| --- | --- | --- | --- |",
    ]
    for record in records:
        lines.append(
            f"| `{record['representative_case_id']}` | `{record['classification']}` | "
            f"`{record['eligible_for_bug_investigation']}` | `{record['qualification_confidence']}` |"
        )
    (out / "qualification_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    report = qualify(Path(args.triage).resolve(), Path(args.out).resolve())
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
