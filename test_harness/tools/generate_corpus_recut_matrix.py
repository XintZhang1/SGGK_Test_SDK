#!/usr/bin/env python3
"""Generate loaded-SGT recut boolean attacks from corpus bodies."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from score_case_complexity import DIMENSIONS, MIN_FLAT_SCORE, evaluate_flat_recipe_candidate
from validate_recipe import validate_recipe

TOPO_TOL = 1e-2
GEOM_TOL = 1e-5
MAX_MODEL_SIZE = 5e5
SUPPORTED_EXTENSIONS = {".sgt"}
PROBE_CACHE_SCHEMA_VERSION = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", action="append", default=[], help="SGT file or directory. Can be passed more than once."
    )
    parser.add_argument(
        "--dataset-list",
        action="append",
        default=[],
        help="Text file with one corpus path per line, or discover_corpus.py JSON with files[*].path.",
    )
    parser.add_argument("--out", required=True, help="Directory for generated flat recipe JSON files")
    parser.add_argument("--case-prefix", default="corpus_recut", help="Prefix for generated case_id values")
    parser.add_argument("--preset", choices=["smoke", "standard", "stress"], default="smoke", help="Generation breadth")
    parser.add_argument("--source-limit", type=int, default=0, help="Maximum SGT sources to use; 0 means all")
    parser.add_argument("--limit", type=int, default=0, help="Maximum recipes to write; 0 means all")
    parser.add_argument("--body-index", type=int, default=0, help="Loaded SGT body index")
    parser.add_argument("--topo-track", action="store_true", help="Enable SDK topo tracking in generated recipes")
    parser.add_argument(
        "--sample-input-properties",
        action="store_true",
        help="Sample target/tool input length/area/volume for boolean volume-relation oracles.",
    )
    parser.add_argument(
        "--runner",
        default="",
        help="Optional sggk_case_runner.exe used to probe exact SGT extrema with coordinate-plane distance checks.",
    )
    parser.add_argument(
        "--no-exact-bbox-probe",
        action="store_true",
        help="Do not run coordinate-plane distance probes; use serialized point/bndbox estimates only.",
    )
    parser.add_argument(
        "--require-exact-bbox-probe",
        action="store_true",
        help=(
            "Skip sources whose coordinate-plane distance probe fails instead of falling back to serialized estimates."
        ),
    )
    parser.add_argument("--probe-out", default="", help="Directory for temporary exact-bbox probe artifacts")
    parser.add_argument(
        "--probe-timeout", type=float, default=60.0, help="Seconds allowed for each exact-bbox probe run"
    )
    parser.add_argument(
        "--probe-cache",
        default="",
        help="Persistent exact-bbox probe cache JSON; defaults to <probe-out>/exact_bbox_cache.json",
    )
    parser.add_argument(
        "--no-probe-cache",
        action="store_true",
        help="Disable the persistent exact-bbox probe cache and probe every source",
    )
    parser.add_argument(
        "--min-complexity-score",
        type=int,
        default=0,
        help="Fail with a nonzero exit when any generated recipe scores below this floor; 0 only reports complexity",
    )
    parser.add_argument("--no-validate", action="store_true", help="Skip recipe validation before writing")
    parser.add_argument(
        "--manifest", default="", help="Optional manifest path; defaults to a sibling <out>_manifest.json"
    )
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def sanitize_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_").lower()
    return text or "case"


def stable_hash(value: Any, length: int = 10) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:length]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def load_dataset_list(path: Path) -> list[str]:
    if not path.exists():
        raise ValueError(f"dataset list not found: {path}")
    if path.suffix.lower() == ".json":
        data = read_json(path)
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
                result.append(raw)
        return result

    result = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        result.append(line)
    return result


def iter_inputs(paths: list[str]) -> list[Path]:
    found: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            found.add(path.resolve())
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS:
                    found.add(child.resolve())
    return sorted(found, key=lambda item: str(item).lower())


def collect_dataset_paths(args: argparse.Namespace) -> list[str]:
    paths = list(args.dataset)
    for raw_list in args.dataset_list:
        paths.extend(load_dataset_list(Path(raw_list)))
    return paths


def numeric_point(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    if not all(
        isinstance(coord, int | float) and not isinstance(coord, bool) and math.isfinite(coord) for coord in value
    ):
        return None
    return [float(coord) for coord in value]


def collect_named_points(value: Any, points: list[list[float]]) -> None:
    if isinstance(value, dict):
        for key in ("pnt", "pos", "origin", "min", "max"):
            point = numeric_point(value.get(key))
            if point is not None:
                points.append(point)
        bbox = value.get("bndbox")
        if isinstance(bbox, dict):
            for key in ("min", "max"):
                point = numeric_point(bbox.get(key))
                if point is not None:
                    points.append(point)
        for child in value.values():
            collect_named_points(child, points)
    elif isinstance(value, list):
        for child in value:
            collect_named_points(child, points)


def estimate_sgt_bbox(path: Path) -> dict[str, Any]:
    data = read_json(path)
    points: list[list[float]] = []
    collect_named_points(data, points)
    if not points:
        return {"ok": False, "error": "no named 3D points found"}
    mins = [min(point[axis] for point in points) for axis in range(3)]
    maxs = [max(point[axis] for point in points) for axis in range(3)]
    dims = [maxs[axis] - mins[axis] for axis in range(3)]
    center = [(mins[axis] + maxs[axis]) * 0.5 for axis in range(3)]
    if any(abs(coord) > MAX_MODEL_SIZE for point in (mins, maxs) for coord in point):
        return {
            "ok": False,
            "error": "bbox exceeds max model size",
            "min": mins,
            "max": maxs,
            "dims": dims,
            "center": center,
            "point_count": len(points),
        }
    return {
        "ok": True,
        "source": "serialized_points",
        "min": mins,
        "max": maxs,
        "dims": dims,
        "center": center,
        "point_count": len(points),
    }


def default_runner_path() -> Path | None:
    candidates = [
        Path("build/test_harness/Release/sggk_case_runner.exe"),
        Path("build/test_harness/sggk_case_runner.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def resolve_probe_runner(args: argparse.Namespace) -> Path | None:
    if args.no_exact_bbox_probe:
        return None
    if args.runner:
        runner = Path(args.runner)
        if not runner.is_file():
            raise ValueError(f"--runner not found: {runner}")
        return runner.resolve()
    return default_runner_path()


def plane_extreme_probe_checks(estimated_bbox: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    mins = estimated_bbox.get("min") if isinstance(estimated_bbox.get("min"), list) else [0.0, 0.0, 0.0]
    maxs = estimated_bbox.get("max") if isinstance(estimated_bbox.get("max"), list) else [0.0, 0.0, 0.0]
    for axis_index, axis in enumerate(("x", "y", "z")):
        checks.append(
            {
                "id": f"{axis}_min",
                "role": "result",
                "axis": axis,
                "side": "min",
                "expected": float(mins[axis_index]),
                "tolerance": TOPO_TOL,
                "required": False,
            }
        )
        checks.append(
            {
                "id": f"{axis}_max",
                "role": "result",
                "axis": axis,
                "side": "max",
                "expected": float(maxs[axis_index]),
                "tolerance": TOPO_TOL,
                "required": False,
            }
        )
    return checks


def exact_bbox_probe_recipe(path: Path, estimated_bbox: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": sanitize_id(f"bbox_probe_{source_label(path)}"),
        "api": "check_sgt",
        "source_file": str(path),
        "modeling_tol": TOPO_TOL,
        "max_model_size": MAX_MODEL_SIZE,
        "expectations": {
            "result_bodies": {"min": 1},
            "require_property_calculations": False,
            "require_finite_properties": False,
            "require_nonnegative_length_area": False,
            "boolean_volume_relation": False,
            "boolean_bbox_relation": False,
            "plane_extreme_checks": plane_extreme_probe_checks(estimated_bbox),
        },
    }


def bbox_from_plane_extreme_records(records: Any) -> dict[str, Any]:
    values: dict[tuple[str, str], float] = {}
    if not isinstance(records, list):
        return {"ok": False, "source": "plane_distance_extrema", "error": "plane_extreme_checks missing"}
    for record in records:
        if not isinstance(record, dict):
            continue
        axis = record.get("axis")
        side = record.get("side")
        actual = record.get("actual_extreme")
        if axis not in {"x", "y", "z"} or side not in {"min", "max"}:
            continue
        if not isinstance(actual, int | float) or isinstance(actual, bool) or not math.isfinite(actual):
            continue
        probe = record.get("probe")
        if not isinstance(probe, dict) or probe.get("success") is not True:
            continue
        values[(str(axis), str(side))] = float(actual)

    missing = [f"{axis}_{side}" for axis in ("x", "y", "z") for side in ("min", "max") if (axis, side) not in values]
    if missing:
        return {
            "ok": False,
            "source": "plane_distance_extrema",
            "error": "missing exact extrema: " + ", ".join(missing),
        }

    mins = [values[(axis, "min")] for axis in ("x", "y", "z")]
    maxs = [values[(axis, "max")] for axis in ("x", "y", "z")]
    if any(abs(coord) > MAX_MODEL_SIZE for point in (mins, maxs) for coord in point):
        return {
            "ok": False,
            "source": "plane_distance_extrema",
            "error": "exact bbox exceeds max model size",
            "min": mins,
            "max": maxs,
        }
    dims = [maxs[axis] - mins[axis] for axis in range(3)]
    if any(dim < -TOPO_TOL for dim in dims):
        return {
            "ok": False,
            "source": "plane_distance_extrema",
            "error": "exact bbox min exceeds max",
            "min": mins,
            "max": maxs,
            "dims": dims,
        }
    dims = [max(0.0, dim) for dim in dims]
    center = [(mins[axis] + maxs[axis]) * 0.5 for axis in range(3)]
    return {
        "ok": True,
        "source": "plane_distance_extrema",
        "min": mins,
        "max": maxs,
        "dims": dims,
        "center": center,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_probe_cache(path: Path) -> dict[str, Any]:
    """Read the persistent probe cache; unreadable or stale files degrade to empty."""

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": PROBE_CACHE_SCHEMA_VERSION, "entries": {}}
    if not isinstance(data, dict) or data.get("schema_version") != PROBE_CACHE_SCHEMA_VERSION:
        return {"schema_version": PROBE_CACHE_SCHEMA_VERSION, "entries": {}}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    return {"schema_version": PROBE_CACHE_SCHEMA_VERSION, "entries": entries}


def write_probe_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    temporary.replace(path)


def validated_cached_bbox(entry: Any) -> dict[str, Any] | None:
    """Return a trusted cached bbox, or None; corrupt entries are ignored, never fatal."""

    if not isinstance(entry, dict):
        return None
    bbox = entry.get("bbox")
    if not isinstance(bbox, dict) or bbox.get("ok") is not True:
        return None
    source = bbox.get("source")
    if not isinstance(source, str) or not source:
        return None
    result: dict[str, Any] = {"ok": True, "source": source}
    for key in ("min", "max", "dims", "center"):
        point = numeric_point(bbox.get(key))
        if point is None:
            return None
        result[key] = point
    return result


class ExactBboxProbeCache:
    """Content-keyed exact-bbox cache that skips runner processes on hits.

    Entries are keyed by ``sha256(sgt bytes):sha256(runner bytes)`` so editing
    either input invalidates the probe. Only successful probes are stored.
    """

    def __init__(self, path: Path, runner: Path) -> None:
        self.path = path
        self.runner_sha256 = file_sha256(runner)
        self.state = load_probe_cache(path)
        self.hits = 0

    def key(self, source: Path) -> str:
        return f"{file_sha256(source)}:{self.runner_sha256}"

    def lookup(self, source: Path) -> dict[str, Any] | None:
        entry = self.state["entries"].get(self.key(source))
        bbox = validated_cached_bbox(entry)
        if bbox is None:
            return None
        self.hits += 1
        bbox["cache_hit"] = True
        artifact_dir = str(entry.get("probe_artifact_dir") or "")
        if artifact_dir and Path(artifact_dir).is_dir():
            bbox["probe_artifact_dir"] = artifact_dir
            case_id = str(entry.get("probe_case_id") or "")
            if case_id:
                bbox["probe_case_id"] = case_id
        return bbox

    def store(self, source: Path, *, probe_case_id: str, probe_artifact_dir: str, bbox: dict[str, Any]) -> None:
        self.state["entries"][self.key(source)] = {
            "source_file": str(source),
            "probe_case_id": probe_case_id,
            "probe_artifact_dir": probe_artifact_dir,
            "cached_at": now_iso_like(),
            "bbox": {key: bbox[key] for key in ("ok", "source", "min", "max", "dims", "center") if key in bbox},
        }
        write_probe_cache(self.path, self.state)


def probe_exact_sgt_bbox(
    *,
    runner: Path,
    source: Path,
    estimated_bbox: dict[str, Any],
    probe_root: Path,
    timeout: float,
    cache: ExactBboxProbeCache | None = None,
) -> dict[str, Any]:
    if cache is not None:
        cached_bbox = cache.lookup(source)
        if cached_bbox is not None:
            return cached_bbox
    recipe = exact_bbox_probe_recipe(source, estimated_bbox)
    recipe_path = probe_root / "recipes" / f"{recipe['case_id']}.json"
    write_json(recipe_path, recipe)
    probe_root.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [str(runner), "--recipe", str(recipe_path), "--out", str(probe_root / "runs")],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "source": "plane_distance_extrema",
            "error": f"probe timeout after {timeout:g}s",
            "stdout": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
        }
    artifact_dir = probe_root / "runs" / str(recipe["case_id"])
    validation_path = artifact_dir / "report" / "validation.json"
    if not validation_path.is_file():
        return {
            "ok": False,
            "source": "plane_distance_extrema",
            "error": "probe validation missing",
            "returncode": completed.returncode,
            "stdout": completed.stdout[-2000:],
            "stderr": completed.stderr[-2000:],
        }
    try:
        validation = read_json(validation_path)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "source": "plane_distance_extrema", "error": f"probe validation read_error: {exc}"}
    bbox = bbox_from_plane_extreme_records(validation.get("plane_extreme_checks"))
    bbox["probe_case_id"] = recipe["case_id"]
    bbox["probe_artifact_dir"] = str(artifact_dir.resolve())
    bbox["probe_returncode"] = completed.returncode
    if not bbox.get("ok"):
        bbox["stdout"] = completed.stdout[-2000:]
        bbox["stderr"] = completed.stderr[-2000:]
    elif cache is not None:
        cache.store(
            source,
            probe_case_id=recipe["case_id"],
            probe_artifact_dir=str(artifact_dir.resolve()),
            bbox=bbox,
        )
    return bbox


def positive_span(value: float) -> float:
    return max(abs(value), 1.0)


def source_label(path: Path) -> str:
    stem = sanitize_id(path.stem)
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    return f"{stem}_{digest}"


def volume_relation_abs_tolerance(bbox: dict[str, Any]) -> float:
    dims = bbox.get("dims") if isinstance(bbox.get("dims"), list) else []
    if len(dims) != 3:
        return TOPO_TOL
    x, y, z = (max(0.0, float(value)) for value in dims)
    # A fuzzy/topological boundary may move by modeling_tol. Convert that
    # length tolerance into a conservative volume tolerance using the target's
    # bbox surface scale; a fixed 0.01 cubic-unit bound is dimensionally wrong
    # for large imported bodies.
    bbox_surface = 2.0 * (x * y + x * z + y * z)
    return max(TOPO_TOL, bbox_surface * TOPO_TOL)


def base_expectations(
    allow_empty: bool,
    sample_input_properties: bool,
    *,
    volume_relation: bool,
    volume_relation_abs_tol: float,
) -> dict[str, Any]:
    return {
        "result_bodies": {"min": 0 if allow_empty else 1},
        "sample_input_properties": sample_input_properties,
        "require_property_calculations": True,
        "require_finite_properties": True,
        "require_nonnegative_length_area": True,
        "require_nonnegative_volume": False,
        "boolean_volume_relation": volume_relation,
        "boolean_bbox_relation": True,
        "volume_relation_abs_tol": volume_relation_abs_tol,
        "volume_relation_rel_tol": 1e-6,
    }


def target_body(path: Path, body_index: int, label: str) -> dict[str, Any]:
    return {
        "target_kind": "loaded_sgt",
        "target_source_file": str(path),
        "target_body_index": body_index,
        "target_operations": [f"load_corpus_sgt:{label}"],
    }


def cylinder_tool(radius: float, height: float, x: float, y: float, z: float, label: str) -> dict[str, Any]:
    return {
        "tool_kind": "solid_cylinder",
        "tool_radius": radius,
        "tool_height": height,
        "tool_angle": math.tau,
        "tool_translate_x": x,
        "tool_translate_y": y,
        "tool_translate_z": z,
        "tool_operations": [f"recut_cylinder:{label}"],
    }


def sweep_tool(radius: float, height: float, x: float, y: float, z: float, label: str) -> dict[str, Any]:
    return {
        "tool_kind": "sweep_circle_line",
        "tool_profile_radius": radius,
        "tool_height": height,
        "tool_operation_tol": TOPO_TOL,
        "tool_g1_tol": 0.1,
        "tool_translate_x": x,
        "tool_translate_y": y,
        "tool_translate_z": z,
        "tool_operations": [f"recut_sweep_circle_line:{label}"],
    }


def extrude_slab_tool(
    length: float, width: float, height: float, x: float, y: float, z: float, label: str
) -> dict[str, Any]:
    return {
        "tool_kind": "extrude_rect",
        "tool_length": length,
        "tool_width": width,
        "tool_height": height,
        "tool_operation_tol": TOPO_TOL,
        "tool_translate_x": x,
        "tool_translate_y": y,
        "tool_translate_z": z,
        "tool_operations": [f"recut_extrude_rect_slab:{label}"],
    }


def offsets_for_preset(preset: str) -> list[tuple[str, float]]:
    if preset == "smoke":
        return [
            ("overlap_geom_tol", -GEOM_TOL),
            ("exact", 0.0),
            ("gap_geom_tol", GEOM_TOL),
        ]
    return [
        ("overlap_topo_tol", -TOPO_TOL),
        ("overlap_geom_tol", -GEOM_TOL),
        ("exact", 0.0),
        ("gap_geom_tol", GEOM_TOL),
        ("gap_topo_tol", TOPO_TOL),
    ]


def boolean_types_for_preset(preset: str) -> list[str]:
    if preset == "smoke":
        return ["SUBTRACTION"]
    if preset == "standard":
        return ["SUBTRACTION", "INTERSECTION"]
    return ["SUBTRACTION", "INTERSECTION", "UNION"]


def tool_families_for_preset(preset: str) -> list[str]:
    if preset == "smoke":
        return ["cylinder_tangent_x"]
    if preset == "standard":
        return ["cylinder_tangent_x", "sweep_tangent_x", "extrude_tangent_x_slab"]
    return [
        "cylinder_tangent_x",
        "cylinder_tangent_y",
        "sweep_tangent_x",
        "sweep_tangent_y",
        "extrude_tangent_x_slab",
    ]


def allow_empty_result(boolean_type: str, variant: str) -> bool:
    if boolean_type != "INTERSECTION":
        return False
    return variant != "overlap_topo_tol"


def case_recipe(
    *,
    case_id: str,
    source_path: Path,
    source_label_text: str,
    boolean_type: str,
    variant: str,
    family: str,
    tool: dict[str, Any],
    topo_track: bool,
    body_index: int,
    sample_input_properties: bool,
    volume_relation: bool,
    volume_relation_abs_tol: float,
) -> dict[str, Any]:
    recipe: dict[str, Any] = {
        "case_id": case_id,
        "api": "api_boolean",
        "family": "corpus_recut",
        "variant": f"{family}_{boolean_type.lower()}_{variant}",
        "hypothesis": (
            "Reuse a corpus/imported SGT body as the target and recut it with a generated tool near "
            "bbox contact, covering tolerance-side boolean behavior on real topology."
        ),
        "boolean_type": boolean_type,
        "modeling_tol": TOPO_TOL,
        "check_valid": True,
        "topo_track": topo_track,
        "non_destructive": True,
        "expectations": base_expectations(
            allow_empty=allow_empty_result(boolean_type, variant),
            sample_input_properties=sample_input_properties,
            volume_relation=volume_relation,
            volume_relation_abs_tol=volume_relation_abs_tol,
        ),
    }
    recipe.update(target_body(source_path, body_index, source_label_text))
    recipe.update(tool)
    return recipe


def make_cases_for_source(args: argparse.Namespace, path: Path, bbox: dict[str, Any]) -> list[dict[str, Any]]:
    mins = bbox["min"]
    maxs = bbox["max"]
    dims = bbox["dims"]
    center = bbox["center"]
    span = max(positive_span(dim) for dim in dims)
    x_span = positive_span(dims[0])
    y_span = positive_span(dims[1])
    z_span = positive_span(dims[2])
    radius = max(0.1, min(span * 0.15, max(TOPO_TOL * 20.0, span * 0.35)))
    pad = max(radius * 2.0, TOPO_TOL * 20.0)
    vertical_height = max(z_span + 2.0 * pad, 4.0 * radius, 1.0)
    z_base = mins[2] - pad
    sweep_radius = max(0.1, radius * 0.65)
    sweep_height = max(1.0, vertical_height * 0.8)
    sweep_z_base = center[2] - sweep_height * 0.5
    slab_length = max(2.0 * TOPO_TOL, min(x_span * 0.2, max(radius, TOPO_TOL * 10.0)))
    slab_width = y_span + 2.0 * pad
    relation_abs_tol = volume_relation_abs_tolerance(bbox)
    label = source_label(path)
    cases: list[dict[str, Any]] = []

    for family in tool_families_for_preset(args.preset):
        for boolean_type in boolean_types_for_preset(args.preset):
            for suffix, delta in offsets_for_preset(args.preset):
                tool: dict[str, Any] | None = None
                if family == "cylinder_tangent_x":
                    tool = cylinder_tool(radius, vertical_height, maxs[0] + radius + delta, center[1], z_base, family)
                elif family == "cylinder_tangent_y":
                    tool = cylinder_tool(radius, vertical_height, center[0], maxs[1] + radius + delta, z_base, family)
                elif family == "sweep_tangent_x":
                    tool = sweep_tool(
                        sweep_radius,
                        sweep_height,
                        maxs[0] + sweep_radius + delta,
                        center[1],
                        sweep_z_base,
                        family,
                    )
                elif family == "sweep_tangent_y":
                    tool = sweep_tool(
                        sweep_radius,
                        sweep_height,
                        center[0],
                        maxs[1] + sweep_radius + delta,
                        sweep_z_base,
                        family,
                    )
                elif family == "extrude_tangent_x_slab":
                    tool = extrude_slab_tool(
                        slab_length,
                        slab_width,
                        vertical_height,
                        maxs[0] + slab_length * 0.5 + delta,
                        center[1],
                        z_base,
                        family,
                    )
                if tool is None:
                    continue
                case_hash = stable_hash(
                    {"source": str(path), "family": family, "boolean": boolean_type, "variant": suffix}
                )
                case_id = sanitize_id(f"{args.case_prefix}_{label}_{family}_{boolean_type}_{suffix}_{case_hash}")
                cases.append(
                    case_recipe(
                        case_id=case_id,
                        source_path=path,
                        source_label_text=label,
                        boolean_type=boolean_type,
                        variant=suffix,
                        family=family,
                        tool=tool,
                        topo_track=args.topo_track,
                        body_index=args.body_index,
                        sample_input_properties=args.sample_input_properties,
                        volume_relation=(
                            args.sample_input_properties
                            and family not in {"sweep_tangent_x", "sweep_tangent_y"}
                        ),
                        volume_relation_abs_tol=relation_abs_tol,
                    )
                )
    return cases


def validate_args(args: argparse.Namespace) -> None:
    if not args.dataset and not args.dataset_list:
        raise ValueError("pass at least one --dataset or --dataset-list")
    if args.source_limit < 0:
        raise ValueError("--source-limit must be >= 0")
    if args.limit < 0:
        raise ValueError("--limit must be >= 0")
    if args.body_index < 0:
        raise ValueError("--body-index must be >= 0")
    if args.probe_timeout <= 0.0:
        raise ValueError("--probe-timeout must be > 0")
    if args.probe_cache and args.no_probe_cache:
        raise ValueError("--probe-cache cannot be combined with --no-probe-cache")
    if args.min_complexity_score < 0:
        raise ValueError("--min-complexity-score must be >= 0")


def score_recipe_complexity(
    recipes: list[dict[str, Any]],
    out_dir: Path,
    min_complexity_score: int,
) -> dict[str, Any]:
    """Score every generated recipe with the model-candidate complexity machinery.

    Host-generated corpus lanes never pass through the fixed gate, so the same
    scorer that floors model candidates is applied here as evidence plus an
    optional hard gate. Recipes are never dropped; low scores are reported.
    """

    entries: list[dict[str, Any]] = []
    histogram: Counter[str] = Counter()
    for recipe in recipes:
        recipe_path = out_dir / f"{recipe['case_id']}.json"
        evaluation = evaluate_flat_recipe_candidate(recipe, str(recipe_path))
        scored = evaluation["case_scores"][0]
        for dimension, value in scored["dimensions"].items():
            if value:
                histogram[dimension] += 1
        entries.append(
            {
                "case_id": scored["case_id"],
                "path": str(recipe_path.resolve()),
                "score": scored["score"],
                "meets_model_flat_floor": scored["score"] >= MIN_FLAT_SCORE,
                "dimensions": scored["dimensions"],
                "dimensions_covered": evaluation["dimensions_covered"],
                "oracle_families": scored["oracle_families"],
            }
        )
    scores = [entry["score"] for entry in entries]
    return {
        "schema_version": 1,
        "generated_at": now_iso_like(),
        "tool": "generate_corpus_recut_matrix.py",
        "scorer": "score_case_complexity.evaluate_flat_recipe_candidate",
        "model_flat_floor": MIN_FLAT_SCORE,
        "min_complexity_score": min_complexity_score,
        "recipe_count": len(entries),
        "aggregate": {
            "min_score": min(scores) if scores else 0,
            "median_score": statistics.median(scores) if scores else 0.0,
            "floor_fraction": (
                round(sum(1 for score in scores if score >= MIN_FLAT_SCORE) / len(scores), 4) if scores else 0.0
            ),
            "below_model_floor_count": sum(1 for score in scores if score < MIN_FLAT_SCORE),
            "below_min_score_count": (
                sum(1 for score in scores if score < min_complexity_score) if min_complexity_score else 0
            ),
        },
        "dimension_histogram": {dimension: int(histogram.get(dimension, 0)) for dimension in DIMENSIONS},
        "recipes": entries,
    }


def write_report(path: Path, manifest: dict[str, Any]) -> None:
    lines = ["# SGGK Corpus Recut Matrix", ""]
    lines.append(f"- Generated: `{manifest['generated_at']}`")
    lines.append(f"- Preset: `{manifest['preset']}`")
    lines.append(f"- Sources scanned: `{manifest['source_count']}`")
    lines.append(f"- Sources used: `{manifest['used_source_count']}`")
    lines.append(f"- Recipes: `{manifest['recipe_count']}`")
    lines.append(f"- Skipped sources: `{len(manifest['skipped_sources'])}`")
    probe = manifest.get("exact_bbox_probe", {})
    if isinstance(probe, dict):
        lines.append(f"- Exact bbox probe: `{probe.get('enabled')}` failures `{probe.get('failure_count')}`")
        bbox_sources = probe.get("bbox_sources")
        if isinstance(bbox_sources, dict) and bbox_sources:
            source_text = ", ".join(f"{key}={value}" for key, value in sorted(bbox_sources.items()))
            lines.append(f"- Bbox sources: `{source_text}`")
    complexity = manifest.get("complexity", {})
    aggregate = complexity.get("aggregate") if isinstance(complexity, dict) else {}
    if isinstance(aggregate, dict) and aggregate:
        lines.append(
            f"- Complexity: min `{aggregate.get('min_score')}` median `{aggregate.get('median_score')}` "
            f"at/above model floor fraction `{aggregate.get('floor_fraction')}`"
        )
    lines.append("")
    lines.append("## Families")
    lines.append("")
    for key, value in manifest["by_family"].items():
        lines.append(f"- `{key}`: {value}")
    if manifest["skipped_sources"]:
        lines.append("")
        lines.append("## Skipped Sources")
        lines.append("")
        for item in manifest["skipped_sources"][:20]:
            lines.append(f"- `{item['path']}`: {item['reason']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(str(exc))
        return 1

    dataset_paths = collect_dataset_paths(args)
    sources = iter_inputs(dataset_paths)
    if args.source_limit:
        sources = sources[: args.source_limit]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.manifest) if args.manifest else out_dir.with_name(out_dir.name + "_manifest.json")
    report_path = manifest_path.with_suffix(".md")
    try:
        probe_runner = resolve_probe_runner(args)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.require_exact_bbox_probe and probe_runner is None:
        print("--require-exact-bbox-probe needs --runner or build/test_harness/Release/sggk_case_runner.exe")
        return 1
    probe_root = Path(args.probe_out) if args.probe_out else out_dir.with_name(out_dir.name + "_exact_bbox_probes")
    probe_cache: ExactBboxProbeCache | None = None
    if probe_runner is not None and not args.no_probe_cache:
        cache_path = Path(args.probe_cache) if args.probe_cache else probe_root / "exact_bbox_cache.json"
        probe_cache = ExactBboxProbeCache(cache_path, probe_runner)

    recipes: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    skipped_sources: list[dict[str, Any]] = []
    probe_failures: list[dict[str, Any]] = []
    bbox_sources: Counter[str] = Counter()
    for source in sources:
        try:
            estimated_bbox = estimate_sgt_bbox(source)
        except (OSError, json.JSONDecodeError) as exc:
            skipped_sources.append({"path": str(source), "reason": f"read_error: {exc}"})
            continue
        if not estimated_bbox.get("ok"):
            skipped_sources.append(
                {
                    "path": str(source),
                    "reason": str(estimated_bbox.get("error", "bbox unavailable")),
                    "bbox": estimated_bbox,
                }
            )
            continue

        bbox = estimated_bbox
        probe_bbox: dict[str, Any] | None = None
        if probe_runner is not None:
            try:
                probe_bbox = probe_exact_sgt_bbox(
                    runner=probe_runner,
                    source=source,
                    estimated_bbox=estimated_bbox,
                    probe_root=probe_root,
                    timeout=args.probe_timeout,
                    cache=probe_cache,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                probe_bbox = {"ok": False, "source": "plane_distance_extrema", "error": f"probe_error: {exc}"}
            if probe_bbox.get("ok"):
                bbox = probe_bbox
            else:
                probe_failures.append({"path": str(source), "probe": probe_bbox})
                if args.require_exact_bbox_probe:
                    skipped_sources.append({
                        "path": str(source),
                        "reason": str(probe_bbox.get("error", "exact bbox probe failed")),
                        "bbox": estimated_bbox,
                        "probe": probe_bbox,
                    })
                    continue
                bbox = dict(estimated_bbox)
                bbox["exact_probe_error"] = probe_bbox

        bbox_sources[str(bbox.get("source", "unknown"))] += 1
        source_cases = make_cases_for_source(args, source, bbox)
        source_records.append({
            "path": str(source),
            "label": source_label(source),
            "bbox": bbox,
            "estimated_bbox": estimated_bbox,
            "case_count": len(source_cases),
        })
        recipes.extend(source_cases)
        if args.limit and len(recipes) >= args.limit:
            recipes = recipes[: args.limit]
            break

    validation_failures: list[dict[str, Any]] = []
    if not args.no_validate:
        for recipe in recipes:
            errors = validate_recipe(recipe)
            if errors:
                validation_failures.append({"case_id": recipe.get("case_id"), "errors": errors})
    if validation_failures:
        write_json(out_dir / "validation_failures.json", validation_failures)
        print(f"recipe validation failed for {len(validation_failures)} generated case(s)")
        return 2

    for recipe in recipes:
        write_json(out_dir / f"{recipe['case_id']}.json", recipe)

    complexity_report_path = out_dir.with_name(out_dir.name + "_complexity_report.json")
    complexity_report = score_recipe_complexity(recipes, out_dir, args.min_complexity_score)

    by_family = Counter(
        str(recipe.get("variant", "")).split("_subtraction_")[0].split("_intersection_")[0].split("_union_")[0]
        for recipe in recipes
    )
    manifest = {
        "generated_at": now_iso_like(),
        "tool": "generate_corpus_recut_matrix.py",
        "preset": args.preset,
        "case_prefix": args.case_prefix,
        "topology_tolerance": TOPO_TOL,
        "geometry_tolerance": GEOM_TOL,
        "max_model_size": MAX_MODEL_SIZE,
        "exact_bbox_probe": {
            "enabled": probe_runner is not None,
            "runner": str(probe_runner) if probe_runner is not None else "",
            "probe_out": str(probe_root),
            "require": bool(args.require_exact_bbox_probe),
            "timeout_seconds": args.probe_timeout,
            "failure_count": len(probe_failures),
            "bbox_sources": dict(sorted(bbox_sources.items())),
            "cache_path": str(probe_cache.path) if probe_cache is not None else "",
            "cache_hits": probe_cache.hits if probe_cache is not None else 0,
        },
        "dataset": args.dataset,
        "dataset_list": args.dataset_list,
        "source_count": len(sources),
        "used_source_count": len(source_records),
        "recipe_count": len(recipes),
        "body_index": args.body_index,
        "topo_track": args.topo_track,
        "sample_input_properties": args.sample_input_properties,
        "by_family": dict(sorted(by_family.items())),
        "sources": source_records,
        "skipped_sources": skipped_sources,
        "probe_failures": probe_failures,
        "recipes": [str((out_dir / f"{recipe['case_id']}.json").resolve()) for recipe in recipes],
        "complexity": {
            "report": str(complexity_report_path),
            "min_complexity_score": args.min_complexity_score,
            "aggregate": complexity_report["aggregate"],
        },
    }
    write_json(manifest_path, manifest)
    write_report(report_path, manifest)
    write_json(complexity_report_path, complexity_report)
    print(f"recipes={out_dir}")
    print(f"manifest={manifest_path}")
    print(f"report={report_path}")
    print(f"complexity_report={complexity_report_path}")
    print(f"sources={manifest['used_source_count']} skipped={len(skipped_sources)} recipes={len(recipes)}")
    if probe_runner is not None:
        print(f"exact_bbox_probe={probe_runner} failures={len(probe_failures)}")
    if args.min_complexity_score > 0:
        below = [entry for entry in complexity_report["recipes"] if entry["score"] < args.min_complexity_score]
        if below:
            preview = ", ".join(f"{entry['case_id']}={entry['score']}" for entry in below[:10])
            print(
                f"complexity gate failed: {len(below)} recipe(s) score below --min-complexity-score "
                f"{args.min_complexity_score}: {preview}"
            )
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
