#!/usr/bin/env python3
"""Validate model-authored harness extension requests.

This is a review gate, not a patch generator. It checks that
needs_harness_extension outputs are structured enough for a human or a later
patch agent to review without reading a long model explanation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time
from typing import Any

from harness_capabilities import load_capabilities, supported_apis


CAPABILITIES = load_capabilities()
SUPPORTED_APIS = set(supported_apis(CAPABILITIES))
REQUIRED_PATCH_LAYERS = {
    "schema",
    "validator",
    "normalizer",
    "runner",
    "tests",
}
KNOWN_PATCH_LAYERS = REQUIRED_PATCH_LAYERS | {
    "compiler",
    "runner_cpp",
    "docs",
    "prompt_pack",
    "triage",
    "regression_asset",
}
REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_LAYER_FILE_HINTS = {
    "schema": (
        "test_harness/forms/api_test_form.schema.json",
        "test_harness/interface_capabilities.json",
        "test_harness/interface_example_packs/",
    ),
    "validator": ("test_harness/tools/validate_recipe.py",),
    "normalizer": ("test_harness/tools/normalize_model_output.py",),
    "runner": ("test_harness/src/sggk_case_runner.cpp",),
    "tests": (
        "test_harness/interface_example_packs/",
        "test_harness/suites/",
        "test_harness/examples/",
        "test_harness/fixtures/",
    ),
}
STRICT_LAYER_FILE_HINTS = {"schema", "validator", "normalizer", "runner"}
FIELD_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
SMOKE_META_KEYS = {"case_id", "api", "notes", "comment", "description"}
REPORTABLE_ARTIFACT_TERMS = {
    ".json",
    ".md",
    ".png",
    "report",
    "summary",
    "validation",
    "triage",
    "diagnostic",
    "preview",
    "log",
}
ORACLE_CONCRETE_TERMS = {
    "area",
    "bbox",
    "body",
    "bodies",
    "distance",
    "drift",
    "finite",
    "length",
    "manifold",
    "properties",
    "property",
    "relation",
    "report",
    "roundtrip",
    "topocheck",
    "topology",
    "triage",
    "validation",
    "volume",
}
ORACLE_STATUS_ONLY_TERMS = {
    "api success",
    "return code",
    "status code",
    "success status",
    "passes if the api",
    "no oracle",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Extension request JSON file(s) or directories")
    parser.add_argument("--report", default="", help="Optional validation report JSON path")
    parser.add_argument("--model-diagnostics", default="", help="Optional model-friendly diagnostics JSON path")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def iter_json_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.json")))
        else:
            files.append(path)
    return files


def diagnostic(
    severity: str,
    code: str,
    path: str,
    message: str,
    repair_hint: str,
    expected_shape: Any | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": severity,
        "error_code": code,
        "path": path,
        "message": message,
        "repair_hint": repair_hint,
    }
    if expected_shape is not None:
        item["expected_shape"] = expected_shape
    return item


def type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def normalize_extension_request(value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return canonical request fields while preserving model-provided detail."""

    result = dict(value)
    notes: list[dict[str, Any]] = []
    alias_map = {
        "why": "why_needed",
        "reason": "why_needed",
        "minimal_extension": "extension_summary",
        "proposed_recipe": "minimum_smoke_case",
        "recipe_fields": "proposed_recipe_fields",
        "required_artifacts": "proposed_artifacts",
        "oracle": "validation_oracle",
    }
    for old_key, new_key in alias_map.items():
        if old_key not in result or new_key in result:
            continue
        result[new_key] = result[old_key]
        notes.append(
            diagnostic(
                "info",
                "NORMALIZED_EXTENSION_ALIAS",
                f"$.{old_key}",
                f"Converted {old_key} to {new_key}.",
                f"Prefer `{new_key}` in needs_harness_extension outputs.",
            )
        )
    if "kind" not in result:
        result["kind"] = "needs_harness_extension"
    return result, notes


def validate_string_field(value: dict[str, Any], key: str, diagnostics: list[dict[str, Any]], *, path: str) -> None:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        diagnostics.append(
            diagnostic(
                "error",
                "MISSING_EXTENSION_FIELD",
                f"{path}.{key}",
                f"{key} must be a non-empty string.",
                f"Add `{key}` so reviewers can understand the requested harness extension.",
            )
        )


def validate_object_field(value: dict[str, Any], key: str, diagnostics: list[dict[str, Any]], *, path: str, allow_empty: bool = False) -> None:
    item = value.get(key)
    if not isinstance(item, dict) or (not item and not allow_empty):
        diagnostics.append(
            diagnostic(
                "error",
                "MISSING_EXTENSION_OBJECT",
                f"{path}.{key}",
                f"{key} must be a {'possibly empty ' if allow_empty else ''}JSON object.",
                f"Add `{key}` as structured JSON, not prose.",
            )
        )


def validate_proposed_recipe_fields(value: dict[str, Any], diagnostics: list[dict[str, Any]], *, path: str) -> set[str]:
    fields = value.get("proposed_recipe_fields")
    if not isinstance(fields, dict) or not fields:
        validate_object_field(value, "proposed_recipe_fields", diagnostics, path=path)
        return set()
    field_names: set[str] = set()
    for key, description in fields.items():
        if not isinstance(key, str) or not FIELD_NAME_RE.match(key):
            diagnostics.append(
                diagnostic(
                    "error",
                    "INVALID_PROPOSED_RECIPE_FIELD_NAME",
                    f"{path}.proposed_recipe_fields",
                    f"proposed_recipe_fields contains an invalid field name: {key!r}.",
                    "Use stable JSON field names such as source_file, blend_radius, or expectations.",
                    {"field_name": "type and meaning"},
                )
            )
            continue
        field_names.add(key)
        if isinstance(description, str) and description.strip():
            continue
        if isinstance(description, dict) and description:
            continue
        diagnostics.append(
            diagnostic(
                "warning",
                "MISSING_PROPOSED_RECIPE_FIELD_DESCRIPTION",
                f"{path}.proposed_recipe_fields.{key}",
                f"proposed_recipe_fields.{key} has no useful type/meaning description.",
                "Describe the type, constraints, and runner meaning so a patch proposal can implement the field without guessing.",
            )
        )
    return field_names


def validate_string_list(value: Any, label: str, diagnostics: list[dict[str, Any]]) -> None:
    if not isinstance(value, list):
        diagnostics.append(
            diagnostic(
                "error",
                "INVALID_EXTENSION_FIELD_TYPE",
                label,
                f"{label} must be a list of strings.",
                "Use a JSON array of short strings.",
            )
        )
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            diagnostics.append(
                diagnostic(
                    "error",
                    "INVALID_EXTENSION_FIELD_TYPE",
                    f"{label}[{index}]",
                    f"{label}[{index}] must be a non-empty string.",
                    "Use a concise string item.",
                )
            )


def normalize_patch_file_hint(value: str) -> str:
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    path = Path(text)
    if path.is_absolute():
        try:
            text = path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
        except ValueError:
            text = path.as_posix()
    return text


def patch_file_matches_layer(value: str, hints: tuple[str, ...]) -> bool:
    text = normalize_patch_file_hint(value)
    for hint in hints:
        if hint.endswith("/") and text.startswith(hint):
            return True
        if text == hint:
            return True
    return False


def validate_patch_step_files(
    step: dict[str, Any],
    *,
    layer: str,
    step_path: str,
    diagnostics: list[dict[str, Any]],
) -> None:
    files = step.get("files")
    if not isinstance(files, list):
        return
    text_files = [item for item in files if isinstance(item, str) and item.strip()]
    if not text_files:
        return
    placeholder_files = [item for item in text_files if "..." in item or "<" in item or ">" in item]
    if placeholder_files:
        diagnostics.append(
            diagnostic(
                "warning",
                "PATCH_PLAN_FILE_PLACEHOLDER",
                f"{step_path}.files",
                "patch_plan files include placeholders instead of concrete harness paths.",
                "Name concrete files in this repository so the patch handoff can be reviewed without guessing.",
            )
        )
    hints = EXPECTED_LAYER_FILE_HINTS.get(layer)
    if not hints:
        return
    if any(patch_file_matches_layer(item, hints) for item in text_files):
        return
    severity = "error" if layer in STRICT_LAYER_FILE_HINTS else "warning"
    diagnostics.append(
        diagnostic(
            severity,
            "PATCH_PLAN_FILES_NOT_HARNESS_TARGETS",
            f"{step_path}.files",
            f"patch_plan files for layer {layer!r} do not point at the expected harness targets.",
            f"Use one or more paths like: {', '.join(hints)}.",
        )
    )


def validate_smoke_case(
    value: dict[str, Any],
    diagnostics: list[dict[str, Any]],
    *,
    path: str,
    proposed_field_names: set[str],
) -> None:
    smoke = value.get("minimum_smoke_case")
    if not isinstance(smoke, dict) or not smoke:
        validate_object_field(value, "minimum_smoke_case", diagnostics, path=path)
        return
    case_id = smoke.get("case_id")
    if not isinstance(case_id, str) or not re.match(r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$", case_id):
        diagnostics.append(
            diagnostic(
                "error",
                "INVALID_EXTENSION_SMOKE_CASE",
                f"{path}.minimum_smoke_case.case_id",
                "minimum_smoke_case.case_id must be a stable non-empty id.",
                "Add a small smoke recipe or DSL case id such as api_offset_body_smoke_001.",
                {"case_id": "api_new_feature_smoke_001"},
            )
        )
    api = smoke.get("api")
    if api is not None and api != value.get("api"):
        diagnostics.append(
            diagnostic(
                "warning",
                "EXTENSION_SMOKE_API_MISMATCH",
                f"{path}.minimum_smoke_case.api",
                "minimum_smoke_case.api differs from the extension api.",
                "Use the same requested API unless the smoke case intentionally targets a helper API and notes why.",
            )
        )
    concrete_keys = [key for key in smoke if key not in {"case_id", "api", "notes", "comment"}]
    if not concrete_keys:
        diagnostics.append(
            diagnostic(
                "error",
                "EXTENSION_SMOKE_CASE_NOT_CONCRETE",
                f"{path}.minimum_smoke_case",
                "minimum_smoke_case must include at least one concrete API-specific input or parameter beyond case_id/api.",
                "Add fields from proposed_recipe_fields with small literal smoke values so reviewers can see the first runnable case shape.",
                {"case_id": "api_new_feature_smoke_001", "api": "api_new_feature", "parameter": 1.0},
            )
        )
        return
    if proposed_field_names:
        unproposed_keys = sorted(key for key in concrete_keys if key not in proposed_field_names and key not in SMOKE_META_KEYS)
        if unproposed_keys:
            diagnostics.append(
                diagnostic(
                    "error",
                    "EXTENSION_SMOKE_FIELD_NOT_PROPOSED",
                    f"{path}.minimum_smoke_case",
                    f"minimum_smoke_case uses fields not declared in proposed_recipe_fields: {unproposed_keys}.",
                    "Either add these fields to proposed_recipe_fields with type/meaning, or change the smoke case to use declared recipe fields.",
                    {"proposed_recipe_fields": {unproposed_keys[0]: "type and meaning"}},
                )
            )
        used_proposed_keys = sorted(key for key in concrete_keys if key in proposed_field_names)
        if not used_proposed_keys:
            diagnostics.append(
                diagnostic(
                    "error",
                    "EXTENSION_SMOKE_CASE_NOT_FROM_PROPOSED_FIELDS",
                    f"{path}.minimum_smoke_case",
                    "minimum_smoke_case does not instantiate any declared proposed_recipe_fields.",
                    "Use at least one field from proposed_recipe_fields with a small literal smoke value.",
                )
            )


def validate_proposed_artifacts(value: dict[str, Any], diagnostics: list[dict[str, Any]], *, path: str) -> None:
    artifacts = value.get("proposed_artifacts")
    if artifacts is None:
        diagnostics.append(
            diagnostic(
                "warning",
                "MISSING_PROPOSED_ARTIFACTS",
                f"{path}.proposed_artifacts",
                "proposed_artifacts is recommended for extension review.",
                "List expected reports, smoke outputs, or debug artifacts the extension should produce.",
            )
        )
        return
    validate_string_list(artifacts, f"{path}.proposed_artifacts", diagnostics)
    if not isinstance(artifacts, list):
        return
    text = " ".join(item.lower() for item in artifacts if isinstance(item, str))
    if text and not any(term in text for term in REPORTABLE_ARTIFACT_TERMS):
        diagnostics.append(
            diagnostic(
                "warning",
                "EXTENSION_ARTIFACTS_NOT_REPORTABLE",
                f"{path}.proposed_artifacts",
                "proposed_artifacts do not look like reviewable report, diagnostic, preview, or validation outputs.",
                "Name concrete artifacts such as recipe_summary.json, validation.json, triage_report.md, diagnostics.json, or preview/contact.png.",
            )
        )


def validate_validation_oracle(value: dict[str, Any], diagnostics: list[dict[str, Any]], *, path: str) -> None:
    if "validation_oracle" not in value:
        diagnostics.append(
            diagnostic(
                "warning",
                "MISSING_VALIDATION_ORACLE",
                f"{path}.validation_oracle",
                "validation_oracle is recommended so new runner support cannot pass on API status alone.",
                "Describe at least one property, topology, relation, or regression oracle.",
            )
        )
        return
    oracle = value["validation_oracle"]
    if not isinstance(oracle, (dict, list, str)):
        diagnostics.append(
            diagnostic(
                "error",
                "INVALID_VALIDATION_ORACLE",
                f"{path}.validation_oracle",
                f"validation_oracle must be object, list, or string; got {type_name(oracle)}.",
                "Use structured oracle JSON where possible.",
            )
        )
        return
    text = extension_request_text(oracle).lower()
    has_concrete_term = any(term in text for term in ORACLE_CONCRETE_TERMS)
    status_only = any(term in text for term in ORACLE_STATUS_ONLY_TERMS)
    if status_only and not has_concrete_term:
        diagnostics.append(
            diagnostic(
                "error",
                "EXTENSION_ORACLE_STATUS_ONLY",
                f"{path}.validation_oracle",
                "validation_oracle appears to rely only on API success, return code, or status.",
                "Add a concrete oracle such as properties, topocheck, distance/relation checks, roundtrip drift, validation report fields, or triage diagnostics.",
            )
        )
    elif not has_concrete_term:
        diagnostics.append(
            diagnostic(
                "warning",
                "EXTENSION_ORACLE_NOT_ACTIONABLE",
                f"{path}.validation_oracle",
                "validation_oracle does not name a concrete reportable check.",
                "Name the report field or oracle family that proves correctness beyond API status.",
            )
        )


def validate_patch_plan(value: dict[str, Any], diagnostics: list[dict[str, Any]], *, path: str) -> None:
    patch_plan = value.get("patch_plan")
    if patch_plan is None:
        diagnostics.append(
            diagnostic(
                "warning",
                "MISSING_PATCH_PLAN",
                f"{path}.patch_plan",
                "patch_plan is recommended for harness extension requests.",
                "Add patch_plan steps for schema, validator, normalizer, runner, and tests.",
            )
        )
        return
    if not isinstance(patch_plan, list) or not patch_plan:
        diagnostics.append(
            diagnostic(
                "error",
                "INVALID_PATCH_PLAN",
                f"{path}.patch_plan",
                "patch_plan must be a non-empty list.",
                "Add reviewable patch steps instead of a prose-only extension request.",
            )
        )
        return
    seen_layers: set[str] = set()
    for index, step in enumerate(patch_plan):
        step_path = f"{path}.patch_plan[{index}]"
        if not isinstance(step, dict):
            diagnostics.append(
                diagnostic(
                    "error",
                    "INVALID_PATCH_PLAN_STEP",
                    step_path,
                    "patch_plan items must be objects.",
                    "Use {\"layer\":\"schema\",\"change\":\"...\",\"files\":[]}.",
                )
            )
            continue
        layer = step.get("layer")
        if not isinstance(layer, str) or layer not in KNOWN_PATCH_LAYERS:
            diagnostics.append(
                diagnostic(
                    "warning",
                    "UNKNOWN_PATCH_LAYER",
                    f"{step_path}.layer",
                    f"Unknown or missing patch layer: {layer!r}.",
                    f"Use one of {sorted(KNOWN_PATCH_LAYERS)}.",
                )
            )
        else:
            seen_layers.add(layer)
        if not isinstance(step.get("change"), str) or not step["change"].strip():
            diagnostics.append(
                diagnostic(
                    "error",
                    "MISSING_PATCH_CHANGE",
                    f"{step_path}.change",
                    "Each patch step needs a non-empty change description.",
                    "Describe the smallest intended harness change for this layer.",
                )
            )
        if "files" in step:
            validate_string_list(step["files"], f"{step_path}.files", diagnostics)
            if isinstance(layer, str):
                validate_patch_step_files(step, layer=layer, step_path=step_path, diagnostics=diagnostics)
    missing_layers = sorted(REQUIRED_PATCH_LAYERS - seen_layers)
    if missing_layers:
        diagnostics.append(
            diagnostic(
                "warning",
                "PATCH_PLAN_MISSING_LAYERS",
                f"{path}.patch_plan",
                f"patch_plan does not mention required layers: {missing_layers}.",
                "Include schema, validator, normalizer, runner, and tests before a patch agent starts.",
            )
        )


def extension_request_text(value: Any) -> str:
    parts: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            parts.append(str(key))
            parts.append(extension_request_text(item))
    elif isinstance(value, list):
        for item in value:
            parts.append(extension_request_text(item))
    elif isinstance(value, str):
        parts.append(value)
    return " ".join(part for part in parts if part)


def validate_not_asset_gap_as_extension(value: dict[str, Any], diagnostics: list[dict[str, Any]], *, path: str) -> None:
    text = extension_request_text(value).lower()
    missing_asset_terms = [
        "asset missing",
        "asset_missing",
        "data is missing",
        "dataset index",
        "dataset is missing",
        "source_file does not exist",
        "source file does not exist",
        "source_file is missing",
        "source file is missing",
        "available_source_files is empty",
        "no concrete source_file",
        "no concrete source file",
        "missing asset",
        "missing local input asset",
    ]
    schema_workaround_terms = [
        "asset_check_mode",
        "dataset discovery",
        "expected_oracle",
        "fallback_source",
        "generate_source",
        "handle missing assets",
        "missing_asset_report",
        "source_file_status",
        "skip_on_missing_source",
        "synthetic_source",
        "skip if source",
        "skip the test case",
        "synthetic body",
        "synthetic_body",
        "runner should not attempt",
    ]
    harness_change_terms = [
        "normalizer",
        "patch_plan",
        "proposed_recipe_fields",
        "recipe schema",
        "runner",
        "schema",
        "validator",
    ]
    if any(term in text for term in missing_asset_terms) and (
        any(term in text for term in schema_workaround_terms)
        or any(term in text for term in harness_change_terms)
    ):
        diagnostics.append(
            diagnostic(
                "error",
                "MISSING_INPUT_ASSET_NOT_HARNESS_EXTENSION",
                path,
                "The request turns a missing local input asset into a schema/runner extension.",
                "Materialize the required source asset, update the form input_assets path, or mark the task as an asset-preparation action; do not add skip/synthetic-source harness behavior to hide missing corpus data.",
            )
        )


def validate_extension_request(value: Any, path: str = "$") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    if not isinstance(value, dict):
        return {}, [
            diagnostic(
                "error",
                "EXTENSION_NOT_OBJECT",
                path,
                "Extension request must be one JSON object.",
                "Return a needs_harness_extension object.",
            )
        ]
    normalized, notes = normalize_extension_request(value)
    diagnostics.extend(notes)
    if normalized.get("kind") != "needs_harness_extension":
        diagnostics.append(
            diagnostic(
                "error",
                "INVALID_EXTENSION_KIND",
                f"{path}.kind",
                "Extension request kind must be needs_harness_extension.",
                "Set kind to needs_harness_extension for unsupported APIs, builders, or oracles.",
                {"kind": "needs_harness_extension"},
            )
        )
    validate_string_field(normalized, "api", diagnostics, path=path)
    validate_string_field(normalized, "why_needed", diagnostics, path=path)
    validate_string_field(normalized, "extension_summary", diagnostics, path=path)
    proposed_field_names = validate_proposed_recipe_fields(normalized, diagnostics, path=path)
    validate_smoke_case(normalized, diagnostics, path=path, proposed_field_names=proposed_field_names)
    validate_proposed_artifacts(normalized, diagnostics, path=path)
    validate_validation_oracle(normalized, diagnostics, path=path)
    validate_patch_plan(normalized, diagnostics, path=path)
    api = normalized.get("api")
    if isinstance(api, str) and api in SUPPORTED_APIS:
        diagnostics.append(
            diagnostic(
                "warning",
                "EXTENSION_API_ALREADY_SUPPORTED",
                f"{path}.api",
                f"{api} is already listed in interface_capabilities.json.",
                "Explain whether the missing support is a body builder, oracle, runner field, or profile rather than a new API.",
            )
        )
    validate_not_asset_gap_as_extension(normalized, diagnostics, path=path)
    return normalized, diagnostics


def validate_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        diagnostics = [
            diagnostic(
                "error",
                "FILE_NOT_FOUND",
                str(path),
                f"file not found: {path}",
                "Save the needs_harness_extension JSON before validating it.",
            )
        ]
        return {"path": str(path), "ok": False, "normalized": {}, "diagnostics": diagnostics}
    try:
        loaded = read_json(path)
    except json.JSONDecodeError as exc:
        diagnostics = [
            diagnostic(
                "error",
                "INVALID_JSON",
                str(path),
                str(exc),
                "Return exactly one valid JSON object with no markdown wrapper.",
            )
        ]
        return {"path": str(path), "ok": False, "normalized": {}, "diagnostics": diagnostics}
    normalized, diagnostics = validate_extension_request(loaded, "$")
    ok = not any(item.get("severity") == "error" for item in diagnostics)
    return {"path": str(path), "ok": ok, "normalized": normalized, "diagnostics": diagnostics}


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    for record in records:
        path = str(record.get("path") or "")
        for item in record.get("diagnostics", []):
            diag = dict(item)
            if diag.get("path", "").startswith("$"):
                diag["path"] = f"{path}:{diag['path']}"
            diagnostics.append(diag)
    error_count = sum(1 for item in diagnostics if item.get("severity") == "error")
    warning_count = sum(1 for item in diagnostics if item.get("severity") == "warning")
    return {
        "generated_at": now_iso_like(),
        "ok": error_count == 0,
        "file_count": len(records),
        "error_count": error_count,
        "warning_count": warning_count,
        "diagnostic_count": len(diagnostics),
        "diagnostics": diagnostics,
        "records": records,
    }


def main() -> int:
    args = parse_args()
    records = [validate_file(path) for path in iter_json_files(args.paths)]
    summary = build_summary(records)
    for record in records:
        path = record["path"]
        if record["ok"]:
            print(f"OK {path}")
        else:
            print(f"FAIL {path}")
        for item in record.get("diagnostics", []):
            if item.get("severity") == "error":
                print(f"  - {item.get('error_code')}: {item.get('message')}")
    if args.report:
        write_json(Path(args.report), summary)
    if args.model_diagnostics:
        write_json(
            Path(args.model_diagnostics),
            {
                "generated_at": summary["generated_at"],
                "ok": summary["ok"],
                "file_count": summary["file_count"],
                "diagnostic_count": summary["diagnostic_count"],
                "diagnostics": summary["diagnostics"],
            },
        )
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
