#!/usr/bin/env python3
"""Profile STEP/IGES corpus files for complex curve/surface features."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import time
from typing import Any


SUPPORTED_EXTENSIONS = {".step", ".stp", ".iges", ".igs"}
EXTENSION_TO_API = {
    ".step": ["step_import", "step_roundtrip"],
    ".stp": ["step_import", "step_roundtrip"],
    ".iges": ["iges_import", "iges_roundtrip"],
    ".igs": ["iges_import", "iges_roundtrip"],
}

FEATURE_RULES = [
    ("bspline_surface", 8, re.compile(r"\bB_SPLINE_SURFACE(?:_WITH_KNOTS)?\b", re.IGNORECASE)),
    ("bspline_curve", 5, re.compile(r"\bB_SPLINE_CURVE(?:_WITH_KNOTS)?\b", re.IGNORECASE)),
    ("trimmed_curve", 4, re.compile(r"\bTRIMMED_CURVE\b", re.IGNORECASE)),
    ("bounded_surface", 4, re.compile(r"\bBOUNDED_SURFACE\b", re.IGNORECASE)),
    ("advanced_face", 1, re.compile(r"\bADVANCED_FACE\b", re.IGNORECASE)),
    ("surface_curve", 3, re.compile(r"\b(?:SURFACE_CURVE|PCURVE|CURVE_ON_SURFACE|SEAM_CURVE)\b", re.IGNORECASE)),
    ("offset_surface", 5, re.compile(r"\bOFFSET_SURFACE\b", re.IGNORECASE)),
    ("swept_surface", 4, re.compile(r"\bSWEPT_SURFACE\b", re.IGNORECASE)),
    ("surface_of_revolution", 3, re.compile(r"\bSURFACE_OF_REVOLUTION\b", re.IGNORECASE)),
    ("surface_of_linear_extrusion", 2, re.compile(r"\bSURFACE_OF_LINEAR_EXTRUSION\b", re.IGNORECASE)),
    ("toroidal_surface", 3, re.compile(r"\bTOROIDAL_SURFACE\b", re.IGNORECASE)),
    ("spherical_surface", 2, re.compile(r"\bSPHERICAL_SURFACE\b", re.IGNORECASE)),
    ("conical_surface", 2, re.compile(r"\bCONICAL_SURFACE\b", re.IGNORECASE)),
    ("cylindrical_surface", 1, re.compile(r"\bCYLINDRICAL_SURFACE\b", re.IGNORECASE)),
]

IGES_TYPE_LABELS = {
    100: ("iges_circular_arc", 1),
    102: ("iges_composite_curve", 2),
    104: ("iges_conic_arc", 2),
    112: ("iges_parametric_spline_curve", 4),
    114: ("iges_parametric_spline_surface", 6),
    118: ("iges_ruled_surface", 2),
    120: ("iges_surface_of_revolution", 3),
    122: ("iges_tabulated_cylinder", 2),
    126: ("iges_rational_bspline_curve", 5),
    128: ("iges_rational_bspline_surface", 8),
    141: ("iges_boundary", 3),
    142: ("iges_curve_on_parametric_surface", 4),
    143: ("iges_bounded_surface", 4),
    144: ("iges_trimmed_surface", 6),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", help="STEP/IGES files or directories to scan")
    parser.add_argument("--dataset-list", action="append", default=[], help="dataset_index.json from discover_corpus.py or plain path list")
    parser.add_argument("--out", default="artifacts/cad_feature_profile/cad_feature_profile.json", help="Output JSON path")
    parser.add_argument("--paths-out", default="", help="Output text file with complex paths")
    parser.add_argument("--subset-out", default="", help="Output discover_corpus-compatible JSON index for complex files")
    parser.add_argument("--report", default="", help="Output Markdown report")
    parser.add_argument("--min-score", type=int, default=8, help="Minimum score for complex_paths.txt")
    parser.add_argument("--max-bytes", type=int, default=8_000_000, help="Maximum bytes to read per file; 0 reads full files")
    parser.add_argument("--top", type=int, default=40, help="Maximum file rows in the Markdown report")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def minimal_entry(path: Path, source: str) -> dict[str, Any]:
    suffix = path.suffix.lower()
    item: dict[str, Any] = {
        "path": str(path.resolve()),
        "extension": suffix,
        "api": EXTENSION_TO_API.get(suffix, [""])[0],
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "root": str(path.parent.resolve()) if path.exists() else "",
        "source": source,
    }
    return item


def dataset_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.suffix.lower() != ".json":
        entries: list[dict[str, Any]] = []
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            candidate = Path(line)
            if candidate.suffix.lower() in SUPPORTED_EXTENSIONS:
                entries.append(minimal_entry(candidate, str(path)))
        return entries

    root = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = []
    if not isinstance(root, dict):
        return entries
    for item in as_list(root.get("files")):
        if not isinstance(item, dict):
            continue
        raw = as_str(item.get("path"))
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.suffix.lower() in SUPPORTED_EXTENSIONS:
            copied = dict(item)
            copied["path"] = str(candidate.resolve())
            copied.setdefault("extension", candidate.suffix.lower())
            copied.setdefault("api", EXTENSION_TO_API.get(candidate.suffix.lower(), [""])[0])
            copied["source_dataset_list"] = str(path.resolve())
            entries.append(copied)
    return entries


def root_entries(raw_roots: list[str]) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for raw in raw_roots:
        root = Path(raw)
        if root.is_file() and root.suffix.lower() in SUPPORTED_EXTENSIONS:
            resolved = root.resolve()
            entries[str(resolved)] = minimal_entry(resolved, raw)
        elif root.is_dir():
            for child in root.rglob("*"):
                if child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS:
                    resolved = child.resolve()
                    entries[str(resolved)] = minimal_entry(resolved, raw)
    return [entries[key] for key in sorted(entries, key=str.lower)]


def collect_source_entries(args: argparse.Namespace) -> list[dict[str, Any]]:
    entries_by_path: dict[str, dict[str, Any]] = {}
    for entry in root_entries(args.roots):
        entries_by_path[str(Path(entry["path"]).resolve())] = entry
    for raw in args.dataset_list:
        for entry in dataset_entries(Path(raw)):
            entries_by_path[str(Path(entry["path"]).resolve())] = entry
    return [entries_by_path[key] for key in sorted(entries_by_path, key=str.lower)]


def read_text_sample(path: Path, max_bytes: int) -> tuple[str, bool]:
    if max_bytes == 0:
        data = path.read_bytes()
    else:
        with path.open("rb") as in_file:
            data = in_file.read(max_bytes)
    truncated = max_bytes > 0 and path.stat().st_size > max_bytes
    return data.decode("utf-8", errors="ignore"), truncated


def iges_directory_types(text: str) -> Counter[int]:
    d_lines = [line for line in text.splitlines() if len(line) >= 73 and line[72].upper() == "D"]
    counts: Counter[int] = Counter()
    for index in range(0, len(d_lines), 2):
        raw = d_lines[index][:8].strip()
        try:
            type_id = int(raw)
        except ValueError:
            continue
        counts[type_id] += 1
    return counts


def score_features(features: dict[str, int], iges_types: dict[str, int]) -> int:
    score = 0
    weights = {name: weight for name, weight, _pattern in FEATURE_RULES}
    for name, count in features.items():
        score += min(int(count), 20) * weights.get(name, 1)
    for raw_type, count in iges_types.items():
        label, weight = IGES_TYPE_LABELS.get(int(raw_type), ("", 0))
        if label:
            score += min(int(count), 20) * weight
    return score


def scan_file(path: Path, max_bytes: int) -> dict[str, Any]:
    suffix = path.suffix.lower()
    item: dict[str, Any] = {
        "path": str(path),
        "extension": suffix,
        "size_bytes": path.stat().st_size,
        "recommended_apis": EXTENSION_TO_API.get(suffix, []),
    }
    try:
        text, truncated = read_text_sample(path, max_bytes)
    except OSError as exc:
        item.update({"ok": False, "error": str(exc), "complexity_score": 0, "features": {}, "iges_types": {}, "tags": []})
        return item
    features = {name: len(pattern.findall(text)) for name, _weight, pattern in FEATURE_RULES}
    features = {name: count for name, count in features.items() if count}
    iges_counts = iges_directory_types(text) if suffix in {".iges", ".igs"} else Counter()
    iges_types = {str(type_id): count for type_id, count in sorted(iges_counts.items()) if type_id in IGES_TYPE_LABELS}
    tags = sorted(features)
    for raw_type in sorted(iges_types, key=lambda value: int(value)):
        tags.append(IGES_TYPE_LABELS[int(raw_type)][0])
    item.update(
        {
            "ok": True,
            "text_truncated": truncated,
            "features": features,
            "iges_types": iges_types,
            "tags": tags,
            "complexity_score": score_features(features, iges_types),
        }
    )
    return item


def build_summary(args: argparse.Namespace, entries: list[dict[str, Any]]) -> dict[str, Any]:
    files = [Path(as_str(entry.get("path"))) for entry in entries if as_str(entry.get("path"))]
    items = [scan_file(path, args.max_bytes) for path in files]
    feature_totals: Counter[str] = Counter()
    iges_type_totals: Counter[str] = Counter()
    by_extension: Counter[str] = Counter()
    for item in items:
        by_extension[as_str(item.get("extension"))] += 1
        feature_totals.update({key: int(value) for key, value in dict(item.get("features", {})).items()})
        iges_type_totals.update({key: int(value) for key, value in dict(item.get("iges_types", {})).items()})
    complex_items = [item for item in items if int(item.get("complexity_score", 0) or 0) >= args.min_score]
    complex_items.sort(key=lambda item: (-int(item.get("complexity_score", 0) or 0), as_str(item.get("path")).lower()))
    return {
        "generated_at": now_iso_like(),
        "inputs": {
            "roots": args.roots,
            "dataset_list": args.dataset_list,
            "max_bytes": args.max_bytes,
            "min_score": args.min_score,
        },
        "total_files": len(items),
        "profiled_files": sum(1 for item in items if item.get("ok")),
        "complex_file_count": len(complex_items),
        "by_extension": dict(sorted(by_extension.items())),
        "feature_totals": dict(sorted(feature_totals.items())),
        "iges_type_totals": dict(sorted(iges_type_totals.items(), key=lambda pair: int(pair[0]))),
        "files": items,
        "complex_files": complex_items,
    }


def build_complex_dataset_index(summary: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    entry_by_path = {str(Path(as_str(entry.get("path"))).resolve()): dict(entry) for entry in entries if as_str(entry.get("path"))}
    files: list[dict[str, Any]] = []
    by_extension: Counter[str] = Counter()
    by_api: Counter[str] = Counter()
    total_bytes = 0
    for index, item in enumerate(as_list(summary.get("complex_files"))):
        if not isinstance(item, dict):
            continue
        path_text = as_str(item.get("path"))
        if not path_text:
            continue
        path = Path(path_text).resolve()
        entry = dict(entry_by_path.get(str(path), minimal_entry(path, "profile_cad_features")))
        entry["index"] = index
        entry["path"] = str(path)
        entry.setdefault("extension", as_str(item.get("extension")) or path.suffix.lower())
        entry.setdefault("api", EXTENSION_TO_API.get(path.suffix.lower(), [""])[0])
        entry.setdefault("size_bytes", item.get("size_bytes", path.stat().st_size if path.is_file() else 0))
        entry["feature_profile"] = {
            "complexity_score": item.get("complexity_score", 0),
            "tags": item.get("tags", []),
            "features": item.get("features", {}),
            "iges_types": item.get("iges_types", {}),
            "recommended_apis": item.get("recommended_apis", []),
            "text_truncated": item.get("text_truncated", False),
        }
        files.append(entry)
        by_extension[as_str(entry.get("extension"))] += 1
        by_api[as_str(entry.get("api"))] += 1
        total_bytes += int(entry.get("size_bytes", 0) or 0)
    source_dataset_lists = sorted({as_str(entry.get("source_dataset_list")) for entry in entries if as_str(entry.get("source_dataset_list"))})
    return {
        "generated_at": now_iso_like(),
        "source": "profile_cad_features",
        "roots": summary.get("inputs", {}).get("roots", []),
        "source_dataset_lists": source_dataset_lists,
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "min_score": summary.get("inputs", {}).get("min_score"),
        "total_files": len(files),
        "total_bytes": total_bytes,
        "by_extension": dict(sorted(by_extension.items())),
        "by_api": dict(sorted(by_api.items())),
        "profile_summary": {
            "total_files": summary.get("total_files"),
            "profiled_files": summary.get("profiled_files"),
            "complex_file_count": summary.get("complex_file_count"),
            "feature_totals": summary.get("feature_totals", {}),
            "iges_type_totals": summary.get("iges_type_totals", {}),
        },
        "files": files,
    }


def markdown_report(summary: dict[str, Any], top: int) -> str:
    lines = [
        "# SGGK CAD Feature Profile",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Files: `{summary.get('total_files')}`",
        f"- Profiled: `{summary.get('profiled_files')}`",
        f"- Complex files: `{summary.get('complex_file_count')}`",
        f"- Min score: `{summary.get('inputs', {}).get('min_score')}`",
        f"- By extension: `{summary.get('by_extension')}`",
        "",
        "## Feature Totals",
        "",
    ]
    for key, value in dict(summary.get("feature_totals", {})).items():
        lines.append(f"- `{key}`: {value}")
    if summary.get("iges_type_totals"):
        lines.extend(["", "## IGES Type Totals", ""])
        for key, value in dict(summary.get("iges_type_totals", {})).items():
            label = IGES_TYPE_LABELS.get(int(key), ("unknown", 0))[0]
            lines.append(f"- `{key}` `{label}`: {value}")
    lines.extend(
        [
            "",
            "## Top Complex Files",
            "",
            "| score | extension | tags | path |",
            "| ---: | --- | --- | --- |",
        ]
    )
    for item in as_list(summary.get("complex_files"))[: max(top, 0)]:
        if not isinstance(item, dict):
            continue
        tags = ", ".join(as_list(item.get("tags"))[:8])
        lines.append(
            f"| {item.get('complexity_score', 0)} | `{item.get('extension', '')}` | {tags} | `{item.get('path', '')}` |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.min_score < 0 or args.max_bytes < 0 or args.top < 0:
        print("--min-score, --max-bytes, and --top must be >= 0")
        return 2
    entries = collect_source_entries(args)
    if not entries:
        print("No STEP/IGES files found. Pass roots or --dataset-list.")
        return 2
    summary = build_summary(args, entries)
    out_path = Path(args.out)
    report_path = Path(args.report) if args.report else out_path.with_suffix(".md")
    paths_path = Path(args.paths_out) if args.paths_out else out_path.with_name("complex_paths.txt")
    subset_path = Path(args.subset_out) if args.subset_out else out_path.with_name("complex_dataset_index.json")
    write_json(out_path, summary)
    write_json(subset_path, build_complex_dataset_index(summary, entries))
    write_text(report_path, markdown_report(summary, args.top))
    complex_paths = [as_str(item.get("path")) for item in as_list(summary.get("complex_files")) if isinstance(item, dict)]
    write_text(paths_path, "\n".join(complex_paths) + ("\n" if complex_paths else ""))
    print(f"profile={out_path}")
    print(f"report={report_path}")
    print(f"complex_paths={paths_path}")
    print(f"complex_dataset_index={subset_path}")
    print(f"files={summary['total_files']} complex={summary['complex_file_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
