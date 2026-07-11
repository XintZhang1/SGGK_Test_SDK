#!/usr/bin/env python3
"""Validate interface form readiness for reusable skills or extension flow.

This gate reads the interface intake forms, the capability registry, selected
example packs, and any explicitly supplied extension request reports. It classifies each form
as ready for an existing interface-family skill, expected to produce a
needs_harness_extension request, or requiring a bounded Message API clarification. It does not call
models, generate prompts, run SDK recipes, apply patches, or commit files.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time
from typing import Any

from build_api_test_task import build_task, validate_form
from harness_capabilities import load_capabilities, supported_apis
from validate_harness_extension import validate_file as validate_extension_file


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FORMS_ROOT = REPO_ROOT / "test_harness" / "forms" / "interface_distillation"
DEFAULT_MANIFEST = DEFAULT_FORMS_ROOT / "00_manifest.json"
SEVERITY_RANK = {"info": 0, "test_gap": 1, "risk": 2, "blocker": 3}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Interface form manifest path")
    parser.add_argument("--forms-root", default=str(DEFAULT_FORMS_ROOT), help="Directory containing interface form JSON files")
    parser.add_argument("--form", action="append", default=[], help="Specific form path to validate; repeatable")
    parser.add_argument(
        "--extension-template",
        action="append",
        default=[],
        help="Canonical needs_harness_extension example to validate; repeatable",
    )
    parser.add_argument("--report", default="", help="Optional JSON report path")
    parser.add_argument("--markdown", default="", help="Optional Markdown report path")
    parser.add_argument(
        "--fail-on",
        choices=sorted(SEVERITY_RANK),
        default="blocker",
        help="Return non-zero when findings include this severity or worse.",
    )
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 after writing reports")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def rel_display(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def finding(
    severity: str,
    code: str,
    message: str,
    *,
    form_id: str = "",
    path: Path | str = "",
    field: str = "",
) -> dict[str, Any]:
    path_text = rel_display(path) if isinstance(path, Path) else path
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "form_id": form_id,
        "path": path_text,
        "field": field,
    }


def manifest_form_paths(manifest_path: Path, forms_root: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    try:
        manifest = read_json(manifest_path)
    except FileNotFoundError:
        return [], [finding("blocker", "INTAKE_MANIFEST_MISSING", "Interface form manifest is missing.", path=manifest_path)]
    except json.JSONDecodeError as exc:
        return [], [
            finding(
                "blocker",
                "INTAKE_MANIFEST_INVALID_JSON",
                f"Interface form manifest is invalid JSON: {exc}",
                path=manifest_path,
            )
        ]
    forms = as_list(as_dict(manifest).get("forms"))
    paths: list[Path] = []
    for index, item in enumerate(forms):
        raw = as_dict(item).get("form")
        if not isinstance(raw, str) or not raw:
            findings.append(
                finding(
                    "blocker",
                    "INTAKE_MANIFEST_FORM_MISSING",
                    "Manifest form entry must include a form filename.",
                    path=manifest_path,
                    field=f"forms[{index}].form",
                )
            )
            continue
        paths.append(forms_root / raw)
    return paths, findings


def load_form(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    try:
        loaded = read_json(path)
    except FileNotFoundError:
        return None, [finding("blocker", "INTAKE_FORM_MISSING", "Interface form file is missing.", path=path)]
    except json.JSONDecodeError as exc:
        return None, [finding("blocker", "INTAKE_FORM_INVALID_JSON", f"Interface form is invalid JSON: {exc}", path=path)]
    if not isinstance(loaded, dict):
        return None, [finding("blocker", "INTAKE_FORM_NOT_OBJECT", "Interface form root must be a JSON object.", path=path)]
    return loaded, []


def warning_code(message: str) -> str:
    if message.startswith("target_api "):
        return "INTAKE_TARGET_API_UNSUPPORTED"
    if message.startswith("unknown oracle "):
        return "INTAKE_ORACLE_UNKNOWN"
    if message.startswith("run_profile "):
        return "INTAKE_RUN_PROFILE_UNKNOWN"
    if message.startswith("case_count "):
        return "INTAKE_CASE_COUNT_OUT_OF_RANGE"
    return "INTAKE_FORM_WARNING"


def warning_severity(message: str) -> str:
    code = warning_code(message)
    if code == "INTAKE_TARGET_API_UNSUPPORTED":
        return "info"
    if code == "INTAKE_CASE_COUNT_OUT_OF_RANGE":
        return "info"
    return "risk"


def has_warning(warnings: list[str], prefix: str) -> bool:
    return any(item.startswith(prefix) for item in warnings)


def classify_record(
    *,
    form: dict[str, Any],
    task: dict[str, Any] | None,
    warnings: list[str],
    supported_api_set: set[str],
) -> tuple[str, list[str]]:
    target_api = str(form.get("target_api") or "")
    preferred_output = str(as_dict(task).get("harness_contract", {}).get("preferred_output_for_api") or "")
    selected_pack = str(as_dict(task).get("harness_contract", {}).get("selected_example_pack") or "")
    reasons: list[str] = []

    if target_api not in supported_api_set or preferred_output == "needs_harness_extension":
        reasons.append("target API is outside current runnable capability registry")
        return "needs_harness_extension_expected", reasons

    if has_warning(warnings, "unknown oracle "):
        reasons.append("one or more requested oracles need exact mapping or extension request")
        return "message_api_clarification_required", reasons

    if has_warning(warnings, "run_profile "):
        reasons.append("run_profile metadata is missing from the capability registry")
        return "message_api_clarification_required", reasons

    if not selected_pack and preferred_output != "campaign_request":
        reasons.append("no interface example pack selected for this runnable form")
        return "ready_missing_example_pack", reasons

    return "ready_for_existing_skill", reasons


def record_for_form(path: Path, supported_api_set: set[str]) -> dict[str, Any]:
    loaded, findings = load_form(path)
    if loaded is None:
        return {
            "form_path": rel_display(path),
            "request_id": path.stem,
            "readiness_state": "intake_blocked",
            "findings": findings,
            "boundary": fixed_boundary(),
        }

    errors, warnings = validate_form(loaded)
    form_id = str(loaded.get("request_id") or path.stem)
    for error in errors:
        findings.append(finding("blocker", "INTAKE_FORM_ERROR", error, form_id=form_id, path=path))
    for warning in warnings:
        findings.append(
            finding(
                warning_severity(warning),
                warning_code(warning),
                warning,
                form_id=form_id,
                path=path,
            )
        )

    task: dict[str, Any] | None = None
    task_error = ""
    if not errors:
        try:
            task = build_task(path.resolve(), loaded, warnings)
        except Exception as exc:  # pragma: no cover - defensive gate output.
            task_error = str(exc)
            findings.append(
                finding(
                    "blocker",
                    "INTAKE_TASK_BUILD_FAILED",
                    f"build_api_test_task failed while building fixed task context: {exc}",
                    form_id=form_id,
                    path=path,
                )
            )

    readiness_state = "intake_blocked"
    state_reasons: list[str] = []
    if not errors and not task_error:
        readiness_state, state_reasons = classify_record(
            form=loaded,
            task=task,
            warnings=warnings,
            supported_api_set=supported_api_set,
        )
        if readiness_state == "ready_missing_example_pack":
            findings.append(
                finding(
                    "risk",
                    "INTAKE_EXAMPLE_PACK_MISSING",
                    "Runnable form did not select an interface example pack.",
                    form_id=form_id,
                    path=path,
                    field="example_pack",
                )
            )

    harness_contract = as_dict(as_dict(task).get("harness_contract")) if task else {}
    run_profile = as_dict(harness_contract.get("run_profile"))
    example_pack = as_dict(task.get("example_pack")) if isinstance(task, dict) else {}
    return {
        "form_path": rel_display(path),
        "request_id": form_id,
        "target_api": str(loaded.get("target_api") or ""),
        "target_api_known": str(loaded.get("target_api") or "") in supported_api_set,
        "preferred_output_for_api": str(harness_contract.get("preferred_output_for_api") or ""),
        "interface_family": str(harness_contract.get("interface_family") or ""),
        "run_profile_id": str(loaded.get("run_profile") or ""),
        "run_profile_known": bool(run_profile.get("known")),
        "selected_example_pack": str(harness_contract.get("selected_example_pack") or ""),
        "example_pack_manifest_path": str(example_pack.get("manifest_path") or ""),
        "example_pack_contract_kinds": string_list(example_pack.get("contract_kinds")),
        "readiness_state": readiness_state,
        "state_reasons": state_reasons,
        "warnings": warnings,
        "findings": findings,
        "boundary": fixed_boundary(),
    }


def fixed_boundary() -> dict[str, Any]:
    return {
        "model_calls": False,
        "direct_api_calls": False,
        "runs_sdk": False,
        "applies_patches": False,
        "commits_changes": False,
        "production_flow": "interface_intake_readiness_only",
    }


def extension_template_paths(raw_paths: list[str]) -> list[Path]:
    return [repo_path(item) for item in raw_paths]


def extension_template_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        result = validate_extension_file(path)
        diagnostics = as_list(result.get("diagnostics"))
        records.append(
            {
                "path": rel_display(path),
                "ok": bool(result.get("ok")),
                "diagnostic_count": len(diagnostics),
                "error_count": sum(1 for item in diagnostics if as_dict(item).get("severity") == "error"),
                "warning_count": sum(1 for item in diagnostics if as_dict(item).get("severity") == "warning"),
                "diagnostics": diagnostics,
            }
        )
    return records


def template_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for record in records:
        path = str(record.get("path") or "")
        for item in as_list(record.get("diagnostics")):
            diag = as_dict(item)
            severity = str(diag.get("severity") or "warning")
            mapped_severity = "blocker" if severity == "error" else "test_gap"
            findings.append(
                finding(
                    mapped_severity,
                    str(diag.get("error_code") or "EXTENSION_TEMPLATE_DIAGNOSTIC"),
                    str(diag.get("message") or "Extension template diagnostic."),
                    path=path,
                    field=str(diag.get("path") or ""),
                )
            )
    return findings


def severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in findings:
        counter[str(item.get("severity") or "risk")] += 1
    return {severity: int(counter.get(severity, 0)) for severity in SEVERITY_RANK}


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    capabilities = load_capabilities()
    supported_api_set = set(supported_apis(capabilities))
    forms_root = repo_path(args.forms_root)
    manifest_path = repo_path(args.manifest)
    if args.form:
        form_paths = [repo_path(item) for item in args.form]
        manifest_findings: list[dict[str, Any]] = []
    else:
        form_paths, manifest_findings = manifest_form_paths(manifest_path, forms_root)
    records = [record_for_form(path, supported_api_set) for path in form_paths]
    extension_records = extension_template_records(extension_template_paths(args.extension_template))
    findings = list(manifest_findings)
    findings.extend(item for record in records for item in as_list(record.get("findings")))
    findings.extend(template_findings(extension_records))
    readiness_counts = Counter(str(record.get("readiness_state") or "unknown") for record in records)
    by_interface_family = Counter(str(record.get("interface_family") or "unknown") for record in records)
    by_target_api = Counter(str(record.get("target_api") or "unknown") for record in records)
    counts = severity_counts(findings)
    return {
        "schema_version": 1,
        "generated_at": now_iso_like(),
        "ok": counts["blocker"] == 0,
        "boundary": fixed_boundary(),
        "inputs": {
            "manifest": rel_display(manifest_path),
            "forms_root": rel_display(forms_root),
            "forms": [rel_display(path) for path in form_paths],
            "extension_templates": [record.get("path") for record in extension_records],
        },
        "record_count": len(records),
        "readiness_counts": dict(sorted(readiness_counts.items())),
        "by_interface_family": dict(sorted(by_interface_family.items())),
        "by_target_api": dict(sorted(by_target_api.items())),
        "extension_template_count": len(extension_records),
        "extension_templates": extension_records,
        "records": records,
        "counts": counts,
        "findings": findings,
    }


def report_ok(report: dict[str, Any], fail_on: str) -> bool:
    threshold = SEVERITY_RANK[fail_on]
    counts = as_dict(report.get("counts"))
    return all(int(counts.get(severity, 0)) == 0 for severity, rank in SEVERITY_RANK.items() if rank >= threshold)


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Interface Intake Readiness",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- OK: `{report.get('ok')}`",
        f"- Forms: `{report.get('record_count')}`",
        f"- Counts: `{report.get('counts')}`",
        f"- Readiness: `{report.get('readiness_counts')}`",
        "",
        "## Boundary",
        "",
        "- This gate is read-only.",
        "- It classifies saved form metadata against the capability registry and example packs.",
        "- Unsupported APIs, builders, or oracles must route to `needs_harness_extension` rather than being forced through runnable gates.",
        "",
        "## Forms",
        "",
        "| form | api | family | profile | example pack | preferred | readiness |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in as_list(report.get("records")):
        if not isinstance(record, dict):
            continue
        lines.append(
            f"| `{record.get('request_id')}` | `{record.get('target_api')}` | `{record.get('interface_family')}` | "
            f"`{record.get('run_profile_id')}` | `{record.get('selected_example_pack')}` | "
            f"`{record.get('preferred_output_for_api')}` | `{record.get('readiness_state')}` |"
        )
    lines.extend(["", "## Extension Templates", "", "| template | ok | errors | warnings |", "| --- | --- | ---: | ---: |"])
    for record in as_list(report.get("extension_templates")):
        if not isinstance(record, dict):
            continue
        lines.append(
            f"| `{record.get('path')}` | `{record.get('ok')}` | {record.get('error_count')} | {record.get('warning_count')} |"
        )
    findings = as_list(report.get("findings"))
    if findings:
        lines.extend(["", "## Findings", "", "| severity | code | form | path | field | message |", "| --- | --- | --- | --- | --- | --- |"])
        for item in findings:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"| `{item.get('severity')}` | `{item.get('code')}` | `{item.get('form_id') or ''}` | "
                f"`{item.get('path') or ''}` | `{item.get('field') or ''}` | {item.get('message')} |"
            )
    else:
        lines.extend(["", "## Findings", "", "- None."])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        report = build_report(args)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "generated_at": now_iso_like(),
            "ok": False,
            "boundary": fixed_boundary(),
            "record_count": 0,
            "readiness_counts": {},
            "counts": {"info": 0, "test_gap": 0, "risk": 0, "blocker": 1},
            "findings": [
                finding("blocker", "INTAKE_READINESS_GATE_FAILED", f"Interface intake readiness gate failed: {exc}")
            ],
        }
    report["ok"] = report_ok(report, args.fail_on)
    if args.report:
        write_json(repo_path(args.report), report)
    if args.markdown:
        write_text(repo_path(args.markdown), markdown_report(report))
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "record_count": report.get("record_count", 0),
                "readiness_counts": report.get("readiness_counts", {}),
                "counts": report.get("counts", {}),
            },
            indent=2,
        )
    )
    return 0 if report["ok"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
