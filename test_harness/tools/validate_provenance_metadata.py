#!/usr/bin/env python3
"""Validate saved-output and regression-asset provenance metadata.

This is a fixed local gate for audit metadata only. It reads saved provenance
JSON and asset manifests, then writes reports. It never calls a model API,
normalizes model output, runs the SDK, applies patches, or commits files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time
from typing import Any

from harness_capabilities import load_capabilities, provenance_source_metadata, provenance_source_types


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPABILITIES = REPO_ROOT / "test_harness" / "interface_capabilities.json"
DEFAULT_MODEL_OUTPUT_PROVENANCE_ROOT = REPO_ROOT / "artifacts" / "model_output_provenance"
SEVERITY_RANK = {"info": 0, "test_gap": 1, "risk": 2, "blocker": 3}
SENSITIVE_KEY_NAMES = {
    "apikey",
    "authorization",
    "bearer",
    "secret",
    "token",
    "accesstoken",
    "refreshtoken",
}
SECRET_VALUE_PATTERN = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._~+/=-]{16,})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capabilities", default=str(DEFAULT_CAPABILITIES), help="interface_capabilities.json path")
    parser.add_argument(
        "--model-output-provenance-root",
        default=str(DEFAULT_MODEL_OUTPUT_PROVENANCE_ROOT),
        help="Directory containing saved-output provenance sidecars.",
    )
    parser.add_argument(
        "--no-model-output-provenance-root",
        action="store_true",
        help="Do not scan the default saved-output provenance sidecar directory.",
    )
    parser.add_argument("--asset", action="append", default=[], help="Regression asset directory or asset_manifest.json")
    parser.add_argument("--path", action="append", default=[], help="Additional provenance JSON path")
    parser.add_argument(
        "--path-context",
        choices=["model_output", "regression_asset"],
        default="model_output",
        help="Context used for --path records.",
    )
    parser.add_argument("--report", default="", help="Optional JSON report path")
    parser.add_argument("--markdown", default="", help="Optional Markdown report path")
    parser.add_argument(
        "--fail-on",
        choices=sorted(SEVERITY_RANK),
        default="blocker",
        help="Return non-zero when findings include this severity or worse.",
    )
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


def repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def finding(severity: str, code: str, message: str, *, path: str = "", context: str = "", source_type: str = "") -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "path": path,
        "context": context,
        "source_type": source_type,
    }


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def collect_secret_findings(value: Any, path: str, context: str, source_type: str, trail: str = "$") -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            next_trail = f"{trail}.{key_text}"
            if normalized_key(key_text) in SENSITIVE_KEY_NAMES:
                findings.append(
                    finding(
                        "blocker",
                        "PROVENANCE_SECRET_KEY",
                        f"Provenance metadata contains sensitive-looking key {next_trail}.",
                        path=path,
                        context=context,
                        source_type=source_type,
                    )
                )
            findings.extend(collect_secret_findings(item, path, context, source_type, next_trail))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(collect_secret_findings(item, path, context, source_type, f"{trail}[{index}]"))
    elif isinstance(value, str) and SECRET_VALUE_PATTERN.search(value):
        findings.append(
            finding(
                "blocker",
                "PROVENANCE_SECRET_VALUE",
                f"Provenance metadata contains secret-looking text at {trail}.",
                path=path,
                context=context,
                source_type=source_type,
            )
        )
    return findings


def truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def validate_boundary_flags(
    data: dict[str, Any],
    path: str,
    context: str,
    source_type: str,
    source_category: str,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    boundary = as_dict(data.get("boundary"))
    message_api_source = source_category in {"message_api", "message_api_test"}
    blocked_flags = [
        "runs_sdk",
        "executes_commands",
        "applies_patches",
        "commits_changes",
        "production_harness_dependency",
        "wired_into_harness",
    ]
    if not message_api_source:
        blocked_flags.extend(["direct_api_calls", "model_calls"])
    for key in blocked_flags:
        raw_value = boundary.get(key, data.get(key))
        if truthy_flag(raw_value):
            findings.append(
                finding(
                    "blocker",
                    "PROVENANCE_BOUNDARY_VIOLATION",
                    f"Provenance metadata sets {key}=true; fixed harness metadata must remain saved-file-only.",
                    path=path,
                    context=context,
                    source_type=source_type,
                )
            )
    if message_api_source:
        for key in ("model_calls", "direct_api_calls"):
            if not truthy_flag(boundary.get(key, data.get(key))):
                findings.append(
                    finding(
                        "blocker",
                        "PROVENANCE_MESSAGE_API_BOUNDARY_INCOMPLETE",
                        f"Message API provenance must record {key}=true.",
                        path=path,
                        context=context,
                        source_type=source_type,
                    )
                )
    return findings


def positive_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def validate_repair_provenance(
    data: dict[str, Any],
    path: str,
    context: str,
    source_type: str,
    source_category: str,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    repair = data.get("repair")
    if repair is None:
        return findings
    if not isinstance(repair, dict):
        return [
            finding(
                "blocker",
                "MODEL_OUTPUT_REPAIR_METADATA_INVALID",
                "Model-output repair metadata must be a JSON object.",
                path=path,
                context=context,
                source_type=source_type,
            )
        ]
    if not truthy_flag(repair.get("is_repair_output")):
        return findings
    if not positive_int(repair.get("iteration")):
        findings.append(
            finding(
                "blocker",
                "MODEL_OUTPUT_REPAIR_ITERATION_INVALID",
                "Repair output metadata must record a positive integer iteration.",
                path=path,
                context=context,
                source_type=source_type,
            )
        )
    if source_category in {"message_api", "message_api_test"}:
        if not positive_int(repair.get("parent_attempt")):
            findings.append(
                finding(
                    "blocker",
                    "MODEL_OUTPUT_REPAIR_PARENT_MISSING",
                    "Message API repair provenance must record a positive parent_attempt.",
                    path=path,
                    context=context,
                    source_type=source_type,
                )
            )
    else:
        if not string_value(repair.get("repair_context_path")):
            findings.append(
                finding(
                    "blocker",
                    "MODEL_OUTPUT_REPAIR_CONTEXT_MISSING",
                    "Repair output metadata must record repair_context_path.",
                    path=path,
                    context=context,
                    source_type=source_type,
                )
            )
        if not string_value(repair.get("parent_output_path")):
            findings.append(
                finding(
                    "test_gap",
                    "MODEL_OUTPUT_REPAIR_PARENT_MISSING",
                    "Repair output metadata should record parent_output_path for audit.",
                    path=path,
                    context=context,
                    source_type=source_type,
                )
            )
    repair_boundary = as_dict(repair.get("boundary"))
    for key in ("direct_api_calls", "model_calls", "runs_sdk", "applies_patches", "commits_changes", "wired_into_harness"):
        if truthy_flag(repair_boundary.get(key)):
            findings.append(
                finding(
                    "blocker",
                    "MODEL_OUTPUT_REPAIR_BOUNDARY_VIOLATION",
                    f"Repair metadata sets {key}=true; repair outputs must remain saved gateway artifacts consumed by fixed gates.",
                    path=path,
                    context=context,
                    source_type=source_type,
                )
            )
    return findings


def validate_model_output_fields(
    data: dict[str, Any],
    path: str,
    context: str,
    source_type: str,
    source_category: str,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if not string_value(data.get("request_id")):
        findings.append(
            finding(
                "blocker",
                "MODEL_OUTPUT_REQUEST_ID_MISSING",
                "Model-output provenance sidecars must record request_id.",
                path=path,
                context=context,
                source_type=source_type,
            )
        )
    if not string_value(data.get("output_path")):
        findings.append(
            finding(
                "test_gap",
                "MODEL_OUTPUT_PATH_MISSING",
                "Model-output provenance should record output_path for audit.",
                path=path,
                context=context,
                source_type=source_type,
            )
        )
    if source_category in {"message_api", "message_api_test"}:
        acceptance = as_dict(data.get("acceptance"))
        fixed_gate = as_dict(data.get("fixed_gate"))
        accepted = (
            acceptance.get("authoring_accepted") is True
            and acceptance.get("accepted_by") == "message_harness_pipeline"
            and fixed_gate.get("ok") is True
        )
        if not accepted:
            findings.append(
                finding(
                    "blocker",
                    "PROVENANCE_AUTHORING_ACCEPTANCE_MISSING",
                    "Message API formal output requires message_harness_pipeline acceptance and a passing fixed gate.",
                    path=path,
                    context=context,
                    source_type=source_type,
                )
            )
    findings.extend(validate_repair_provenance(data, path, context, source_type, source_category))
    return findings


def validate_regression_asset_fields(
    data: dict[str, Any],
    path: str,
    context: str,
    source_type: str,
    source_category: str,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if source_category in {"message_api", "message_api_test"}:
        for key in ("form", "model_output"):
            if not string_value(data.get(key)):
                findings.append(
                    finding(
                        "test_gap",
                        "REGRESSION_ASSET_SOURCE_LINK_MISSING",
                        f"Regression asset provenance should record {key} for model-derived assets.",
                        path=path,
                        context=context,
                        source_type=source_type,
                    )
                )
        if not string_value(data.get("model")):
            findings.append(
                finding(
                    "test_gap",
                    "REGRESSION_ASSET_MODEL_MISSING",
                    "Regression asset provenance should record model for model-derived assets.",
                    path=path,
                    context=context,
                    source_type=source_type,
                )
            )
    return findings


def validate_provenance_object(
    data: Any,
    *,
    context: str,
    path: Path,
    capabilities: dict[str, Any],
    source_location: str = "$",
) -> dict[str, Any]:
    path_text = str(path)
    findings: list[dict[str, str]] = []
    if not isinstance(data, dict):
        findings.append(
            finding(
                "blocker",
                "PROVENANCE_ROOT_INVALID",
                f"Provenance at {source_location} must be a JSON object.",
                path=path_text,
                context=context,
            )
        )
        return {
            "path": path_text,
            "context": context,
            "source_location": source_location,
            "source_type": "invalid_provenance",
            "source_category": "invalid",
            "known_source": True,
            "ok": False,
            "findings": findings,
        }

    source_type = string_value(data.get("source_type"))
    if not source_type:
        source_type = "unspecified_saved_output"
        findings.append(
            finding(
                "risk",
                "PROVENANCE_SOURCE_TYPE_MISSING",
                "Provenance metadata should explicitly set source_type.",
                path=path_text,
                context=context,
                source_type=source_type,
            )
        )
    source_info = provenance_source_metadata(source_type, capabilities)
    source_category = string_value(source_info.get("category")) or "unknown"
    if not source_info.get("known"):
        findings.append(
            finding(
                "blocker",
                "PROVENANCE_SOURCE_TYPE_UNKNOWN",
                "source_type is not registered in interface_capabilities.json.",
                path=path_text,
                context=context,
                source_type=source_type,
            )
        )
    allowed_contexts = [item for item in as_list(source_info.get("allowed_contexts")) if isinstance(item, str)]
    if allowed_contexts and context not in allowed_contexts:
        findings.append(
            finding(
                "blocker",
                "PROVENANCE_CONTEXT_NOT_ALLOWED",
                f"source_type is not allowed in context {context!r}.",
                path=path_text,
                context=context,
                source_type=source_type,
            )
        )
    if not data.get("schema_version"):
        findings.append(
            finding(
                "test_gap",
                "PROVENANCE_SCHEMA_VERSION_MISSING",
                "Provenance metadata should record schema_version.",
                path=path_text,
                context=context,
                source_type=source_type,
            )
        )

    findings.extend(collect_secret_findings(data, path_text, context, source_type))
    findings.extend(validate_boundary_flags(data, path_text, context, source_type, source_category))
    if context == "model_output":
        findings.extend(validate_model_output_fields(data, path_text, context, source_type, source_category))
    if context == "regression_asset":
        findings.extend(validate_regression_asset_fields(data, path_text, context, source_type, source_category))

    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    return {
        "path": path_text,
        "context": context,
        "source_location": source_location,
        "source_type": source_type,
        "source_category": source_category,
        "known_source": bool(source_info.get("known")),
        "allowed_contexts": allowed_contexts,
        "ok": blocker_count == 0,
        "finding_count": len(findings),
        "findings": findings,
    }


def load_json_record(path: Path, context: str, capabilities: dict[str, Any], source_location: str = "$") -> dict[str, Any]:
    try:
        loaded = read_json(path)
    except FileNotFoundError:
        item = finding("blocker", "PROVENANCE_PATH_MISSING", "Provenance JSON path does not exist.", path=str(path), context=context)
        return {
            "path": str(path),
            "context": context,
            "source_location": source_location,
            "source_type": "missing_output",
            "source_category": "synthetic_status",
            "known_source": True,
            "ok": False,
            "finding_count": 1,
            "findings": [item],
        }
    except json.JSONDecodeError as exc:
        item = finding("blocker", "PROVENANCE_JSON_INVALID", f"Invalid JSON: {exc}", path=str(path), context=context)
        return {
            "path": str(path),
            "context": context,
            "source_location": source_location,
            "source_type": "invalid_provenance",
            "source_category": "invalid",
            "known_source": True,
            "ok": False,
            "finding_count": 1,
            "findings": [item],
        }
    return validate_provenance_object(loaded, context=context, path=path, capabilities=capabilities, source_location=source_location)


def scan_model_output_provenance_root(root: Path, capabilities: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not root.exists():
        return [], [
            finding(
                "info",
                "MODEL_OUTPUT_PROVENANCE_ROOT_MISSING",
                "Saved-output provenance root does not exist yet.",
                path=str(root),
                context="model_output",
            )
        ]
    if not root.is_dir():
        return [], [
            finding(
                "blocker",
                "MODEL_OUTPUT_PROVENANCE_ROOT_NOT_DIR",
                "Saved-output provenance root is not a directory.",
                path=str(root),
                context="model_output",
            )
        ]
    records = [load_json_record(path, "model_output", capabilities) for path in sorted(root.glob("*.json"))]
    return records, []


def normalize_asset_manifest_path(raw: str) -> Path:
    path = repo_path(raw)
    return path / "asset_manifest.json" if path.is_dir() else path


def load_asset_records(raw: str, capabilities: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    manifest_path = normalize_asset_manifest_path(raw)
    records: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []
    try:
        manifest = read_json(manifest_path)
    except FileNotFoundError:
        findings.append(
            finding(
                "blocker",
                "ASSET_MANIFEST_MISSING",
                "Regression asset manifest does not exist.",
                path=str(manifest_path),
                context="regression_asset",
            )
        )
        return records, findings
    except json.JSONDecodeError as exc:
        findings.append(
            finding(
                "blocker",
                "ASSET_MANIFEST_JSON_INVALID",
                f"Regression asset manifest is invalid JSON: {exc}",
                path=str(manifest_path),
                context="regression_asset",
            )
        )
        return records, findings

    if isinstance(manifest, dict) and isinstance(manifest.get("provenance"), dict):
        records.append(
            validate_provenance_object(
                manifest["provenance"],
                context="regression_asset",
                path=manifest_path,
                capabilities=capabilities,
                source_location="$.provenance",
            )
        )
    else:
        findings.append(
            finding(
                "risk",
                "ASSET_MANIFEST_PROVENANCE_MISSING",
                "Regression asset manifest should contain a provenance object.",
                path=str(manifest_path),
                context="regression_asset",
            )
        )

    sidecar_path = manifest_path.parent / "asset_provenance.json"
    if sidecar_path.is_file():
        records.append(load_json_record(sidecar_path, "regression_asset", capabilities))
    else:
        findings.append(
            finding(
                "test_gap",
                "ASSET_PROVENANCE_SIDECAR_MISSING",
                "Regression asset should include asset_provenance.json for quick audit.",
                path=str(sidecar_path),
                context="regression_asset",
            )
        )
    if len(records) == 2 and records[0].get("source_type") != records[1].get("source_type"):
        findings.append(
            finding(
                "risk",
                "ASSET_PROVENANCE_SIDECAR_MISMATCH",
                "asset_manifest.provenance and asset_provenance.json disagree on source_type.",
                path=str(sidecar_path),
                context="regression_asset",
            )
        )
    return records, findings


def severity_counts(findings: list[dict[str, str]]) -> dict[str, int]:
    return {severity: sum(1 for item in findings if item.get("severity") == severity) for severity in SEVERITY_RANK}


def should_fail(counts: dict[str, int], fail_on: str) -> bool:
    threshold = SEVERITY_RANK[fail_on]
    return any(count and SEVERITY_RANK[severity] >= threshold for severity, count in counts.items())


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    capabilities_path = Path(args.capabilities)
    capabilities = load_capabilities(capabilities_path)
    source_registry = provenance_source_types(capabilities)
    records: list[dict[str, Any]] = []
    global_findings: list[dict[str, str]] = []

    if not source_registry:
        global_findings.append(
            finding(
                "blocker",
                "PROVENANCE_SOURCE_REGISTRY_MISSING",
                "interface_capabilities.json must contain provenance_source_types.",
                path=str(capabilities_path),
            )
        )

    if not args.no_model_output_provenance_root:
        root_records, root_findings = scan_model_output_provenance_root(Path(args.model_output_provenance_root), capabilities)
        records.extend(root_records)
        global_findings.extend(root_findings)

    for raw_asset in args.asset:
        asset_records, asset_findings = load_asset_records(raw_asset, capabilities)
        records.extend(asset_records)
        global_findings.extend(asset_findings)

    for raw_path in args.path:
        records.append(load_json_record(repo_path(raw_path), args.path_context, capabilities))

    findings = [*global_findings, *[item for record in records for item in as_list(record.get("findings"))]]
    counts = severity_counts(findings)
    by_source_type: dict[str, int] = {}
    by_context: dict[str, int] = {}
    for record in records:
        by_source_type[str(record.get("source_type") or "unknown")] = by_source_type.get(str(record.get("source_type") or "unknown"), 0) + 1
        by_context[str(record.get("context") or "unknown")] = by_context.get(str(record.get("context") or "unknown"), 0) + 1

    return {
        "schema_version": 1,
        "generated_at": now_iso_like(),
        "ok": not should_fail(counts, args.fail_on),
        "fail_on": args.fail_on,
        "capabilities": str(capabilities_path),
        "registered_source_types": sorted(source_registry),
        "record_count": len(records),
        "records": records,
        "findings": findings,
        "counts": counts,
        "by_source_type": dict(sorted(by_source_type.items())),
        "by_context": dict(sorted(by_context.items())),
        "boundary": {
            "model_calls": False,
            "direct_api_calls": False,
            "runs_sdk": False,
            "applies_patches": False,
            "commits_changes": False,
        },
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Provenance Metadata Validation",
        "",
        f"- status: `{'passed' if report.get('ok') else 'failed'}`",
        f"- fail_on: `{report.get('fail_on')}`",
        f"- records: `{report.get('record_count', 0)}`",
        f"- blockers: `{report.get('counts', {}).get('blocker', 0)}`",
        f"- risks: `{report.get('counts', {}).get('risk', 0)}`",
        f"- test_gaps: `{report.get('counts', {}).get('test_gap', 0)}`",
        "",
        "## Sources",
        "",
        "| source_type | records |",
        "| --- | ---: |",
    ]
    for source_type, count in report.get("by_source_type", {}).items():
        lines.append(f"| `{source_type}` | {count} |")
    lines.extend(["", "## Records", "", "| context | source_type | category | ok | path |", "| --- | --- | --- | --- | --- |"])
    for record in report.get("records", []):
        lines.append(
            f"| `{record.get('context')}` | `{record.get('source_type')}` | `{record.get('source_category')}` | "
            f"`{record.get('ok')}` | `{record.get('path')}` |"
        )
    if report.get("findings"):
        lines.extend(["", "## Findings", ""])
        for item in report["findings"]:
            lines.append(
                f"- [{item.get('severity')}] `{item.get('code')}` context=`{item.get('context')}` "
                f"source=`{item.get('source_type')}` path=`{item.get('path')}` {item.get('message')}"
            )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    report = build_report(args)
    if args.report:
        write_json(Path(args.report), report)
    if args.markdown:
        write_text(Path(args.markdown), markdown_report(report))
    print(json.dumps({"ok": report["ok"], "record_count": report["record_count"], "counts": report["counts"]}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
