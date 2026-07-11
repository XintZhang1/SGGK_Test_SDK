#!/usr/bin/env python3
"""Validate the diagnostic catalog against fixed harness tool diagnostics.

This is a read-only intranet gate. It scans source files for structured
diagnostic codes and checks that each code is covered by
test_harness/diagnostic_catalog.json. It does not call a model, run the SDK,
apply patches, or commit files.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = REPO_ROOT / "test_harness" / "diagnostic_catalog.json"
DEFAULT_SCAN_PATHS = [
    "test_harness/tools",
]
DEFAULT_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "artifacts",
    "build",
    "cmake-build-debug",
    "cmake-build-release",
    "node_modules",
}
CODE_KEYS = {"error_code", "code", "finding_code"}
CODE_FUNCTIONS = {
    "add_finding",
    "add_structure_finding",
    "diagnostic",
    "finding",
}
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+$")
DIAGNOSTIC_PREFIX_RE = re.compile(
    r"^(?:"
    r"API|ASSET|ATTACK|BINARY|BOUNDARY|BRITTLE|BUILD|CAPABILITY|CATALOG|CONDITIONALLY|DIAGNOSTIC|DIFF|DIRECT_MODEL|DSL|EMPTY|ERROR|"
    r"EXAMPLE|EXTENSION|EXTRA|FILE|FLAT|HARNESS|INVALID|MISSING|MODEL_OUTPUT|NEGATIVE|NO|NORMALIZED|ORACLE|"
    r"PACK|PATCH|PROPOSAL|PROVENANCE|RECIPE|REGRESSION_ASSET|REPAIR|RUNNER|SCHEMA|SDK|SECRET|SEMANTIC|"
    r"TARGET|TOPOCHECK|UNSAFE|UNKNOWN|UNSUPPORTED|VALIDATOR|WORKSPACE"
    r")_"
)
SEVERITIES = {"blocker", "risk", "error", "warning", "test_gap", "info"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="diagnostic_catalog.json path")
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="File or directory to scan. Repeatable. Defaults to fixed harness tool roots.",
    )
    parser.add_argument("--report", default="", help="Optional JSON report path")
    parser.add_argument("--markdown", default="", help="Optional Markdown report path")
    parser.add_argument(
        "--strict-exact",
        action="store_true",
        help="Report family-only coverage as test_gap findings.",
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


def finding(
    severity: str,
    code: str,
    message: str,
    *,
    path: str = "",
    diagnostic_code: str = "",
) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "path": path,
        "diagnostic_code": diagnostic_code,
    }


def should_skip(path: Path) -> bool:
    return bool(set(path.parts) & DEFAULT_EXCLUDED_PARTS)


def iter_scan_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        path = raw if raw.is_absolute() else REPO_ROOT / raw
        if not path.exists():
            continue
        candidates = [path] if path.is_file() else sorted(item for item in path.rglob("*.py") if item.is_file())
        for candidate in candidates:
            if candidate.suffix.lower() != ".py" or should_skip(candidate):
                continue
            key = str(candidate.resolve())
            if key in seen:
                continue
            seen.add(key)
            files.append(candidate)
    return files


def literal_string(node: ast.AST | None) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def code_param_indexes(tree: ast.AST) -> dict[str, int]:
    indexes: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in CODE_FUNCTIONS:
            continue
        params = [arg.arg for arg in node.args.args]
        if "code" in params:
            indexes[node.name] = params.index("code")
    return indexes


def add_discovered(
    discovered: dict[str, dict[str, Any]],
    code: str,
    *,
    source: str,
    path: Path,
    line: int,
    confidence: str,
) -> None:
    if not CODE_RE.match(code) or not DIAGNOSTIC_PREFIX_RE.match(code):
        return
    record = discovered.setdefault(
        code,
        {
            "code": code,
            "occurrence_count": 0,
            "sources": set(),
            "confidences": set(),
            "locations": [],
        },
    )
    record["occurrence_count"] += 1
    record["sources"].add(source)
    record["confidences"].add(confidence)
    if len(record["locations"]) < 12:
        record["locations"].append(
            {
                "path": rel_display(path),
                "line": line,
                "source": source,
                "confidence": confidence,
            }
        )


def extract_codes_from_tree(path: Path, tree: ast.AST) -> dict[str, dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    local_code_indexes = code_param_indexes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for raw_key, raw_value in zip(node.keys, node.values):
                key = literal_string(raw_key)
                value = literal_string(raw_value)
                if key in CODE_KEYS and value:
                    add_discovered(
                        discovered,
                        value,
                        source=f"dict.{key}",
                        path=path,
                        line=getattr(raw_value, "lineno", getattr(node, "lineno", 0)),
                        confidence="high",
                    )
        elif isinstance(node, ast.Call):
            name = dotted_name(node.func)
            index = local_code_indexes.get(name)
            if index is None or index >= len(node.args):
                continue
            value = literal_string(node.args[index])
            if value:
                add_discovered(
                    discovered,
                    value,
                    source=f"call.{name}",
                    path=path,
                    line=getattr(node.args[index], "lineno", getattr(node, "lineno", 0)),
                    confidence="high",
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            add_discovered(
                discovered,
                node.value,
                source="literal",
                path=path,
                line=getattr(node, "lineno", 0),
                confidence="medium",
            )
    return discovered


def merge_discovered(records: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        for code, item in record.items():
            target = merged.setdefault(
                code,
                {
                    "code": code,
                    "occurrence_count": 0,
                    "sources": set(),
                    "confidences": set(),
                    "locations": [],
                },
            )
            target["occurrence_count"] += int(item["occurrence_count"])
            target["sources"].update(item["sources"])
            target["confidences"].update(item["confidences"])
            remaining = max(0, 12 - len(target["locations"]))
            target["locations"].extend(item["locations"][:remaining])
    return merged


def discover_codes(paths: list[Path]) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], int]:
    findings: list[dict[str, str]] = []
    extracted: list[dict[str, dict[str, Any]]] = []
    files = iter_scan_files(paths)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            findings.append(
                finding(
                    "risk",
                    "DIAGNOSTIC_SOURCE_SCAN_FAILED",
                    f"Could not scan source file: {exc}",
                    path=rel_display(path),
                )
            )
            continue
        extracted.append(extract_codes_from_tree(path, tree))
    return merge_discovered(extracted), findings, len(files)


def compile_families(catalog: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    compiled: list[dict[str, Any]] = []
    raw_families = catalog.get("families")
    if not isinstance(raw_families, list):
        return [], [finding("blocker", "CATALOG_FAMILIES_INVALID", "Catalog families must be a list.")]
    for index, item in enumerate(raw_families):
        path = f"families[{index}]"
        if not isinstance(item, dict):
            findings.append(finding("blocker", "CATALOG_FAMILY_INVALID", "Catalog family must be an object.", path=path))
            continue
        pattern = item.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            findings.append(finding("blocker", "CATALOG_FAMILY_PATTERN_MISSING", "Catalog family pattern is missing.", path=path))
            continue
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            findings.append(finding("blocker", "CATALOG_FAMILY_PATTERN_INVALID", f"Invalid family regex: {exc}", path=path))
            continue
        if item.get("default_severity") not in SEVERITIES:
            findings.append(
                finding(
                    "risk",
                    "CATALOG_FAMILY_SEVERITY_INVALID",
                    "Catalog family default_severity is missing or unknown.",
                    path=path,
                )
            )
        if not isinstance(item.get("repair_hint"), str) or not item["repair_hint"].strip():
            findings.append(finding("risk", "CATALOG_FAMILY_HINT_MISSING", "Catalog family repair_hint is missing.", path=path))
        compiled.append({"pattern": pattern, "regex": regex, "record": item})
    return compiled, findings


def matching_family(code: str, families: list[dict[str, Any]]) -> dict[str, Any] | None:
    for family in families:
        if family["regex"].search(code):
            return family["record"]
    return None


def validate_code_entries(catalog: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    raw_codes = catalog.get("codes")
    if not isinstance(raw_codes, dict):
        return {}, [finding("blocker", "CATALOG_CODES_INVALID", "Catalog codes must be an object.")]
    codes: dict[str, dict[str, Any]] = {}
    for code, item in raw_codes.items():
        path = f"codes.{code}"
        if not isinstance(code, str) or not CODE_RE.match(code):
            findings.append(finding("blocker", "CATALOG_CODE_MALFORMED", "Catalog code key is malformed.", path=path))
            continue
        if not isinstance(item, dict):
            findings.append(finding("blocker", "CATALOG_CODE_ENTRY_INVALID", "Catalog code entry must be an object.", path=path))
            continue
        codes[code] = item
        if item.get("severity") not in SEVERITIES:
            findings.append(finding("risk", "CATALOG_CODE_SEVERITY_INVALID", "Catalog code severity is missing or unknown.", path=path, diagnostic_code=code))
        if not isinstance(item.get("category"), str) or not item["category"].strip():
            findings.append(finding("risk", "CATALOG_CODE_CATEGORY_MISSING", "Catalog code category is missing.", path=path, diagnostic_code=code))
        if not isinstance(item.get("message"), str) or not item["message"].strip():
            findings.append(finding("risk", "CATALOG_CODE_MESSAGE_MISSING", "Catalog code message is missing.", path=path, diagnostic_code=code))
        if not isinstance(item.get("repair_hint"), str) or not item["repair_hint"].strip():
            findings.append(finding("risk", "CATALOG_CODE_HINT_MISSING", "Catalog code repair_hint is missing.", path=path, diagnostic_code=code))
        if not isinstance(item.get("operator_action"), str) or not item["operator_action"].strip():
            findings.append(finding("risk", "CATALOG_CODE_ACTION_MISSING", "Catalog code operator_action is missing.", path=path, diagnostic_code=code))
    return codes, findings


def validate_boundary(catalog: dict[str, Any]) -> list[dict[str, str]]:
    boundary = catalog.get("boundary")
    if not isinstance(boundary, dict):
        return [finding("blocker", "CATALOG_BOUNDARY_MISSING", "Catalog boundary must be present.")]
    findings: list[dict[str, str]] = []
    for key in ("model_calls", "direct_api_calls", "runs_sdk", "applies_patches", "commits_changes"):
        if boundary.get(key) is not False:
            findings.append(
                finding(
                    "blocker",
                    "CATALOG_BOUNDARY_FLAG_NOT_FALSE",
                    f"Catalog boundary {key} must be false.",
                    path=f"boundary.{key}",
                )
            )
    if boundary.get("model_authoring_role") != "message_api_gateway_only":
        findings.append(
            finding(
                "risk",
                "CATALOG_AUTHORING_ROLE_INVALID",
                "Catalog must route model authoring through the Message API gateway outside fixed harness execution.",
                path="boundary.model_authoring_role",
            )
        )
    return findings


def normalize_discovered_for_report(discovered: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in sorted(discovered):
        item = discovered[code]
        rows.append(
            {
                "code": code,
                "occurrence_count": item["occurrence_count"],
                "sources": sorted(item["sources"]),
                "confidences": sorted(item["confidences"]),
                "locations": item["locations"],
            }
        )
    return rows


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    catalog_path = repo_path(args.catalog)
    findings: list[dict[str, str]] = []
    try:
        catalog = read_json(catalog_path)
    except (OSError, json.JSONDecodeError) as exc:
        catalog = {}
        findings.append(finding("blocker", "CATALOG_READ_FAILED", f"Could not read catalog: {exc}", path=rel_display(catalog_path)))
    if not isinstance(catalog, dict):
        catalog = {}
        findings.append(finding("blocker", "CATALOG_ROOT_INVALID", "Catalog root must be a JSON object.", path=rel_display(catalog_path)))

    findings.extend(validate_boundary(catalog))
    families, family_findings = compile_families(catalog)
    findings.extend(family_findings)
    exact_codes, code_findings = validate_code_entries(catalog)
    findings.extend(code_findings)

    scan_paths = [repo_path(path) for path in (args.path or DEFAULT_SCAN_PATHS)]
    discovered, scan_findings, scanned_file_count = discover_codes(scan_paths)
    findings.extend(scan_findings)

    exact_count = 0
    family_only: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    for code, item in discovered.items():
        if code in exact_codes:
            exact_count += 1
            continue
        family = matching_family(code, families)
        if family is None:
            uncovered.append(item)
            findings.append(
                finding(
                    "risk",
                    "DIAGNOSTIC_CODE_UNCATALOGED",
                    "Discovered diagnostic code is not covered by an exact catalog entry or family default.",
                    diagnostic_code=code,
                    path=item["locations"][0]["path"] if item["locations"] else "",
                )
            )
        else:
            family_only.append(
                {
                    "code": code,
                    "family_pattern": family.get("pattern", ""),
                    "category": family.get("category", ""),
                    "occurrence_count": item.get("occurrence_count", 0),
                    "locations": item.get("locations", []),
                }
            )
            if args.strict_exact:
                findings.append(
                    finding(
                        "test_gap",
                        "DIAGNOSTIC_CODE_FAMILY_ONLY",
                        "Discovered diagnostic code is covered only by a family default; promote it if it becomes common.",
                        diagnostic_code=code,
                        path=item["locations"][0]["path"] if item["locations"] else "",
                    )
                )

    unused_exact = sorted(code for code in exact_codes if code not in discovered)
    for code in unused_exact:
        findings.append(
            finding(
                "info",
                "CATALOG_CODE_UNUSED_IN_SCAN",
                "Exact catalog entry was not found in the scanned source paths.",
                diagnostic_code=code,
                path=f"codes.{code}",
            )
        )

    counts = {severity: sum(1 for item in findings if item["severity"] == severity) for severity in ("blocker", "risk", "error", "warning", "test_gap", "info")}
    return {
        "schema_version": 1,
        "generated_at": now_iso_like(),
        "ok": counts["blocker"] == 0 and counts["risk"] == 0 and counts["error"] == 0,
        "catalog": rel_display(catalog_path),
        "scan_paths": [rel_display(path) for path in scan_paths],
        "scanned_file_count": scanned_file_count,
        "discovered_code_count": len(discovered),
        "exact_covered_count": exact_count,
        "family_only_count": len(family_only),
        "uncataloged_count": len(uncovered),
        "unused_exact_count": len(unused_exact),
        "family_only_codes": sorted(family_only, key=lambda item: item["code"]),
        "uncataloged_codes": normalize_discovered_for_report({item["code"]: item for item in uncovered}),
        "unused_exact_codes": unused_exact,
        "discovered_codes": normalize_discovered_for_report(discovered),
        "findings": findings,
        "counts": counts,
        "boundary": {
            "model_calls": False,
            "direct_api_calls": False,
            "runs_sdk": False,
            "applies_patches": False,
            "commits_changes": False,
            "model_authoring_role": "message_api_gateway_only",
        },
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Diagnostic Catalog Validation",
        "",
        f"- status: `{'passed' if report.get('ok') else 'failed'}`",
        f"- catalog: `{report.get('catalog')}`",
        f"- scanned_files: `{report.get('scanned_file_count', 0)}`",
        f"- discovered_codes: `{report.get('discovered_code_count', 0)}`",
        f"- exact_covered: `{report.get('exact_covered_count', 0)}`",
        f"- family_only: `{report.get('family_only_count', 0)}`",
        f"- uncataloged: `{report.get('uncataloged_count', 0)}`",
        f"- blockers: `{report.get('counts', {}).get('blocker', 0)}`",
        f"- risks: `{report.get('counts', {}).get('risk', 0)}`",
        "",
        "## Boundary",
        "",
        "- This validator only scans saved source files and catalog metadata.",
        "- It does not call a Message API; authoring transport belongs to the separate gateway.",
        "- Family-only coverage is acceptable as a bridge; frequent real failures should become exact entries.",
    ]
    if report.get("uncataloged_codes"):
        lines.extend(["", "## Uncataloged Codes", ""])
        for item in report["uncataloged_codes"]:
            first = item.get("locations", [{}])[0]
            lines.append(f"- `{item.get('code')}` first_seen=`{first.get('path', '')}:{first.get('line', 0)}`")
    if report.get("family_only_codes"):
        lines.extend(["", "## Family-Only Codes", ""])
        for item in report["family_only_codes"][:80]:
            lines.append(f"- `{item.get('code')}` via `{item.get('family_pattern')}`")
        if len(report["family_only_codes"]) > 80:
            lines.append(f"- ... {len(report['family_only_codes']) - 80} more")
    if report.get("findings"):
        lines.extend(["", "## Findings", ""])
        for item in report["findings"]:
            diagnostic_code = f" diagnostic=`{item.get('diagnostic_code')}`" if item.get("diagnostic_code") else ""
            lines.append(f"- [{item.get('severity')}] `{item.get('code')}`{diagnostic_code}: {item.get('message')}")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    report = build_report(args)
    if args.report:
        write_json(repo_path(args.report), report)
    if args.markdown:
        write_text(repo_path(args.markdown), markdown_report(report))
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "discovered_code_count": report["discovered_code_count"],
                "exact_covered_count": report["exact_covered_count"],
                "family_only_count": report["family_only_count"],
                "uncataloged_count": report["uncataloged_count"],
            },
            indent=2,
        )
    )
    return 0 if report["ok"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
