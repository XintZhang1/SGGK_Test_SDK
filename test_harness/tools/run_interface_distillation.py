#!/usr/bin/env python3
"""Prepare and optionally execute the interface distillation workflow.

The default mode is SDK-free: read the interface distillation manifest, build
one small-model task per developer form, and write a campaign report. When
saved, gate-passing model outputs and a Windows runner are available, pass
``--execute`` to normalize, check, compile, run, triage, preview, and report
those outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from build_api_test_task import build_task, render_markdown, validate_form
from diagnostic_catalog import catalog_summary, enrich_diagnostics
from harness_capabilities import load_capabilities, provenance_source_metadata
from campaign_profiles import CampaignRequestError, resolve_campaign_argv, validate_campaign_request


CAPABILITIES = load_capabilities()
REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--forms-dir",
        default="test_harness/forms/interface_distillation",
        help="Directory containing interface distillation form JSON files.",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="Manifest JSON. Defaults to <forms-dir>/00_manifest.json.",
    )
    parser.add_argument("--out", default="artifacts/interface_distillation", help="Workflow output root.")
    parser.add_argument(
        "--model-output-root",
        default="artifacts/model_outputs",
        help="Directory where gateway-promoted model JSON outputs are saved.",
    )
    parser.add_argument(
        "--model-output-provenance-root",
        default="artifacts/model_output_provenance",
        help="Optional alternate sidecar directory; outputs still require hash-matching Message API pipeline acceptance.",
    )
    parser.add_argument(
        "--runner",
        default="build/test_harness/Release/sggk_case_runner.exe",
        help="Path to sggk_case_runner.exe when --execute is used.",
    )
    parser.add_argument("--check-model-outputs", action="store_true", help="Run SDK-free DSL/recipe checks for model outputs.")
    parser.add_argument("--execute", action="store_true", help="Check/compile/run gate-passing model outputs when present.")
    parser.add_argument("--require-model-outputs", action="store_true", help="Return non-zero if any model output is missing.")
    parser.add_argument("--api-smoke", action="store_true", help="Run the current API smoke suite after model-output lanes.")
    parser.add_argument("--abc-sample-smoke", action="store_true", help="Run the focused ABC sample smoke lane.")
    parser.add_argument("--abc-fetch-root", default="", help="ABC fetch root; defaults to manifest abc_inputs.preferred_fetch_root.")
    parser.add_argument("--source-root", action="append", default=[], help="SGGK source/include root to scan for source-guided tasks.")
    parser.add_argument("--source-scan-max-findings", type=int, default=120)
    parser.add_argument("--source-scan-max-seeds", type=int, default=30)
    parser.add_argument("--source-task-max-tasks", type=int, default=80)
    parser.add_argument("--source-task-context-lines", type=int, default=12)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--fail-on-failures", action="store_true", help="Return non-zero for SDK/test failures.")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def safe_id(value: str) -> str:
    result = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in value)
    result = result.strip("._-")
    return result or "task"


def run_command(name: str, cmd: list[str], acceptable: set[int] | None = None) -> dict[str, Any]:
    if acceptable is None:
        acceptable = {0}
    print(f"[interface-distillation] {name}")
    print("  " + " ".join(cmd))
    started = time.perf_counter()
    completed = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    elapsed = time.perf_counter() - started
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return {
        "name": name,
        "command": cmd,
        "returncode": completed.returncode,
        "acceptable": sorted(acceptable),
        "ok": completed.returncode in acceptable,
        "elapsed_seconds": elapsed,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def manifest_forms(forms_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    entries = manifest.get("forms")
    if isinstance(entries, list) and entries:
        result: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            form_name = entry.get("form")
            if not isinstance(form_name, str) or not form_name:
                continue
            item = dict(entry)
            item["path"] = str(forms_dir / form_name)
            result.append(item)
        return sorted(result, key=lambda item: int(item.get("order", 999999)))

    result = []
    for path in sorted(forms_dir.glob("*.json")):
        if path.name == "00_manifest.json":
            continue
        result.append({"form": path.name, "path": str(path)})
    return result


def build_tasks(forms_dir: Path, manifest: dict[str, Any], out_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    tasks_dir = out_root / "model_tasks"
    prompts_dir = out_root / "model_prompts"
    for entry in manifest_forms(forms_dir, manifest):
        form_path = Path(str(entry["path"]))
        form = read_json(form_path)
        errors, warnings = validate_form(form)
        request_id = str(form.get("request_id", form_path.stem)) if isinstance(form, dict) else form_path.stem
        task_path = tasks_dir / f"{safe_id(request_id)}.json"
        prompt_path = prompts_dir / f"{safe_id(request_id)}.md"
        record: dict[str, Any] = {
            "request_id": request_id,
            "form": str(form_path),
            "manifest_entry": entry,
            "task_path": str(task_path),
            "prompt_path": str(prompt_path),
            "errors": errors,
            "warnings": warnings,
            "stage": "form_error" if errors else "task_built",
        }
        if not errors:
            task = build_task(form_path.resolve(), form, warnings)
            write_json(task_path, task)
            write_text(prompt_path, render_markdown(task))
            record["target_api"] = form.get("target_api")
            record["geometry_family"] = (form.get("geometry") or {}).get("family")
            record["preferred_output"] = (task.get("api_guidance") or {}).get("preferred_format")
            record["selected_example_pack"] = (task.get("harness_contract") or {}).get("selected_example_pack", "")
            record["interface_family"] = task.get("interface_family", "")
            record["run_profile_id"] = task.get("run_profile_id", "")
            record["run_profile"] = (task.get("harness_contract") or {}).get("run_profile", {})
            record["allowed_campaign_profiles"] = task.get("allowed_campaign_profiles", {})
            record["campaign_bindings"] = task.get("campaign_bindings", {})
        records.append(record)
    return records


def model_output_provenance_path(request_id: str, provenance_root: Path) -> Path:
    return provenance_root / f"{safe_id(request_id)}.json"


def find_model_output(request_id: str, model_output_root: Path) -> tuple[str, Path | None]:
    base = safe_id(request_id)
    candidates = [
        ("raw", model_output_root / f"{base}.json"),
        ("dsl", model_output_root / f"{base}_dsl.json"),
        ("recipe", model_output_root / f"{base}_recipe.json"),
        ("cluster_seed", model_output_root / f"{base}_cluster_seed.json"),
    ]
    for kind, path in candidates:
        if path.is_file():
            return kind, path
    return "missing", None


def compact_provenance_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def compact_positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def load_model_output_provenance(
    request_id: str,
    found_path: Path | None,
    provenance_root: Path,
) -> dict[str, Any]:
    base = safe_id(request_id)
    candidates = [model_output_provenance_path(request_id, provenance_root)]
    if found_path is not None:
        candidates.extend(
            [
                found_path.with_name(f"{found_path.stem}.provenance.json"),
                found_path.with_name(f"{base}.provenance.json"),
            ]
        )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            loaded = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "found": True,
                "path": str(path),
                "source_type": "invalid_provenance",
                "error": str(exc),
            }
        if not isinstance(loaded, dict):
            return {
                "found": True,
                "path": str(path),
                "source_type": "invalid_provenance",
                "error": "provenance sidecar must be a JSON object",
            }
        source_type = compact_provenance_value(loaded.get("source_type")) or "unspecified_saved_output"
        repair = loaded.get("repair") if isinstance(loaded.get("repair"), dict) else {}
        acceptance = loaded.get("acceptance") if isinstance(loaded.get("acceptance"), dict) else {}
        fixed_gate = loaded.get("fixed_gate") if isinstance(loaded.get("fixed_gate"), dict) else {}
        boundary = loaded.get("boundary") if isinstance(loaded.get("boundary"), dict) else {}
        result = {
            "found": True,
            "path": str(path),
            "schema_version": loaded.get("schema_version"),
            "request_id": compact_provenance_value(loaded.get("request_id")),
            "source_type": source_type,
            "source_label": compact_provenance_value(loaded.get("source_label")),
            "source_path": compact_provenance_value(loaded.get("source_path")),
            "model": compact_provenance_value(loaded.get("model")),
            "interface": compact_provenance_value(loaded.get("interface")),
            "run_id": compact_provenance_value(loaded.get("run_id")),
            "prompt_sha256": compact_provenance_value(loaded.get("prompt_sha256")),
            "message_content_sha256": compact_provenance_value(
                loaded.get("message_content_sha256")
            ),
            "prompt_pack": compact_provenance_value(loaded.get("prompt_pack")),
            "saved_at": compact_provenance_value(loaded.get("saved_at")),
            "candidate_sha256": compact_provenance_value(loaded.get("candidate_sha256")),
            "authoring_accepted": acceptance.get("authoring_accepted") is True,
            "accepted_by": compact_provenance_value(acceptance.get("accepted_by")),
            "requires_fixed_gate": acceptance.get("requires_fixed_gate") is True,
            "fixed_gate_ok": fixed_gate.get("ok") is True,
            "fixed_gate_kind": compact_provenance_value(fixed_gate.get("kind")),
            "fixed_gate_report_path": compact_provenance_value(fixed_gate.get("report_path")),
            "model_calls": boundary.get("model_calls") is True,
            "direct_api_calls": boundary.get("direct_api_calls") is True,
            "production_flow": compact_provenance_value(boundary.get("production_flow")),
        }
        if repair:
            result["repair_output"] = bool(repair.get("is_repair_output"))
            result["repair_iteration"] = compact_positive_int(repair.get("iteration"))
            result["repair_context_path"] = compact_provenance_value(repair.get("repair_context_path"))
            result["repair_parent_request_id"] = compact_provenance_value(repair.get("parent_request_id"))
            result["repair_parent_output_path"] = compact_provenance_value(repair.get("parent_output_path"))
        return result
    return {
        "found": False,
        "path": "",
        "source_type": "missing_output" if found_path is None else "unknown_saved_output",
    }


def pipeline_acceptance_error(found_path: Path, provenance: dict[str, Any]) -> str:
    """Require a hash-bound promotion record from the Message API pipeline."""

    try:
        candidate_value = read_json(found_path)
    except (OSError, json.JSONDecodeError) as exc:
        return f"model output is not valid JSON: {exc}"
    candidate_sha256 = hashlib.sha256(
        json.dumps(
            candidate_value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    accepted_source_types = {"intranet_message_api", "siliconflow_test_message_api"}
    if provenance.get("found") is not True:
        return "Message API pipeline provenance is missing"
    if provenance.get("schema_version") != 3:
        return "provenance is not a schema_version=3 pipeline acceptance record"
    if provenance.get("source_type") not in accepted_source_types:
        return "provenance source is not an allowed Message API profile"
    if provenance.get("interface") != "openai_compatible_chat_completions_message_content_json":
        return "provenance interface is not Message API message.content JSON"
    if not all(
        isinstance(provenance.get(key), str) and provenance.get(key)
        for key in ("model", "run_id", "prompt_sha256", "message_content_sha256")
    ):
        return "provenance is missing immutable Message API call identity"
    if provenance.get("authoring_accepted") is not True:
        return "provenance does not mark authoring_accepted=true"
    if provenance.get("accepted_by") != "message_harness_pipeline":
        return "provenance was not accepted by message_harness_pipeline"
    if provenance.get("requires_fixed_gate") is not False or provenance.get("fixed_gate_ok") is not True:
        return "provenance does not prove a completed fixed gate"
    if (
        provenance.get("model_calls") is not True
        or provenance.get("direct_api_calls") is not True
        or provenance.get("production_flow")
        != "message_api_fixed_gate_repair_atomic_acceptance"
    ):
        return "provenance does not prove the Message API pipeline boundary"
    report_path_raw = provenance.get("fixed_gate_report_path")
    if not isinstance(report_path_raw, str) or not report_path_raw:
        return "provenance is missing the fixed-gate report path"
    report_path = Path(report_path_raw)
    if not report_path.is_absolute():
        report_path = REPO_ROOT / report_path
    try:
        report = read_json(report_path)
    except (OSError, json.JSONDecodeError):
        return "fixed-gate report is missing or unreadable"
    if (
        not isinstance(report, dict)
        or report.get("ok") is not True
        or report.get("kind") != provenance.get("fixed_gate_kind")
    ):
        return "fixed-gate report does not match the accepted provenance"
    if provenance.get("candidate_sha256") != candidate_sha256:
        return "provenance candidate_sha256 does not match the saved output"
    return ""


def normalize_model_output(
    record: dict[str, Any],
    model_output_root: Path,
    provenance_root: Path,
    out_root: Path,
) -> dict[str, Any]:
    request_id = str(record["request_id"])
    found_kind, found_path = find_model_output(request_id, model_output_root)
    base = safe_id(request_id)
    model_record: dict[str, Any] = {
        "found": found_path is not None,
        "input_kind": found_kind,
        "input_path": str(found_path) if found_path else "",
        "kind": "",
        "normalized_path": "",
        "diagnostics_path": "",
        "notes": [],
    }
    model_record["provenance"] = load_model_output_provenance(request_id, found_path, provenance_root)
    if found_path is None:
        model_record["kind"] = "missing"
        return model_record
    provenance = model_record["provenance"]
    acceptance_error = pipeline_acceptance_error(found_path, provenance)
    if acceptance_error:
        model_record["kind"] = "invalid"
        model_record["notes"] = [acceptance_error]
        return model_record

    diagnostics_path = out_root / "model_normalize" / f"{base}_diagnostics.json"
    normalize_command = run_command(
        f"{request_id}: normalize model output",
        [
            sys.executable,
            "test_harness/tools/normalize_model_output.py",
            str(found_path),
            "--request-id",
            request_id,
            "--model-output-root",
            str(model_output_root),
            "--diagnostics",
            str(diagnostics_path),
        ],
        acceptable={0, 1},
    )
    model_record["normalize_command"] = normalize_command
    model_record["diagnostics_path"] = str(diagnostics_path)
    if not diagnostics_path.is_file():
        model_record["kind"] = "invalid"
        model_record["notes"].append("normalizer did not write diagnostics")
        return model_record

    report = read_json(diagnostics_path)
    if not isinstance(report, dict):
        model_record["kind"] = "invalid"
        model_record["notes"].append("normalizer diagnostics must be an object")
        return model_record

    reported_kind = report.get("kind")
    model_record["reported_kind"] = reported_kind if isinstance(reported_kind, str) else ""
    model_record["normalizer_report"] = report
    model_record["normalized_path"] = str(report.get("normalized_path") or "")
    for item in report.get("diagnostics", []):
        if isinstance(item, dict) and item.get("severity") == "error":
            model_record["notes"].append(str(item.get("message") or item.get("error_code") or "normalization error"))
    if not report.get("ok"):
        model_record["kind"] = "invalid"
        return model_record
    model_record["kind"] = model_record["reported_kind"]
    return model_record


def check_attack_dsl(request_id: str, dsl_path: Path, out_root: Path) -> dict[str, Any]:
    base = safe_id(request_id)
    checks_dir = out_root / "model_checks"
    compiled_dir = out_root / "compiled_model_recipes" / base
    check_report = checks_dir / f"{base}_check.json"
    check_diagnostics = checks_dir / f"{base}_diagnostics.json"
    commands: list[dict[str, Any]] = []
    commands.append(
        run_command(
            f"{request_id}: check DSL",
            [
                sys.executable,
                "test_harness/tools/compile_attack_dsl.py",
                str(dsl_path),
                "--check",
                "--report",
                str(check_report),
                "--model-diagnostics",
                str(check_diagnostics),
            ],
        )
    )
    if commands[-1]["ok"]:
        commands.append(
            run_command(
                f"{request_id}: compile DSL",
                [
                    sys.executable,
                    "test_harness/tools/compile_attack_dsl.py",
                    str(dsl_path),
                    "--out",
                    str(compiled_dir),
                ],
            )
        )
    return {
        "kind": "attack_dsl",
        "ok": all(command["ok"] for command in commands),
        "commands": commands,
        "check_report": str(check_report),
        "model_diagnostics": str(check_diagnostics),
        "compiled_dir": str(compiled_dir),
    }


def check_flat_recipe(request_id: str, recipe_path: Path, out_root: Path) -> dict[str, Any]:
    base = safe_id(request_id)
    diagnostics_path = out_root / "model_checks" / f"{base}_recipe_diagnostics.json"
    commands = [
        run_command(
            f"{request_id}: validate recipe",
            [
                sys.executable,
                "test_harness/tools/validate_recipe.py",
                str(recipe_path),
                "--check-assets",
                "--model-diagnostics",
                str(diagnostics_path),
            ],
        )
    ]
    return {
        "kind": "flat_recipe",
        "ok": all(command["ok"] for command in commands),
        "commands": commands,
        "model_diagnostics": str(diagnostics_path),
    }


def check_cluster_seed(request_id: str, seed_path: Path, out_root: Path) -> dict[str, Any]:
    base = safe_id(request_id)
    expanded_dsl = out_root / "expanded_cluster_dsl" / f"{base}.json"
    commands = [
        run_command(
            f"{request_id}: expand cluster seed",
            [
                sys.executable,
                "test_harness/tools/build_source_guided_cluster.py",
                str(seed_path),
                "--out",
                str(expanded_dsl),
            ],
        )
    ]
    if not commands[-1]["ok"]:
        return {"kind": "cluster_seed", "ok": False, "commands": commands, "expanded_dsl": str(expanded_dsl)}
    dsl_result = check_attack_dsl(request_id, expanded_dsl, out_root)
    return {
        "kind": "cluster_seed",
        "ok": commands[-1]["ok"] and dsl_result["ok"],
        "commands": commands + dsl_result.get("commands", []),
        "expanded_dsl": str(expanded_dsl),
        "check_report": dsl_result.get("check_report"),
        "compiled_dir": dsl_result.get("compiled_dir"),
    }


def check_harness_extension(request_id: str, extension_path: Path, out_root: Path) -> dict[str, Any]:
    base = safe_id(request_id)
    report_path = out_root / "model_checks" / f"{base}_extension_report.json"
    diagnostics_path = out_root / "model_checks" / f"{base}_extension_diagnostics.json"
    commands = [
        run_command(
            f"{request_id}: validate harness extension request",
            [
                sys.executable,
                "test_harness/tools/validate_harness_extension.py",
                str(extension_path),
                "--report",
                str(report_path),
                "--model-diagnostics",
                str(diagnostics_path),
            ],
        )
    ]
    return {
        "kind": "needs_harness_extension",
        "ok": all(command["ok"] for command in commands),
        "reason": "extension request validation",
        "commands": commands,
        "check_report": str(report_path),
        "model_diagnostics": str(diagnostics_path),
    }


def campaign_diagnostic(code: str, message: str, repair_hint: str, *, path: str = "$", expected_shape: Any | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": "error",
        "error_code": code,
        "path": path,
        "message": message,
        "repair_hint": repair_hint,
    }
    if expected_shape is not None:
        item["expected_shape"] = expected_shape
    return item


def check_campaign_request(request_id: str, request_path: Path, record: dict[str, Any], out_root: Path) -> dict[str, Any]:
    base = safe_id(request_id)
    report_path = out_root / "model_checks" / f"{base}_campaign_request_review.json"
    diagnostics_path = out_root / "model_checks" / f"{base}_campaign_request_diagnostics.json"
    diagnostics: list[dict[str, Any]] = []
    loaded: Any = {}
    try:
        loaded = read_json(request_path)
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append(
            campaign_diagnostic(
                "INVALID_CAMPAIGN_REQUEST_JSON",
                f"Campaign request output could not be read as JSON: {exc}",
                "Return exactly one campaign_request JSON object in message.content.",
                path="$",
            )
        )
    allowed_profiles = record.get("allowed_campaign_profiles") if isinstance(record.get("allowed_campaign_profiles"), dict) else {}
    normalized: dict[str, Any] | None = None
    resolved_argv: list[str] = []
    if not diagnostics:
        normalized, request_errors = validate_campaign_request(loaded, allowed_profiles)
        for message in request_errors:
            diagnostics.append(
                campaign_diagnostic(
                    "CAMPAIGN_REQUEST_INVALID",
                    message,
                    "Select one allowed profile_id and provide only bounded args accepted by its args_schema.",
                    expected_shape={
                        "kind": "campaign_request",
                        "profile_id": "one key from allowed_campaign_profiles",
                        "args": {},
                        "allowed_campaign_profiles": allowed_profiles,
                    },
                )
            )
    if normalized is not None:
        bindings_by_profile = record.get("campaign_bindings") if isinstance(record.get("campaign_bindings"), dict) else {}
        bindings = bindings_by_profile.get(normalized["profile_id"], {})
        try:
            resolved_argv = resolve_campaign_argv(
                normalized,
                allowed_profiles=allowed_profiles,
                bindings=bindings if isinstance(bindings, dict) else {},
            )
        except CampaignRequestError as exc:
            diagnostics.append(
                campaign_diagnostic(
                    "CAMPAIGN_PROFILE_BINDINGS_INVALID",
                    str(exc),
                    "Repair the fixed task profile/bindings; do not add paths or executable fields to model output.",
                )
            )

    diagnostics = enrich_diagnostics(diagnostics)
    ok = not error_diagnostics(diagnostics)
    diagnostics_payload = {
        "generated_at": now_iso_like(),
        "request_id": request_id,
        "kind": "campaign_request",
        "ok": ok,
        "diagnostics": diagnostics,
    }
    report = {
        "generated_at": now_iso_like(),
        "request_id": request_id,
        "kind": "campaign_request",
        "ok": ok,
        "skipped": ok,
        "reason": "typed campaign request validated" if ok else "campaign request failed typed profile validation",
        "normalized_request": normalized or {},
        "allowed_campaign_profiles": allowed_profiles,
        "resolved_argv": resolved_argv,
        "shell": False,
        "model_diagnostics": str(diagnostics_path),
    }
    write_json(diagnostics_path, diagnostics_payload)
    write_json(report_path, report)
    return {
        "kind": "campaign_request",
        "ok": ok,
        "skipped": ok,
        "reason": report["reason"],
        "check_report": str(report_path),
        "model_diagnostics": str(diagnostics_path),
        "profile_id": normalized.get("profile_id", "") if isinstance(normalized, dict) else "",
        "shell": False,
    }


def check_model_output(record: dict[str, Any], out_root: Path) -> dict[str, Any]:
    model = record.get("model_output") or {}
    normalized_path = Path(str(model.get("normalized_path", "")))
    kind = model.get("kind")
    if kind == "attack_dsl":
        return check_attack_dsl(str(record["request_id"]), normalized_path, out_root)
    if kind == "flat_recipe":
        return check_flat_recipe(str(record["request_id"]), normalized_path, out_root)
    if kind == "cluster_seed":
        return check_cluster_seed(str(record["request_id"]), normalized_path, out_root)
    if kind == "campaign_request":
        return check_campaign_request(str(record["request_id"]), normalized_path, record, out_root)
    if kind == "needs_harness_extension":
        return check_harness_extension(str(record["request_id"]), normalized_path, out_root)
    return {"kind": kind, "ok": False, "skipped": True, "reason": f"unsupported model kind: {kind}"}


def execute_attack_dsl(
    request_id: str,
    dsl_path: Path,
    out_root: Path,
    runner: Path,
    jobs: int,
    timeout: float,
) -> dict[str, Any]:
    base = safe_id(request_id)
    checks_dir = out_root / "model_checks"
    compiled_dir = out_root / "compiled_model_recipes" / base
    run_dir = out_root / "model_runs" / base
    triage_dir = out_root / "model_triage" / base
    preview_dir = out_root / "model_previews" / base
    audit_dir = out_root / "model_geometry_audit" / base
    check_report = checks_dir / f"{base}_check.json"
    check_diagnostics = checks_dir / f"{base}_diagnostics.json"
    commands: list[dict[str, Any]] = []

    check_cmd = [
        sys.executable,
        "test_harness/tools/compile_attack_dsl.py",
        str(dsl_path),
        "--check",
        "--report",
        str(check_report),
        "--model-diagnostics",
        str(check_diagnostics),
    ]
    commands.append(run_command(f"{request_id}: check DSL", check_cmd))
    if not commands[-1]["ok"]:
        return {
            "kind": "attack_dsl",
            "ok": False,
            "commands": commands,
            "check_report": str(check_report),
            "model_diagnostics": str(check_diagnostics),
        }

    compile_cmd = [
        sys.executable,
        "test_harness/tools/compile_attack_dsl.py",
        str(dsl_path),
        "--out",
        str(compiled_dir),
    ]
    commands.append(run_command(f"{request_id}: compile DSL", compile_cmd))
    if not commands[-1]["ok"]:
        return {"kind": "attack_dsl", "ok": False, "commands": commands, "compiled_dir": str(compiled_dir)}

    run_cmd = [
        sys.executable,
        "test_harness/tools/run_recipes.py",
        "--runner",
        str(runner),
        "--recipe",
        str(compiled_dir),
        "--out",
        str(run_dir),
        "--jobs",
        str(jobs),
        "--timeout",
        str(timeout),
        "--triage-out",
        str(triage_dir),
        "--triage-include-passed",
        "--preview-out",
        str(preview_dir),
        "--contact-sheet",
        str(preview_dir / "contact.png"),
        "--geometry-audit-out",
        str(audit_dir),
    ]
    commands.append(run_command(f"{request_id}: run DSL recipes", run_cmd, acceptable={0, 2}))
    return {
        "kind": "attack_dsl",
        "ok": all(command["ok"] for command in commands),
        "commands": commands,
        "check_report": str(check_report),
        "model_diagnostics": str(check_diagnostics),
        "compiled_dir": str(compiled_dir),
        "run_dir": str(run_dir),
        "triage_report": str(triage_dir / "triage_report.md"),
        "contact_sheet": str(preview_dir / "contact.png"),
        "geometry_audit": str(audit_dir / "geometry_audit.md"),
    }


def execute_flat_recipe(
    request_id: str,
    recipe_path: Path,
    out_root: Path,
    runner: Path,
    jobs: int,
    timeout: float,
) -> dict[str, Any]:
    base = safe_id(request_id)
    run_dir = out_root / "model_runs" / base
    triage_dir = out_root / "model_triage" / base
    preview_dir = out_root / "model_previews" / base
    diagnostics_path = out_root / "model_checks" / f"{base}_recipe_diagnostics.json"
    commands: list[dict[str, Any]] = []
    validate_cmd = [
        sys.executable,
        "test_harness/tools/validate_recipe.py",
        str(recipe_path),
        "--check-assets",
        "--model-diagnostics",
        str(diagnostics_path),
    ]
    commands.append(run_command(f"{request_id}: validate recipe", validate_cmd))
    if not commands[-1]["ok"]:
        return {"kind": "flat_recipe", "ok": False, "commands": commands, "model_diagnostics": str(diagnostics_path)}
    run_cmd = [
        sys.executable,
        "test_harness/tools/run_recipes.py",
        "--runner",
        str(runner),
        "--recipe",
        str(recipe_path),
        "--out",
        str(run_dir),
        "--jobs",
        str(jobs),
        "--timeout",
        str(timeout),
        "--triage-out",
        str(triage_dir),
        "--triage-include-passed",
        "--preview-out",
        str(preview_dir),
        "--contact-sheet",
        str(preview_dir / "contact.png"),
    ]
    commands.append(run_command(f"{request_id}: run recipe", run_cmd, acceptable={0, 2}))
    return {
        "kind": "flat_recipe",
        "ok": all(command["ok"] for command in commands),
        "commands": commands,
        "model_diagnostics": str(diagnostics_path),
        "run_dir": str(run_dir),
        "triage_report": str(triage_dir / "triage_report.md"),
        "contact_sheet": str(preview_dir / "contact.png"),
    }


def execute_cluster_seed(
    request_id: str,
    seed_path: Path,
    out_root: Path,
    runner: Path,
    jobs: int,
    timeout: float,
) -> dict[str, Any]:
    base = safe_id(request_id)
    expanded_dsl = out_root / "expanded_cluster_dsl" / f"{base}.json"
    cmd = [
        sys.executable,
        "test_harness/tools/build_source_guided_cluster.py",
        str(seed_path),
        "--out",
        str(expanded_dsl),
    ]
    commands = [run_command(f"{request_id}: expand cluster seed", cmd)]
    if not commands[-1]["ok"]:
        return {"kind": "cluster_seed", "ok": False, "commands": commands, "expanded_dsl": str(expanded_dsl)}
    dsl_result = execute_attack_dsl(request_id, expanded_dsl, out_root, runner, jobs, timeout)
    dsl_result["kind"] = "cluster_seed"
    dsl_result["expanded_dsl"] = str(expanded_dsl)
    dsl_result["commands"] = commands + dsl_result.get("commands", [])
    dsl_result["ok"] = all(command["ok"] for command in dsl_result["commands"])
    return dsl_result


def execute_model_outputs(records: list[dict[str, Any]], args: argparse.Namespace, out_root: Path) -> None:
    runner = Path(args.runner)
    runner_available = runner.is_file()

    model_output_root = Path(args.model_output_root)
    provenance_root = Path(args.model_output_provenance_root)
    for record in records:
        if record.get("errors"):
            continue
        model = normalize_model_output(record, model_output_root, provenance_root, out_root)
        record["model_output"] = model
        if model["kind"] == "missing":
            record["stage"] = "pending_model_output"
            continue
        if model["kind"] == "invalid":
            record["stage"] = "invalid_model_output"
            continue
        should_check = args.check_model_outputs or args.execute
        if should_check:
            check_result = check_model_output(record, out_root)
            record["check_result"] = check_result
            if not check_result.get("ok"):
                record["stage"] = "model_output_check_failed"
                continue
        if model["kind"] == "needs_harness_extension":
            record["stage"] = "needs_harness_extension"
            continue
        if not args.execute:
            record["stage"] = "model_output_checked" if should_check else "model_output_ready"
            continue
        if model["kind"] == "campaign_request":
            record["execute_result"] = {
                "kind": "campaign_request",
                "ok": True,
                "skipped": True,
                "reason": "execute typed campaigns through run_message_harness_pipeline.py with --campaign-dataset",
            }
            record["stage"] = "campaign_request_ready"
            continue
        if not runner_available:
            record["stage"] = "model_output_checked"
            record["execute_result"] = {
                "ok": False,
                "skipped": True,
                "reason": f"runner not found: {runner}",
            }
            continue

        normalized_path = Path(str(model["normalized_path"]))
        if model["kind"] == "attack_dsl":
            result = execute_attack_dsl(str(record["request_id"]), normalized_path, out_root, runner, args.jobs, args.timeout)
        elif model["kind"] == "flat_recipe":
            result = execute_flat_recipe(str(record["request_id"]), normalized_path, out_root, runner, args.jobs, args.timeout)
        elif model["kind"] == "cluster_seed":
            result = execute_cluster_seed(str(record["request_id"]), normalized_path, out_root, runner, args.jobs, args.timeout)
        else:
            result = {"ok": False, "skipped": True, "reason": f"unsupported model kind: {model['kind']}"}
        record["execute_result"] = result
        record["stage"] = "executed"


def run_api_smoke(args: argparse.Namespace, out_root: Path) -> dict[str, Any] | None:
    if not args.api_smoke:
        return None
    runner = Path(args.runner)
    if not runner.is_file():
        return {"ok": False, "skipped": True, "reason": f"runner not found: {runner}"}
    run_dir = out_root / "api_smoke_suite"
    triage_dir = out_root / "api_smoke_suite_triage"
    preview_dir = out_root / "api_smoke_suite_preview"
    cmd = [
        sys.executable,
        "test_harness/tools/run_recipes.py",
        "--runner",
        str(runner),
        "--recipe-list",
        "test_harness/suites/api_smoke_suite.txt",
        "--out",
        str(run_dir),
        "--jobs",
        "1",
        "--timeout",
        str(args.timeout),
        "--triage-out",
        str(triage_dir),
        "--triage-include-passed",
        "--preview-out",
        str(preview_dir),
        "--contact-sheet",
        str(preview_dir / "contact.png"),
    ]
    return run_command("api smoke suite", cmd, acceptable={0, 2})


def run_abc_sample(args: argparse.Namespace, manifest: dict[str, Any], out_root: Path) -> dict[str, Any] | None:
    if not args.abc_sample_smoke:
        return None
    runner = Path(args.runner)
    if not runner.is_file():
        return {"ok": False, "skipped": True, "reason": f"runner not found: {runner}"}
    abc_inputs = manifest.get("abc_inputs") if isinstance(manifest.get("abc_inputs"), dict) else {}
    fetch_root = Path(args.abc_fetch_root or str(abc_inputs.get("preferred_fetch_root") or "artifacts/abc_fetch_smoke"))
    cmd = [
        sys.executable,
        "test_harness/tools/run_abc_sample_smoke.py",
        "--fetch-root",
        str(fetch_root),
        "--runner",
        str(runner),
        "--out",
        str(out_root / "abc_sample_smoke"),
        "--jobs",
        str(args.jobs),
        "--timeout",
        str(args.timeout),
    ]
    return run_command("abc sample smoke", cmd, acceptable={0, 2})


def run_source_tasks(args: argparse.Namespace, out_root: Path) -> dict[str, Any] | None:
    if not args.source_root:
        return None
    scan_out = out_root / "source_scan"
    tasks_out = out_root / "source_attack_tasks"
    commands: list[dict[str, Any]] = []
    scan_cmd = [
        sys.executable,
        "test_harness/tools/scan_source_risks.py",
        *args.source_root,
        "--out",
        str(scan_out),
        "--max-findings",
        str(args.source_scan_max_findings),
        "--max-seeds",
        str(args.source_scan_max_seeds),
    ]
    commands.append(run_command("source risk scan", scan_cmd))
    if commands[-1]["ok"]:
        task_cmd = [
            sys.executable,
            "test_harness/tools/build_source_attack_tasks.py",
            str(scan_out),
            "--out",
            str(tasks_out),
            "--max-tasks",
            str(args.source_task_max_tasks),
            "--context-lines",
            str(args.source_task_context_lines),
            "--write-dsl-seeds",
        ]
        commands.append(run_command("source attack task build", task_cmd))
    return {
        "ok": all(command["ok"] for command in commands),
        "commands": commands,
        "source_scan": str(scan_out),
        "source_attack_tasks": str(tasks_out),
    }


def summarize_records(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        stage = str(record.get("stage") or "unknown")
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def diagnostics_from_payload(payload: Any) -> list[dict[str, Any]]:
    diagnostics: list[Any]
    if isinstance(payload, dict):
        diagnostics = payload.get("diagnostics", [])
        if not diagnostics and (payload.get("error_code") or payload.get("code")):
            diagnostics = [payload]
    elif isinstance(payload, list):
        diagnostics = payload
    else:
        diagnostics = []
    if not isinstance(diagnostics, list):
        return []
    return enrich_diagnostics(diagnostics)


def diagnostics_from_path(path_value: str) -> list[dict[str, Any]]:
    if not path_value:
        return []
    path = Path(path_value)
    if not path.is_file():
        return []
    try:
        loaded = read_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    return diagnostics_from_payload(loaded)


def error_diagnostics(diagnostics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in diagnostics:
        severity = str(item.get("severity") or "error").lower()
        if severity not in {"error", "blocker"}:
            continue
        result.append(item)
    return result


def unique_diagnostic_values(diagnostics: list[dict[str, Any]], key: str, *, errors_only: bool = True) -> list[str]:
    values: list[str] = []
    source = error_diagnostics(diagnostics) if errors_only else diagnostics
    for item in source:
        value = item.get(key)
        if not value and key == "error_code":
            value = item.get("code")
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return values


def normalizer_diagnostics(model: dict[str, Any]) -> list[dict[str, Any]]:
    report = model.get("normalizer_report")
    return diagnostics_from_payload(report) if isinstance(report, dict) else []


def normalizer_error_codes(model: dict[str, Any]) -> list[str]:
    return unique_diagnostic_values(normalizer_diagnostics(model), "error_code")


def diagnostics_error_codes(path_value: str) -> list[str]:
    return unique_diagnostic_values(diagnostics_from_path(path_value), "error_code")


def matrix_empty_bucket() -> dict[str, int]:
    return {
        "forms": 0,
        "saved_outputs": 0,
        "missing_outputs": 0,
        "normalized_ok": 0,
        "normalized_failed": 0,
        "gate_attempted": 0,
        "gate_passed": 0,
        "gate_failed": 0,
        "gate_skipped": 0,
        "executed": 0,
        "execute_passed": 0,
        "execute_failed": 0,
        "execute_skipped": 0,
        "needs_harness_extension": 0,
        "campaign_request": 0,
        "saved_repair_outputs": 0,
        "saved_repair_gate_passed": 0,
        "saved_repair_gate_failed": 0,
    }


def add_matrix_row(bucket: dict[str, int], row: dict[str, Any]) -> None:
    bucket["forms"] += 1
    if row["saved_output"]:
        bucket["saved_outputs"] += 1
    else:
        bucket["missing_outputs"] += 1
    if row["normalized_ok"]:
        bucket["normalized_ok"] += 1
    elif row["saved_output"]:
        bucket["normalized_failed"] += 1
    if row["gate_attempted"]:
        bucket["gate_attempted"] += 1
        if row["gate_skipped"]:
            bucket["gate_skipped"] += 1
        elif row["gate_ok"]:
            bucket["gate_passed"] += 1
        else:
            bucket["gate_failed"] += 1
    if row["execute_attempted"]:
        bucket["executed"] += 1
        if row["execute_skipped"]:
            bucket["execute_skipped"] += 1
        elif row["execute_ok"]:
            bucket["execute_passed"] += 1
        else:
            bucket["execute_failed"] += 1
    if row["output_kind"] == "needs_harness_extension":
        bucket["needs_harness_extension"] += 1
    if row["output_kind"] == "campaign_request":
        bucket["campaign_request"] += 1
    if row.get("provenance_repair_output"):
        bucket["saved_repair_outputs"] += 1
        if row["gate_attempted"] and row["gate_ok"]:
            bucket["saved_repair_gate_passed"] += 1
        elif row["gate_attempted"] and not row["gate_skipped"]:
            bucket["saved_repair_gate_failed"] += 1


def build_model_output_matrix(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize saved model JSON only; this never generates or repairs output."""

    counts = matrix_empty_bucket()
    by_stage: dict[str, int] = {}
    by_output_kind: dict[str, int] = {}
    by_input_kind: dict[str, int] = {}
    by_target_api: dict[str, dict[str, int]] = {}
    by_example_pack: dict[str, dict[str, int]] = {}
    by_interface_family: dict[str, dict[str, int]] = {}
    by_run_profile: dict[str, dict[str, int]] = {}
    by_provenance_source: dict[str, dict[str, int]] = {}
    by_saved_repair_iteration: dict[str, dict[str, int]] = {}
    rows: list[dict[str, Any]] = []
    matrix_diagnostics: list[dict[str, Any]] = []

    for record in records:
        request_id = str(record.get("request_id") or "")
        model = record.get("model_output") if isinstance(record.get("model_output"), dict) else {}
        provenance = model.get("provenance") if isinstance(model.get("provenance"), dict) else {}
        check_result = record.get("check_result") if isinstance(record.get("check_result"), dict) else {}
        execute_result = record.get("execute_result") if isinstance(record.get("execute_result"), dict) else {}
        output_kind = str(model.get("kind") or "not_checked")
        saved_output = bool(model.get("found"))
        normalized_ok = saved_output and output_kind not in {"missing", "invalid", "not_checked"}
        gate_attempted = bool(check_result)
        execute_attempted = bool(execute_result)
        normalizer_items = normalizer_diagnostics(model)
        gate_items = diagnostics_from_path(str(check_result.get("model_diagnostics") or ""))
        combined_diagnostics = normalizer_items + gate_items
        matrix_diagnostics.extend(combined_diagnostics)
        gate_codes = unique_diagnostic_values(gate_items, "error_code")
        source_type = str(provenance.get("source_type") or ("missing_output" if not saved_output else "unknown_saved_output"))
        source_info = provenance_source_metadata(source_type, CAPABILITIES)
        row = {
            "request_id": request_id,
            "target_api": record.get("target_api") or "unknown",
            "geometry_family": record.get("geometry_family") or "unknown",
            "preferred_output": record.get("preferred_output") or "",
            "selected_example_pack": record.get("selected_example_pack") or "",
            "interface_family": record.get("interface_family") or "unknown",
            "run_profile_id": record.get("run_profile_id") or "unknown",
            "stage": record.get("stage") or "unknown",
            "saved_output": saved_output,
            "input_kind": model.get("input_kind") or ("missing" if not saved_output else ""),
            "input_path": model.get("input_path") or "",
            "provenance_found": bool(provenance.get("found")),
            "provenance_path": provenance.get("path") or "",
            "provenance_source_type": source_type,
            "provenance_source_known": bool(source_info.get("known")),
            "provenance_source_category": source_info.get("category") or "unknown",
            "provenance_source_label": provenance.get("source_label") or "",
            "provenance_model": provenance.get("model") or "",
            "provenance_interface": provenance.get("interface") or "",
            "provenance_repair_output": bool(provenance.get("repair_output")),
            "provenance_repair_iteration": int(provenance.get("repair_iteration") or 0),
            "provenance_repair_context_path": provenance.get("repair_context_path") or "",
            "provenance_repair_parent_output_path": provenance.get("repair_parent_output_path") or "",
            "output_kind": output_kind,
            "normalized_ok": normalized_ok,
            "normalizer_error_codes": unique_diagnostic_values(normalizer_items, "error_code"),
            "normalizer_diagnostic_categories": unique_diagnostic_values(normalizer_items, "catalog_category", errors_only=False),
            "normalizer_diagnostic_operator_actions": unique_diagnostic_values(normalizer_items, "operator_action", errors_only=False),
            "normalizer_diagnostic_catalog_coverages": unique_diagnostic_values(normalizer_items, "catalog_coverage", errors_only=False),
            "gate_attempted": gate_attempted,
            "gate_ok": bool(check_result.get("ok")) if gate_attempted else False,
            "gate_skipped": bool(check_result.get("skipped")) if gate_attempted else False,
            "gate_error_codes": gate_codes,
            "gate_diagnostic_categories": unique_diagnostic_values(gate_items, "catalog_category", errors_only=False),
            "gate_diagnostic_operator_actions": unique_diagnostic_values(gate_items, "operator_action", errors_only=False),
            "gate_diagnostic_catalog_coverages": unique_diagnostic_values(gate_items, "catalog_coverage", errors_only=False),
            "diagnostic_catalog_summary": catalog_summary(combined_diagnostics),
            "execute_attempted": execute_attempted,
            "execute_ok": bool(execute_result.get("ok")) if execute_attempted else False,
            "execute_skipped": bool(execute_result.get("skipped")) if execute_attempted else False,
        }
        rows.append(row)
        add_matrix_row(counts, row)

        stage = str(row["stage"])
        by_stage[stage] = by_stage.get(stage, 0) + 1
        by_output_kind[output_kind] = by_output_kind.get(output_kind, 0) + 1
        input_kind = str(row["input_kind"] or "unknown")
        by_input_kind[input_kind] = by_input_kind.get(input_kind, 0) + 1
        api_key = str(row["target_api"] or "unknown")
        pack_key = str(row["selected_example_pack"] or "none")
        family_key = str(row["interface_family"] or "unknown")
        profile_key = str(row["run_profile_id"] or "unknown")
        provenance_key = str(row["provenance_source_type"] or "unknown_saved_output")
        saved_repair_key = str(row["provenance_repair_iteration"]) if row["provenance_repair_output"] else "not_repair"
        api_bucket = by_target_api.setdefault(api_key, matrix_empty_bucket())
        pack_bucket = by_example_pack.setdefault(pack_key, matrix_empty_bucket())
        family_bucket = by_interface_family.setdefault(family_key, matrix_empty_bucket())
        profile_bucket = by_run_profile.setdefault(profile_key, matrix_empty_bucket())
        provenance_bucket = by_provenance_source.setdefault(provenance_key, matrix_empty_bucket())
        saved_repair_bucket = by_saved_repair_iteration.setdefault(saved_repair_key, matrix_empty_bucket())
        add_matrix_row(api_bucket, row)
        add_matrix_row(pack_bucket, row)
        add_matrix_row(family_bucket, row)
        add_matrix_row(profile_bucket, row)
        add_matrix_row(provenance_bucket, row)
        add_matrix_row(saved_repair_bucket, row)

    return {
        "boundary": "saved_json_only_no_model_calls",
        "counts": counts,
        "diagnostic_catalog_summary": catalog_summary(matrix_diagnostics),
        "by_stage": by_stage,
        "by_output_kind": by_output_kind,
        "by_input_kind": by_input_kind,
        "by_target_api": by_target_api,
        "by_example_pack": by_example_pack,
        "by_interface_family": by_interface_family,
        "by_run_profile": by_run_profile,
        "by_provenance_source": by_provenance_source,
        "by_saved_repair_iteration": by_saved_repair_iteration,
        "rows": rows,
    }


def markdown_bucket_table(title: str, buckets: dict[str, dict[str, int]]) -> list[str]:
    if not buckets:
        return []
    lines = [f"### {title}", "", "| key | forms | saved | normalized | gate passed | gate failed | extension | pending |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for key, bucket in sorted(buckets.items()):
        lines.append(
            f"| `{key}` | {bucket.get('forms', 0)} | {bucket.get('saved_outputs', 0)} | "
            f"{bucket.get('normalized_ok', 0)} | {bucket.get('gate_passed', 0)} | "
            f"{bucket.get('gate_failed', 0)} | {bucket.get('needs_harness_extension', 0)} | "
            f"{bucket.get('missing_outputs', 0)} |"
        )
    lines.append("")
    return lines


def markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Interface Distillation Campaign",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Manifest: `{summary.get('manifest')}`",
        f"- Output root: `{summary.get('out_root')}`",
        f"- Execute enabled: `{summary.get('execute')}`",
        "",
        "## Stage Counts",
        "",
    ]
    for stage, count in sorted((summary.get("stage_counts") or {}).items()):
        lines.append(f"- `{stage}`: `{count}`")
    matrix = summary.get("model_output_matrix") if isinstance(summary.get("model_output_matrix"), dict) else {}
    if matrix:
        counts = matrix.get("counts") if isinstance(matrix.get("counts"), dict) else {}
        lines.extend(
            [
                "",
                "## Model Output Matrix",
                "",
                f"- Boundary: `{matrix.get('boundary')}`",
                f"- Forms: `{counts.get('forms', 0)}`",
                f"- Saved outputs: `{counts.get('saved_outputs', 0)}`",
                f"- Missing outputs: `{counts.get('missing_outputs', 0)}`",
                f"- Normalized ok: `{counts.get('normalized_ok', 0)}`",
                f"- Normalized failed: `{counts.get('normalized_failed', 0)}`",
                f"- Gate passed: `{counts.get('gate_passed', 0)}`",
                f"- Gate failed: `{counts.get('gate_failed', 0)}`",
                f"- Saved repair outputs: `{counts.get('saved_repair_outputs', 0)}`",
                f"- Saved repair gate passed: `{counts.get('saved_repair_gate_passed', 0)}`",
                f"- Saved repair gate failed: `{counts.get('saved_repair_gate_failed', 0)}`",
                f"- Needs harness extension: `{counts.get('needs_harness_extension', 0)}`",
                "",
            ]
        )
        diagnostic_summary = matrix.get("diagnostic_catalog_summary") if isinstance(matrix.get("diagnostic_catalog_summary"), dict) else {}
        if diagnostic_summary:
            lines.extend(
                [
                    "### Diagnostic Catalog Summary",
                    "",
                    f"- coverage: `{diagnostic_summary.get('coverage', {})}`",
                    f"- categories: `{diagnostic_summary.get('categories', {})}`",
                    f"- operator_actions: `{diagnostic_summary.get('operator_actions', {})}`",
                    f"- uncataloged_count: `{diagnostic_summary.get('uncataloged_count', 0)}`",
                    "",
                ]
            )
        by_api = matrix.get("by_target_api") if isinstance(matrix.get("by_target_api"), dict) else {}
        by_pack = matrix.get("by_example_pack") if isinstance(matrix.get("by_example_pack"), dict) else {}
        by_family = matrix.get("by_interface_family") if isinstance(matrix.get("by_interface_family"), dict) else {}
        by_profile = matrix.get("by_run_profile") if isinstance(matrix.get("by_run_profile"), dict) else {}
        by_provenance = matrix.get("by_provenance_source") if isinstance(matrix.get("by_provenance_source"), dict) else {}
        by_saved_repair = matrix.get("by_saved_repair_iteration") if isinstance(matrix.get("by_saved_repair_iteration"), dict) else {}
        lines.extend(markdown_bucket_table("By Interface Family", by_family))
        lines.extend(markdown_bucket_table("By Run Profile", by_profile))
        lines.extend(markdown_bucket_table("By Provenance Source", by_provenance))
        lines.extend(markdown_bucket_table("By Saved Repair Iteration", by_saved_repair))
        lines.extend(markdown_bucket_table("By Target API", by_api))
        lines.extend(markdown_bucket_table("By Example Pack", by_pack))
    matrix_rows_by_request: dict[str, dict[str, Any]] = {}
    if matrix and isinstance(matrix.get("rows"), list):
        for row in matrix["rows"]:
            if isinstance(row, dict) and row.get("request_id"):
                matrix_rows_by_request[str(row["request_id"])] = row
    lines.extend(["", "## Forms", ""])
    for record in summary.get("records", []):
        model = record.get("model_output") or {}
        check_result = record.get("check_result") or {}
        execute_result = record.get("execute_result") or {}
        provenance = model.get("provenance") if isinstance(model.get("provenance"), dict) else {}
        matrix_row = matrix_rows_by_request.get(str(record.get("request_id") or ""), {})
        lines.append(
            f"- `{record.get('request_id')}` stage=`{record.get('stage')}` api=`{record.get('target_api')}` "
            f"geometry=`{record.get('geometry_family')}` preferred=`{record.get('preferred_output')}` "
            f"family=`{record.get('interface_family', '')}` run_profile=`{record.get('run_profile_id', '')}` "
            f"example_pack=`{record.get('selected_example_pack', '')}` model=`{model.get('kind', '')}` "
            f"source=`{provenance.get('source_type', '')}` "
            f"saved_repair=`{matrix_row.get('provenance_repair_iteration', 0) if matrix_row.get('provenance_repair_output') else 0}` "
            f"check_ok=`{check_result.get('ok', '')}` run_ok=`{execute_result.get('ok', '')}`"
        )
        if record.get("prompt_path"):
            lines.append(f"  prompt: `{record['prompt_path']}`")
        if model.get("input_path"):
            lines.append(f"  model_output: `{model.get('input_path')}`")
        if matrix_row.get("gate_diagnostic_operator_actions") or matrix_row.get("normalizer_diagnostic_operator_actions"):
            actions = sorted(
                set(matrix_row.get("normalizer_diagnostic_operator_actions", []))
                | set(matrix_row.get("gate_diagnostic_operator_actions", []))
            )
            categories = sorted(
                set(matrix_row.get("normalizer_diagnostic_categories", []))
                | set(matrix_row.get("gate_diagnostic_categories", []))
            )
            lines.append(f"  diagnostic_actions: `{actions}` categories: `{categories}`")
        if check_result.get("check_report"):
            lines.append(f"  check_report: `{check_result.get('check_report')}`")
        if check_result.get("compiled_dir"):
            lines.append(f"  compiled_recipes: `{check_result.get('compiled_dir')}`")
        if execute_result.get("triage_report"):
            lines.append(f"  triage: `{execute_result.get('triage_report')}`")
        if execute_result.get("contact_sheet"):
            lines.append(f"  preview: `{execute_result.get('contact_sheet')}`")
    lines.append("")
    if summary.get("api_smoke"):
        lines.extend(["## API Smoke", "", f"- `{summary['api_smoke']}`", ""])
    if summary.get("abc_sample_smoke"):
        lines.extend(["## ABC Sample Smoke", "", f"- `{summary['abc_sample_smoke']}`", ""])
    if summary.get("source_tasks"):
        lines.extend(["## Source Tasks", "", f"- `{summary['source_tasks']}`", ""])
    return "\n".join(lines)


def has_hard_failure(summary: dict[str, Any], require_outputs: bool, fail_on_failures: bool) -> bool:
    stage_counts = summary.get("stage_counts") or {}
    if stage_counts.get("form_error") or stage_counts.get("invalid_model_output"):
        return True
    if require_outputs and stage_counts.get("pending_model_output"):
        return True
    if not fail_on_failures:
        return False
    for key in ("api_smoke", "abc_sample_smoke", "source_tasks"):
        value = summary.get(key)
        if isinstance(value, dict) and value.get("ok") is False:
            return True
    for record in summary.get("records", []):
        check = record.get("check_result")
        if isinstance(check, dict) and check.get("ok") is False and not check.get("skipped"):
            return True
        result = record.get("execute_result")
        if isinstance(result, dict) and result.get("ok") is False and not result.get("skipped"):
            return True
    return False


def main() -> int:
    args = parse_args()
    forms_dir = Path(args.forms_dir)
    manifest_path = Path(args.manifest) if args.manifest else forms_dir / "00_manifest.json"
    out_root = Path(args.out)
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        print(f"manifest must be a JSON object: {manifest_path}", file=sys.stderr)
        return 1

    records = build_tasks(forms_dir, manifest, out_root)
    execute_model_outputs(records, args, out_root)
    api_smoke = run_api_smoke(args, out_root)
    abc_sample_smoke = run_abc_sample(args, manifest, out_root)
    source_tasks = run_source_tasks(args, out_root)
    summary = {
        "generated_at": now_iso_like(),
        "manifest": str(manifest_path),
        "forms_dir": str(forms_dir),
        "out_root": str(out_root),
        "model_output_root": args.model_output_root,
        "model_output_provenance_root": args.model_output_provenance_root,
        "runner": args.runner,
        "execute": args.execute,
        "check_model_outputs": args.check_model_outputs,
        "records": records,
        "stage_counts": summarize_records(records),
        "model_output_matrix": build_model_output_matrix(records),
        "api_smoke": api_smoke,
        "abc_sample_smoke": abc_sample_smoke,
        "source_tasks": source_tasks,
    }
    summary_path = out_root / "interface_distillation_summary.json"
    report_path = out_root / "interface_distillation_report.md"
    write_json(summary_path, summary)
    write_text(report_path, markdown_report(summary))
    print(f"summary={summary_path}")
    print(f"report={report_path}")
    return 1 if has_hard_failure(summary, args.require_model_outputs, args.fail_on_failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
