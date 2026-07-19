from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from run_corpus import input_hash_issues, load_dataset_list  # noqa: E402


def test_dataset_list_resolves_relative_entries_against_list_parent(tmp_path: Path) -> None:
    step = tmp_path / "data" / "sample.step"
    step.parent.mkdir()
    step.write_text("ISO-10303-21;", encoding="utf-8")
    digest = hashlib.sha256(step.read_bytes()).hexdigest()
    index = tmp_path / "dataset_index.json"
    index.write_text(
        json.dumps({"files": [{"path": "data/sample.step", "sha256": digest}]}),
        encoding="utf-8",
    )
    expected: dict[str, tuple[str, str]] = {}

    loaded = load_dataset_list(index, expected)

    assert loaded == [str(step.resolve())]
    assert input_hash_issues([step.resolve()], expected, require_sha256=True) == []


def test_selected_input_content_must_match_bound_index_hash(tmp_path: Path) -> None:
    step = tmp_path / "sample.step"
    step.write_text("original", encoding="utf-8")
    digest = hashlib.sha256(step.read_bytes()).hexdigest()
    index = tmp_path / "dataset_index.json"
    index.write_text(
        json.dumps({"files": [{"path": str(step), "sha256": digest}]}),
        encoding="utf-8",
    )
    expected: dict[str, tuple[str, str]] = {}
    load_dataset_list(index, expected)
    step.write_text("replaced after approval", encoding="utf-8")

    issues = input_hash_issues([step.resolve()], expected, require_sha256=True)

    assert len(issues) == 1
    assert "content hash mismatch" in issues[0]
