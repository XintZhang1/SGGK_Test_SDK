#!/usr/bin/env python3
"""Build a failure triage index from SGGK runner and corpus artifacts."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Any


CASE_ID_RE = re.compile(r"^case_id=(?P<case_id>.+)$", re.MULTILINE)
ARTIFACT_DIR_RE = re.compile(r"^artifact_dir=(?P<artifact_dir>.+)$", re.MULTILINE)
NUMBER_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s`\"']+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifacts",
        nargs="+",
        help="Artifact directory, case directory, or corpus summary to scan.",
    )
    parser.add_argument("--out", default="artifacts/triage", help="Output directory")
    parser.add_argument(
        "--include-passed",
        action="store_true",
        help="Include passed cases in triage_summary.json.",
    )
    parser.add_argument(
        "--max-localized",
        type=int,
        default=12,
        help="Maximum localized input topology entries per case.",
    )
    parser.add_argument(
        "--max-contact-candidates",
        type=int,
        default=12,
        help="Maximum target/tool bbox contact candidates per case when topo-track localization is unavailable.",
    )
    parser.add_argument(
        "--fail-on-failures",
        action="store_true",
        help="Return exit code 2 when failures or command failures are found.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"_json_error": f"{exc.msg} at line {exc.lineno}, column {exc.colno}"}


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def stable_hash(value: Any, length: int = 16) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:length]


def normalize_text(value: Any) -> str:
    text = as_str(value).lower()
    text = WINDOWS_PATH_RE.sub("<path>", text)
    text = NUMBER_RE.sub("<num>", text)
    text = " ".join(text.split())
    return text[:240]


def path_for_json(path: Path) -> str:
    return str(path)


def iter_roots(inputs: list[str]) -> list[Path]:
    roots: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.name in {"corpus_summary.json", "recipe_summary.json"}:
            roots.append(path.parent.resolve())
        else:
            roots.append(path.resolve())
    return sorted(set(roots), key=lambda item: str(item).lower())


def is_case_dir(path: Path) -> bool:
    return (path / "manifest.json").is_file() and (path / "report" / "status.json").is_file()


def iter_case_dirs(roots: list[Path]) -> list[Path]:
    case_dirs: set[Path] = set()
    for root in roots:
        if is_case_dir(root):
            case_dirs.add(root.resolve())
            continue
        if root.is_file() and root.name == "corpus_summary.json":
            root = root.parent
        if not root.is_dir():
            continue
        for manifest in root.rglob("manifest.json"):
            case_dir = manifest.parent
            if "_recipes" in case_dir.parts:
                continue
            if is_case_dir(case_dir):
                case_dirs.add(case_dir.resolve())
    return sorted(case_dirs, key=lambda item: str(item).lower())


def iter_command_summaries(roots: list[Path]) -> list[Path]:
    summaries: set[Path] = set()
    names = {"corpus_summary.json", "recipe_summary.json"}
    for root in roots:
        if root.is_file() and root.name in names:
            summaries.add(root.resolve())
            continue
        if root.is_dir():
            for name in names:
                for summary in root.rglob(name):
                    summaries.add(summary.resolve())
    return sorted(summaries, key=lambda item: str(item).lower())


def parse_stdout_field(stdout: str, regex: re.Pattern[str], group: str) -> str:
    match = regex.search(stdout or "")
    return match.group(group).strip() if match else ""


def infer_artifact_dir(summary: dict[str, Any], summary_path: Path, case_id: str) -> str:
    if not case_id:
        return ""
    roots = [as_str(summary.get("out_root")), str(summary_path.parent)]
    for raw_root in roots:
        if not raw_root:
            continue
        candidate = Path(raw_root) / case_id
        if candidate.is_dir():
            return str(candidate)
    return ""


def load_command_records(summary_paths: list[Path]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    by_case_dir: dict[str, dict[str, Any]] = {}
    for summary_path in summary_paths:
        summary = load_json(summary_path)
        if not isinstance(summary, dict):
            continue
        for item in summary.get("results", []):
            if not isinstance(item, dict):
                continue
            stdout = as_str(item.get("stdout"))
            case_id = as_str(item.get("case_id")) or parse_stdout_field(stdout, CASE_ID_RE, "case_id")
            artifact_dir = as_str(item.get("artifact_dir")) or parse_stdout_field(stdout, ARTIFACT_DIR_RE, "artifact_dir")
            if not artifact_dir:
                artifact_dir = infer_artifact_dir(summary, summary_path, case_id)
            record = {
                "summary_path": path_for_json(summary_path),
                "case_id": case_id,
                "artifact_dir": artifact_dir,
                "recipe": as_str(item.get("recipe")),
                "source_file": as_str(item.get("source_file")),
                "returncode": as_int(item.get("returncode")),
                "timed_out": bool(item.get("timed_out")),
                "elapsed_seconds": item.get("elapsed_seconds"),
                "stderr": as_str(item.get("stderr")),
            }
            records.append(record)
            if artifact_dir:
                by_case_dir[str(Path(artifact_dir).resolve())] = record
    return records, by_case_dir


def topo_check_failures(topo_check: Any) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if not isinstance(topo_check, dict):
        return failures
    for body in topo_check.get("bodies", []):
        if not isinstance(body, dict):
            continue
        if body.get("ok") is False:
            failures.append(body)
    for topology in topo_check.get("topologies", []):
        if not isinstance(topology, dict):
            continue
        if topology.get("ok") is False:
            failures.append(topology)
    return failures


def validation_failures(validation: Any) -> list[str]:
    if not isinstance(validation, dict):
        return []
    failures = validation.get("failures")
    if not isinstance(failures, list):
        return []
    return [as_str(item) for item in failures if as_str(item)]


def roundtrip_failures(roundtrip: Any) -> list[str]:
    if not isinstance(roundtrip, dict):
        return []
    failures = roundtrip.get("failures")
    if isinstance(failures, list):
        return [as_str(item) for item in failures if as_str(item)]
    if roundtrip.get("ok") is False:
        return ["roundtrip_comparison_failed"]
    return []


def roundtrip_oracle_details(roundtrip: Any) -> list[dict[str, Any]]:
    if not isinstance(roundtrip, dict):
        return []
    details: list[dict[str, Any]] = []
    metrics = roundtrip.get("metrics") if isinstance(roundtrip.get("metrics"), dict) else {}
    for name, metric in metrics.items():
        if not isinstance(metric, dict) or metric.get("ok") is not False:
            continue
        details.append(
            {
                "oracle_kind": "roundtrip_metric",
                "id": f"roundtrip_{name}",
                "metric": name,
                "source": metric.get("source"),
                "result": metric.get("result"),
                "delta": metric.get("delta"),
                "tolerance": metric.get("tolerance"),
                "ok": metric.get("ok"),
            }
        )
    bbox = roundtrip.get("bbox") if isinstance(roundtrip.get("bbox"), dict) else {}
    if bbox.get("ok") is False:
        details.append(
            {
                "oracle_kind": "roundtrip_bbox",
                "id": "roundtrip_bbox",
                "source": bbox.get("source"),
                "result": bbox.get("result"),
                "ok": bbox.get("ok"),
            }
        )
    return details


def compact_topology(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key in ("role", "type", "id", "local_index", "terminal_operation", "operation_chain"):
        if key in value:
            result[key] = value[key]
    return result if result else value


def compact_validation_record(kind: str, record: dict[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "oracle_kind": kind,
        "id": as_str(record.get("id")),
        "ok": record.get("ok"),
    }
    for key in (
        "role",
        "role_a",
        "role_b",
        "body_index",
        "body_index_a",
        "body_index_b",
        "axis",
        "side",
        "face_index",
        "face_id",
        "face_id_set",
        "expected",
        "actual",
        "reason",
        "error",
        "tolerance",
        "mode",
        "success",
        "dist_type",
        "actual_face",
        "uv_bound",
        "uv",
        "point",
        "point_from_surface",
        "target",
        "sub_clash_count",
        "metric_failures",
        "actual",
        "actual_extreme",
        "probe_coordinate",
        "probe_coordinate_source",
        "plane_span",
        "point_a",
        "point_b",
        "probe",
        "debug_geometry",
        "topology_a",
        "topology_b",
    ):
        if key in record:
            detail[key] = record[key]
    if "kind" in record:
        detail["check_kind"] = record["kind"]
    if isinstance(record.get("sub_clashes"), list):
        detail["sub_clashes"] = record["sub_clashes"][:4]
    for key in ("topology_a", "topology_b", "actual_face"):
        if key in detail:
            detail[key] = compact_topology(detail[key])
    return detail


def validation_oracle_details(validation: Any, max_entries: int = 12) -> list[dict[str, Any]]:
    if not isinstance(validation, dict):
        return []
    details: list[dict[str, Any]] = []
    for key, kind in (
        ("point_relations", "point_relation"),
        ("face_point_relations", "face_point_relation"),
        ("clash_checks", "clash_check"),
        ("distance_checks", "distance_check"),
        ("plane_extreme_checks", "plane_extreme_check"),
    ):
        records = validation.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("ok") is False or record.get("reason") or record.get("error"):
                details.append(compact_validation_record(kind, record))
                if len(details) >= max_entries:
                    return details
    return details


def build_locator_index(input_index: Any) -> dict[tuple[str, str, int, int], Any]:
    lookup: dict[tuple[str, str, int, int], Any] = {}
    if not isinstance(input_index, dict):
        return lookup
    for input_item in input_index.get("inputs", []):
        if not isinstance(input_item, dict):
            continue
        role = as_str(input_item.get("role"))
        for topo in input_item.get("topologies", []):
            if not isinstance(topo, dict):
                continue
            key = (
                role,
                as_str(topo.get("type")),
                as_int(topo.get("id")),
                as_int(topo.get("local_index")),
            )
            lookup[key] = topo.get("locator")
    return lookup


def as_float_list(value: Any, length: int) -> list[float] | None:
    if not isinstance(value, list) or len(value) != length:
        return None
    result: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            return None
        result.append(float(item))
    return result


def bbox_from_locator(locator: Any) -> dict[str, list[float]] | None:
    if not isinstance(locator, dict):
        return None
    bbox = locator.get("bbox")
    if isinstance(bbox, dict) and not bbox.get("empty"):
        mins = as_float_list(bbox.get("min"), 3)
        maxs = as_float_list(bbox.get("max"), 3)
        if mins is not None and maxs is not None:
            return {"min": mins, "max": maxs}
    point = as_float_list(locator.get("point"), 3)
    if point is not None:
        return {"min": point, "max": point}
    return None


def bbox_distance(a: dict[str, list[float]], b: dict[str, list[float]]) -> tuple[float, list[float], list[float], int]:
    gaps: list[float] = []
    overlaps: list[float] = []
    overlap_axes = 0
    for axis in range(3):
        a_min, a_max = a["min"][axis], a["max"][axis]
        b_min, b_max = b["min"][axis], b["max"][axis]
        if a_max < b_min:
            gaps.append(b_min - a_max)
            overlaps.append(0.0)
        elif b_max < a_min:
            gaps.append(a_min - b_max)
            overlaps.append(0.0)
        else:
            gaps.append(0.0)
            overlaps.append(min(a_max, b_max) - max(a_min, b_min))
            overlap_axes += 1
    distance = sum(gap * gap for gap in gaps) ** 0.5
    return distance, gaps, overlaps, overlap_axes


def topology_specificity(topology_type: str) -> int:
    return {
        "Face": 0,
        "Edge": 1,
        "Vertex": 2,
        "Body": 3,
        "Shell": 4,
        "Lump": 5,
    }.get(topology_type, 6)


def topology_ref(input_item: dict[str, Any], topo: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": as_str(input_item.get("role")),
        "type": as_str(topo.get("type")),
        "id": as_int(topo.get("id")),
        "local_index": as_int(topo.get("local_index")),
        "terminal_operation": as_str(input_item.get("terminal_operation")),
        "operation_chain": input_item.get("operation_chain", []),
    }


def contact_topologies(input_index: Any, role: str) -> list[dict[str, Any]]:
    if not isinstance(input_index, dict):
        return []
    allowed_types = {"Body", "Face", "Edge", "Vertex"}
    result: list[dict[str, Any]] = []
    for input_item in input_index.get("inputs", []):
        if not isinstance(input_item, dict) or as_str(input_item.get("role")) != role:
            continue
        for topo in input_item.get("topologies", []):
            if not isinstance(topo, dict):
                continue
            topology_type = as_str(topo.get("type"))
            if topology_type not in allowed_types:
                continue
            locator = topo.get("locator")
            bbox = bbox_from_locator(locator)
            if bbox is None:
                continue
            result.append(
                {
                    "ref": topology_ref(input_item, topo),
                    "locator": locator,
                    "bbox": bbox,
                    "specificity": topology_specificity(topology_type),
                }
            )
    return result


def contact_signature(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    for item in candidates[:6]:
        target = item.get("target", {}) if isinstance(item.get("target"), dict) else {}
        tool = item.get("tool", {}) if isinstance(item.get("tool"), dict) else {}
        signature.append(
            {
                "target_type": as_str(target.get("type")),
                "target_terminal_operation": as_str(target.get("terminal_operation")),
                "tool_type": as_str(tool.get("type")),
                "tool_terminal_operation": as_str(tool.get("terminal_operation")),
                "distance_bucket": round(float(item.get("bbox_distance", 0.0)), 8),
                "axis_gaps": [round(float(value), 8) for value in item.get("axis_gaps", [])[:3]],
            }
        )
    return signature


def summarize_input_contact_candidates(input_index: Any, max_entries: int) -> list[dict[str, Any]]:
    targets = contact_topologies(input_index, "target")
    tools = contact_topologies(input_index, "tool")
    candidates: list[dict[str, Any]] = []
    for target in targets:
        for tool in tools:
            distance, gaps, overlaps, overlap_axes = bbox_distance(target["bbox"], tool["bbox"])
            candidates.append(
                {
                    "target": target["ref"],
                    "tool": tool["ref"],
                    "bbox_distance": distance,
                    "axis_gaps": gaps,
                    "axis_overlaps": overlaps,
                    "overlap_axes": overlap_axes,
                    "target_locator": target["locator"],
                    "tool_locator": tool["locator"],
                }
            )

    def sort_key(item: dict[str, Any]) -> tuple[float, int, int, int, str, str]:
        target = item["target"]
        tool = item["tool"]
        specificity = topology_specificity(as_str(target.get("type"))) + topology_specificity(as_str(tool.get("type")))
        return (
            float(item.get("bbox_distance", 0.0)),
            -as_int(item.get("overlap_axes")),
            specificity,
            1 if target.get("type") == "Body" and tool.get("type") == "Body" else 0,
            as_str(target.get("type")),
            as_str(tool.get("type")),
        )

    return sorted(candidates, key=sort_key)[:max_entries]


def summarize_localized_topologies(
    topo_track: Any,
    input_index: Any,
    max_entries: int,
) -> list[dict[str, Any]]:
    if not isinstance(topo_track, dict):
        return []

    locators = build_locator_index(input_index)
    records: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for item in topo_track.get("items", []):
        if not isinstance(item, dict):
            continue
        track_type = as_str(item.get("track_type"))
        desc = item.get("descendent") if isinstance(item.get("descendent"), dict) else {}
        desc_type = as_str(desc.get("type")) if isinstance(desc, dict) else ""
        for ancestor in item.get("ancestors", []):
            if not isinstance(ancestor, dict):
                continue
            ref = ancestor.get("input_ref")
            if not isinstance(ref, dict):
                continue
            key = (
                as_str(ref.get("role")),
                as_str(ref.get("type")),
                as_int(ref.get("id")),
                as_int(ref.get("local_index")),
            )
            entry = records.setdefault(
                key,
                {
                    "role": key[0],
                    "type": key[1],
                    "id": key[2],
                    "local_index": key[3],
                    "terminal_operation": as_str(ref.get("terminal_operation")),
                    "operation_chain": ref.get("operation_chain", []),
                    "count": 0,
                    "track_type_counts": Counter(),
                    "descendent_type_counts": Counter(),
                    "locator": locators.get(key),
                },
            )
            entry["count"] += 1
            if track_type:
                entry["track_type_counts"][track_type] += 1
            if desc_type:
                entry["descendent_type_counts"][desc_type] += 1

    result = sorted(
        records.values(),
        key=lambda item: (-item["count"], item["role"], item["type"], item["id"], item["local_index"]),
    )[:max_entries]
    for entry in result:
        entry["track_type_counts"] = dict(entry["track_type_counts"])
        entry["descendent_type_counts"] = dict(entry["descendent_type_counts"])
    return result


def topo_check_signature(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    for failure in failures[:5]:
        topo = failure.get("error_topology")
        signature.append(
            {
                "error_code": as_int(failure.get("error_code")),
                "error_string": normalize_text(failure.get("error_string")),
                "topology_type": as_str(topo.get("type")) if isinstance(topo, dict) else "",
            }
        )
    return signature


def localized_signature(localized_inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    for item in localized_inputs[:6]:
        signature.append(
            {
                "role": as_str(item.get("role")),
                "type": as_str(item.get("type")),
                "terminal_operation": as_str(item.get("terminal_operation")),
                "operation_chain": [
                    as_str(value)
                    for value in item.get("operation_chain", [])
                    if isinstance(value, str)
                ],
                "track_type_counts": item.get("track_type_counts", {}),
                "descendent_type_counts": item.get("descendent_type_counts", {}),
            }
        )
    return signature


def command_record_summary(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return {
        "summary_path": as_str(record.get("summary_path")),
        "returncode": as_int(record.get("returncode")),
        "timed_out": bool(record.get("timed_out")),
        "elapsed_seconds": record.get("elapsed_seconds"),
        "stderr": as_str(record.get("stderr"))[:1000],
    }


def build_failure_fingerprint(case: dict[str, Any]) -> dict[str, Any]:
    status = case.get("status") if isinstance(case.get("status"), dict) else {}
    topo_summary = (
        case.get("topo_track_summary")
        if isinstance(case.get("topo_track_summary"), dict)
        else {}
    )
    data_exchange = (
        case.get("data_exchange")
        if isinstance(case.get("data_exchange"), dict)
        else {}
    )
    dsl = case.get("dsl") if isinstance(case.get("dsl"), dict) else {}
    runner = command_record_summary(case.get("corpus"))
    components = {
        "api": as_str(case.get("api")),
        "reasons": case.get("reasons", []),
        "runner": {
            "returncode": runner.get("returncode"),
            "timed_out": runner.get("timed_out"),
        },
        "error_code": as_int(status.get("error_code")),
        "error_message": normalize_text(status.get("error_message")),
        "topo_check": topo_check_signature(case.get("topo_check_failures", [])),
        "data_exchange": {
            "failed_item_count": as_int(data_exchange.get("failed_item_count")),
            "invalid_topology_count": as_int(data_exchange.get("invalid_topology_count")),
        },
        "validation": [
            normalize_text(item)
            for item in case.get("validation_failures", [])[:8]
        ],
        "roundtrip": [
            normalize_text(item)
            for item in case.get("roundtrip_failures", [])[:8]
        ],
        "topo_track_counts": {
            "track_type_counts": topo_summary.get("track_type_counts", {}),
            "descendent_type_counts": topo_summary.get("descendent_type_counts", {}),
            "ancestor_type_counts": topo_summary.get("ancestor_type_counts", {}),
            "ancestor_input_role_counts": topo_summary.get("ancestor_input_role_counts", {}),
        },
        "localized": localized_signature(case.get("localized_inputs", [])),
        "contact_candidates": contact_signature(case.get("input_contact_candidates", [])),
        "dsl_case_id": as_str(dsl.get("case_id")),
    }
    return {
        "id": stable_hash(components),
        "components": components,
    }


def classify_case(
    case_dir: Path,
    corpus_record: dict[str, Any] | None,
    max_localized: int,
    max_contact_candidates: int,
) -> dict[str, Any]:
    report_dir = case_dir / "report"
    manifest = load_json(case_dir / "manifest.json")
    status = load_json(report_dir / "status.json")
    topo_check = load_json(report_dir / "topo_check.json")
    topo_summary = load_json(report_dir / "topo_track_summary.json")
    topo_track = load_json(report_dir / "topo_track.json")
    input_index = load_json(report_dir / "input_topology_index.json")
    data_exchange = load_json(report_dir / "data_exchange.json")
    validation = load_json(report_dir / "validation.json")
    roundtrip = load_json(report_dir / "roundtrip_comparison.json")

    reasons: list[str] = []
    warnings: list[str] = []

    if not isinstance(status, dict):
        reasons.append("missing_status")
        status = {}
    elif status.get("_json_error"):
        reasons.append("invalid_status_json")
    else:
        succeeded = as_bool(status.get("succeeded"))
        if succeeded is False:
            reasons.append("api_failed")
        if as_int(status.get("error_code")) != 0:
            reasons.append("api_error")
        if as_int(status.get("error_entity_count")) > 0:
            warnings.append("error_entities_present")

    topo_failures = topo_check_failures(topo_check)
    if topo_failures:
        reasons.append("topology_invalid")
    elif isinstance(topo_check, dict) and topo_check.get("_json_error"):
        reasons.append("invalid_topo_check_json")

    val_failures = validation_failures(validation)
    val_oracle_details = validation_oracle_details(validation)
    if isinstance(validation, dict) and validation.get("_json_error"):
        reasons.append("invalid_validation_json")
    elif isinstance(validation, dict):
        if validation.get("ok") is False or val_failures:
            reasons.append("validation_failed")
    elif validation is None:
        warnings.append("missing_validation_report")

    rt_failures = roundtrip_failures(roundtrip)
    rt_oracle_details = roundtrip_oracle_details(roundtrip)
    if isinstance(roundtrip, dict) and roundtrip.get("_json_error"):
        reasons.append("invalid_roundtrip_comparison_json")
    elif isinstance(roundtrip, dict):
        if roundtrip.get("ok") is False or rt_failures:
            reasons.append("roundtrip_comparison_failed")

    if isinstance(data_exchange, dict) and data_exchange.get("_json_error"):
        reasons.append("invalid_data_exchange_json")
    elif isinstance(data_exchange, dict):
        if as_int(data_exchange.get("failed_item_count")) > 0:
            reasons.append("exchange_failed_items")
        if as_int(data_exchange.get("invalid_topology_count")) > 0:
            reasons.append("exchange_invalid_topology")

    if isinstance(topo_summary, dict):
        if as_int(topo_summary.get("unresolved_ancestor_count")) > 0:
            warnings.append("unresolved_topo_track_ancestors")
        if as_int(topo_summary.get("ambiguous_ancestor_count")) > 0:
            warnings.append("ambiguous_topo_track_ancestors")
    elif topo_summary is not None:
        warnings.append("invalid_topo_track_summary_json")

    if corpus_record:
        if corpus_record.get("timed_out"):
            reasons.append("runner_timeout")
        if as_int(corpus_record.get("returncode")) != 0:
            reasons.append("runner_nonzero_exit")

    case_id = case_dir.name
    api = ""
    dsl = {}
    recipe_path = ""
    if isinstance(manifest, dict):
        case_id = as_str(manifest.get("case_id")) or case_id
        api = as_str(manifest.get("api"))
        recipe_path = as_str(manifest.get("recipe_path"))
        dsl = manifest.get("dsl") if isinstance(manifest.get("dsl"), dict) else {}

    localized = summarize_localized_topologies(topo_track, input_index, max_localized)
    contact_candidates = summarize_input_contact_candidates(input_index, max_contact_candidates)
    result = {
        "case_id": case_id,
        "case_dir": path_for_json(case_dir),
        "api": api,
        "recipe_path": recipe_path,
        "status": status,
        "reasons": sorted(set(reasons)),
        "warnings": sorted(set(warnings)),
        "failed": bool(reasons),
        "topo_check_failures": topo_failures,
        "validation": validation if isinstance(validation, dict) else {},
        "validation_failures": val_failures,
        "validation_oracle_details": val_oracle_details,
        "roundtrip_comparison": roundtrip if isinstance(roundtrip, dict) else {},
        "roundtrip_failures": rt_failures,
        "roundtrip_oracle_details": rt_oracle_details,
        "topo_track_summary": topo_summary if isinstance(topo_summary, dict) else {},
        "localized_inputs": localized,
        "input_contact_candidates": contact_candidates,
        "dsl": dsl,
    }
    if corpus_record:
        result["corpus"] = corpus_record
    if isinstance(data_exchange, dict):
        result["data_exchange"] = data_exchange
    if result["failed"]:
        fingerprint = build_failure_fingerprint(result)
        result["fingerprint"] = fingerprint["id"]
        result["fingerprint_components"] = fingerprint["components"]
    return result


def command_failures(corpus_records: list[dict[str, Any]], case_dirs: set[str]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for record in corpus_records:
        artifact_dir = as_str(record.get("artifact_dir"))
        has_case_artifact = artifact_dir and str(Path(artifact_dir).resolve()) in case_dirs
        failed = bool(record.get("timed_out")) or as_int(record.get("returncode")) != 0
        if failed and not has_case_artifact:
            failures.append(record)
    return failures


def build_failure_groups(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups_by_id: dict[str, dict[str, Any]] = {}
    for case in failures:
        fingerprint = as_str(case.get("fingerprint")) or build_failure_fingerprint(case)["id"]
        group = groups_by_id.setdefault(
            fingerprint,
            {
                "fingerprint": fingerprint,
                "count": 0,
                "reasons": case.get("reasons", []),
                "apis": set(),
                "case_ids": [],
                "case_dirs": [],
                "source_files": [],
                "recipe_paths": [],
                "representative_case_id": case.get("case_id"),
                "representative_case_dir": case.get("case_dir"),
                "representative_warnings": case.get("warnings", []),
                "representative_runner": command_record_summary(case.get("corpus")),
                "representative_dsl": case.get("dsl", {}),
                "representative_topo_track_summary": case.get("topo_track_summary", {}),
                "representative_localized_inputs": case.get("localized_inputs", []),
                "representative_input_contact_candidates": case.get("input_contact_candidates", []),
                "representative_topo_check_failures": case.get("topo_check_failures", []),
                "representative_validation_failures": case.get("validation_failures", []),
                "representative_validation_oracle_details": case.get("validation_oracle_details", []),
                "representative_roundtrip_failures": case.get("roundtrip_failures", []),
                "representative_roundtrip_oracle_details": case.get("roundtrip_oracle_details", []),
                "fingerprint_components": case.get("fingerprint_components", {}),
            },
        )
        group["count"] += 1
        if case.get("api"):
            group["apis"].add(case.get("api"))
        if case.get("case_id"):
            group["case_ids"].append(case.get("case_id"))
        if case.get("case_dir"):
            group["case_dirs"].append(case.get("case_dir"))
        if case.get("recipe_path"):
            group["recipe_paths"].append(case.get("recipe_path"))
        corpus = case.get("corpus") if isinstance(case.get("corpus"), dict) else {}
        if corpus.get("source_file"):
            group["source_files"].append(corpus.get("source_file"))

    groups = list(groups_by_id.values())
    for group in groups:
        group["apis"] = sorted(group["apis"])
        group["case_ids"] = sorted(set(group["case_ids"]))
        group["case_dirs"] = sorted(set(group["case_dirs"]))
        group["source_files"] = sorted(set(group["source_files"]))
        group["recipe_paths"] = sorted(set(group["recipe_paths"]))
    return sorted(groups, key=lambda item: (-item["count"], item["fingerprint"]))


def existing_file(path: Path) -> str:
    return str(path) if path.is_file() else ""


def build_regression_seeds(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for group in groups:
        case_dir = Path(as_str(group.get("representative_case_dir")))
        seed = {
            "fingerprint": group.get("fingerprint"),
            "count": group.get("count"),
            "representative_case_id": group.get("representative_case_id"),
            "representative_case_dir": as_str(group.get("representative_case_dir")),
            "apis": group.get("apis", []),
            "reasons": group.get("reasons", []),
            "recipe_paths": group.get("recipe_paths", []),
            "source_files": group.get("source_files", []),
            "case_dirs": group.get("case_dirs", []),
            "runner": group.get("representative_runner", {}),
            "dsl": group.get("representative_dsl", {}),
            "artifact_inputs": {
                "target_sgt": existing_file(case_dir / "input" / "target.sgt"),
                "tool_sgt": existing_file(case_dir / "input" / "tool.sgt"),
                "source_sgt": existing_file(case_dir / "input" / "source.sgt"),
                "source_step": existing_file(case_dir / "input" / "source.step"),
                "source_stp": existing_file(case_dir / "input" / "source.stp"),
                "source_iges": existing_file(case_dir / "input" / "source.iges"),
                "source_igs": existing_file(case_dir / "input" / "source.igs"),
            },
            "localized_inputs": group.get("representative_localized_inputs", [])[:6],
            "input_contact_candidates": group.get("representative_input_contact_candidates", [])[:6],
            "validation_failures": group.get("representative_validation_failures", [])[:8],
            "validation_oracle_details": group.get("representative_validation_oracle_details", [])[:8],
            "roundtrip_failures": group.get("representative_roundtrip_failures", [])[:8],
            "roundtrip_oracle_details": group.get("representative_roundtrip_oracle_details", [])[:8],
            "notes": "Use recipe_paths when present. Use artifact input SGT files as load_sgt/check_sgt seeds when the original corpus or DSL context is unavailable.",
        }
        seed["artifact_inputs"] = {
            key: value for key, value in seed["artifact_inputs"].items() if value
        }
        seeds.append(seed)
    return seeds


def format_locator(locator: Any) -> str:
    if not isinstance(locator, dict):
        return ""
    if "point" in locator:
        return f"point={locator.get('point')} tol={locator.get('tolerance')}"
    if "start_point" in locator or "end_point" in locator:
        return (
            f"start={locator.get('start_point')} end={locator.get('end_point')} "
            f"length={locator.get('length')} tol={locator.get('tolerance')}"
        )
    if "area" in locator:
        return f"area={locator.get('area')} sense={locator.get('sense')}"
    bbox = locator.get("bbox")
    if isinstance(bbox, dict) and not bbox.get("empty"):
        return f"bbox_min={bbox.get('min')} bbox_max={bbox.get('max')}"
    if "edge_error" in locator:
        return f"edge_error={locator.get('edge_error')}"
    if "face_error" in locator:
        return f"face_error={locator.get('face_error')}"
    return ""


def format_ref(ref: Any) -> str:
    if not isinstance(ref, dict):
        return ""
    op = as_str(ref.get("terminal_operation"))
    op_text = f" op=`{op}`" if op else ""
    return f"{as_str(ref.get('role'))} {as_str(ref.get('type'))}#{as_int(ref.get('id'))}[{as_int(ref.get('local_index'))}]{op_text}"


def format_contact_candidate(candidate: Any) -> str:
    if not isinstance(candidate, dict):
        return ""
    distance = candidate.get("bbox_distance")
    gaps = candidate.get("axis_gaps")
    overlaps = candidate.get("axis_overlaps")
    distance_text = f"{distance:.8g}" if isinstance(distance, (int, float)) and not isinstance(distance, bool) else str(distance)
    return (
        f"{format_ref(candidate.get('target'))} <-> {format_ref(candidate.get('tool'))} "
        f"bbox_gap={distance_text} axis_gaps={gaps} axis_overlaps={overlaps}"
    )


def format_validation_oracle_detail(detail: Any) -> str:
    if not isinstance(detail, dict):
        return ""
    oracle = as_str(detail.get("oracle_kind"))
    ident = as_str(detail.get("id"))
    expected = detail.get("expected")
    actual = detail.get("actual")
    parts = [f"{oracle} `{ident}`".strip()]
    if expected not in (None, "") or actual not in (None, ""):
        parts.append(f"expected={expected} actual={actual}")
    if detail.get("reason"):
        parts.append(f"reason={detail.get('reason')}")
    if detail.get("error"):
        parts.append(f"error={detail.get('error')}")
    if detail.get("actual_face"):
        parts.append(f"face={detail.get('actual_face')}")
    if detail.get("uv"):
        parts.append(f"uv={detail.get('uv')}")
    if detail.get("point"):
        parts.append(f"point={detail.get('point')}")
    if detail.get("sub_clash_count") not in (None, ""):
        parts.append(f"sub_clash_count={detail.get('sub_clash_count')}")
    if detail.get("metric_failures"):
        parts.append(f"metric_failures={detail.get('metric_failures')}")
    if detail.get("point_a") or detail.get("point_b"):
        parts.append(f"points={detail.get('point_a')} -> {detail.get('point_b')}")
    if detail.get("topology_a") or detail.get("topology_b"):
        parts.append(f"topology={detail.get('topology_a')} -> {detail.get('topology_b')}")
    return "; ".join(parts)


def format_roundtrip_oracle_detail(detail: Any) -> str:
    if not isinstance(detail, dict):
        return ""
    kind = as_str(detail.get("oracle_kind"))
    ident = as_str(detail.get("id"))
    if kind == "roundtrip_metric":
        return (
            f"{ident}: source={detail.get('source')} result={detail.get('result')} "
            f"delta={detail.get('delta')} tolerance={detail.get('tolerance')}"
        )
    if kind == "roundtrip_bbox":
        return f"{ident}: source={detail.get('source')} result={detail.get('result')}"
    return ident


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    lines: list[str] = []
    lines.append("# SGGK Artifact Triage")
    lines.append("")
    lines.append(f"- Roots: {', '.join(summary['roots'])}")
    lines.append(f"- Cases: {summary['total_cases']}")
    lines.append(f"- Failed cases: {summary['failed_cases']}")
    lines.append(f"- Failure groups: {summary['failure_group_count']}")
    lines.append(f"- Command failures without case artifact: {summary['command_failures']}")
    lines.append(f"- Triage warning cases: {summary['warning_cases']}")
    lines.append("")

    if summary["command_failure_records"]:
        lines.append("## Command Failures")
        lines.append("")
        for record in summary["command_failure_records"]:
            lines.append(
                f"- `{record.get('case_id') or record.get('recipe')}`: "
                f"returncode={record.get('returncode')} timeout={record.get('timed_out')} "
                f"source=`{record.get('source_file')}`"
            )
        lines.append("")

    failures = summary["failures"]
    if not failures:
        lines.append("## Failures")
        lines.append("")
        lines.append("No failed cases were detected.")
        lines.append("")
    else:
        lines.append("## Failure Groups")
        lines.append("")
        for group in summary.get("failure_groups", []):
            lines.append(f"### {group['fingerprint']}")
            lines.append("")
            lines.append(f"- Count: {group['count']}")
            lines.append(f"- APIs: {', '.join(group.get('apis', []))}")
            lines.append(f"- Reasons: {', '.join(group.get('reasons', []))}")
            lines.append(f"- Representative: `{group.get('representative_case_id')}`")
            dsl = group.get("representative_dsl") if isinstance(group.get("representative_dsl"), dict) else {}
            if dsl.get("source_task_id") or dsl.get("source_ref"):
                lines.append(
                    f"- Source task: `{dsl.get('source_task_id', '')}` source_ref=`{dsl.get('source_ref', '')}` risk=`{dsl.get('source_risk_id', '')}`"
                )
            if group.get("source_files"):
                lines.append(f"- Sources: {len(group['source_files'])}")
            if group.get("representative_localized_inputs"):
                lines.append("- Top localized input topology:")
                for loc in group["representative_localized_inputs"][:5]:
                    lines.append(
                        f"  - {loc['role']} {loc['type']}#{loc['id']}[{loc['local_index']}] "
                        f"op=`{loc.get('terminal_operation', '')}` count={loc.get('count')} "
                        f"{format_locator(loc.get('locator'))}"
                    )
            if group.get("representative_input_contact_candidates"):
                lines.append("- Top input contact candidates:")
                for candidate in group["representative_input_contact_candidates"][:5]:
                    lines.append(f"  - {format_contact_candidate(candidate)}")
            if group.get("representative_validation_failures"):
                lines.append("- Validation failures:")
                for failure in group["representative_validation_failures"][:5]:
                    lines.append(f"  - {failure}")
            if group.get("representative_validation_oracle_details"):
                lines.append("- Validation oracle details:")
                for detail in group["representative_validation_oracle_details"][:5]:
                    text = format_validation_oracle_detail(detail)
                    if text:
                        lines.append(f"  - {text}")
            if group.get("representative_roundtrip_failures"):
                lines.append("- Roundtrip failures:")
                for failure in group["representative_roundtrip_failures"][:5]:
                    lines.append(f"  - {failure}")
            if group.get("representative_roundtrip_oracle_details"):
                lines.append("- Roundtrip oracle details:")
                for detail in group["representative_roundtrip_oracle_details"][:5]:
                    text = format_roundtrip_oracle_detail(detail)
                    if text:
                        lines.append(f"  - {text}")
            lines.append("")

        lines.append("## Failed Cases")
        lines.append("")
        for case in failures:
            lines.append(f"### {case['case_id']}")
            lines.append("")
            lines.append(f"- Artifact: `{case['case_dir']}`")
            if case.get("fingerprint"):
                lines.append(f"- Fingerprint: `{case['fingerprint']}`")
            lines.append(f"- API: `{case.get('api', '')}`")
            if case.get("recipe_path"):
                lines.append(f"- Recipe: `{case['recipe_path']}`")
            if case.get("corpus", {}).get("source_file"):
                lines.append(f"- Source file: `{case['corpus']['source_file']}`")
            lines.append(f"- Reasons: {', '.join(case['reasons'])}")
            if case["warnings"]:
                lines.append(f"- Warnings: {', '.join(case['warnings'])}")
            status = case.get("status", {})
            if isinstance(status, dict):
                lines.append(
                    f"- Status: succeeded={status.get('succeeded')} "
                    f"error_code={status.get('error_code')} "
                    f"error_message=`{status.get('error_message', '')}`"
                )

            topo_summary = case.get("topo_track_summary") or {}
            if topo_summary:
                lines.append(
                    "- Topo track: "
                    f"items={topo_summary.get('item_count')} "
                    f"ancestors={topo_summary.get('ancestor_count')} "
                    f"resolved={topo_summary.get('resolved_ancestor_count')} "
                    f"unresolved={topo_summary.get('unresolved_ancestor_count')} "
                    f"roles={topo_summary.get('ancestor_input_role_counts')}"
                )

            if case.get("topo_check_failures"):
                lines.append("- TopoCheck failures:")
                for failure in case["topo_check_failures"][:5]:
                    topo = failure.get("error_topology", {})
                    lines.append(
                        f"  - body={failure.get('index')} code={failure.get('error_code')} "
                        f"{failure.get('error_string')} topo={topo}"
                    )

            if case.get("validation_failures"):
                lines.append("- Validation failures:")
                for failure in case["validation_failures"][:8]:
                    lines.append(f"  - {failure}")
                validation = case.get("validation") if isinstance(case.get("validation"), dict) else {}
                totals = validation.get("totals") if isinstance(validation.get("totals"), dict) else {}
                if totals:
                    lines.append(f"- Validation totals: {totals}")
            if case.get("validation_oracle_details"):
                lines.append("- Validation oracle details:")
                for detail in case["validation_oracle_details"][:8]:
                    text = format_validation_oracle_detail(detail)
                    if text:
                        lines.append(f"  - {text}")
            if case.get("roundtrip_failures"):
                lines.append("- Roundtrip failures:")
                for failure in case["roundtrip_failures"][:8]:
                    lines.append(f"  - {failure}")
            if case.get("roundtrip_oracle_details"):
                lines.append("- Roundtrip oracle details:")
                for detail in case["roundtrip_oracle_details"][:8]:
                    text = format_roundtrip_oracle_detail(detail)
                    if text:
                        lines.append(f"  - {text}")

            if case.get("localized_inputs"):
                lines.append("- Localized input topology:")
                for loc in case["localized_inputs"][:8]:
                    lines.append(
                        f"  - {loc['role']} {loc['type']}#{loc['id']}[{loc['local_index']}] "
                        f"op=`{loc.get('terminal_operation', '')}` count={loc.get('count')} "
                        f"{format_locator(loc.get('locator'))}"
                    )
            if case.get("input_contact_candidates"):
                lines.append("- Input contact candidates:")
                for candidate in case["input_contact_candidates"][:8]:
                    lines.append(f"  - {format_contact_candidate(candidate)}")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    roots = iter_roots(args.artifacts)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    command_records_by_run, command_by_case_dir = load_command_records(iter_command_summaries(roots))
    case_dirs = iter_case_dirs(roots)
    case_dir_keys = {str(path.resolve()) for path in case_dirs}

    cases = [
        classify_case(
            case_dir,
            command_by_case_dir.get(str(case_dir.resolve())),
            args.max_localized,
            args.max_contact_candidates,
        )
        for case_dir in case_dirs
    ]
    failures = [case for case in cases if case["failed"]]
    warning_cases = [case for case in cases if case["warnings"]]
    command_failure_records = command_failures(command_records_by_run, case_dir_keys)
    failure_groups = build_failure_groups(failures)
    regression_seeds = build_regression_seeds(failure_groups)

    summary: dict[str, Any] = {
        "roots": [path_for_json(root) for root in roots],
        "total_cases": len(cases),
        "passed_cases": sum(1 for case in cases if not case["failed"]),
        "failed_cases": len(failures),
        "failure_group_count": len(failure_groups),
        "warning_cases": len(warning_cases),
        "command_failures": len(command_failure_records),
        "failures": failures,
        "failure_groups": failure_groups,
        "regression_seeds": regression_seeds,
        "command_failure_records": command_failure_records,
    }
    if args.include_passed:
        summary["cases"] = cases

    summary_path = out_dir / "triage_summary.json"
    report_path = out_dir / "triage_report.md"
    seeds_path = out_dir / "regression_seeds.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    seeds_path.write_text(json.dumps(regression_seeds, indent=2), encoding="utf-8")
    write_markdown(summary, report_path)

    print(f"summary={summary_path}")
    print(f"report={report_path}")
    print(f"seeds={seeds_path}")
    print(
        f"cases={summary['total_cases']} failures={summary['failed_cases']} "
        f"groups={summary['failure_group_count']} "
        f"command_failures={summary['command_failures']} warnings={summary['warning_cases']}"
    )
    if args.fail_on_failures and (failures or command_failure_records):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
