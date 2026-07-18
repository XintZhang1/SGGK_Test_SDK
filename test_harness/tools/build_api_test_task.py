#!/usr/bin/env python3
"""Build a deterministic model task from a host-generated internal API-test IR."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from typing import Any

from jsonschema import Draft202012Validator

from harness_capabilities import (
    api_guidance,
    derive_interface_family,
    load_capabilities,
    oracle_guidance,
    run_profile_metadata,
    run_profiles,
    supported_apis,
    supported_body_builders,
    supported_oracles,
)
from campaign_profiles import CAMPAIGN_PROFILES, allowed_campaign_profiles
from model_fixed_gate_contracts import fixed_gate_contract_for_api
from plugin_catalog import ALLOWED_ARCHETYPES

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = load_capabilities()
SUPPORTED_APIS = supported_apis(CAPABILITIES)
SUPPORTED_BODY_BUILDERS = supported_body_builders(CAPABILITIES)
SUPPORTED_ORACLES = supported_oracles(CAPABILITIES)
API_GUIDANCE: dict[str, dict[str, Any]] = api_guidance(CAPABILITIES)
ORACLE_GUIDANCE: dict[str, str] = oracle_guidance(CAPABILITIES)
RUN_PROFILES: dict[str, dict[str, Any]] = run_profiles(CAPABILITIES)
FORM_SCHEMA_PATH = REPO_ROOT / "test_harness/forms/api_test_form.schema.json"
FORM_SCHEMA = json.loads(FORM_SCHEMA_PATH.read_text(encoding="utf-8-sig"))
FORM_VALIDATOR = Draft202012Validator(FORM_SCHEMA)

INTERFACE_DESIGN_TASK_TYPE = "interface_dsl_design"

# Fixed vocabulary for the interface-design subagent.  The cluster taxonomy
# mirrors compile_attack_dsl.CLUSTER_TYPES; keep them aligned.
INTERFACE_DESIGN_CLUSTER_TYPES = [
    "translate_axis",
    "translate_line",
    "scale_uniform",
    "size_dimension",
    "contact_band",
    "tolerance_sweep",
    "angle_sweep",
    "large_coordinate_shift",
    "boolean_type_cycle",
    "option_toggle",
    "mirror_sign",
    "seeded_jitter",
    "uv_domain",
    "enum_cycle",
]
INTERFACE_DESIGN_COMPLEXITY_DIMENSIONS = [
    "multi_op_chain",
    "generated_topology",
    "tolerance_band",
    "oracle_strength",
    "large_coordinate",
    "degenerate_or_negative",
    "transform_usage",
]

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("form", help="Harness-generated internal API-test IR JSON")
    parser.add_argument("--out", help="Output task path. Defaults to stdout.")
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Task output format",
    )
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def rel_display(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def read_text_if_present(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig")


def repo_path(raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def matches_example_pack(form: dict[str, Any], pack: dict[str, Any]) -> bool:
    match = pack.get("match") if isinstance(pack.get("match"), dict) else {}
    target_apis = as_string_list(match.get("target_apis"))
    if target_apis and form.get("target_api") not in target_apis:
        return False

    geometry = form.get("geometry") if isinstance(form.get("geometry"), dict) else {}
    family = geometry.get("family")
    geometry_families = as_string_list(match.get("geometry_families"))
    if geometry_families and family in geometry_families:
        return True

    form_oracles = set(as_string_list(form.get("oracles")))
    oracle_terms = set(as_string_list(match.get("oracles_any")))
    if oracle_terms and form_oracles.intersection(oracle_terms):
        return True

    terms = [term.lower() for term in as_string_list(match.get("builder_terms"))]
    if not terms:
        return not geometry_families and not oracle_terms
    haystack = " ".join(
        str(value)
        for value in (
            geometry.get("target_builder"),
            geometry.get("tool_builder"),
            form.get("test_goal"),
            form.get("risk_summary"),
        )
        if value
    ).lower()
    return any(term in haystack for term in terms)


def select_example_pack(form: dict[str, Any]) -> dict[str, Any] | None:
    packs = CAPABILITIES.get("example_packs") if isinstance(CAPABILITIES.get("example_packs"), dict) else {}
    for pack_id, raw_pack in packs.items():
        if not isinstance(pack_id, str) or not isinstance(raw_pack, dict):
            continue
        if not matches_example_pack(form, raw_pack):
            continue
        md_path = repo_path(raw_pack.get("path"))
        manifest_path = repo_path(raw_pack.get("manifest_path"))
        manifest: dict[str, Any] = {}
        if manifest_path and manifest_path.is_file():
            loaded_manifest = read_json(manifest_path)
            if isinstance(loaded_manifest, dict):
                manifest = loaded_manifest
        positive_paths = as_string_list(manifest.get("positive_example_paths")) or as_string_list(raw_pack.get("example_paths"))
        example_paths = [repo_path(item) for item in positive_paths]
        for single_key in ("example_dsl_path", "example_recipe_path", "example_json_path"):
            single_path = repo_path(raw_pack.get(single_key))
            if single_path is not None:
                example_paths.append(single_path)
        deduped_example_paths: list[Path] = []
        seen_example_paths: set[str] = set()
        for path in example_paths:
            if path is None:
                continue
            key = str(path.resolve() if path.exists() else path)
            if key in seen_example_paths:
                continue
            seen_example_paths.add(key)
            deduped_example_paths.append(path)
        example_paths = deduped_example_paths
        negative_example_paths = [
            path
            for path in (repo_path(item) for item in as_string_list(manifest.get("negative_example_paths") or raw_pack.get("negative_example_paths")))
            if path is not None
        ]
        markdown = read_text_if_present(md_path) if md_path else ""
        example_parts = []
        for example_path in example_paths:
            example_text = read_text_if_present(example_path)
            if not example_text:
                continue
            example_parts.append(f"Example `{example_path.name}`:\n```json\n{example_text.strip()}\n```")
        excerpt_parts = [part for part in (markdown.strip(), "\n\n".join(example_parts)) if part]
        return {
            "id": pack_id,
            "title": raw_pack.get("title", pack_id),
            "path": str(md_path) if md_path else "",
            "manifest_path": str(manifest_path) if manifest_path else "",
            "contract_kinds": as_string_list(manifest.get("contract_kinds")),
            "example_paths": [str(path) for path in example_paths],
            "negative_example_paths": [str(path) for path in negative_example_paths],
            "fallback": str(manifest.get("fallback") or ""),
            "excerpt": "\n\n".join(excerpt_parts),
        }
    return None


SOURCE_FILE_SUFFIXES = {".step", ".stp", ".iges", ".igs", ".sgt"}
API_SOURCE_SUFFIXES = {
    "step_import": {".step", ".stp"},
    "iges_import": {".iges", ".igs"},
    "step_roundtrip": {".sgt"},
    "iges_roundtrip": {".sgt"},
    "check_sgt": {".sgt"},
}
WINDOWS_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
PLACEHOLDER_RE = re.compile(r"<[^<>]+>")


def walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(walk_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(walk_strings(item))
        return result
    return []


def dataset_source_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if Path(value).suffix.lower() in SOURCE_FILE_SUFFIXES else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(dataset_source_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            if isinstance(item, str) and key in {"path", "source_file", "file"}:
                result.append(item)
            elif isinstance(item, (dict, list)):
                result.extend(dataset_source_strings(item))
        return result
    return []


def source_suffixes_for_form(form: dict[str, Any]) -> set[str]:
    target_api = str(form.get("target_api") or "")
    if target_api in API_SOURCE_SUFFIXES:
        return set(API_SOURCE_SUFFIXES[target_api])
    geometry = form.get("geometry")
    input_assets = form.get("input_assets")
    text = json.dumps({"geometry": geometry, "input_assets": input_assets}, ensure_ascii=False).lower()
    if target_api == "api_boolean" and ("loaded_sgt" in text or ".sgt" in text or "result_1.sgt" in text):
        return {".sgt"}
    return set(SOURCE_FILE_SUFFIXES)


def is_corpus_metadata_index(form: dict[str, Any], label: str, raw_path: str) -> bool:
    if str(form.get("target_api") or "") != "api_boolean":
        return False
    if str(form.get("run_profile") or "").lower() != "corpus":
        return False
    if "dataset_index" not in label:
        return False
    return Path(raw_path).name.lower() == "corpus_summary.json"


def source_file_matches(path_text: str, allowed_suffixes: set[str]) -> bool:
    suffix = Path(path_text).suffix.lower()
    return suffix in SOURCE_FILE_SUFFIXES and (not allowed_suffixes or suffix in allowed_suffixes)


def is_absolute_path_text(path_text: str) -> bool:
    return bool(WINDOWS_ABSOLUTE_RE.match(path_text)) or Path(path_text).is_absolute()


def prompt_safe_source_file_display(path_text: str) -> str:
    path = Path(path_text)
    candidate = path if is_absolute_path_text(path_text) else REPO_ROOT / path_text
    try:
        resolved = candidate.resolve()
        resolved.relative_to(REPO_ROOT.resolve())
    except (OSError, ValueError):
        return ""
    if not resolved.is_file():
        return ""
    return rel_display(resolved)


def placeholder_root(raw_path: str) -> Path:
    first = PLACEHOLDER_RE.split(raw_path, maxsplit=1)[0].rstrip("/\\")
    if not first:
        return REPO_ROOT
    path = Path(first)
    return path if path.is_absolute() else REPO_ROOT / path


def source_file_candidates(root: Path, allowed_suffixes: set[str], limit: int = 20) -> tuple[list[str], bool]:
    files: list[str] = []
    limit_reached = False
    if not root.exists():
        return files, limit_reached
    if root.is_file():
        display = prompt_safe_source_file_display(str(root))
        if display and source_file_matches(display, allowed_suffixes):
            return [display], False
        return files, False
    for child in sorted(root.rglob("*")):
        if not child.is_file() or not source_file_matches(str(child), allowed_suffixes):
            continue
        display = prompt_safe_source_file_display(str(child))
        if not display or display in files:
            continue
        files.append(display)
        if len(files) >= limit:
            limit_reached = True
            break
    return files, limit_reached


def dataset_index_source_files(
    path: Path,
    allowed_suffixes: set[str],
    limit: int = 20,
    expose_files: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    try:
        loaded = read_json(path)
    except ValueError:
        return [], {"parse_error": "invalid_json", "source_file_count": 0}
    files: list[str] = []
    by_suffix: dict[str, int] = {}
    seen_source_files: set[str] = set()
    for text in dataset_source_strings(loaded):
        suffix = Path(text).suffix.lower()
        if suffix not in SOURCE_FILE_SUFFIXES:
            continue
        if text in seen_source_files:
            continue
        seen_source_files.add(text)
        by_suffix[suffix] = by_suffix.get(suffix, 0) + 1
        display = prompt_safe_source_file_display(text)
        if expose_files and display and source_file_matches(display, allowed_suffixes) and display not in files:
            files.append(display)
            if len(files) >= limit:
                break
    return files, {"source_file_suffix_counts": dict(sorted(by_suffix.items())), "source_file_count": sum(by_suffix.values())}


def asset_path_record(
    label: str,
    raw_path: str,
    allowed_suffixes: set[str],
    *,
    metadata_only_index: bool = False,
) -> dict[str, Any]:
    path = repo_path(raw_path)
    exists = bool(path and path.exists())
    record: dict[str, Any] = {
        "label": label,
        "path": raw_path,
        "exists": exists,
        "kind": "missing",
        "metadata_only_index": metadata_only_index,
        "available_source_files": [],
    }
    if not path:
        return record
    if PLACEHOLDER_RE.search(raw_path):
        root = placeholder_root(raw_path)
        files, limit_reached = source_file_candidates(root, allowed_suffixes)
        record.update(
            {
                "exists": bool(files),
                "kind": "placeholder_pattern",
                "pattern_root": rel_display(root),
                "pattern_root_exists": root.exists(),
                "candidate_limit_reached": limit_reached,
                "available_source_files": files,
            }
        )
        return record
    if path.is_file():
        record["kind"] = "file"
        if source_file_matches(str(path), allowed_suffixes):
            record["available_source_files"] = [rel_display(path)]
        elif path.suffix.lower() == ".json":
            files, dataset_summary = dataset_index_source_files(
                path,
                allowed_suffixes,
                expose_files=not metadata_only_index,
            )
            record["kind"] = "dataset_index"
            record["dataset_index"] = dataset_summary
            record["available_source_files"] = files
        return record
    if path.is_dir():
        record["kind"] = "directory"
        files: list[str] = []
        for child in sorted(path.rglob("*")):
            if not child.is_file() or not source_file_matches(str(child), allowed_suffixes):
                continue
            files.append(rel_display(child))
            if len(files) >= 20:
                break
        record["available_source_files"] = files
    return record


def input_asset_availability(form: dict[str, Any]) -> dict[str, Any]:
    allowed_suffixes = source_suffixes_for_form(form)
    candidates: list[tuple[str, str]] = []
    geometry = form.get("geometry")
    if isinstance(geometry, dict) and isinstance(geometry.get("input_asset"), str):
        candidates.append(("geometry.input_asset", geometry["input_asset"]))
    input_assets = form.get("input_assets")
    if isinstance(input_assets, dict):
        for key, value in input_assets.items():
            if isinstance(key, str) and isinstance(value, str) and value.strip():
                candidates.append((f"input_assets.{key}", value))

    merged: dict[str, list[str]] = {}
    order: list[str] = []
    for label, raw_path in candidates:
        if raw_path not in merged:
            merged[raw_path] = []
            order.append(raw_path)
        merged[raw_path].append(label)

    records: list[dict[str, Any]] = []
    for raw_path in order:
        label = ", ".join(merged[raw_path])
        records.append(
            asset_path_record(
                label,
                raw_path,
                allowed_suffixes,
                metadata_only_index=is_corpus_metadata_index(form, label, raw_path),
            )
        )

    available_source_files: list[str] = []
    for record in records:
        for source_file in record.get("available_source_files", []):
            if isinstance(source_file, str) and source_file not in available_source_files:
                available_source_files.append(source_file)

    return {
        "records": records,
        "expected_source_suffixes": sorted(allowed_suffixes),
        "available_source_files": available_source_files,
        "has_available_source_files": bool(available_source_files),
        "source_file_policy": {
            "use_only_listed_source_files": True,
            "placeholder_paths_are_not_valid_source_files": True,
            "copy_source_file_verbatim": True,
            "required_suffixes": sorted(allowed_suffixes),
        },
    }


def validate_form(form: Any) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(form, dict):
        return ["form root must be an object"], warnings
    for item in sorted(FORM_VALIDATOR.iter_errors(form), key=lambda error: list(error.absolute_path)):
        field = ".".join(str(part) for part in item.absolute_path) or "$"
        errors.append(f"{field}: {item.message}")

    target_api = form.get("target_api")
    if target_api not in SUPPORTED_APIS:
        warnings.append(
            f"target_api {target_api!r} is not currently runnable; model output should be needs_harness_extension"
        )

    oracles = form.get("oracles")
    if isinstance(oracles, list):
        for oracle in oracles:
            if oracle not in SUPPORTED_ORACLES:
                warnings.append(f"unknown oracle {oracle!r}; model should map it to a supported oracle or request extension")

    run_profile = form.get("run_profile")
    if run_profile not in RUN_PROFILES:
        warnings.append(f"run_profile {run_profile!r} is not listed in interface_capabilities.json")

    campaign_profile = form.get("campaign_profile")
    if campaign_profile is not None:
        if not isinstance(campaign_profile, str) or campaign_profile not in CAMPAIGN_PROFILES:
            errors.append(f"campaign_profile must be one of {sorted(CAMPAIGN_PROFILES)}")
        elif CAMPAIGN_PROFILES[campaign_profile].run_profile_id != run_profile:
            errors.append(
                f"campaign_profile {campaign_profile!r} requires run_profile "
                f"{CAMPAIGN_PROFILES[campaign_profile].run_profile_id!r}"
            )

    return errors, warnings


def render_prompt(
    form: dict[str, Any],
    guidance: dict[str, Any],
    oracle_notes: list[str],
    example_pack: dict[str, Any] | None,
    asset_availability: dict[str, Any],
    campaign_profiles: dict[str, Any],
) -> str:
    form_json = json.dumps(form, indent=2, ensure_ascii=False)
    body_builders = ", ".join(SUPPORTED_BODY_BUILDERS)
    oracles = ", ".join(SUPPORTED_ORACLES)
    guidance_json = json.dumps(guidance, indent=2, ensure_ascii=False)
    oracle_json = json.dumps(oracle_notes, indent=2, ensure_ascii=False)
    example_section = ""
    if example_pack and example_pack.get("excerpt"):
        example_section = f"""
Selected interface example pack: {example_pack.get("id")}

{example_pack.get("excerpt")}
"""
    asset_json = json.dumps(asset_availability, indent=2, ensure_ascii=False)
    campaign_profiles_json = json.dumps(campaign_profiles, indent=2, ensure_ascii=False)
    fixed_gate_contract = fixed_gate_contract_for_api(str(form.get("target_api") or ""))
    return f"""You are generating SGGK test-harness input, not direct SDK code.

Return exactly one JSON object. Use this shape for runnable DSL tests:
{{
  "kind": "attack_dsl",
  "dsl": {{ "...": "valid SGGK attack DSL" }},
  "notes": ["short review notes"]
}}

Use this shape for supported flat-recipe APIs:
{{
  "kind": "flat_recipe",
  "recipe": {{ "...": "valid flat sggk_case_runner recipe" }},
  "notes": ["short review notes"]
}}

Use this shape for fixed large campaigns that should not enumerate every case:
{{
  "kind": "campaign_request",
  "profile_id": "select one key from Allowed campaign profiles",
  "args": {{"bounded_argument": "value accepted by that profile's args_schema"}},
  "notes": ["why this fixed campaign profile matches the form"],
  "expected_artifacts": ["summary/report paths"]
}}

If the requested API or body builder is unsupported, return:
{{
  "kind": "needs_harness_extension",
  "api": "requested_api_name",
  "why_needed": "why current harness cannot express this test",
  "extension_summary": "smallest runner/schema addition",
  "proposed_recipe_fields": {{ "field_name": "type and meaning" }},
  "proposed_artifacts": ["reports or debug outputs the extension should produce"],
  "validation_oracle": {{ "oracle_family": "how the smoke proves correctness" }},
  "minimum_smoke_case": {{ "case_id": "requested_api_smoke_001", "api": "requested_api_name" }},
  "patch_plan": [
    {{"layer": "schema", "change": "add recipe/form fields", "files": ["test_harness/..."]}},
    {{"layer": "validator", "change": "reject missing or invalid fields", "files": ["test_harness/tools/..."]}},
    {{"layer": "normalizer", "change": "normalize safe aliases only", "files": ["test_harness/tools/..."]}},
    {{"layer": "runner", "change": "route recipe to fixed runner support", "files": ["test_harness/..."]}},
    {{"layer": "tests", "change": "add positive and negative smoke coverage", "files": ["test_harness/..."]}}
  ]
}}

Hard rules:
- Prefer attack DSL for api_boolean.
- For 100k+ corpus campaigns, do not emit individual DSL cases; emit campaign_request only when Allowed campaign profiles lists a profile.
- For mass coverage inside attack_dsl, do not enumerate cases: declare `cluster_bases` (named base geometries) plus `parameter_clusters` (typed clusters over one varying parameter each). Fixed code expands each cluster deterministically into at most 50 cases, so combine many bases, cluster types, and grids to reach 100k+ runnable recipes.
- Parameter cluster types: translate_axis, translate_line, scale_uniform, size_dimension, contact_band, tolerance_sweep, angle_sweep, large_coordinate_shift, boolean_type_cycle, option_toggle, mirror_sign, seeded_jitter, uv_domain, enum_cycle. Every cluster vary path must resolve in its base; grids use {{"kind":"linspace"|"geomspace"|"values", ...}} with count <= 50.
- Complexity is gated: the fixed complexity gate scores every case and rejects simple-only candidates. Cover at least 4 of these dimensions across the candidate: multi-op chains, generated topology builders, tolerance bands around exact contact/geom_tol/topo_tol, large coordinates within max_model_size, degenerate or empty-result inputs, non-trivial transforms, and at least two measurable oracle families per case. At least half of the cases must each combine 3+ dimensions, and at least one case must use a multi-op chain or generated topology builder. Simple primitive pairs are allowed only as a minority of smoke anchors.
- Do not invent SDK calls outside the runner schema.
- Never return command, commands, tool, executable, runner, dataset, out, cwd, env, or shell fields. For campaign_request, select only profile_id plus bounded args.
- For flat_recipe source_file fields, use only a concrete path from Input asset availability when available. Do not copy example source_file paths unless they appear there as available.
- If no concrete source_file is available for a source-file recipe, return needs_harness_extension or an allowed campaign_request instead of inventing a path.
- Use constants topo_tol=0.01, geom_tol=0.00001, max_model_size=500000.0 unless the form overrides them.
- Use stable id values on all important chain steps.
- Add real oracles, not only API status checks.
- Use only supported expectation fields. Do not emit an `expectations.properties` array; property checks use direct fields such as `require_finite_properties`, `require_nonnegative_length_area`, `total_volume`, or `total_abs_volume`.
- `expectations.result_bodies` must be an object such as `{{"min": 1}}` or `{{"min": 1, "max": 1}}`; do not emit a scalar.
- Boolean expectation fields such as `boolean_volume_relation` and `boolean_bbox_relation` must be literal true/false values, never objects, formulas, or relation strings.
- For chain bodies, put profile builders before generated-body ops: `rect_profile -> extrude`, `circle_profile -> extrude` for a cylinder, `rect_profile -> thicken` or `thicken_rect_sheet`, `circle_profile -> sweep_line`, and `line_profile/radial_rect_profile -> revolve`.
- For simple primitive tools such as cylinders and spheres, prefer direct body builders (`solid_cylinder`, `solid_sphere`) unless the generated-chain behavior is the point of the test. In a chain, a direct body builder can be used as `{{"op":"solid_cylinder", ...}}`.
- When using `point_ref`, declare the point under root `key_points` or case `key_points`; otherwise use an explicit `point` array.
- Distance oracles must use `distance_checks` as a list with roles and expected/min/max fields; do not use `expectations.distance`.
- `support_sweep` / `support_sweep_bspline_surface` requires concrete `path_radius`, `profile_radius`, and `height` numeric fields.
- `line_profile -> revolve` requires `bottom_radius`, `top_radius`, and `height`; do not use a free-form `points` array for the revolved profile.
- Nested boolean chain steps use `op:"boolean"` with a supported pattern; do not invent `boolean_subtract`, `boolean_union`, or `boolean_intersect` ops. Either include a `tool` object in the boolean step, or put base body then tool body/transform immediately before `op:"boolean"`.
- Metric expectations such as `total_volume` should be objects like `{{"min":0.0}}` or `{{"expected":0.0}}`; scalar shorthand is accepted, but object form is clearer.
- For multi-value tolerance boundaries, prefer `sweeps` or `paired_sweeps`; do not put vector-valued fields such as `translate`, `axis`, or `point` into scalar sweep shorthand.
- Use sweeps or paired_sweeps for tolerance boundaries.
- Emit valid JSON only.

Supported body builders: {body_builders}
Supported oracle families: {oracles}

{fixed_gate_contract}

API guidance:
{guidance_json}

Oracle guidance selected for this form:
{oracle_json}

Input asset availability in the current workspace:
{asset_json}

Allowed campaign profiles for this task:
{campaign_profiles_json}

{example_section}

Developer form:
{form_json}
"""


def render_interface_design_prompt(form: dict[str, Any]) -> str:
    """Prompt for the interface-design subagent (unknown API route).

    The subagent designs complete harness support for one unsupported public
    interface from SDK header evidence.  Its output is a structured
    needs_harness_extension design that fixed gates validate and the human
    reviews; the subagent never writes runner code, commands, or paths.
    """

    form_json = json.dumps(form, indent=2, ensure_ascii=False)
    declarations = form.get("sdk_source_refs")
    declarations_json = json.dumps(declarations if isinstance(declarations, list) else [], indent=2, ensure_ascii=False)
    archetypes_json = json.dumps(sorted(ALLOWED_ARCHETYPES), indent=2, ensure_ascii=False)
    cluster_types_json = json.dumps(INTERFACE_DESIGN_CLUSTER_TYPES, indent=2, ensure_ascii=False)
    dimensions_json = json.dumps(INTERFACE_DESIGN_COMPLEXITY_DIMENSIONS, indent=2, ensure_ascii=False)
    return f"""You are the SGGK interface-design subagent. One unsupported public interface was requested.
Design complete harness test support for it. You produce a structured design, not code.

Return exactly one JSON object with this shape:
{{
  "kind": "needs_harness_extension",
  "api": "requested_api_name",
  "why_needed": "why the current harness cannot express this test",
  "extension_summary": "smallest runner/schema addition",
  "interface_signature": {{
    "parameters": [{{"name": "...", "type": "...", "role": "input|output|option"}}],
    "return_type": "...",
    "return_channels": ["curves|points|bodies|status|topology"]
  }},
  "builder_requirements": [
    {{"builder_id": "...", "geometry_kind": "plane|cylinder_surface|bspline_curve|...", "parameters": {{"name": "type"}}, "rationale": "..."}}
  ],
  "archetype_match": {{"archetype": "one registered archetype id", "fit": "exact|partial|none", "gaps": ["..."]}},
  "proposed_recipe_fields": {{"field_name": "type and meaning"}},
  "proposed_artifacts": ["reports or debug outputs the extension should produce"],
  "validation_oracle": {{"oracle_family": "how the smoke proves correctness beyond API status"}},
  "parameter_cluster_plan": [
    {{"cluster_type": "one registered cluster type", "target_parameter": "recipe field or chain path", "rationale": "...", "estimated_cases": 50}}
  ],
  "complexity_plan": {{
    "dimensions": ["dimensions from the fixed list that apply to this interface"],
    "degenerate_inputs": ["..."],
    "tolerance_boundaries": ["..."]
  }},
  "minimum_smoke_case": {{"case_id": "requested_api_smoke_001", "api": "requested_api_name", "...": "concrete smoke values from proposed_recipe_fields"}},
  "patch_plan": [
    {{"layer": "schema", "change": "add recipe/form fields", "files": ["test_harness/..."]}},
    {{"layer": "validator", "change": "reject missing or invalid fields", "files": ["test_harness/tools/..."]}},
    {{"layer": "normalizer", "change": "normalize safe aliases only", "files": ["test_harness/tools/..."]}},
    {{"layer": "runner", "change": "route recipe to fixed runner support", "files": ["test_harness/..."]}},
    {{"layer": "tests", "change": "add positive and negative smoke coverage", "files": ["test_harness/..."]}}
  ]
}}

Hard rules:
- Read the SDK header declarations below carefully: parameters, options structs, return types, and overloads drive interface_signature and builder_requirements.
- archetype_match.archetype must be one of the registered archetypes below; use fit=none only when no archetype family matches and explain the gap.
- parameter_cluster_plan must use only the registered cluster types below; each cluster expands to at most 50 cases, so plan several cluster types per interface.
- complexity_plan.dimensions must use only the fixed dimension list below.
- validation_oracle must name measurable oracles (counts, properties, topology relations), never API status alone.
- minimum_smoke_case must instantiate proposed_recipe_fields with small literal values.
- Never return command, commands, tool, executable, runner, dataset, out, cwd, env, or shell fields.
- Do not write C++, CMake, or patch content; patch_plan describes intent for the human-reviewed backlog only.
- Emit valid JSON only.

Registered adapter archetypes:
{archetypes_json}

Registered parameter cluster types:
{cluster_types_json}

Fixed complexity dimensions:
{dimensions_json}

SDK header declarations for the requested interface:
{declarations_json}

Developer form:
{form_json}
"""


def build_task(form_path: Path, form: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    target_api = str(form.get("target_api", "needs_harness_extension"))
    request_id = str(form.get("request_id", form_path.stem))
    guidance = API_GUIDANCE.get(target_api, API_GUIDANCE["needs_harness_extension"])
    selected_oracles = as_string_list(form.get("oracles"))
    oracle_notes = [ORACLE_GUIDANCE.get(oracle, f"Map {oracle} to a supported oracle or request extension.") for oracle in selected_oracles]
    example_pack = select_example_pack(form)
    asset_availability = input_asset_availability(form)
    selected_example_pack = example_pack["id"] if example_pack else ""
    interface_family = derive_interface_family(form, selected_example_pack, CAPABILITIES)
    run_profile_id = str(form.get("run_profile", ""))
    run_profile_info = run_profile_metadata(run_profile_id, CAPABILITIES)
    preferred_format = guidance["preferred_format"]
    campaign_profile_id = str(form.get("campaign_profile") or "")
    if request_id == "iface_15_boolean_abc_mass_recut" and not campaign_profile_id:
        campaign_profile_id = "abc_boolean_mass_recut"
    campaign_profiles = allowed_campaign_profiles([campaign_profile_id]) if campaign_profile_id else {}
    campaign_bindings: dict[str, dict[str, str]] = {}
    if campaign_profile_id == "abc_boolean_mass_recut":
        input_assets = form.get("input_assets") if isinstance(form.get("input_assets"), dict) else {}
        campaign_bindings[campaign_profile_id] = {
            "runner": "build/test_harness/Release/sggk_case_runner.exe",
            "dataset": str(input_assets.get("dataset_root") or "artifacts/datasets/abc/imported_sgt"),
            "out": "artifacts/abc_boolean_mass_recut",
        }
        guidance = dict(guidance)
        guidance["preferred_format"] = "campaign_request"
        guidance["notes"] = list(guidance.get("notes", [])) + [
            "Use campaign_request profile_id=abc_boolean_mass_recut; fixed code expands the bounded corpus lane and filters explicit unsupported failures from bug reports.",
        ]
        preferred_format = "campaign_request"
    elif campaign_profile_id == "abc_step_import":
        campaign_bindings[campaign_profile_id] = {
            "runner": "build/test_harness/Release/sggk_case_runner.exe",
            "dataset": "artifacts/abc_dataset_full/dataset_index.json",
            "out": "artifacts/abc_step_import",
        }
        guidance = dict(guidance)
        guidance["preferred_format"] = "campaign_request"
        guidance["notes"] = list(guidance.get("notes", [])) + [
            "Use campaign_request profile_id=abc_step_import; fixed code binds the host-selected ABC index and runs deterministic sharded STEP import with triage.",
        ]
        preferred_format = "campaign_request"

    task = {
        "task_version": 1,
        "created_at": now_iso_like(),
        "form_path": str(form_path),
        "request_id": request_id,
        "warnings": warnings,
        "model_role": "Generate JSON-only SGGK harness input or a needs_harness_extension object.",
        "developer_form": form,
        "harness_contract": {
            "supported_apis": SUPPORTED_APIS,
            "supported_body_builders": SUPPORTED_BODY_BUILDERS,
            "supported_oracles": SUPPORTED_ORACLES,
            "preferred_output_for_api": preferred_format,
            "constants": {
                "topo_tol": 0.01,
                "geom_tol": 0.00001,
                "max_model_size": 500000.0,
            },
            "output_must_be_json_only": True,
            "selected_example_pack": selected_example_pack,
            "interface_family": interface_family,
            "run_profile": run_profile_info,
            "input_asset_availability": asset_availability,
            "allowed_campaign_profiles": campaign_profiles,
        },
        "interface_family": interface_family,
        "run_profile_id": run_profile_id,
        "example_pack": {
            key: value for key, value in (example_pack or {}).items() if key != "excerpt"
        },
        "api_guidance": guidance,
        "oracle_guidance": oracle_notes,
        "input_asset_availability": asset_availability,
        "allowed_campaign_profiles": campaign_profiles,
        "campaign_bindings": campaign_bindings,
    }
    task["prompt"] = render_prompt(form, guidance, oracle_notes, example_pack, asset_availability, campaign_profiles)
    return task


def render_markdown(task: dict[str, Any]) -> str:
    return f"""# SGGK API Test Task: {task["request_id"]}

## Prompt

```text
{task["prompt"]}
```

## Allowed Campaign Profiles

```json
{json.dumps(task.get("allowed_campaign_profiles", {}), indent=2, ensure_ascii=False)}
```
"""


def main() -> int:
    args = parse_args()
    form_path = Path(args.form)
    try:
        loaded = read_json(form_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    errors, warnings = validate_form(loaded)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if args.strict and warnings:
        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        return 2
    if warnings:
        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)

    task = build_task(form_path.resolve(), loaded, warnings)
    if args.format == "json":
        output = json.dumps(task, indent=2, ensure_ascii=False) + "\n"
    else:
        output = render_markdown(task)

    if args.out:
        write_text(Path(args.out), output)
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
