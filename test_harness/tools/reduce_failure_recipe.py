#!/usr/bin/env python3
"""Greedily reduce a failing SGGK flat recipe while preserving its failure predicate."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from failure_predicate import build_failure_signature, signatures_match
from validate_recipe import validate_recipe


CASE_ID_RE = re.compile(r"^case_id=(?P<case_id>.+)$", re.MULTILINE)
ARTIFACT_DIR_RE = re.compile(r"^artifact_dir=(?P<artifact_dir>.+)$", re.MULTILINE)
NUMBER_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
PATH_RE = re.compile(r"[A-Za-z]:\\[^\s`\"']+")
DEFAULT_MIN_DIMENSION = 1e-2

PRESERVE_NUMERIC_KEYS = {
    "modeling_tol",
    "target_operation_tol",
    "tool_operation_tol",
    "target_g1_tol",
    "tool_g1_tol",
    "target_angle",
    "tool_angle",
    "target_body_index",
    "tool_body_index",
}

POSITIVE_FIELD_SUFFIXES = (
    "_radius",
    "_profile_radius",
    "_height",
    "_length",
    "_width",
    "_bottom_radius",
    "_top_radius",
    "_long_radius",
    "_short_radius",
    "_scale",
)


@dataclass(frozen=True)
class Predicate:
    reasons: tuple[str, ...]
    error_code: int | None
    validation_failures: tuple[str, ...]
    topo_failures: tuple[str, ...]
    failure_signature: dict[str, Any]


@dataclass
class Observation:
    label: str
    recipe_path: Path
    returncode: int
    elapsed_seconds: float
    timed_out: bool
    stdout: str
    stderr: str
    case_id: str
    artifact_dir: str
    status: dict[str, Any]
    validation: dict[str, Any]
    topo_check: dict[str, Any]
    run_state: dict[str, Any]


@dataclass
class TrialResult:
    index: int
    description: str
    accepted: bool
    preserved: bool
    validation_errors: list[str]
    observation: Observation | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="Path to sggk_case_runner.exe")
    parser.add_argument("--recipe", required=True, help="Failing flat recipe JSON")
    parser.add_argument("--out", default="artifacts/reduced_failure", help="Reducer output directory")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-trial timeout in seconds")
    parser.add_argument("--max-trials", type=int, default=120, help="Maximum candidate trials")
    parser.add_argument(
        "--min-dimension",
        type=float,
        default=DEFAULT_MIN_DIMENSION,
        help="Do not reduce positive geometry dimensions below this value",
    )
    parser.add_argument(
        "--match-error-code",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="For API-error baselines, require the same SDK error code",
    )
    parser.add_argument(
        "--allow-passing-baseline",
        action="store_true",
        help="Do not fail immediately if the input recipe no longer reproduces",
    )
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def sanitize_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return text or "case"


def stable_recipe_payload(recipe: dict[str, Any]) -> str:
    payload = dict(recipe)
    payload.pop("case_id", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def parse_stdout_field(stdout: str, regex: re.Pattern[str], group: str) -> str:
    match = regex.search(stdout or "")
    return match.group(group).strip() if match else ""


def load_json_or_empty(path: Path) -> dict[str, Any]:
    try:
        loaded = read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def run_recipe(
    runner: Path,
    recipe: dict[str, Any],
    original_case_id: str,
    label: str,
    out_root: Path,
    timeout: float,
) -> Observation:
    case_id = sanitize_id(f"{original_case_id}_{label}")
    run_recipe = dict(recipe)
    run_recipe["case_id"] = case_id
    recipe_path = out_root / "_recipes" / f"{case_id}.json"
    write_json(recipe_path, run_recipe)

    started = time.perf_counter()
    cmd = [str(runner), "--recipe", str(recipe_path), "--out", str(out_root / "runs")]
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
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        returncode = 124
        timed_out = True

    artifact_dir = parse_stdout_field(stdout, ARTIFACT_DIR_RE, "artifact_dir")
    if not artifact_dir:
        artifact_dir = str((out_root / "runs" / case_id).resolve())
    artifact_path = Path(artifact_dir)
    return Observation(
        label=label,
        recipe_path=recipe_path,
        returncode=returncode,
        elapsed_seconds=elapsed,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        case_id=parse_stdout_field(stdout, CASE_ID_RE, "case_id") or case_id,
        artifact_dir=artifact_dir,
        status=load_json_or_empty(artifact_path / "report" / "status.json"),
        validation=load_json_or_empty(artifact_path / "report" / "validation.json"),
        topo_check=load_json_or_empty(artifact_path / "report" / "topo_check.json"),
        run_state=load_json_or_empty(artifact_path / "run_state.json"),
    )


def normalize_failure_text(value: Any) -> str:
    text = str(value or "").lower()
    text = PATH_RE.sub("<path>", text)
    text = NUMBER_RE.sub("<num>", text)
    text = " ".join(text.split())
    return text[:240]


def validation_failure_keys(validation: dict[str, Any]) -> tuple[str, ...]:
    failures = validation.get("failures")
    if not isinstance(failures, list):
        return ()
    return tuple(sorted(normalize_failure_text(item) for item in failures if normalize_failure_text(item)))


def topo_failure_keys(topo_check: dict[str, Any]) -> tuple[str, ...]:
    keys: list[str] = []
    bodies = topo_check.get("bodies")
    if not isinstance(bodies, list):
        return ()
    for body in bodies:
        if not isinstance(body, dict) or body.get("ok") is not False:
            continue
        key = {
            "error_code": body.get("error_code"),
            "error_string": normalize_failure_text(body.get("error_string")),
        }
        keys.append(json.dumps(key, sort_keys=True, separators=(",", ":")))
    return tuple(sorted(keys))


def build_predicate(observation: Observation) -> Predicate:
    reasons: list[str] = []
    error_code: int | None = None
    if observation.timed_out:
        reasons.append("timed_out")
    if observation.status:
        if observation.status.get("succeeded") is False:
            reasons.append("api_failed")
            raw_code = observation.status.get("error_code")
            if isinstance(raw_code, int) and not isinstance(raw_code, bool):
                error_code = raw_code
    if observation.validation and observation.validation.get("ok") is False:
        reasons.append("validation_failed")
    topo_keys = topo_failure_keys(observation.topo_check)
    if topo_keys:
        reasons.append("topology_invalid")
    if observation.returncode != 0 and not reasons:
        reasons.append("runner_nonzero_exit")
    return Predicate(
        reasons=tuple(sorted(set(reasons))),
        error_code=error_code,
        validation_failures=validation_failure_keys(observation.validation),
        topo_failures=topo_keys,
        failure_signature=build_failure_signature(
            returncode=observation.returncode,
            timed_out=observation.timed_out,
            stderr=observation.stderr,
            status=observation.status,
            validation=observation.validation,
            topo_check=observation.topo_check,
            run_state=observation.run_state,
        ),
    )


def observation_preserves(observation: Observation, predicate: Predicate, match_error_code: bool) -> bool:
    expected = dict(predicate.failure_signature)
    if not match_error_code and expected.get("kind") == "sdk_api_error":
        expected["sdk_error_code"] = None
    observed = build_failure_signature(
        returncode=observation.returncode,
        timed_out=observation.timed_out,
        stderr=observation.stderr,
        status=observation.status,
        validation=observation.validation,
        topo_check=observation.topo_check,
        run_state=observation.run_state,
    )
    matched, _ = signatures_match(expected, observed)
    return matched


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def is_positive_numeric_field(key: str) -> bool:
    return key.endswith(POSITIVE_FIELD_SUFFIXES)


def numeric_field_candidates(key: str, value: float, min_dimension: float) -> list[float]:
    if key in PRESERVE_NUMERIC_KEYS or key.startswith("min_") or key.startswith("max_"):
        return []
    if "_tol" in key or key.endswith("_angle") or key.endswith("_body_index"):
        return []

    positive = is_positive_numeric_field(key)
    sign = -1.0 if value < 0 else 1.0
    abs_value = abs(value)
    if abs_value == 0:
        return []

    raw_candidates: list[float] = []
    if not positive:
        raw_candidates.append(0.0)
    raw_candidates.extend(sign * item for item in [1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0])
    raw_candidates.extend([value * 0.5, value * 0.75])

    candidates: list[float] = []
    for raw in raw_candidates:
        candidate = float(raw)
        if positive and candidate <= 0:
            continue
        if positive and candidate < min_dimension:
            continue
        if abs(candidate - value) < 1e-12:
            continue
        if abs(candidate) > abs_value + 1e-12:
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def with_update(recipe: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    result = dict(recipe)
    result.update(updates)
    return result


def protected_contact_keys(recipe: dict[str, Any]) -> set[str]:
    protected: set[str] = set()
    if contact_offset_x_sweep_extrude(recipe, "target", "tool") is not None:
        protected.update(
            {
                "target_profile_radius",
                "target_height",
                "target_translate_x",
                "tool_length",
                "tool_width",
                "tool_height",
                "tool_translate_x",
                "tool_translate_z",
            }
        )
    if contact_offset_x_extrude_sweep(recipe, "target", "tool") is not None:
        protected.update(
            {
                "target_length",
                "target_height",
                "target_translate_x",
                "tool_profile_radius",
                "tool_height",
                "tool_translate_x",
                "tool_translate_z",
            }
        )
    if body_kind(recipe, "target") == "sweep_circle_line" and body_kind(recipe, "tool") == "sweep_circle_line":
        protected.update(
            {
                "target_profile_radius",
                "target_translate_x",
                "target_translate_y",
                "tool_profile_radius",
                "tool_translate_x",
                "tool_translate_y",
            }
        )
    if contact_offset_x_revolve_cylinder(recipe, "target", "tool") is not None:
        protected.update(
            {
                "target_bottom_radius",
                "target_top_radius",
                "target_inner_radius",
                "target_outer_radius",
                "target_height",
                "target_translate_x",
                "target_translate_z",
                "tool_radius",
                "tool_height",
                "tool_translate_x",
                "tool_translate_z",
            }
        )
    return protected


def single_numeric_mutations(recipe: dict[str, Any], min_dimension: float) -> list[tuple[str, dict[str, Any]]]:
    mutations: list[tuple[str, dict[str, Any]]] = []
    protected = protected_contact_keys(recipe)
    for key in sorted(recipe):
        value = recipe[key]
        if key in protected:
            continue
        if not is_number(value):
            continue
        for candidate in numeric_field_candidates(key, float(value), min_dimension):
            display = f"{candidate:.12g}"
            mutations.append((f"set {key}={display}", with_update(recipe, {key: candidate})))
    return mutations


def num(recipe: dict[str, Any], key: str, fallback: float = 0.0) -> float:
    value = recipe.get(key)
    return float(value) if is_number(value) else fallback


def body_kind(recipe: dict[str, Any], prefix: str) -> str:
    value = recipe.get(f"{prefix}_kind")
    return value if isinstance(value, str) else ""


def contact_offset_x_sweep_extrude(recipe: dict[str, Any], target: str, tool: str) -> float | None:
    if body_kind(recipe, target) != "sweep_circle_line" or body_kind(recipe, tool) != "extrude_rect":
        return None
    target_max = num(recipe, f"{target}_translate_x") + num(recipe, f"{target}_profile_radius")
    tool_min = num(recipe, f"{tool}_translate_x") - 0.5 * num(recipe, f"{tool}_length")
    return tool_min - target_max


def contact_offset_x_extrude_sweep(recipe: dict[str, Any], target: str, tool: str) -> float | None:
    if body_kind(recipe, target) != "extrude_rect" or body_kind(recipe, tool) != "sweep_circle_line":
        return None
    target_max = num(recipe, f"{target}_translate_x") + 0.5 * num(recipe, f"{target}_length")
    tool_min = num(recipe, f"{tool}_translate_x") - num(recipe, f"{tool}_profile_radius")
    return tool_min - target_max


def contact_offset_x_revolve_cylinder(recipe: dict[str, Any], target: str, tool: str) -> float | None:
    if body_kind(recipe, target) not in {"revolve_line", "revolve_rect"} or body_kind(recipe, tool) != "solid_cylinder":
        return None
    if body_kind(recipe, target) == "revolve_rect":
        target_radius = num(recipe, f"{target}_outer_radius")
    else:
        target_radius = max(num(recipe, f"{target}_bottom_radius"), num(recipe, f"{target}_top_radius"))
    tool_radius = num(recipe, f"{tool}_radius")
    if target_radius <= 0.0 or tool_radius <= 0.0:
        return None
    target_max = num(recipe, f"{target}_translate_x") + target_radius
    tool_min = num(recipe, f"{tool}_translate_x") - tool_radius
    return tool_min - target_max


def small_positive_values(current: float, floor: float = 1.0, min_dimension: float = DEFAULT_MIN_DIMENSION) -> list[float]:
    seeds = [max(floor, min_dimension), 2.0, 5.0, 10.0, 20.0, 40.0, 60.0, 80.0, 100.0, 160.0]
    values: list[float] = []
    for seed in seeds:
        if seed >= min_dimension and seed < current - 1e-12:
            values.append(seed)
    return values


def coordinated_sweep_extrude_mutations(
    recipe: dict[str, Any],
    target: str,
    tool: str,
    min_dimension: float,
) -> list[tuple[str, dict[str, Any]]]:
    offset = contact_offset_x_sweep_extrude(recipe, target, tool)
    if offset is None:
        return []

    mutations: list[tuple[str, dict[str, Any]]] = []
    target_tx = num(recipe, f"{target}_translate_x")
    radius_current = num(recipe, f"{target}_profile_radius")
    length_current = num(recipe, f"{tool}_length")

    for radius in small_positive_values(radius_current, min_dimension=min_dimension):
        for length in small_positive_values(length_current, min_dimension=min_dimension):
            tool_tx = target_tx + radius + 0.5 * length + offset
            updates = {
                f"{target}_profile_radius": radius,
                f"{tool}_length": length,
                f"{tool}_translate_x": tool_tx,
            }
            mutations.append(
                (
                    f"preserve x contact {target}_profile_radius={radius:g} {tool}_length={length:g}",
                    with_update(recipe, updates),
                )
            )

    height_current = num(recipe, f"{target}_height")
    tool_height_current = num(recipe, f"{tool}_height")
    tool_z = num(recipe, f"{tool}_translate_z")
    for height in small_positive_values(height_current, floor=10.0, min_dimension=min_dimension):
        cover_height = height + max(1.0, abs(tool_z) * 2.0)
        if cover_height < tool_height_current - 1e-12:
            mutations.append(
                (
                    f"shrink z span {target}_height={height:g} {tool}_height={cover_height:g}",
                    with_update(recipe, {f"{target}_height": height, f"{tool}_height": cover_height}),
                )
            )

    tool_width_current = num(recipe, f"{tool}_width")
    for width in small_positive_values(tool_width_current, floor=10.0, min_dimension=min_dimension):
        mutations.append((f"set {tool}_width={width:g}", with_update(recipe, {f"{tool}_width": width})))

    return mutations


def coordinated_extrude_sweep_mutations(
    recipe: dict[str, Any],
    target: str,
    tool: str,
    min_dimension: float,
) -> list[tuple[str, dict[str, Any]]]:
    offset = contact_offset_x_extrude_sweep(recipe, target, tool)
    if offset is None:
        return []

    mutations: list[tuple[str, dict[str, Any]]] = []
    target_tx = num(recipe, f"{target}_translate_x")
    length_current = num(recipe, f"{target}_length")
    radius_current = num(recipe, f"{tool}_profile_radius")
    for length in small_positive_values(length_current, min_dimension=min_dimension):
        for radius in small_positive_values(radius_current, min_dimension=min_dimension):
            tool_tx = target_tx + 0.5 * length + radius + offset
            updates = {
                f"{target}_length": length,
                f"{tool}_profile_radius": radius,
                f"{tool}_translate_x": tool_tx,
            }
            mutations.append(
                (
                    f"preserve x contact {target}_length={length:g} {tool}_profile_radius={radius:g}",
                    with_update(recipe, updates),
                )
            )
    return mutations


def coordinated_sweep_sweep_mutations(
    recipe: dict[str, Any],
    target: str,
    tool: str,
    min_dimension: float,
) -> list[tuple[str, dict[str, Any]]]:
    if body_kind(recipe, target) != "sweep_circle_line" or body_kind(recipe, tool) != "sweep_circle_line":
        return []
    target_r = num(recipe, f"{target}_profile_radius")
    tool_r = num(recipe, f"{tool}_profile_radius")
    dx = num(recipe, f"{tool}_translate_x") - num(recipe, f"{target}_translate_x")
    dy = num(recipe, f"{tool}_translate_y") - num(recipe, f"{target}_translate_y")
    distance = math.hypot(dx, dy)
    if distance <= 0.0:
        return []
    phase_x = dx / distance
    phase_y = dy / distance
    offset = distance - target_r - tool_r
    mutations: list[tuple[str, dict[str, Any]]] = []
    for new_target_r in small_positive_values(target_r, min_dimension=min_dimension):
        for new_tool_r in small_positive_values(tool_r, min_dimension=min_dimension):
            new_distance = new_target_r + new_tool_r + offset
            if new_distance <= 0.0:
                continue
            updates = {
                f"{target}_profile_radius": new_target_r,
                f"{tool}_profile_radius": new_tool_r,
                f"{tool}_translate_x": num(recipe, f"{target}_translate_x") + phase_x * new_distance,
                f"{tool}_translate_y": num(recipe, f"{target}_translate_y") + phase_y * new_distance,
            }
            mutations.append(
                (
                    f"preserve radial contact {target}_r={new_target_r:g} {tool}_r={new_tool_r:g}",
                    with_update(recipe, updates),
                )
            )
    return mutations


def coordinated_revolve_cylinder_mutations(
    recipe: dict[str, Any],
    target: str,
    tool: str,
    min_dimension: float,
) -> list[tuple[str, dict[str, Any]]]:
    offset = contact_offset_x_revolve_cylinder(recipe, target, tool)
    if offset is None:
        return []

    mutations: list[tuple[str, dict[str, Any]]] = []
    target_tx = num(recipe, f"{target}_translate_x")
    target_kind = body_kind(recipe, target)
    bottom_current = num(recipe, f"{target}_bottom_radius")
    top_current = num(recipe, f"{target}_top_radius")
    inner_current = num(recipe, f"{target}_inner_radius")
    outer_current = num(recipe, f"{target}_outer_radius")
    max_radius_current = outer_current if target_kind == "revolve_rect" else max(bottom_current, top_current)
    tool_radius_current = num(recipe, f"{tool}_radius")
    if max_radius_current <= 0.0 or tool_radius_current <= 0.0:
        return []

    for max_radius in small_positive_values(max_radius_current, floor=10.0, min_dimension=min_dimension):
        scale = max_radius / max_radius_current
        radius_updates: dict[str, Any] = {}
        if target_kind == "revolve_rect":
            inner_radius = max(inner_current * scale, min_dimension)
            outer_radius = max(outer_current * scale, inner_radius + min_dimension)
            actual_max_radius = outer_radius
            radius_updates[f"{target}_inner_radius"] = inner_radius
            radius_updates[f"{target}_outer_radius"] = outer_radius
        else:
            bottom_radius = max(bottom_current * scale, min_dimension)
            top_radius = max(top_current * scale, min_dimension)
            actual_max_radius = max(bottom_radius, top_radius)
            radius_updates[f"{target}_bottom_radius"] = bottom_radius
            radius_updates[f"{target}_top_radius"] = top_radius
        for tool_radius in small_positive_values(tool_radius_current, floor=5.0, min_dimension=min_dimension):
            tool_tx = target_tx + actual_max_radius + tool_radius + offset
            updates = dict(radius_updates)
            updates[f"{tool}_radius"] = tool_radius
            updates[f"{tool}_translate_x"] = tool_tx
            mutations.append(
                (
                    f"preserve revolve/cylinder x contact {target}_max_radius={actual_max_radius:g} {tool}_radius={tool_radius:g}",
                    with_update(recipe, updates),
                )
            )

    target_height_current = num(recipe, f"{target}_height")
    tool_height_current = num(recipe, f"{tool}_height")
    target_tz = num(recipe, f"{target}_translate_z")
    margin = max(0.0, 0.5 * (tool_height_current - target_height_current))
    for height in small_positive_values(target_height_current, floor=10.0, min_dimension=min_dimension):
        tool_height = height + 2.0 * margin
        if tool_height < min_dimension:
            continue
        if tool_height > tool_height_current + 1e-12:
            continue
        tool_tz = target_tz - 0.5 * tool_height
        mutations.append(
            (
                f"preserve revolve/cylinder z cover {target}_height={height:g} {tool}_height={tool_height:g}",
                with_update(
                    recipe,
                    {
                        f"{target}_height": height,
                        f"{tool}_height": tool_height,
                        f"{tool}_translate_z": tool_tz,
                    },
                ),
            )
        )

    return mutations


def coordinated_mutations(recipe: dict[str, Any], min_dimension: float) -> list[tuple[str, dict[str, Any]]]:
    mutations: list[tuple[str, dict[str, Any]]] = []
    mutations.extend(coordinated_sweep_extrude_mutations(recipe, "target", "tool", min_dimension))
    mutations.extend(coordinated_extrude_sweep_mutations(recipe, "target", "tool", min_dimension))
    mutations.extend(coordinated_sweep_sweep_mutations(recipe, "target", "tool", min_dimension))
    mutations.extend(coordinated_revolve_cylinder_mutations(recipe, "target", "tool", min_dimension))
    return mutations


def candidate_mutations(recipe: dict[str, Any], min_dimension: float) -> list[tuple[str, dict[str, Any]]]:
    return [*coordinated_mutations(recipe, min_dimension), *single_numeric_mutations(recipe, min_dimension)]


def observation_json(observation: Observation | None) -> dict[str, Any] | None:
    if observation is None:
        return None
    return {
        "label": observation.label,
        "recipe_path": str(observation.recipe_path),
        "returncode": observation.returncode,
        "elapsed_seconds": observation.elapsed_seconds,
        "timed_out": observation.timed_out,
        "case_id": observation.case_id,
        "artifact_dir": observation.artifact_dir,
        "status": observation.status,
        "validation": observation.validation,
        "topo_check": observation.topo_check,
        "run_state": observation.run_state,
        "stderr": observation.stderr,
    }


def trial_json(trial: TrialResult) -> dict[str, Any]:
    return {
        "index": trial.index,
        "description": trial.description,
        "accepted": trial.accepted,
        "preserved": trial.preserved,
        "validation_errors": trial.validation_errors,
        "observation": observation_json(trial.observation),
    }


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# SGGK Failure Reduction",
        "",
        f"- Started: `{summary.get('started_at')}`",
        f"- Input recipe: `{summary.get('input_recipe')}`",
        f"- Reduced recipe: `{summary.get('reduced_recipe')}`",
        f"- Baseline predicate: `{summary.get('predicate')}`",
        f"- Trials: `{summary.get('trials')}`",
        f"- Accepted reductions: `{summary.get('accepted_reductions')}`",
        "",
        "## Accepted Steps",
        "",
    ]
    accepted = [item for item in summary.get("trial_results", []) if isinstance(item, dict) and item.get("accepted")]
    if accepted:
        for item in accepted:
            obs = item.get("observation") if isinstance(item.get("observation"), dict) else {}
            lines.append(f"- `#{item.get('index')}` {item.get('description')} -> `{obs.get('artifact_dir')}`")
    else:
        lines.append("- None.")
    lines.extend(["", "## Final Observation", ""])
    final_obs = summary.get("final_observation") if isinstance(summary.get("final_observation"), dict) else {}
    status = final_obs.get("status") if isinstance(final_obs.get("status"), dict) else {}
    validation = final_obs.get("validation") if isinstance(final_obs.get("validation"), dict) else {}
    lines.extend(
        [
            f"- Return code: `{final_obs.get('returncode')}`",
            f"- SDK succeeded: `{status.get('succeeded')}`",
            f"- Error code: `{status.get('error_code')}`",
            f"- Validation ok: `{validation.get('ok')}`",
            f"- Artifact: `{final_obs.get('artifact_dir')}`",
            "",
        ]
    )
    write_text(path, "\n".join(lines))


def main() -> int:
    args = parse_args()
    runner = Path(args.runner).resolve()
    recipe_path = Path(args.recipe).resolve()
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    min_dimension = max(float(args.min_dimension), 0.0)

    original_recipe = read_json(recipe_path)
    if not isinstance(original_recipe, dict):
        raise SystemExit(f"recipe root must be object: {recipe_path}")
    original_case_id = sanitize_id(str(original_recipe.get("case_id") or recipe_path.stem))

    started_at = now_iso_like()
    baseline = run_recipe(runner, original_recipe, original_case_id, "baseline", out_root, args.timeout)
    predicate = build_predicate(baseline)
    if baseline.returncode == 0 and not args.allow_passing_baseline:
        summary = {
            "started_at": started_at,
            "input_recipe": str(recipe_path),
            "baseline_observation": observation_json(baseline),
            "error": "baseline_did_not_fail",
        }
        write_json(out_root / "reduction_summary.json", summary)
        raise SystemExit("baseline recipe did not fail; use --allow-passing-baseline to still emit reports")

    current = dict(original_recipe)
    seen = {stable_recipe_payload(current)}
    trials: list[TrialResult] = []
    trial_index = 0
    accepted_count = 0
    changed = True
    while changed and trial_index < args.max_trials:
        changed = False
        for description, candidate in candidate_mutations(current, min_dimension):
            if trial_index >= args.max_trials:
                break
            payload = stable_recipe_payload(candidate)
            if payload in seen:
                continue
            seen.add(payload)
            validation_errors = validate_recipe(candidate)
            if validation_errors:
                trial_index += 1
                trials.append(
                    TrialResult(trial_index, description, False, False, validation_errors, None)
                )
                continue
            trial_index += 1
            observation = run_recipe(runner, candidate, original_case_id, f"trial_{trial_index:04d}", out_root, args.timeout)
            preserved = observation_preserves(observation, predicate, args.match_error_code)
            accepted = preserved
            trials.append(TrialResult(trial_index, description, accepted, preserved, [], observation))
            if accepted:
                current = candidate
                accepted_count += 1
                changed = True
                break

    reduced_recipe = dict(current)
    reduced_recipe["case_id"] = sanitize_id(f"{original_case_id}_reduced")
    reduced_path = out_root / "reduced_recipe.json"
    write_json(reduced_path, reduced_recipe)
    final_observation = run_recipe(runner, reduced_recipe, original_case_id, "final", out_root, args.timeout)

    summary = {
        "started_at": started_at,
        "updated_at": now_iso_like(),
        "input_recipe": str(recipe_path),
        "reduced_recipe": str(reduced_path),
        "runner": str(runner),
        "min_dimension": min_dimension,
        "predicate": {
            "reasons": list(predicate.reasons),
            "error_code": predicate.error_code,
            "validation_failures": list(predicate.validation_failures),
            "topo_failures": list(predicate.topo_failures),
            "match_error_code": args.match_error_code,
            "failure_signature": predicate.failure_signature,
        },
        "baseline_observation": observation_json(baseline),
        "final_observation": observation_json(final_observation),
        "trials": trial_index,
        "accepted_reductions": accepted_count,
        "trial_results": [trial_json(trial) for trial in trials],
    }
    write_json(out_root / "reduction_summary.json", summary)
    write_report(out_root / "reduction_report.md", summary)

    print(f"summary={out_root / 'reduction_summary.json'}")
    print(f"report={out_root / 'reduction_report.md'}")
    print(f"reduced_recipe={reduced_path}")
    print(f"trials={trial_index} accepted={accepted_count} final_rc={final_observation.returncode}")
    final_preserved = observation_preserves(final_observation, predicate, args.match_error_code)
    return 0 if final_preserved else 2


if __name__ == "__main__":
    raise SystemExit(main())
