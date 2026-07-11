#!/usr/bin/env python3
"""Collect qualified failure candidates without labelling them confirmed bugs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from collect_bug_registry import (
    build_summary,
    collect_from_bundle_indices,
    collect_from_triage,
    collect_replay_summaries,
)


def enrich_from_bundle_manifest(failure: dict[str, Any]) -> None:
    paths = failure.get("paths") if isinstance(failure.get("paths"), dict) else {}
    manifest_path = paths.get("bundle_manifest")
    if not isinstance(manifest_path, str) or not manifest_path:
        return
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(manifest, dict):
        return
    probe = manifest.get("topotrack_probe")
    if isinstance(probe, dict):
        failure["isolated_topotrack_probe"] = probe
    reduction = manifest.get("reduction")
    if isinstance(reduction, dict):
        failure["reduction"] = reduction


def build_failure_registry(
    triage: list[str],
    replay: list[str],
    bundle_index: list[str],
) -> dict[str, Any]:
    replay_lookup = collect_replay_summaries(replay)
    entries: dict[str, dict[str, Any]] = {}
    collect_from_triage(entries, triage, replay_lookup)
    collect_from_bundle_indices(entries, bundle_index, replay_lookup)
    legacy = build_summary(entries)
    failures = legacy.pop("bugs", [])
    for failure in failures:
        if isinstance(failure, dict):
            enrich_from_bundle_manifest(failure)
            failure["record_kind"] = "qualified_failure_candidate"
            failure["assessment_status"] = "candidate_only"
            failure["confirmed_bug"] = False
            failure["confirmed_root_cause"] = False
    return {
        "schema_version": 1,
        "record_kind": "failure_registry",
        "assessment_status": "candidate_only",
        **legacy,
        "failures": failures,
    }


def markdown_report(registry: dict[str, Any]) -> str:
    lines = [
        "# SGGK Qualified Failure Registry",
        "",
        "Entries are investigation candidates only. Promotion to a confirmed bug requires a separate trusted gate.",
        "",
        f"- Total: `{registry.get('total', 0)}`",
        f"- Replay status: `{registry.get('by_replay_status', {})}`",
        "",
        "| fingerprint | case | API | replay | TopoTrack probe | assessment |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in registry.get("failures", []):
        if not isinstance(item, dict):
            continue
        lines.append(
            f"| `{item.get('fingerprint', '')}` | `{item.get('representative_case_id', '')}` | "
            f"`{item.get('api', '')}` | `{item.get('replay_status', '')}` | "
            f"`{(item.get('isolated_topotrack_probe') or {}).get('classification', '')}` | "
            "`candidate_only` |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-index", action="append", default=[])
    parser.add_argument("--triage", action="append", default=[])
    parser.add_argument("--replay", action="append", default=[])
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    registry = build_failure_registry(args.triage, args.replay, args.bundle_index)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "failure_registry.json").write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out / "failure_registry.md").write_text(markdown_report(registry), encoding="utf-8")
    print(f"registry={out / 'failure_registry.json'}")
    print(f"failures={registry['total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
