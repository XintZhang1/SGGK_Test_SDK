from __future__ import annotations

import json

import pytest

from test_harness.ui.state import active_session_root, read_artifact, session_snapshot


def write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_ui_state_projects_events_and_text_artifacts(tmp_path) -> None:
    root = tmp_path / "artifacts" / "harness_sessions" / "session-1"
    write_json(tmp_path / "artifacts/harness_sessions/active.json", {"session_id": "session-1"})
    write_json(
        root / "session.json",
        {"session_id": "session-1", "public_function": "api_boolean", "state": "awaiting_comment", "current_round": 1},
    )
    write_json(
        root / "events/000001.json",
        {"sequence": 1, "event_type": "PUBLIC_FUNCTION_RESOLVED", "recorded_at": "2026-01-01T00:00:00Z", "payload": {}},
    )
    write_json(
        root / "events/000002.json",
        {"sequence": 2, "event_type": "ROUND_READY_FOR_REVIEW", "recorded_at": "2026-01-01T00:00:01Z", "payload": {}},
    )
    (root / "rounds/0001/candidate").mkdir(parents=True)
    (root / "rounds/0001/candidate/test.cpp").write_text("int main() {}", encoding="utf-8")
    (root / "rounds/0001/candidate/blob.bin").write_bytes(b"secret")

    state = session_snapshot(tmp_path)

    assert state["session"]["public_function"] == "api_boolean"
    assert state["stages"][1]["status"] == "done"
    assert state["stages"][2]["status"] == "done"
    assert [item["path"] for item in state["artifacts"]] == [
        "events/000001.json",
        "events/000002.json",
        "rounds/0001/candidate/test.cpp",
        "session.json",
    ]
    assert read_artifact(active_session_root(tmp_path), "rounds/0001/candidate/test.cpp")["content"] == "int main() {}"


def test_ui_artifact_reader_blocks_path_traversal(tmp_path) -> None:
    root = tmp_path / "session"
    root.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        read_artifact(root, "../secret.txt")
