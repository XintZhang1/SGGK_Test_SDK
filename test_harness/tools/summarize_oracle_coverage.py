#!/usr/bin/env python3
"""Summarize real-result oracle coverage from SGGK campaign artifacts."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import time
from typing import Any


ORACLE_KINDS = [
    "result_body_count",
    "generic_topology",
    "property_snapshot",
    "finite_properties",
    "nonnegative_length_area",
    "nonnegative_volume",
    "metric_total_length",
    "metric_total_area",
    "metric_total_volume",
    "metric_total_abs_volume",
    "boolean_volume_relation",
    "point_relation",
    "face_point_relation",
    "clash_check",
    "distance_check",
    "plane_extreme_check",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "roots",
        nargs="*",
        help="Case directory, artifact root, or recipe/corpus summary to scan.",
    )
    parser.add_argument(
        "--campaign",
        action="append",
        default=[],
        help="Campaign root or campaign_summary.json. Can be passed more than once.",
    )
    parser.add_argument("--out", default="artifacts/oracle_coverage", help="Output directory")
    parser.add_argument(
        "--fail-on-passed-missing-validation",
        action="store_true",
        help="Return 2 when a passed case has no report/validation.json.",
    )
    parser.add_argument(
        "--min-oracle-kinds-per-passed-case",
        type=int,
        default=0,
        help="Minimum classified oracle kinds required for passed cases. 0 disables this gate.",
    )
    parser.add_argument(
        "--max-cases-in-md",
        type=int,
        default=80,
        help="Maximum per-case rows to show in the Markdown report. JSON always keeps all cases.",
    )
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


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def numeric_expectation_enabled(spec: Any) -> bool:
    data = as_dict(spec)
    return any(bool(data.get(key)) for key in ("min_set", "max_set", "expected_set"))


def summary_path_for_campaign(raw: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        return (path / "campaign_summary.json").resolve()
    return path.resolve()


def normalize_summary_path(raw: str | Path) -> Path:
    path = Path(raw)
    return path.resolve()


def is_case_dir(path: Path) -> bool:
    return (path / "report" / "status.json").is_file() or (path / "report" / "validation.json").is_file()


def path_text(path: Path | str | None) -> str:
    if not path:
        return ""
    return str(path)


def infer_artifact_dir(summary: dict[str, Any], summary_path: Path, case_id: str) -> str:
    if not case_id:
        return ""
    roots = [as_str(summary.get("out_root")), str(summary_path.parent)]
    for raw_root in roots:
        if not raw_root:
            continue
        candidate = Path(raw_root) / case_id
        if candidate.is_dir():
            return str(candidate.resolve())
    return ""


def add_ref(refs: dict[str, dict[str, Any]], ref: dict[str, Any]) -> None:
    artifact_dir = as_str(ref.get("artifact_dir"))
    key = str(Path(artifact_dir).resolve()) if artifact_dir else f"{ref.get('source')}::{ref.get('case_id')}"
    existing = refs.get(key)
    if existing is None:
        ref["sources"] = [ref.get("source", "")]
        refs[key] = ref
        return
    source = as_str(ref.get("source"))
    if source and source not in existing.setdefault("sources", []):
        existing["sources"].append(source)
    for key_name in ("returncode", "timed_out", "recipe", "source_file"):
        if existing.get(key_name) in (None, "", False) and ref.get(key_name) not in (None, ""):
            existing[key_name] = ref.get(key_name)


def refs_from_command_summary(summary_path: Path, source_label: str) -> list[dict[str, Any]]:
    summary = read_json(summary_path)
    if not isinstance(summary, dict):
        return []
    refs: list[dict[str, Any]] = []
    for index, item in enumerate(as_list(summary.get("results"))):
        if not isinstance(item, dict):
            continue
        case_id = as_str(item.get("case_id"))
        artifact_dir = as_str(item.get("artifact_dir")) or infer_artifact_dir(summary, summary_path, case_id)
        refs.append(
            {
                "source": source_label,
                "source_summary": str(summary_path),
                "result_index": index,
                "case_id": case_id,
                "artifact_dir": artifact_dir,
                "recipe": as_str(item.get("recipe")),
                "source_file": as_str(item.get("source_file")),
                "returncode": item.get("returncode"),
                "timed_out": bool(item.get("timed_out")),
            }
        )
    return refs


def refs_from_campaign(summary_path: Path) -> list[dict[str, Any]]:
    summary = read_json(summary_path)
    if not isinstance(summary, dict):
        return []
    refs: list[dict[str, Any]] = []
    for lane in as_list(summary.get("lanes")):
        if not isinstance(lane, dict):
            continue
        lane_summary = as_str(lane.get("summary_path"))
        if lane_summary:
            label = "lane:" + (as_str(lane.get("name")) or Path(lane_summary).parent.name)
            refs.extend(refs_from_command_summary(normalize_summary_path(lane_summary), label))
    replay = as_dict(summary.get("replay"))
    if replay.get("summary_path"):
        refs.extend(refs_from_command_summary(normalize_summary_path(as_str(replay.get("summary_path"))), "aggregate_replay"))
    known = as_dict(summary.get("known_bug_regression"))
    if known.get("replay_summary"):
        refs.extend(refs_from_command_summary(normalize_summary_path(as_str(known.get("replay_summary"))), "known_bug_replay"))
    return refs


def refs_from_root(raw: str) -> list[dict[str, Any]]:
    path = Path(raw).resolve()
    if path.is_file() and path.name in {"recipe_summary.json", "corpus_summary.json", "replay_summary.json"}:
        return refs_from_command_summary(path, "summary:" + path.stem)
    if path.is_file() and path.name == "campaign_summary.json":
        return refs_from_campaign(path)
    if path.is_file() and path.name == "validation.json" and path.parent.name == "report":
        path = path.parent.parent
    refs: list[dict[str, Any]] = []
    if path.is_dir() and is_case_dir(path):
        status = as_dict(read_json(path / "report" / "status.json"))
        refs.append(
            {
                "source": "case_root",
                "case_id": path.name,
                "artifact_dir": str(path),
                "returncode": 0 if status.get("succeeded") is True else None,
                "timed_out": False,
            }
        )
        return refs
    if path.is_dir():
        for validation in sorted(path.rglob("report/validation.json"), key=lambda item: str(item).lower()):
            case_dir = validation.parent.parent
            status = as_dict(read_json(case_dir / "report" / "status.json"))
            refs.append(
                {
                    "source": "artifact_scan",
                    "case_id": case_dir.name,
                    "artifact_dir": str(case_dir),
                    "returncode": 0 if status.get("succeeded") is True else None,
                    "timed_out": False,
                }
            )
    return refs


def new_oracle_stats() -> dict[str, dict[str, int]]:
    return {
        kind: {"cases": 0, "checks": 0, "ok": 0, "failed": 0, "skipped": 0}
        for kind in ORACLE_KINDS
    }


def failure_matches(failures: list[str], *prefixes: str) -> bool:
    return any(any(item.startswith(prefix) for prefix in prefixes) for item in failures)


def skipped_matches(skipped: list[str], *prefixes: str) -> bool:
    return any(any(item.startswith(prefix) for prefix in prefixes) for item in skipped)


def add_kind(
    stats: dict[str, dict[str, int]],
    case_kinds: set[str],
    kind: str,
    checks: int = 1,
    ok: int = 0,
    failed: int = 0,
    skipped: int = 0,
) -> None:
    if kind not in stats:
        stats[kind] = {"cases": 0, "checks": 0, "ok": 0, "failed": 0, "skipped": 0}
    case_kinds.add(kind)
    stats[kind]["checks"] += checks
    stats[kind]["ok"] += ok
    stats[kind]["failed"] += failed
    stats[kind]["skipped"] += skipped


def record_case_presence(stats: dict[str, dict[str, int]], case_kinds: set[str]) -> None:
    for kind in case_kinds:
        stats.setdefault(kind, {"cases": 0, "checks": 0, "ok": 0, "failed": 0, "skipped": 0})
        stats[kind]["cases"] += 1


def classify_record_array(
    stats: dict[str, dict[str, int]],
    case_kinds: set[str],
    kind: str,
    records: list[Any],
    expected_count: int = 0,
) -> None:
    valid = [item for item in records if isinstance(item, dict)]
    if not valid and expected_count <= 0:
        return
    ok = sum(1 for item in valid if item.get("ok") is True or item.get("success") is True and item.get("ok") is not False)
    failed = sum(1 for item in valid if item.get("ok") is False)
    skipped = max(0, expected_count - len(valid))
    add_kind(stats, case_kinds, kind, max(len(valid), expected_count), ok, failed, skipped)


def classify_validation(validation: dict[str, Any], stats: dict[str, dict[str, int]]) -> tuple[list[str], dict[str, int]]:
    case_kinds: set[str] = set()
    kind_counts: dict[str, int] = defaultdict(int)
    failures = [as_str(item) for item in as_list(validation.get("failures")) if as_str(item)]
    skipped = [as_str(item) for item in as_list(validation.get("skipped_checks")) if as_str(item)]
    expectations = as_dict(validation.get("expectations"))

    if "result_body_count" in validation or expectations.get("min_result_bodies") is not None:
        failed = 1 if failure_matches(failures, "result_body_count", "generic_sgt_topology_count") else 0
        add_kind(stats, case_kinds, "result_body_count", 1, 0 if failed else 1, failed, 0)

    if "result_topology_count" in validation or skipped_matches(skipped, "non_body_sgt_body_property_oracles_skipped"):
        failed = 1 if failure_matches(failures, "generic_sgt_topology_count") else 0
        add_kind(stats, case_kinds, "generic_topology", 1, 0 if failed else 1, failed, 0)

    totals = as_dict(validation.get("totals"))
    metric_names = ["length", "area", "volume", "abs_volume"]
    present_metrics = [name for name in metric_names if name in totals]
    if present_metrics:
        add_kind(stats, case_kinds, "property_snapshot", len(present_metrics), len(present_metrics), 0, 0)

    if expectations.get("require_finite_properties") is True:
        failed = 1 if failure_matches(failures, "body_") and any("nonfinite" in item for item in failures) else 0
        add_kind(stats, case_kinds, "finite_properties", 1, 0 if failed else 1, failed, 0)
    if expectations.get("require_nonnegative_length_area") is True:
        failed = 1 if any("negative_length" in item or "negative_area" in item for item in failures) else 0
        add_kind(stats, case_kinds, "nonnegative_length_area", 1, 0 if failed else 1, failed, 0)
    if expectations.get("require_nonnegative_volume") is True:
        failed = 1 if any("negative_volume" in item for item in failures) else 0
        add_kind(stats, case_kinds, "nonnegative_volume", 1, 0 if failed else 1, failed, 0)

    metric_map = {
        "total_length": "metric_total_length",
        "total_area": "metric_total_area",
        "total_volume": "metric_total_volume",
        "total_abs_volume": "metric_total_abs_volume",
    }
    for expectation_name, kind in metric_map.items():
        if numeric_expectation_enabled(expectations.get(expectation_name)):
            failed = 1 if failure_matches(failures, expectation_name) else 0
            add_kind(stats, case_kinds, kind, 1, 0 if failed else 1, failed, 0)

    if expectations.get("boolean_volume_relation") is True or failure_matches(failures, "boolean_") or skipped_matches(skipped, "boolean_volume_relation"):
        failed = 1 if failure_matches(failures, "boolean_") else 0
        skipped_count = 1 if skipped_matches(skipped, "boolean_volume_relation") else 0
        ok = 0 if failed or skipped_count else 1
        add_kind(stats, case_kinds, "boolean_volume_relation", 1, ok, failed, skipped_count)

    point_expected = len(as_list(expectations.get("point_relations")))
    face_point_expected = len(as_list(expectations.get("face_point_relations")))
    clash_expected = len(as_list(expectations.get("clash_checks")))
    distance_expected = len(as_list(expectations.get("distance_checks")))
    plane_expected = len(as_list(expectations.get("plane_extreme_checks")))
    classify_record_array(stats, case_kinds, "point_relation", as_list(validation.get("point_relations")), point_expected)
    classify_record_array(stats, case_kinds, "face_point_relation", as_list(validation.get("face_point_relations")), face_point_expected)
    classify_record_array(stats, case_kinds, "clash_check", as_list(validation.get("clash_checks")), clash_expected)
    classify_record_array(stats, case_kinds, "distance_check", as_list(validation.get("distance_checks")), distance_expected)
    classify_record_array(stats, case_kinds, "plane_extreme_check", as_list(validation.get("plane_extreme_checks")), plane_expected)

    record_case_presence(stats, case_kinds)
    for kind in case_kinds:
        kind_counts[kind] += 1
    return sorted(case_kinds), dict(kind_counts)


def passed_case(ref: dict[str, Any], validation: dict[str, Any] | None) -> bool:
    returncode = ref.get("returncode")
    if isinstance(returncode, int):
        return returncode == 0 and not ref.get("timed_out")
    if validation is not None:
        return validation.get("ok") is True
    status = as_dict(read_json(Path(as_str(ref.get("artifact_dir"))) / "report" / "status.json")) if ref.get("artifact_dir") else {}
    return status.get("succeeded") is True


def summarize(refs: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    stats = new_oracle_stats()
    cases: list[dict[str, Any]] = []
    totals = {
        "total_cases": 0,
        "validation_present": 0,
        "validation_missing": 0,
        "validation_ok": 0,
        "validation_failed": 0,
        "passed_cases": 0,
        "passed_missing_validation": 0,
        "passed_below_min_oracle_kinds": 0,
        "failure_count": 0,
        "skipped_check_count": 0,
    }

    for ref in sorted(refs, key=lambda item: (as_str(item.get("source")), as_str(item.get("case_id")), as_str(item.get("artifact_dir")))):
        totals["total_cases"] += 1
        artifact_dir = as_str(ref.get("artifact_dir"))
        validation_path = Path(artifact_dir) / "report" / "validation.json" if artifact_dir else Path()
        validation = as_dict(read_json(validation_path)) if artifact_dir else {}
        validation_present = bool(validation)
        if validation_present:
            totals["validation_present"] += 1
        else:
            totals["validation_missing"] += 1

        is_passed = passed_case(ref, validation if validation_present else None)
        if is_passed:
            totals["passed_cases"] += 1
        oracle_kinds: list[str] = []
        if validation_present:
            if validation.get("ok") is True:
                totals["validation_ok"] += 1
            else:
                totals["validation_failed"] += 1
            oracle_kinds, _ = classify_validation(validation, stats)
        failures = [as_str(item) for item in as_list(validation.get("failures")) if as_str(item)] if validation_present else []
        skipped = [as_str(item) for item in as_list(validation.get("skipped_checks")) if as_str(item)] if validation_present else []
        totals["failure_count"] += len(failures)
        totals["skipped_check_count"] += len(skipped)
        passed_missing_validation = is_passed and not validation_present
        passed_below_min = is_passed and validation_present and len(oracle_kinds) < args.min_oracle_kinds_per_passed_case
        if passed_missing_validation:
            totals["passed_missing_validation"] += 1
        if passed_below_min:
            totals["passed_below_min_oracle_kinds"] += 1
        cases.append(
            {
                "case_id": as_str(ref.get("case_id")) or (Path(artifact_dir).name if artifact_dir else ""),
                "artifact_dir": artifact_dir,
                "sources": ref.get("sources") or [as_str(ref.get("source"))],
                "source_summary": as_str(ref.get("source_summary")),
                "recipe": as_str(ref.get("recipe")),
                "source_file": as_str(ref.get("source_file")),
                "returncode": ref.get("returncode"),
                "timed_out": bool(ref.get("timed_out")),
                "passed": is_passed,
                "validation_path": str(validation_path) if artifact_dir else "",
                "validation_present": validation_present,
                "validation_ok": as_bool(validation.get("ok")) if validation_present else None,
                "oracle_kinds": oracle_kinds,
                "oracle_kind_count": len(oracle_kinds),
                "failures": failures,
                "skipped_checks": skipped,
                "passed_missing_validation": passed_missing_validation,
                "passed_below_min_oracle_kinds": passed_below_min,
            }
        )

    gate_failures: list[str] = []
    if args.fail_on_passed_missing_validation and totals["passed_missing_validation"]:
        gate_failures.append("passed_missing_validation")
    if args.min_oracle_kinds_per_passed_case > 0 and totals["passed_below_min_oracle_kinds"]:
        gate_failures.append("passed_below_min_oracle_kinds")

    active_stats = {kind: value for kind, value in stats.items() if any(value.values())}
    return {
        "generated_at": now_iso_like(),
        "total_cases": totals["total_cases"],
        "validation_present": totals["validation_present"],
        "validation_missing": totals["validation_missing"],
        "validation_ok": totals["validation_ok"],
        "validation_failed": totals["validation_failed"],
        "passed_cases": totals["passed_cases"],
        "passed_missing_validation": totals["passed_missing_validation"],
        "passed_below_min_oracle_kinds": totals["passed_below_min_oracle_kinds"],
        "failure_count": totals["failure_count"],
        "skipped_check_count": totals["skipped_check_count"],
        "min_oracle_kinds_per_passed_case": args.min_oracle_kinds_per_passed_case,
        "fail_on_passed_missing_validation": bool(args.fail_on_passed_missing_validation),
        "ok": not gate_failures,
        "gate_failures": gate_failures,
        "oracle_counts": active_stats,
        "cases": cases,
    }


def markdown_report(summary: dict[str, Any], max_cases: int) -> str:
    lines = [
        "# SGGK Oracle Coverage",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Ok: `{summary.get('ok')}`",
        f"- Total cases: `{summary.get('total_cases')}`",
        f"- Validation present: `{summary.get('validation_present')}`",
        f"- Validation missing: `{summary.get('validation_missing')}`",
        f"- Passed cases: `{summary.get('passed_cases')}`",
        f"- Passed missing validation: `{summary.get('passed_missing_validation')}`",
        f"- Passed below min oracle kinds: `{summary.get('passed_below_min_oracle_kinds')}`",
        f"- Gate failures: `{summary.get('gate_failures')}`",
        "",
        "## Oracle Counts",
        "",
        "| kind | cases | checks | ok | failed | skipped |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    counts = as_dict(summary.get("oracle_counts"))
    for kind in sorted(counts):
        item = as_dict(counts.get(kind))
        lines.append(
            f"| `{kind}` | {as_int(item.get('cases'))} | {as_int(item.get('checks'))} | "
            f"{as_int(item.get('ok'))} | {as_int(item.get('failed'))} | {as_int(item.get('skipped'))} |"
        )
    lines.extend(["", "## Cases", ""])
    cases = as_list(summary.get("cases"))
    if not cases:
        lines.append("_No cases._")
        lines.append("")
        return "\n".join(lines)
    lines.extend([
        "| case | passed | validation | oracle kinds | failures | skipped |",
        "| --- | ---: | ---: | --- | ---: | ---: |",
    ])
    for case in cases[: max(0, max_cases)]:
        if not isinstance(case, dict):
            continue
        kinds = ", ".join(as_list(case.get("oracle_kinds")))
        failures = len(as_list(case.get("failures")))
        skipped = len(as_list(case.get("skipped_checks")))
        lines.append(
            f"| `{as_str(case.get('case_id'))}` | `{case.get('passed')}` | `{case.get('validation_ok')}` | "
            f"{kinds or '-'} | {failures} | {skipped} |"
        )
    if len(cases) > max_cases:
        lines.append(f"| _{len(cases) - max_cases} more cases omitted from Markdown_ |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.min_oracle_kinds_per_passed_case < 0:
        print("--min-oracle-kinds-per-passed-case must be >= 0")
        return 1
    refs: dict[str, dict[str, Any]] = {}
    for raw_campaign in args.campaign:
        for ref in refs_from_campaign(summary_path_for_campaign(raw_campaign)):
            add_ref(refs, ref)
    for raw_root in args.roots:
        for ref in refs_from_root(raw_root):
            add_ref(refs, ref)

    summary = summarize(list(refs.values()), args)
    out_dir = Path(args.out).resolve()
    write_json(out_dir / "oracle_coverage.json", summary)
    (out_dir / "oracle_coverage.md").write_text(markdown_report(summary, args.max_cases_in_md), encoding="utf-8")
    print(f"summary={out_dir / 'oracle_coverage.json'}")
    print(f"report={out_dir / 'oracle_coverage.md'}")
    print(
        "ok={ok} cases={cases} validation_present={present} passed_missing_validation={missing} "
        "passed_below_min_oracle_kinds={below}".format(
            ok=summary.get("ok"),
            cases=summary.get("total_cases"),
            present=summary.get("validation_present"),
            missing=summary.get("passed_missing_validation"),
            below=summary.get("passed_below_min_oracle_kinds"),
        )
    )
    return 0 if summary.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
