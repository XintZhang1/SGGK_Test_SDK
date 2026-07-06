#!/usr/bin/env python3
"""Scan source files for SGGK source-directed attack hints."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any


TOPO_TOL = 1e-2
GEOM_TOL = 1e-5
MAX_MODEL_SIZE = 5e5

DEFAULT_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".ipp",
    ".inl",
    ".py",
    ".md",
    ".txt",
    ".json",
}
DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".vs",
    "__pycache__",
    "artifacts",
    "build",
    "build-vs18",
    "cmake-build-debug",
    "cmake-build-release",
    "node_modules",
    "out",
    "release",
    "x64",
}

NUMERIC_RE = re.compile(r"(?<![A-Za-z_])[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?(?![A-Za-z_])")
COMPARISON_RE = re.compile(
    r"(<=|>=|==|!=|<|>)|\b(fabs|abs|IsZero|IsEqual|Compare|Less|Greater|near|parallel|coincident)\b",
    re.IGNORECASE,
)
TOL_WORD_RE = re.compile(r"\b(tol|eps|epsilon|precision|precis|disttol|localtol|modelingtol|toler)\b", re.IGNORECASE)


@dataclass(frozen=True)
class TokenRule:
    category: str
    score: int
    pattern: re.Pattern[str]


TOKEN_RULES = [
    TokenRule("todo_or_fixme", 2, re.compile(r"\b(TODO|FIXME|HACK|XXX|BUG|temporary|workaround|magic|strict|assume|unreachable|disabled)\b", re.IGNORECASE)),
    TokenRule("boolean_api", 4, re.compile(r"\b(api_boolean|Boolean|Bool|SUBTRACTION|INTERSECTION|UNION|imprint|split|slice|make[_ ]?volume|multi[_ ]?union)\b", re.IGNORECASE)),
    TokenRule("extrude_api", 3, re.compile(r"\b(extrude|prism)\b", re.IGNORECASE)),
    TokenRule("sweep_api", 3, re.compile(r"\b(sweep|pipe|loft)\b", re.IGNORECASE)),
    TokenRule("revolve_api", 3, re.compile(r"\b(revolve|revol|rotat)\b", re.IGNORECASE)),
    TokenRule("offset_heal_api", 3, re.compile(r"\b(offset|thicken|heal|sew|gap|tighten|fillet|chamfer)\b", re.IGNORECASE)),
    TokenRule("intersection_api", 4, re.compile(r"\b(intersect|intersection|IntCurve|SSI|GeomInt|section)\b", re.IGNORECASE)),
    TokenRule("distance_relation_api", 4, re.compile(r"\b(PtBodyRelation|BodyPtRelation|FacePtRelation|Relation|minimum[_ ]?distance|maximum[_ ]?distance|clash|distance)\b", re.IGNORECASE)),
    TokenRule("topo_track_api", 4, re.compile(r"\b(TopoTrack|QueryAncestors|QueryDescendents|QueryDescendants|QueryTopoTrackItems|SetToTopoTrack|Descendent|Ancestor)\b", re.IGNORECASE)),
    TokenRule("topology_mutation", 3, re.compile(r"\b(merge|split|imprint|delete|purge|clone|owner|partner|coedge|loop|shell|lump|wire|edge|vertex|face)\b", re.IGNORECASE)),
    TokenRule("boundary_geometry", 3, re.compile(r"\b(seam|periodic|singular|pole|trim|uv|degenerate|sliver|zero[_ -]?length|tangent|coincident|parallel)\b", re.IGNORECASE)),
    TokenRule("exchange_api", 3, re.compile(r"\b(STEP|IGES|stp|iges|roundtrip|import|export|DataExchange)\b", re.IGNORECASE)),
    TokenRule("success_or_status_branch", 3, re.compile(r"\b(Succeeded\s*\(|Status\s*\(|ErrorCode\s*\(|return\s+true|return\s+false|kSuccess|SUCCESS)\b", re.IGNORECASE)),
    TokenRule("exception_or_null", 2, re.compile(r"\b(nullptr|NULL|throw|assert|return\s+nullptr|return\s+NULL)\b", re.IGNORECASE)),
    TokenRule("validation_skip", 2, re.compile(r"\b(skip|skipped|NoCheck|CheckValid|unchecked|ignore|ignored)\b", re.IGNORECASE)),
]

CATEGORY_SCORES = {
    "todo_or_fixme": 2,
    "boolean_api": 4,
    "extrude_api": 3,
    "sweep_api": 3,
    "revolve_api": 3,
    "offset_heal_api": 3,
    "intersection_api": 4,
    "distance_relation_api": 4,
    "topo_track_api": 4,
    "topology_mutation": 3,
    "boundary_geometry": 3,
    "exchange_api": 3,
    "success_or_status_branch": 3,
    "exception_or_null": 2,
    "validation_skip": 2,
    "tolerance_literal": 5,
    "comparison_near_threshold": 4,
    "large_dimension_literal": 4,
    "magic_number": 1,
}

FAMILY_PRIORITY = [
    "exchange_roundtrip",
    "large_coordinate_tolerance",
    "relation_distance_oracle",
    "geometry_intersection_tolerance",
    "generated_topology_boolean",
    "boolean_tolerance_band",
    "offset_heal_degenerate",
    "topo_track_localization",
    "source_branch_threshold",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Source files or directories to scan")
    parser.add_argument("--out", required=True, help="Output directory for source_risk_report.* and attack_seed_drafts.json")
    parser.add_argument(
        "--include-ext",
        default=",".join(sorted(DEFAULT_EXTENSIONS)),
        help="Comma-separated extensions to scan; use '*' for all files",
    )
    parser.add_argument("--exclude-dir", action="append", default=[], help="Directory name to skip; can be repeated")
    parser.add_argument("--max-file-bytes", type=int, default=1_500_000, help="Skip individual files larger than this")
    parser.add_argument("--max-findings", type=int, default=500, help="Maximum findings to emit after scoring")
    parser.add_argument("--candidate-limit", type=int, default=20000, help="Stop collecting raw candidates after this many hits")
    parser.add_argument("--max-seeds", type=int, default=120, help="Maximum attack seed drafts to emit")
    parser.add_argument("--context-lines", type=int, default=1, help="Context lines to include around each finding")
    parser.add_argument("--seed-prefix", default="src_attack", help="Prefix for attack seed IDs")
    parser.add_argument("--fail-on-empty", action="store_true", help="Return 2 when no findings are emitted")
    return parser.parse_args()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_extensions(raw: str) -> set[str]:
    if raw.strip() == "*":
        return {"*"}
    exts = set()
    for item in raw.split(","):
        text = item.strip().lower()
        if not text:
            continue
        exts.add(text if text.startswith(".") else f".{text}")
    return exts or set(DEFAULT_EXTENSIONS)


def normalize_path_text(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def rel_to_cwd(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return normalize_path_text(path)


def should_scan_file(path: Path, extensions: set[str]) -> bool:
    return "*" in extensions or path.suffix.lower() in extensions


def iter_files(roots: list[Path], extensions: set[str], exclude_dirs: set[str], max_file_bytes: int) -> tuple[list[Path], Counter[str]]:
    files: list[Path] = []
    skipped: Counter[str] = Counter()
    exclude_lower = {item.lower() for item in exclude_dirs}
    for root in roots:
        if not root.exists():
            skipped["missing_path"] += 1
            continue
        if root.is_file():
            if not should_scan_file(root, extensions):
                skipped["extension"] += 1
                continue
            try:
                if root.stat().st_size > max_file_bytes:
                    skipped["too_large"] += 1
                    continue
            except OSError:
                skipped["stat_error"] += 1
                continue
            files.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name.lower() not in exclude_lower)
            for filename in sorted(filenames):
                path = Path(dirpath) / filename
                if not should_scan_file(path, extensions):
                    skipped["extension"] += 1
                    continue
                try:
                    if path.stat().st_size > max_file_bytes:
                        skipped["too_large"] += 1
                        continue
                except OSError:
                    skipped["stat_error"] += 1
                    continue
                files.append(path)
    return sorted(files, key=lambda item: rel_to_cwd(item).lower()), skipped


def read_text(path: Path) -> tuple[str | None, str | None]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"read_error:{exc.__class__.__name__}"
    if b"\x00" in data[:4096]:
        return None, "binary"
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "decode_replace"


def context_lines(lines: list[str], line_index: int, radius: int) -> list[dict[str, Any]]:
    start = max(0, line_index - radius)
    end = min(len(lines), line_index + radius + 1)
    return [
        {"line": i + 1, "text": lines[i].rstrip("\n\r")}
        for i in range(start, end)
    ]


def parse_numeric_literals(line: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for match in NUMERIC_RE.finditer(line):
        raw = match.group(0)
        try:
            value = float(raw)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        values.append({"raw": raw, "value": value, "column": match.start() + 1})
    return values


def near(value: float, target: float, rel: float = 1e-6, abs_tol: float = 1e-12) -> bool:
    return math.isclose(abs(value), target, rel_tol=rel, abs_tol=abs_tol)


def is_trivial_number(value: float) -> bool:
    return abs(value) in {0.0, 1.0, 2.0, 3.0}


def numeric_categories(line: str, numeric_literals: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    categories: set[str] = set()
    terms: set[str] = set()
    has_tol_word = bool(TOL_WORD_RE.search(line))
    has_comparison = bool(COMPARISON_RE.search(line))
    for literal in numeric_literals:
        value = float(literal["value"])
        abs_value = abs(value)
        raw = str(literal["raw"])
        if abs_value > 0.0:
            if near(abs_value, TOPO_TOL) or near(abs_value, GEOM_TOL) or (has_tol_word and abs_value <= 0.1) or "1e-" in raw.lower():
                categories.add("tolerance_literal")
                terms.add(raw)
            if abs_value >= 1e5:
                categories.add("large_dimension_literal")
                terms.add(raw)
            if not is_trivial_number(value) and (has_comparison or has_tol_word or any(char in line for char in "*/+-")):
                categories.add("magic_number")
                terms.add(raw)
    if numeric_literals and (has_comparison or has_tol_word):
        categories.add("comparison_near_threshold")
    return categories, terms


def severity(score: int) -> str:
    if score >= 13:
        return "critical"
    if score >= 9:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def first_column(line: str, numeric_literals: list[dict[str, Any]]) -> int:
    if numeric_literals:
        return int(numeric_literals[0]["column"])
    stripped = len(line) - len(line.lstrip())
    return stripped + 1


def category_score(categories: set[str]) -> int:
    return sum(CATEGORY_SCORES.get(category, 1) for category in categories)


def suggested_family(categories: set[str]) -> str:
    families: set[str] = set()
    if "exchange_api" in categories:
        families.add("exchange_roundtrip")
    if "large_dimension_literal" in categories:
        families.add("large_coordinate_tolerance")
    if "distance_relation_api" in categories:
        families.add("relation_distance_oracle")
    if "intersection_api" in categories and "boolean_api" not in categories:
        families.add("geometry_intersection_tolerance")
    if categories & {"extrude_api", "sweep_api", "revolve_api", "boundary_geometry"}:
        families.add("generated_topology_boolean")
    if categories & {"boolean_api", "topology_mutation", "tolerance_literal", "comparison_near_threshold"}:
        families.add("boolean_tolerance_band")
    if "offset_heal_api" in categories:
        families.add("offset_heal_degenerate")
    if "topo_track_api" in categories:
        families.add("topo_track_localization")
    if not families:
        families.add("source_branch_threshold")
    for family in FAMILY_PRIORITY:
        if family in families:
            return family
    return sorted(families)[0]


def suggested_apis(categories: set[str], family: str) -> list[str]:
    apis: list[str] = []
    if family in {"boolean_tolerance_band", "generated_topology_boolean", "large_coordinate_tolerance"}:
        apis.append("api_boolean")
    if "intersection_api" in categories:
        apis.append("api_boolean")
        apis.append("geometry_intersection_adapter")
    if "distance_relation_api" in categories:
        apis.extend(["PtBodyRelation", "FacePtRelation", "api_topo_minimum_distance", "api_body_clash"])
    if "exchange_api" in categories:
        apis.extend(["step_import", "iges_import", "step_roundtrip", "iges_roundtrip"])
    if "offset_heal_api" in categories:
        apis.extend(["offset_or_heal_harness_extension", "api_boolean"])
    if "topo_track_api" in categories:
        apis.extend(["ModelingRet::QueryTopoTrackItems", "QueryAncestors", "QueryDescendents"])
    return stable_unique(apis)


def stable_unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def suggested_oracles(categories: set[str], family: str) -> list[str]:
    oracles = ["TopoCheck", "properties_finite", "debug_geometry_sgt"]
    if family in {"boolean_tolerance_band", "generated_topology_boolean", "large_coordinate_tolerance"}:
        oracles.extend(["result_bodies", "total_area_volume", "clash_checks", "distance_checks"])
    if family == "large_coordinate_tolerance":
        oracles.append("plane_extreme_checks")
    if "distance_relation_api" in categories:
        oracles.extend(["point_relations", "face_point_relations", "distance_checks", "clash_checks"])
    if "exchange_api" in categories:
        oracles.extend(["roundtrip_comparison", "source_properties"])
    if "topo_track_api" in categories:
        oracles.extend(["topo_track_summary", "localized_inputs"])
    return stable_unique(oracles)


def finding_id(rel_path: str, line_number: int, categories: set[str], text: str) -> str:
    key = f"{rel_path}:{line_number}:{','.join(sorted(categories))}:{text.strip()}"
    return "src-risk-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def scan_file(path: Path, rel_path: str, context_radius: int) -> tuple[list[dict[str, Any]], str | None]:
    text, warning = read_text(path)
    if text is None:
        return [], warning
    lines = text.splitlines()
    findings: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        categories: set[str] = set()
        terms: set[str] = set()
        for rule in TOKEN_RULES:
            matches = list(rule.pattern.finditer(line))
            if matches:
                categories.add(rule.category)
                terms.update(match.group(0) for match in matches[:6])
        numeric_literals = parse_numeric_literals(line)
        numeric_cats, numeric_terms = numeric_categories(line, numeric_literals)
        categories.update(numeric_cats)
        terms.update(numeric_terms)
        if not categories:
            continue
        score = category_score(categories)
        family = suggested_family(categories)
        apis = suggested_apis(categories, family)
        finding = {
            "id": finding_id(rel_path, index + 1, categories, line),
            "severity": severity(score),
            "score": score,
            "category": sorted(categories)[0],
            "categories": sorted(categories),
            "suggested_attack_family": family,
            "suggested_apis": apis,
            "suggested_oracles": suggested_oracles(categories, family),
            "source": {"file": rel_path, "line": index + 1, "column": first_column(line, numeric_literals)},
            "text": line.strip(),
            "context": context_lines(lines, index, context_radius),
            "matched_terms": sorted(terms)[:20],
            "numeric_literals": numeric_literals[:20],
        }
        findings.append(finding)
    return findings, warning


def slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", text.strip()).strip("_").lower()
    return value or "risk"


def seed_hypothesis(finding: dict[str, Any]) -> str:
    categories = ", ".join(finding["categories"])
    source = finding["source"]
    values = ", ".join(str(item["raw"]) for item in finding.get("numeric_literals", [])[:4])
    value_text = f" Numeric literals: {values}." if values else ""
    return (
        f"{source['file']}:{source['line']} has {categories}; generate legal near-boundary geometry "
        f"around the cited branch and verify with hard result oracles.{value_text}"
    )


def default_boolean_dsl(seed_id: str, finding: dict[str, Any], large_coordinate: bool = False) -> dict[str, Any]:
    if large_coordinate:
        target_place = [399800.0, 0.0, 0.0]
        tool_x = 399970.0
    else:
        target_place = [0.0, 0.0, 0.0]
        tool_x = 170.0
    expectations: dict[str, Any] = {
        "result_bodies": {"min": 1},
        "require_property_calculations": True,
        "require_finite_properties": True,
        "require_nonnegative_length_area": True,
        "sample_input_properties": True,
    }
    return {
        "dsl_version": 1,
        "constants": {"topo_tol": TOPO_TOL, "geom_tol": GEOM_TOL, "max_model_size": MAX_MODEL_SIZE, "tau": "2 * pi"},
        "defaults": {
            "api": "api_boolean",
            "boolean_type": "SUBTRACTION",
            "modeling_tol": "topo_tol",
            "check_valid": True,
            "topo_track": True,
            "non_destructive": True,
            "expectations": expectations,
        },
        "cases": [
            {
                "case_id": seed_id,
                "hypothesis": seed_hypothesis(finding),
                "source_ref": f"{finding['source']['file']}:{finding['source']['line']}",
                "target": {
                    "chain": [
                        {"id": "target_rect_profile", "op": "rect_profile", "length": 260.0, "width": 180.0},
                        {"id": "target_extrude", "op": "extrude", "height": 220.0, "operation_tol": "topo_tol"},
                        {"id": "target_place", "op": "transform", "translate": target_place},
                    ]
                },
                "tool": {
                    "chain": [
                        {"id": "tool_circle_profile", "op": "circle_profile", "radius": 40.0, "operation_tol": "topo_tol", "g1_tol": 0.1},
                        {"id": "tool_line_sweep", "op": "sweep_line", "height": 260.0, "operation_tol": "topo_tol", "g1_tol": 0.1},
                        {"id": "tool_side_place", "op": "transform", "translate_x": tool_x, "translate_y": 0.0, "translate_z": -20.0},
                    ]
                },
                "paired_sweeps": [
                    {
                        "paths": [
                            "options.boolean_type",
                            "tool.chain.2.translate_x",
                            "expectations.result_bodies.min",
                        ],
                        "values": [
                            {"suffix": "sub_overlap_topo", "values": ["SUBTRACTION", f"{tool_x} - topo_tol", 1]},
                            {"suffix": "sub_overlap_geom", "values": ["SUBTRACTION", f"{tool_x} - geom_tol", 1]},
                            {"suffix": "int_exact", "values": ["INTERSECTION", tool_x, 0]},
                            {"suffix": "int_gap_geom", "values": ["INTERSECTION", f"{tool_x} + geom_tol", 0]},
                            {"suffix": "int_gap_topo", "values": ["INTERSECTION", f"{tool_x} + topo_tol", 0]},
                        ],
                    }
                ],
            }
        ],
    }


def relation_dsl(seed_id: str, finding: dict[str, Any]) -> dict[str, Any]:
    dsl = default_boolean_dsl(seed_id, finding, large_coordinate=False)
    dsl["defaults"]["expectations"].update(
        {
            "point_relations": [
                {
                    "id": "result_center_inside",
                    "role": "result",
                    "body_index": 0,
                    "point": [0.0, 0.0, 0.0],
                    "expected": "Inside",
                    "tolerance": "topo_tol",
                    "check_boundary": True,
                }
            ],
            "face_point_relations": [
                {
                    "id": "target_face_mid",
                    "role": "target",
                    "body_index": 0,
                    "face_index": 0,
                    "uv_fraction": [0.5, 0.5],
                    "expected": "OnBoundary",
                    "tolerance": "topo_tol",
                    "check_boundary": True,
                }
            ],
            "distance_checks": [
                {
                    "id": "target_tool_near_clearance",
                    "role_a": "target",
                    "body_index_a": 0,
                    "role_b": "tool",
                    "body_index_b": 0,
                    "kind": "minimum",
                    "min": 0.0,
                    "abs_tol": "geom_tol",
                }
            ],
        }
    )
    return dsl


def exchange_hint(seed_id: str, finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft_status": "review_required",
        "seed_id": seed_id,
        "source_refs": [finding["source"]],
        "recommended_lane": "discover_corpus -> run_corpus with check_sgt/step_roundtrip/iges_roundtrip, then generate_corpus_recut_matrix for imported output SGTs",
        "recipe_hint": {
            "api": "step_roundtrip",
            "case_id": seed_id,
            "source_file": "<sgt produced by import/check or reduced corpus seed>",
            "source_body_index": 0,
            "check_valid": True,
            "roundtrip_abs_tol": TOPO_TOL,
            "roundtrip_rel_tol": GEOM_TOL,
        },
        "suggested_followup": [
            "Run IGES sibling when the source path mentions IGES-specific conversion.",
            "Add corpus recut after successful import so generated boolean tools attack real imported topology.",
        ],
    }


def dsl_for_seed(seed_id: str, finding: dict[str, Any]) -> dict[str, Any]:
    family = str(finding["suggested_attack_family"])
    if family == "exchange_roundtrip":
        return exchange_hint(seed_id, finding)
    if family == "relation_distance_oracle":
        return relation_dsl(seed_id, finding)
    if family == "large_coordinate_tolerance":
        return default_boolean_dsl(seed_id, finding, large_coordinate=True)
    return default_boolean_dsl(seed_id, finding, large_coordinate=False)


def build_attack_seeds(findings: list[dict[str, Any]], max_seeds: int, seed_prefix: str) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for index, finding in enumerate(findings[:max_seeds], start=1):
        family = str(finding["suggested_attack_family"])
        seed_id = f"{slug(seed_prefix)}_{index:04d}_{slug(family)}"
        seeds.append(
            {
                "seed_id": seed_id,
                "draft_status": "review_required",
                "source": finding["source"],
                "risk_categories": finding["categories"],
                "severity": finding["severity"],
                "score": finding["score"],
                "matched_terms": finding["matched_terms"],
                "numeric_literals": finding["numeric_literals"],
                "hypothesis": seed_hypothesis(finding),
                "suggested_attack_family": family,
                "suggested_apis": finding["suggested_apis"],
                "suggested_oracles": finding["suggested_oracles"],
                "dsl_seed": dsl_for_seed(seed_id, finding),
                "minimization_hints": [
                    "Keep source numeric literals in one exact-contact variant, then add +/- geom_tol and +/- topo_tol siblings.",
                    "Render contact sheets and run geometry audit before treating a tolerance family as distinct.",
                    "If modeling result is bad and topo track is missing, record missing tracking as diagnostic context.",
                ],
            }
        )
    return seeds


def sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(
        findings,
        key=lambda item: (
            severity_rank.get(str(item["severity"]), 9),
            -int(item["score"]),
            str(item["source"]["file"]).lower(),
            int(item["source"]["line"]),
        ),
    )


def markdown_report(report: dict[str, Any]) -> str:
    scan = report["scan"]
    lines = [
        "# Source Risk Scan",
        "",
        f"- Files scanned: {scan['files_scanned']}",
        f"- Findings emitted: {len(report['findings'])}",
        f"- Attack seed drafts: {len(report['attack_seed_drafts'])}",
        f"- Candidate collection truncated: {str(scan['candidate_truncated']).lower()}",
        "",
        "## Severity Counts",
        "",
    ]
    for key, value in sorted(report["severity_counts"].items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Category Counts", ""])
    for key, value in sorted(report["category_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Top Findings", ""])
    for finding in report["findings"][:30]:
        source = finding["source"]
        categories = ", ".join(finding["categories"][:5])
        values = ", ".join(str(item["raw"]) for item in finding.get("numeric_literals", [])[:4]) or "-"
        text = str(finding["text"]).replace("|", "\\|")
        if len(text) > 160:
            text = text[:157] + "..."
        lines.append(
            f"- `{finding['severity']}` `{source['file']}:{source['line']}` "
            f"{categories}; values `{values}`; family `{finding['suggested_attack_family']}`"
        )
        lines.append(f"  - `{text}`")
    lines.extend(
        [
            "",
            "## Next Commands",
            "",
            "```powershell",
            "python .\\test_harness\\tools\\compile_attack_dsl.py <reviewed-dsl.json> --out .\\artifacts\\compiled_source_attack",
            "python .\\test_harness\\tools\\run_recipes.py --runner .\\build\\test_harness\\Release\\sggk_case_runner.exe --recipe .\\artifacts\\compiled_source_attack --out .\\artifacts\\source_attack_run --triage-out .\\artifacts\\source_attack_triage --preview-out .\\artifacts\\source_attack_preview --geometry-audit-out .\\artifacts\\source_attack_audit",
            "```",
            "",
            "Review `attack_seed_drafts.json` before running. The seeds preserve source references and propose DSL patterns, but they are intentionally marked `review_required`.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    roots = [Path(item) for item in args.paths]
    extensions = parse_extensions(args.include_ext)
    exclude_dirs = set(DEFAULT_EXCLUDE_DIRS)
    exclude_dirs.update(args.exclude_dir)
    files, skipped = iter_files(roots, extensions, exclude_dirs, args.max_file_bytes)

    all_findings: list[dict[str, Any]] = []
    read_warnings: Counter[str] = Counter()
    candidate_truncated = False
    for path in files:
        rel_path = rel_to_cwd(path)
        findings, warning = scan_file(path, rel_path, args.context_lines)
        if warning:
            read_warnings[warning] += 1
        all_findings.extend(findings)
        if len(all_findings) >= args.candidate_limit:
            candidate_truncated = True
            all_findings = all_findings[: args.candidate_limit]
            break

    sorted_findings = sort_findings(all_findings)[: args.max_findings]
    attack_seeds = build_attack_seeds(sorted_findings, args.max_seeds, args.seed_prefix)
    category_counts = Counter()
    severity_counts = Counter()
    for finding in sorted_findings:
        severity_counts[str(finding["severity"])] += 1
        for category in finding["categories"]:
            category_counts[category] += 1

    report = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "scan": {
            "cwd": str(Path.cwd()),
            "paths": [str(item) for item in roots],
            "include_ext": sorted(extensions),
            "exclude_dirs": sorted(exclude_dirs),
            "files_scanned": len(files),
            "skipped": dict(skipped),
            "read_warnings": dict(read_warnings),
            "candidate_count": len(all_findings),
            "candidate_truncated": candidate_truncated,
            "emitted_findings": len(sorted_findings),
            "constants": {
                "topology_modeling_tolerance": TOPO_TOL,
                "geometry_tolerance": GEOM_TOL,
                "max_model_size": MAX_MODEL_SIZE,
            },
        },
        "severity_counts": dict(severity_counts),
        "category_counts": dict(category_counts),
        "findings": sorted_findings,
        "attack_seed_drafts": attack_seeds,
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "source_risk_report.json", report)
    write_json(
        out_dir / "attack_seed_drafts.json",
        {
            "schema_version": 1,
            "generated_at": report["generated_at"],
            "source_risk_report": str(out_dir / "source_risk_report.json"),
            "seeds": attack_seeds,
        },
    )
    risky_files = sorted({finding["source"]["file"] for finding in sorted_findings})
    (out_dir / "source_risk_files.txt").write_text("\n".join(risky_files) + ("\n" if risky_files else ""), encoding="utf-8")
    (out_dir / "source_risk_report.md").write_text(markdown_report(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "files_scanned": len(files),
                "findings": len(sorted_findings),
                "attack_seed_drafts": len(attack_seeds),
                "out": str(out_dir),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    if args.fail_on_empty and not sorted_findings:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
