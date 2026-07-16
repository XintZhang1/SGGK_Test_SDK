from __future__ import annotations

import argparse
from hashlib import sha1, sha256
import json
from pathlib import Path

from test_harness.tools.audit_corpus_dataset import audit_datasets, markdown_report


def _audit(index: Path, *, require_hashes: bool) -> dict[str, object]:
    return audit_datasets(
        argparse.Namespace(
            dataset_list=[str(index)],
            min_files=1,
            warn_tiny_bytes=0,
            require_hashes=require_hashes,
            fail_duplicate_ratio=-1.0,
        )
    )


def _write_index(tmp_path: Path, hashes: dict[str, str]) -> Path:
    step = tmp_path / "sample.step"
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    index = tmp_path / "dataset_index.json"
    index.write_text(
        json.dumps(
            {
                "total_files": 1,
                "hash_inputs": True,
                "files": [{"path": str(step), **hashes}],
            }
        ),
        encoding="utf-8",
    )
    return index


def test_require_hashes_rejects_sha1_only_legacy_index(tmp_path: Path) -> None:
    payload = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
    summary = _audit(
        _write_index(tmp_path, {"sha1": sha1(payload).hexdigest()}),
        require_hashes=True,
    )

    assert summary["ok"] is False
    assert summary["hash_coverage_ratio"] == 1.0
    assert summary["sha1_present_count"] == 1
    assert summary["sha256_present_count"] == 0
    assert summary["sha256_missing_count"] == 1
    assert any(
        issue["kind"] == "missing_sha256" and issue["severity"] == "error"
        for issue in summary["issues"]
    )


def test_require_hashes_accepts_sha256_only_index(tmp_path: Path) -> None:
    payload = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
    summary = _audit(
        _write_index(tmp_path, {"sha256": sha256(payload).hexdigest()}),
        require_hashes=True,
    )

    assert summary["ok"] is True
    assert summary["sha1_present_count"] == 0
    assert summary["sha256_present_count"] == 1
    assert summary["sha256_missing_count"] == 0
    assert summary["sha256_coverage_ratio"] == 1.0
    assert "SHA-256 campaign-binding coverage: `1/1`" in markdown_report(summary)


def test_non_strict_audit_keeps_sha1_legacy_index_compatible(tmp_path: Path) -> None:
    payload = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
    summary = _audit(
        _write_index(tmp_path, {"sha1": sha1(payload).hexdigest()}),
        require_hashes=False,
    )

    assert summary["ok"] is True
    assert any(
        issue["kind"] == "missing_sha256" and issue["severity"] == "warning"
        for issue in summary["issues"]
    )


def test_require_hashes_rejects_malformed_sha256_even_with_sha1(tmp_path: Path) -> None:
    payload = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
    summary = _audit(
        _write_index(
            tmp_path,
            {
                "sha1": sha1(payload).hexdigest(),
                "sha256": "not-a-valid-sha256",
            },
        ),
        require_hashes=True,
    )

    assert summary["ok"] is False
    assert summary["sha256_invalid_count"] == 1
    issue = next(issue for issue in summary["issues"] if issue["kind"] == "missing_sha256")
    assert issue["severity"] == "error"
    assert "invalid sha256=1" in issue["message"]
