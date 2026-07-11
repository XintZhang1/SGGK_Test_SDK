#!/usr/bin/env python3
"""Validate structured interface example-pack manifests.

This is a fixed local gate. It reads only repository files and never calls a
model, executes SDK code, applies patches, or writes source files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any

from validate_recipe import validate_file as validate_recipe_file


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPABILITIES = REPO_ROOT / "test_harness" / "interface_capabilities.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capabilities", default=str(DEFAULT_CAPABILITIES), help="interface_capabilities.json path")
    parser.add_argument("--report", default="", help="Optional JSON report path")
    parser.add_argument("--markdown", default="", help="Optional Markdown report path")
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


def repo_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def finding(severity: str, code: str, message: str, *, pack_id: str = "", path: str = "") -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "pack_id": pack_id,
        "path": path,
    }


def validate_json_file(path: Path, pack_id: str, findings: list[dict[str, str]]) -> None:
    if not path.is_file():
        findings.append(finding("blocker", "EXAMPLE_PATH_MISSING", "Example JSON path does not exist.", pack_id=pack_id, path=str(path)))
        return
    try:
        read_json(path)
    except json.JSONDecodeError as exc:
        findings.append(finding("blocker", "EXAMPLE_JSON_INVALID", f"Example JSON is invalid: {exc}", pack_id=pack_id, path=str(path)))


def validate_pack(pack_id: str, record: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    manifest_path = repo_path(record.get("manifest_path"))
    markdown_path = repo_path(record.get("path"))
    if manifest_path is None:
        findings.append(finding("blocker", "PACK_MANIFEST_PATH_MISSING", "Registry pack has no manifest_path.", pack_id=pack_id))
        manifest: dict[str, Any] = {}
    elif not manifest_path.is_file():
        findings.append(
            finding("blocker", "PACK_MANIFEST_MISSING", "Pack manifest_path does not exist.", pack_id=pack_id, path=str(manifest_path))
        )
        manifest = {}
    else:
        loaded = read_json(manifest_path)
        manifest = loaded if isinstance(loaded, dict) else {}
        if not isinstance(loaded, dict):
            findings.append(finding("blocker", "PACK_MANIFEST_INVALID", "Pack manifest root must be an object.", pack_id=pack_id, path=str(manifest_path)))

    if markdown_path is None or not markdown_path.is_file():
        findings.append(finding("blocker", "PACK_MARKDOWN_MISSING", "Pack Markdown path does not exist.", pack_id=pack_id, path=str(markdown_path or "")))
    elif not markdown_path.read_text(encoding="utf-8-sig").strip():
        findings.append(finding("blocker", "PACK_MARKDOWN_EMPTY", "Pack Markdown file is empty.", pack_id=pack_id, path=str(markdown_path)))

    expected = {
        "pack_id": pack_id,
        "title": record.get("title"),
        "interface_family": record.get("interface_family"),
        "markdown_path": record.get("path"),
    }
    for key, expected_value in expected.items():
        if manifest and manifest.get(key) != expected_value:
            findings.append(
                finding(
                    "blocker",
                    "PACK_MANIFEST_MISMATCH",
                    f"Manifest {key}={manifest.get(key)!r} does not match registry {expected_value!r}.",
                    pack_id=pack_id,
                    path=str(manifest_path or ""),
                )
            )

    positive_paths = string_list(manifest.get("positive_example_paths"))
    negative_paths = string_list(manifest.get("negative_example_paths"))
    contract_kinds = set(string_list(manifest.get("contract_kinds")))
    if not positive_paths:
        findings.append(finding("test_gap", "PACK_POSITIVE_EXAMPLE_MISSING", "Pack manifest has no positive_example_paths.", pack_id=pack_id))
    for raw_path in positive_paths + negative_paths:
        example_path = repo_path(raw_path)
        if example_path is None:
            findings.append(finding("blocker", "EXAMPLE_PATH_INVALID", "Example path must be a non-empty string.", pack_id=pack_id))
        else:
            validate_json_file(example_path, pack_id, findings)

    if "flat_recipe" in contract_kinds:
        for raw_path in positive_paths:
            example_path = repo_path(raw_path)
            if example_path is None or not example_path.is_file():
                continue
            errors = validate_recipe_file(example_path)
            if errors:
                findings.append(
                    finding(
                        "blocker",
                        "POSITIVE_RECIPE_REJECTED",
                        f"Positive flat recipe failed strict validation: {errors}",
                        pack_id=pack_id,
                        path=str(example_path),
                    )
                )
        for raw_path in negative_paths:
            example_path = repo_path(raw_path)
            if example_path is None or not example_path.is_file():
                continue
            if not validate_recipe_file(example_path):
                findings.append(
                    finding(
                        "blocker",
                        "NEGATIVE_RECIPE_ACCEPTED",
                        "Negative flat recipe unexpectedly passed strict validation.",
                        pack_id=pack_id,
                        path=str(example_path),
                    )
                )

    registry_positive = set(string_list(record.get("example_paths")))
    for single_key in ("example_dsl_path", "example_recipe_path", "example_json_path"):
        value = record.get(single_key)
        if isinstance(value, str) and value:
            registry_positive.add(value)
    overlap = sorted(registry_positive.intersection(set(negative_paths)))
    if overlap:
        findings.append(
            finding(
                "risk",
                "NEGATIVE_EXAMPLE_IN_PROMPT_SET",
                f"Negative example path(s) are also listed as prompt positive examples: {overlap}",
                pack_id=pack_id,
            )
        )

    return {
        "pack_id": pack_id,
        "manifest_path": str(manifest_path or ""),
        "markdown_path": str(markdown_path or ""),
        "positive_examples": len(positive_paths),
        "negative_examples": len(negative_paths),
        "findings": findings,
        "ok": not any(item["severity"] in {"blocker", "risk"} for item in findings),
    }


def build_report(capabilities_path: Path) -> dict[str, Any]:
    capabilities = read_json(capabilities_path)
    packs = capabilities.get("example_packs") if isinstance(capabilities, dict) else None
    if not isinstance(packs, dict):
        return {
            "schema_version": 1,
            "generated_at": now_iso_like(),
            "ok": False,
            "records": [],
            "findings": [finding("blocker", "EXAMPLE_PACKS_REGISTRY_MISSING", "interface_capabilities.json has no example_packs object.")],
            "counts": {"blocker": 1, "risk": 0, "test_gap": 0, "info": 0},
        }

    records = [validate_pack(pack_id, record) for pack_id, record in packs.items() if isinstance(pack_id, str) and isinstance(record, dict)]
    findings = [item for record in records for item in record["findings"]]
    counts = {severity: sum(1 for item in findings if item["severity"] == severity) for severity in ("blocker", "risk", "test_gap", "info")}
    return {
        "schema_version": 1,
        "generated_at": now_iso_like(),
        "capabilities": str(capabilities_path),
        "ok": counts["blocker"] == 0 and counts["risk"] == 0,
        "pack_count": len(records),
        "records": records,
        "findings": findings,
        "counts": counts,
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
        "# Interface Example Pack Validation",
        "",
        f"- status: `{'passed' if report.get('ok') else 'failed'}`",
        f"- pack_count: `{report.get('pack_count', 0)}`",
        f"- blockers: `{report.get('counts', {}).get('blocker', 0)}`",
        f"- risks: `{report.get('counts', {}).get('risk', 0)}`",
        f"- test_gaps: `{report.get('counts', {}).get('test_gap', 0)}`",
        "",
        "## Packs",
        "",
        "| pack | positive | negative | ok |",
        "| --- | ---: | ---: | --- |",
    ]
    for record in report.get("records", []):
        lines.append(
            f"| `{record.get('pack_id')}` | {record.get('positive_examples', 0)} | "
            f"{record.get('negative_examples', 0)} | `{record.get('ok')}` |"
        )
    if report.get("findings"):
        lines.extend(["", "## Findings", ""])
        for item in report["findings"]:
            lines.append(f"- [{item.get('severity')}] `{item.get('code')}` pack=`{item.get('pack_id')}` {item.get('message')}")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    report = build_report(Path(args.capabilities))
    if args.report:
        write_json(Path(args.report), report)
    if args.markdown:
        write_text(Path(args.markdown), markdown_report(report))
    print(json.dumps({"ok": report["ok"], "pack_count": report.get("pack_count", 0), "counts": report["counts"]}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
