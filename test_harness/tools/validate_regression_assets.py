#!/usr/bin/env python3
"""Validate regression asset replay and triage health.

This read-only gate checks saved regression assets for the evidence needed to
replay and compare future SDK runs. It never calls a model, runs the SDK,
generates recipes, applies patches, or commits files.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SEVERITY_RANK = {"info": 0, "test_gap": 1, "risk": 2, "blocker": 3}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", action="append", default=[], help="Regression asset directory or asset_manifest.json")
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


def as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def finding(severity: str, code: str, message: str, *, path: str = "", asset_id: str = "") -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "path": path,
        "asset_id": asset_id,
    }


def asset_manifest_path(raw: str) -> Path:
    path = repo_path(raw)
    return path / "asset_manifest.json" if path.is_dir() else path


def existing_repo_path(raw: str, base: Path) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        candidates = [REPO_ROOT / path, base / path]
    else:
        candidates = [path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else None


def load_optional_json(path: Path) -> Any:
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def compact_counts(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in items:
        counter[str(item.get(key) or "unknown")] += 1
    return dict(sorted(counter.items()))


def validate_one_asset(raw: str) -> dict[str, Any]:
    manifest_path = asset_manifest_path(raw)
    asset_root = manifest_path.parent
    path_text = rel_display(manifest_path)
    findings: list[dict[str, str]] = []
    try:
        loaded = read_json(manifest_path)
    except FileNotFoundError:
        return {
            "asset_id": asset_root.name,
            "path": path_text,
            "ok": False,
            "counts": {"blocker": 1},
            "summary": {},
            "findings": [
                finding(
                    "blocker",
                    "REGRESSION_ASSET_MANIFEST_MISSING",
                    "Regression asset manifest is missing.",
                    path=path_text,
                    asset_id=asset_root.name,
                )
            ],
        }
    except json.JSONDecodeError as exc:
        return {
            "asset_id": asset_root.name,
            "path": path_text,
            "ok": False,
            "counts": {"blocker": 1},
            "summary": {},
            "findings": [
                finding(
                    "blocker",
                    "REGRESSION_ASSET_MANIFEST_INVALID_JSON",
                    f"Regression asset manifest is invalid JSON: {exc}",
                    path=path_text,
                    asset_id=asset_root.name,
                )
            ],
        }
    if not isinstance(loaded, dict):
        findings.append(
            finding(
                "blocker",
                "REGRESSION_ASSET_MANIFEST_INVALID",
                "Regression asset manifest root must be a JSON object.",
                path=path_text,
                asset_id=asset_root.name,
            )
        )
        loaded = {}

    asset_id = as_str(loaded.get("asset_id")) or asset_root.name
    cases = [case for case in as_list(loaded.get("cases")) if isinstance(case, dict)]
    failed_cases = [case for case in cases if str(case.get("baseline_status") or "").startswith("failed")]
    unclassified = [case for case in cases if case.get("baseline_status") == "failed_unclassified"]
    failed_without_fp = [case for case in failed_cases if not as_str(case.get("baseline_fingerprint"))]
    status_counts = compact_counts(cases, "baseline_status")

    if loaded.get("schema") != "sggk.regression_asset.v1":
        findings.append(
            finding(
                "risk",
                "REGRESSION_ASSET_SCHEMA_UNKNOWN",
                "Regression asset schema is missing or not sggk.regression_asset.v1.",
                path=path_text,
                asset_id=asset_id,
            )
        )
    if not cases:
        findings.append(
            finding(
                "blocker",
                "REGRESSION_ASSET_NO_CASES",
                "Regression asset has no replay cases.",
                path=path_text,
                asset_id=asset_id,
            )
        )
    if unclassified:
        findings.append(
            finding(
                "test_gap",
                "REGRESSION_ASSET_FAILED_UNCLASSIFIED",
                f"{len(unclassified)} baseline failed cases are still failed_unclassified.",
                path=path_text,
                asset_id=asset_id,
            )
        )
    if failed_without_fp:
        findings.append(
            finding(
                "test_gap",
                "REGRESSION_ASSET_FAILED_WITHOUT_FINGERPRINT",
                f"{len(failed_without_fp)} baseline failed cases lack baseline_fingerprint.",
                path=path_text,
                asset_id=asset_id,
            )
        )

    triage_summary = as_str(loaded.get("triage_summary")) or as_str(as_dict(loaded.get("campaign")).get("triage_summary"))
    triage_path = existing_repo_path(triage_summary, asset_root)
    if failed_cases and (not triage_summary or triage_path is None or not triage_path.is_file()):
        findings.append(
            finding(
                "test_gap",
                "REGRESSION_ASSET_TRIAGE_SUMMARY_MISSING",
                "Failed baseline cases exist, but no readable triage_summary.json is linked.",
                path=path_text,
                asset_id=asset_id,
            )
        )

    bug_registry_path = asset_root / "bug_registry.json"
    bug_registry = load_optional_json(bug_registry_path) if bug_registry_path.is_file() else None
    bug_total = 0
    if isinstance(bug_registry, dict):
        bug_total = int(bug_registry.get("total") or 0)
    if failed_cases and bug_total == 0:
        findings.append(
            finding(
                "test_gap",
                "REGRESSION_ASSET_BUG_REGISTRY_EMPTY",
                "Failed baseline cases exist, but bug_registry.json has no candidate fingerprints.",
                path=rel_display(bug_registry_path),
                asset_id=asset_id,
            )
        )

    replay_plan = asset_root / "replay_plan" / "replay_plan.json"
    if not replay_plan.is_file():
        findings.append(
            finding(
                "test_gap",
                "REGRESSION_ASSET_REPLAY_PLAN_MISSING",
                "Replay plan is missing; run manage_regression_assets.py plan-replay for this asset.",
                path=rel_display(replay_plan),
                asset_id=asset_id,
            )
        )

    provenance = as_dict(loaded.get("provenance"))
    provenance_path = asset_root / "asset_provenance.json"
    if not provenance and provenance_path.is_file():
        loaded_provenance = load_optional_json(provenance_path)
        provenance = as_dict(loaded_provenance)
    provenance_fingerprints = set(as_list(provenance.get("fingerprints")))
    baseline_fingerprints = {as_str(case.get("baseline_fingerprint")) for case in failed_cases if as_str(case.get("baseline_fingerprint"))}
    if not provenance:
        findings.append(
            finding(
                "risk",
                "REGRESSION_ASSET_PROVENANCE_MISSING",
                "Regression asset has no provenance metadata.",
                path=path_text,
                asset_id=asset_id,
            )
        )
    elif provenance_fingerprints and not baseline_fingerprints:
        findings.append(
            finding(
                "test_gap",
                "REGRESSION_ASSET_PROVENANCE_FINGERPRINTS_NOT_IN_CASES",
                "Provenance lists known fingerprints, but failed baseline cases do not preserve them.",
                path=path_text,
                asset_id=asset_id,
            )
        )

    counts = compact_counts(findings, "severity")
    return {
        "asset_id": asset_id,
        "path": path_text,
        "ok": counts.get("blocker", 0) == 0,
        "summary": {
            "case_count": len(cases),
            "failed_case_count": len(failed_cases),
            "failed_unclassified_count": len(unclassified),
            "failed_without_fingerprint_count": len(failed_without_fp),
            "baseline_status_counts": status_counts,
            "triage_summary": rel_display(triage_path) if triage_path and triage_path.exists() else triage_summary,
            "bug_registry_total": bug_total,
            "replay_plan_exists": replay_plan.is_file(),
            "provenance_source_type": as_str(provenance.get("source_type")),
        },
        "counts": counts,
        "findings": findings,
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    assets = [validate_one_asset(raw) for raw in args.asset]
    findings = [finding for asset in assets for finding in as_list(asset.get("findings")) if isinstance(finding, dict)]
    counts = compact_counts(findings, "severity")
    return {
        "ok": counts.get("blocker", 0) == 0,
        "generated_at": now_iso_like(),
        "boundary": {
            "model_calls": False,
            "direct_api_calls": False,
            "runs_sdk": False,
            "generates_recipes": False,
            "applies_patches": False,
            "commits_changes": False,
            "production_flow": "read_saved_regression_asset_metadata_only",
        },
        "asset_count": len(assets),
        "counts": {severity: counts.get(severity, 0) for severity in ("info", "test_gap", "risk", "blocker")},
        "assets": assets,
        "findings": findings,
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Regression Asset Health Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- OK: `{report.get('ok')}`",
        f"- Asset count: `{report.get('asset_count')}`",
        f"- Counts: `{report.get('counts')}`",
        "",
        "## Assets",
        "",
        "| asset | ok | cases | failed | unclassified | no fingerprint | bug fingerprints | replay plan |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for asset in as_list(report.get("assets")):
        summary = as_dict(asset.get("summary"))
        lines.append(
            f"| `{asset.get('asset_id')}` | `{asset.get('ok')}` | {summary.get('case_count', 0)} | "
            f"{summary.get('failed_case_count', 0)} | {summary.get('failed_unclassified_count', 0)} | "
            f"{summary.get('failed_without_fingerprint_count', 0)} | {summary.get('bug_registry_total', 0)} | "
            f"`{summary.get('replay_plan_exists')}` |"
        )
    findings = as_list(report.get("findings"))
    if findings:
        lines.extend(["", "## Findings", ""])
        for item in findings:
            lines.append(
                f"- `{item.get('severity')}` `{item.get('code')}` `{item.get('asset_id')}` "
                f"`{item.get('path')}` {item.get('message')}"
            )
    return "\n".join(lines) + "\n"


def should_fail(report: dict[str, Any], fail_on: str) -> bool:
    threshold = SEVERITY_RANK[fail_on]
    for item in as_list(report.get("findings")):
        if SEVERITY_RANK.get(str(item.get("severity")), 0) >= threshold:
            return True
    return False


def main() -> int:
    args = parse_args()
    report = build_report(args)
    if args.report:
        write_json(repo_path(args.report), report)
    if args.markdown:
        write_text(repo_path(args.markdown), markdown_report(report))
    print(json.dumps({"ok": report["ok"], "asset_count": report["asset_count"], "counts": report["counts"]}, indent=2))
    return 1 if should_fail(report, args.fail_on) else 0


if __name__ == "__main__":
    raise SystemExit(main())
