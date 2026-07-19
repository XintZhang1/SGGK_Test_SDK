from __future__ import annotations

import argparse
import json
from pathlib import Path

from test_harness.tools.verify_campaign_artifacts import Verifier


def verifier_args() -> argparse.Namespace:
    return argparse.Namespace(
        allow_duplicate_inputs=False,
        allow_duplicate_geometry=False,
        allow_tolerance_mismatches=False,
        expect_known_bug_status=[],
    )


def direct_campaign_summary(root: Path) -> dict[str, object]:
    return {
        "args": {"no_preview": True, "no_geometry_audit": True},
        "commands": [],
        "lanes": [],
        "bug_registry": {
            "summary_path": str(root / "bug_registry" / "bug_registry.json"),
            "report_path": str(root / "bug_registry" / "bug_registry.md"),
            "replay_recipes": str(root / "bug_registry" / "registry_replay_recipes.txt"),
        },
        "bug_record_drafts": {
            "draft_path": str(root / "bug_record_drafts" / "drafts.json"),
        },
    }


def test_direct_campaign_verifier_requires_canonical_registry_draft_and_chinese_report_keys(
    tmp_path: Path,
) -> None:
    (tmp_path / "campaign_report.md").write_text("report\n", encoding="utf-8")
    verifier = Verifier(tmp_path, verifier_args())

    result = verifier.verify_summary(
        direct_campaign_summary(tmp_path),
        tmp_path / "campaign_summary.json",
    )

    assert result["ok"] is False
    missing_paths = {
        Path(item["path"]).name
        for item in result["checks"]
        if item["severity"] == "error" and item["kind"] == "missing_file"
    }
    assert {
        "campaign_report.zh-CN.md",
        "bug_registry.json",
        "bug_registry.md",
        "registry_replay_recipes.txt",
        "drafts.json",
    } <= missing_paths


def test_direct_campaign_verifier_accepts_complete_registry_drafts_and_chinese_report(
    tmp_path: Path,
) -> None:
    (tmp_path / "campaign_report.md").write_text("report\n", encoding="utf-8")
    (tmp_path / "campaign_report.zh-CN.md").write_text("中文报告\n", encoding="utf-8")
    registry_root = tmp_path / "bug_registry"
    registry_root.mkdir()
    (registry_root / "bug_registry.json").write_text(
        json.dumps({"schema_version": 1, "total": 0}),
        encoding="utf-8",
    )
    (registry_root / "bug_registry.md").write_text("registry\n", encoding="utf-8")
    (registry_root / "registry_replay_recipes.txt").write_text("# none\n", encoding="utf-8")
    draft_root = tmp_path / "bug_record_drafts"
    draft_root.mkdir()
    (draft_root / "drafts.json").write_text(
        json.dumps({"schema_version": 1, "records": []}),
        encoding="utf-8",
    )
    verifier = Verifier(tmp_path, verifier_args())

    result = verifier.verify_summary(
        direct_campaign_summary(tmp_path),
        tmp_path / "campaign_summary.json",
    )

    assert result["ok"] is True
    assert not [item for item in result["checks"] if item["severity"] == "error"]
