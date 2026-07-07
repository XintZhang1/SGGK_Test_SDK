#!/usr/bin/env python3
"""Build a deterministic small-model task from an SGGK API test form."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any


SUPPORTED_APIS = [
    "api_boolean",
    "check_sgt",
    "step_import",
    "iges_import",
    "step_roundtrip",
    "iges_roundtrip",
    "needs_harness_extension",
]

SUPPORTED_BODY_BUILDERS = [
    "solid_cylinder",
    "solid_wedge",
    "solid_sphere",
    "solid_cone",
    "solid_torus",
    "extrude_rect",
    "thicken_rect_sheet",
    "sweep_circle_line",
    "support_sweep_bspline_surface",
    "revolve_line",
    "revolve_rect",
    "pre_boolean_cylinder_wedge",
    "loaded_sgt",
]

SUPPORTED_ORACLES = [
    "result_bodies",
    "properties",
    "boolean_volume_relation",
    "point_relation",
    "face_point_relation",
    "clash",
    "distance",
    "plane_extreme",
    "roundtrip_comparison",
    "topocheck",
]

API_GUIDANCE: dict[str, dict[str, Any]] = {
    "api_boolean": {
        "preferred_format": "attack_dsl",
        "body_required": ["target", "tool"],
        "notes": [
            "Use DSL target/tool builders or chains.",
            "Set stable id fields on chain steps that create, split, trim, or transform topology.",
            "For tolerance risks, use sweeps or paired_sweeps around exact contact, geom_tol, and topo_tol.",
        ],
    },
    "check_sgt": {
        "preferred_format": "flat_recipe",
        "body_required": ["source_file"],
        "notes": [
            "Use a flat recipe with api=check_sgt and a source_file.",
            "For non-body SGT topology assets, report topology counts rather than body property oracles.",
        ],
    },
    "step_import": {
        "preferred_format": "flat_recipe",
        "body_required": ["source_file"],
        "notes": [
            "Use source_file pointing at .step or .stp.",
            "Follow import with corpus discovery or SGT recut when imported bodies should be attacked further.",
        ],
    },
    "iges_import": {
        "preferred_format": "flat_recipe",
        "body_required": ["source_file"],
        "notes": [
            "Use source_file pointing at .iges or .igs.",
            "Follow import with corpus discovery or SGT recut when imported bodies should be attacked further.",
        ],
    },
    "step_roundtrip": {
        "preferred_format": "flat_recipe",
        "body_required": ["source_file", "source_body_index"],
        "notes": [
            "Use a source .sgt body, export to STEP, import back, then compare properties and bbox.",
            "Select AP203, AP214, or AP242 explicitly when source risk names an exchange protocol.",
        ],
    },
    "iges_roundtrip": {
        "preferred_format": "flat_recipe",
        "body_required": ["source_file", "source_body_index"],
        "notes": [
            "Use a source .sgt body, export to IGES, import back, then compare properties and bbox.",
            "Set face-only or SGK specified data flags only when the risk calls for them.",
        ],
    },
    "needs_harness_extension": {
        "preferred_format": "needs_harness_extension",
        "body_required": [],
        "notes": [
            "Return an extension request instead of pretending the runner supports the API.",
            "Include the minimal new recipe shape and one concrete smoke case.",
        ],
    },
}

ORACLE_GUIDANCE: dict[str, str] = {
    "result_bodies": "Assert result_bodies min/max when non-empty or empty output is part of the truth.",
    "properties": "Assert finite length/area/volume and metric ranges when analytic bounds are known.",
    "boolean_volume_relation": "Use sample_input_properties=true only for stable solid boolean inputs.",
    "point_relation": "Use named key_points and point_ref for critical inside/outside/boundary probes.",
    "face_point_relation": "Prefer uv_fraction or uv on selected faces unless an exact 3D face point is known.",
    "clash": "Use role_a/role_b among target, tool, and result with NoClash, AnyClash, or exact ClashType.",
    "distance": "Use minimum distance for clearance/tangency and set expected/min/max with explicit tolerances.",
    "plane_extreme": "Use exact coordinate-plane distance probes for hard min/max coordinate oracles.",
    "roundtrip_comparison": "Rely on roundtrip_comparison.json for STEP/IGES source/result drift.",
    "topocheck": "TopoCheck is always part of runner pass/fail for supported body outputs.",
}

REQUIRED_FIELDS = [
    "request_id",
    "owner",
    "target_api",
    "test_goal",
    "risk_summary",
    "geometry",
    "oracles",
    "run_profile",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("form", help="Developer-filled API test form JSON")
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


def as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def validate_form(form: Any) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(form, dict):
        return ["form root must be an object"], warnings

    for field in REQUIRED_FIELDS:
        if field not in form:
            errors.append(f"missing required field: {field}")

    request_id = form.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        errors.append("request_id must be a non-empty string")

    target_api = form.get("target_api")
    if target_api not in SUPPORTED_APIS:
        warnings.append(
            "target_api is not currently runnable; model output should be needs_harness_extension"
        )

    geometry = form.get("geometry")
    if not isinstance(geometry, dict):
        errors.append("geometry must be an object")
    elif not isinstance(geometry.get("family"), str) or not geometry.get("family"):
        errors.append("geometry.family must be a non-empty string")

    oracles = form.get("oracles")
    if not isinstance(oracles, list) or not oracles:
        errors.append("oracles must be a non-empty list")
    else:
        for oracle in oracles:
            if oracle not in SUPPORTED_ORACLES:
                warnings.append(f"unknown oracle {oracle!r}; model should map it to a supported oracle or request extension")

    case_count = form.get("case_count", 3)
    if not isinstance(case_count, int) or isinstance(case_count, bool) or case_count < 1 or case_count > 100:
        warnings.append("case_count should be an integer from 1 to 100; using model judgment")

    return errors, warnings


def command_lines(request_id: str, preferred_format: str) -> list[str]:
    safe_id = request_id.replace("/", "_").replace("\\", "_")
    commands: list[str] = []
    if preferred_format == "attack_dsl":
        commands.extend(
            [
                (
                    "python .\\test_harness\\tools\\compile_attack_dsl.py "
                    f".\\artifacts\\model_outputs\\{safe_id}_dsl.json --check "
                    f"--report .\\artifacts\\model_checks\\{safe_id}_check.json"
                ),
                (
                    "python .\\test_harness\\tools\\compile_attack_dsl.py "
                    f".\\artifacts\\model_outputs\\{safe_id}_dsl.json "
                    f"--out .\\artifacts\\compiled_model_recipes\\{safe_id}"
                ),
                (
                    "python .\\test_harness\\tools\\run_recipes.py "
                    "--runner .\\build\\test_harness\\Release\\sggk_case_runner.exe "
                    f"--recipe .\\artifacts\\compiled_model_recipes\\{safe_id} "
                    f"--out .\\artifacts\\model_runs\\{safe_id} --jobs 1 --timeout 120 "
                    f"--triage-out .\\artifacts\\model_triage\\{safe_id} "
                    f"--preview-out .\\artifacts\\model_previews\\{safe_id} "
                    f"--contact-sheet .\\artifacts\\model_previews\\{safe_id}\\contact.png "
                    f"--geometry-audit-out .\\artifacts\\model_geometry_audit\\{safe_id}"
                ),
            ]
        )
    elif preferred_format == "flat_recipe":
        commands.extend(
            [
                (
                    "python .\\test_harness\\tools\\validate_recipe.py "
                    f".\\artifacts\\model_outputs\\{safe_id}_recipe.json"
                ),
                (
                    "python .\\test_harness\\tools\\run_recipes.py "
                    "--runner .\\build\\test_harness\\Release\\sggk_case_runner.exe "
                    f"--recipe .\\artifacts\\model_outputs\\{safe_id}_recipe.json "
                    f"--out .\\artifacts\\model_runs\\{safe_id} --jobs 1 --timeout 120 "
                    f"--triage-out .\\artifacts\\model_triage\\{safe_id} "
                    f"--preview-out .\\artifacts\\model_previews\\{safe_id} "
                    f"--contact-sheet .\\artifacts\\model_previews\\{safe_id}\\contact.png"
                ),
            ]
        )
    commands.append(
        (
            "python .\\test_harness\\tools\\run_recipes.py "
            "--runner .\\build\\test_harness\\Release\\sggk_case_runner.exe "
            "--recipe-list .\\test_harness\\suites\\api_smoke_suite.txt "
            "--out .\\artifacts\\api_smoke_suite --jobs 1 --timeout 120 "
            "--triage-out .\\artifacts\\api_smoke_suite_triage "
            "--preview-out .\\artifacts\\api_smoke_suite_preview "
            "--contact-sheet .\\artifacts\\api_smoke_suite_preview\\contact.png"
        )
    )
    return commands


def render_prompt(form: dict[str, Any], guidance: dict[str, Any], oracle_notes: list[str]) -> str:
    form_json = json.dumps(form, indent=2, ensure_ascii=False)
    body_builders = ", ".join(SUPPORTED_BODY_BUILDERS)
    oracles = ", ".join(SUPPORTED_ORACLES)
    guidance_json = json.dumps(guidance, indent=2, ensure_ascii=False)
    oracle_json = json.dumps(oracle_notes, indent=2, ensure_ascii=False)
    return f"""You are generating SGGK test-harness input, not direct SDK code.

Return exactly one JSON object. Use this shape for runnable DSL tests:
{{
  "kind": "attack_dsl",
  "dsl": {{ "...": "valid SGGK attack DSL" }},
  "notes": ["short review notes"],
  "commands": ["compile/check/run commands"]
}}

Use this shape for supported flat-recipe APIs:
{{
  "kind": "flat_recipe",
  "recipe": {{ "...": "valid flat sggk_case_runner recipe" }},
  "notes": ["short review notes"],
  "commands": ["validate/run commands"]
}}

Use this shape for fixed large campaigns that should not enumerate every case:
{{
  "kind": "campaign_command",
  "command": "python .\\test_harness\\tools\\run_abc_boolean_mass_recut.py ...",
  "notes": ["why this fixed-code campaign matches the form"],
  "expected_artifacts": ["summary/report paths"]
}}

If the requested API or body builder is unsupported, return:
{{
  "kind": "needs_harness_extension",
  "api": "requested_api_name",
  "why": "why current harness cannot express this test",
  "minimal_extension": "smallest runner/schema addition",
  "proposed_recipe": {{ "...": "concrete future recipe" }}
}}

Hard rules:
- Prefer attack DSL for api_boolean.
- For 100k+ corpus campaigns, do not emit individual DSL cases; emit campaign_command and use fixed code to expand recipes.
- Do not invent SDK calls outside the runner schema.
- Use constants topo_tol=0.01, geom_tol=0.00001, max_model_size=500000.0 unless the form overrides them.
- Use stable id values on all important chain steps.
- Add real oracles, not only API status checks.
- Use sweeps or paired_sweeps for tolerance boundaries.
- Emit valid JSON only.

Supported body builders: {body_builders}
Supported oracle families: {oracles}

API guidance:
{guidance_json}

Oracle guidance selected for this form:
{oracle_json}

Developer form:
{form_json}
"""


def build_task(form_path: Path, form: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    target_api = str(form.get("target_api", "needs_harness_extension"))
    request_id = str(form.get("request_id", form_path.stem))
    guidance = API_GUIDANCE.get(target_api, API_GUIDANCE["needs_harness_extension"])
    selected_oracles = as_string_list(form.get("oracles"))
    oracle_notes = [ORACLE_GUIDANCE.get(oracle, f"Map {oracle} to a supported oracle or request extension.") for oracle in selected_oracles]
    preferred_format = guidance["preferred_format"]
    fixed_commands = command_lines(request_id, preferred_format)
    if request_id == "iface_15_boolean_abc_mass_recut":
        fixed_commands = [
            (
                "python .\\test_harness\\tools\\run_abc_boolean_mass_recut.py "
                "--runner .\\build\\test_harness\\Release\\sggk_case_runner.exe "
                "--dataset .\\artifacts\\interface_distillation_windows_full_40chunk_v2\\abc_sample_smoke\\top_complex_import "
                "--out .\\artifacts\\abc_boolean_mass_recut "
                "--target-cases 100000 --preset stress --shard-count 100 --shard-index 0 "
                "--jobs 1 --timeout 180 --resume"
            ),
        ]
        guidance = dict(guidance)
        guidance["preferred_format"] = "campaign_command"
        guidance["notes"] = list(guidance.get("notes", [])) + [
            "Use campaign_command for this large ABC recut form; fixed code expands 100k+ recipes and filters explicit unsupported failures from bug reports.",
        ]
        preferred_format = "campaign_command"

    task = {
        "task_version": 1,
        "created_at": now_iso_like(),
        "form_path": str(form_path),
        "request_id": request_id,
        "warnings": warnings,
        "model_role": "Generate SGGK harness DSL or a needs_harness_extension object.",
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
        },
        "api_guidance": guidance,
        "oracle_guidance": oracle_notes,
        "fixed_commands": fixed_commands,
    }
    task["prompt"] = render_prompt(form, guidance, oracle_notes)
    return task


def render_markdown(task: dict[str, Any]) -> str:
    commands = "\n".join(f"- `{command}`" for command in task["fixed_commands"])
    return f"""# SGGK API Test Task: {task["request_id"]}

## Prompt

```text
{task["prompt"]}
```

## Fixed Commands

{commands}
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
