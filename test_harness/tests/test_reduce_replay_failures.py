from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import reduce_replay_failures as batch


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def configure_fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    runner = tmp_path / "build" / "sggk_case_runner.exe"
    reducer = tmp_path / "test_harness" / "tools" / "reduce_failure_recipe.py"
    runner.parent.mkdir(parents=True)
    reducer.parent.mkdir(parents=True)
    runner.write_bytes(b"runner")
    reducer.write_text("# fixed reducer fixture\n", encoding="utf-8")
    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(batch, "REDUCER_SCRIPT", reducer)
    return runner, reducer


def failure_summary(reduced_recipe: Path, *, preserved: bool = True) -> dict[str, object]:
    expected = {
        "schema_version": 1,
        "kind": "oracle_failure",
        "returncode": 2,
        "phase": "oracle",
        "exception_code": "",
        "sdk_error_code": None,
        "validation_failures": ["wrong body count"],
        "topology_failures": [],
        "message_signature": "",
    }
    final_validation: dict[str, object] = {"ok": False, "failures": ["wrong body count"]}
    if not preserved:
        final_validation = {"ok": True, "failures": []}
    return {
        "reduced_recipe": str(reduced_recipe),
        "predicate": {"failure_signature": expected},
        "baseline_observation": {
            "returncode": 2,
            "timed_out": False,
            "stderr": "",
            "status": {"succeeded": True},
            "validation": {"ok": False, "failures": ["wrong body count"]},
            "topo_check": {},
            "run_state": {"phase": "oracle"},
        },
        "final_observation": {
            "returncode": 2 if preserved else 0,
            "timed_out": False,
            "stderr": "",
            "status": {"succeeded": True},
            "validation": final_validation,
            "topo_check": {},
            "run_state": {"phase": "oracle"},
        },
        "trials": 4,
        "accepted_reductions": 2,
    }


def replay_result(recipe: Path, fingerprint: str, status: str) -> dict[str, object]:
    signature = {
        "schema_version": 1,
        "kind": "oracle_failure",
        "returncode": 2,
        "phase": "oracle",
        "exception_code": "",
        "sdk_error_code": None,
        "validation_failures": ["wrong body count"],
        "topology_failures": [],
        "message_signature": "",
    }
    return {
        "fingerprint": fingerprint,
        "representative_case_id": f"case_{fingerprint}",
        "status": status,
        "expected_failure_signature": signature,
        "attempt_count": 3,
        "attempts": [
            {
                "matches_expected": True,
                "failure_signature": signature,
                "returncode": 2,
            }
            for _ in range(3)
        ],
        "seed": {"recipe_paths": [str(recipe)], "failure_signature": signature},
    }


def test_selects_stable_existing_recipes_honors_limit_and_uses_fixed_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, reducer = configure_fake_repo(tmp_path, monkeypatch)
    recipes = []
    for index in range(4):
        recipe = tmp_path / "recipes" / f"recipe_{index}.json"
        write_json(recipe, {"case_id": f"case_{index}"})
        recipes.append(recipe)
    replay_dir = tmp_path / "replay"
    write_json(
        replay_dir / "replay_summary.json",
        {
            "results": [
                replay_result(recipes[0], "first", "stable_same_failure"),
                replay_result(recipes[1], "skip", "changed_failure"),
                replay_result(tmp_path / "recipes" / "missing.json", "missing", "stable_failure"),
                replay_result(recipes[2], "second", "stable_failure"),
                replay_result(recipes[3], "limited", "stable_same_failure"),
            ]
        },
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        reduce_out = Path(command[command.index("--out") + 1])
        reduced = reduce_out / "reduced_recipe.json"
        write_json(reduced, {"case_id": "reduced"})
        write_json(reduce_out / "reduction_summary.json", failure_summary(reduced))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    out = tmp_path / "reductions"

    result = batch.main(["--runner", str(runner), "--replay", str(replay_dir), "--out", str(out), "--limit", "2"])

    assert result == 0
    assert len(calls) == 2
    assert all(call[0][1] == str(reducer) for call in calls)
    assert all(call[1]["shell"] is False for call in calls)
    assert all("--max-trials" in call[0] and call[0][call[0].index("--max-trials") + 1] == "60" for call in calls)
    index = json.loads((out / "reduction_index.json").read_text(encoding="utf-8"))
    assert index["candidate_count"] == 2
    assert index["selected_count"] == 2
    assert [item["fingerprint"] for item in index["reductions"]] == ["first", "limited"]
    assert index["preserved_count"] == 2


def test_returncode_two_is_accepted_when_summary_proves_failure_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = configure_fake_repo(tmp_path, monkeypatch)
    recipe = tmp_path / "recipe.json"
    write_json(recipe, {"case_id": "failure"})
    replay = tmp_path / "replay_summary.json"
    write_json(replay, {"results": [replay_result(recipe, "same", "stable_same_failure")]})

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        reduce_out = Path(command[command.index("--out") + 1])
        reduced = reduce_out / "reduced_recipe.json"
        write_json(reduced, {"case_id": "reduced"})
        write_json(reduce_out / "reduction_summary.json", failure_summary(reduced))
        return subprocess.CompletedProcess(command, 2, "", "")

    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    out = tmp_path / "out"

    result = batch.main(["--runner", str(runner), "--replay", str(replay), "--out", str(out)])

    assert result == 0
    entry = json.loads((out / "reduction_index.json").read_text(encoding="utf-8"))["reductions"][0]
    assert entry["returncode"] == 2
    assert entry["status"] == "preserved"
    assert entry["ok"] is True


def test_summary_signature_controls_not_preserved_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = configure_fake_repo(tmp_path, monkeypatch)
    recipe = tmp_path / "recipe.json"
    write_json(recipe, {"case_id": "failure"})
    replay = tmp_path / "replay_summary.json"
    write_json(replay, {"results": [replay_result(recipe, "changed", "stable_same_failure")]})

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        reduce_out = Path(command[command.index("--out") + 1])
        reduced = reduce_out / "reduced_recipe.json"
        write_json(reduced, {"case_id": "reduced"})
        write_json(reduce_out / "reduction_summary.json", failure_summary(reduced, preserved=False))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    out = tmp_path / "out"

    result = batch.main(["--runner", str(runner), "--replay", str(replay), "--out", str(out)])

    assert result == 2
    entry = json.loads((out / "reduction_index.json").read_text(encoding="utf-8"))["reductions"][0]
    assert entry["status"] == "not_preserved"
    assert entry["ok"] is False


def test_reducer_baseline_must_match_the_trusted_replay_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = configure_fake_repo(tmp_path, monkeypatch)
    recipe = tmp_path / "recipe.json"
    write_json(recipe, {"case_id": "failure"})
    replay_record = replay_result(recipe, "rebound", "stable_same_failure")
    crash_signature = {
        "schema_version": 1,
        "kind": "crash",
        "returncode": -1073741819,
        "phase": "invoke_api",
        "exception_code": "0xC0000005",
        "sdk_error_code": None,
        "validation_failures": [],
        "topology_failures": [],
        "message_signature": "access violation",
    }
    replay_record["expected_failure_signature"] = crash_signature
    replay_record["seed"]["failure_signature"] = crash_signature
    replay_record["attempts"] = [
        {
            "matches_expected": True,
            "failure_signature": crash_signature,
            "returncode": -1073741819,
        }
        for _ in range(3)
    ]
    replay = tmp_path / "replay_summary.json"
    write_json(replay, {"results": [replay_record]})

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        reduce_out = Path(command[command.index("--out") + 1])
        reduced = reduce_out / "reduced_recipe.json"
        write_json(reduced, {"case_id": "reduced"})
        write_json(reduce_out / "reduction_summary.json", failure_summary(reduced))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    out = tmp_path / "out"

    result = batch.main(["--runner", str(runner), "--replay", str(replay), "--out", str(out)])

    assert result == 2
    entry = json.loads((out / "reduction_index.json").read_text(encoding="utf-8"))["reductions"][0]
    assert entry["status"] == "not_preserved"
    assert "trusted_replay_signature" in entry["reason"]
    assert entry["reduced_recipe"] == ""


def test_rejects_runner_replay_output_and_recipe_paths_outside_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = configure_fake_repo(tmp_path, monkeypatch)
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir(exist_ok=True)
    outside_runner = outside / "runner.exe"
    outside_runner.write_bytes(b"runner")
    replay = tmp_path / "replay_summary.json"
    inside_recipe = tmp_path / "inside.json"
    write_json(inside_recipe, {})
    write_json(replay, {"results": [replay_result(inside_recipe, "inside", "stable_same_failure")]})

    assert batch.main(["--runner", str(outside_runner), "--replay", str(replay), "--out", str(tmp_path / "out")]) == 1
    assert batch.main(["--runner", str(runner), "--replay", str(outside), "--out", str(tmp_path / "out")]) == 1
    assert batch.main(["--runner", str(runner), "--replay", str(replay), "--out", str(outside / "out")]) == 1

    write_json(replay, {"results": [replay_result(outside_runner, "outside", "stable_same_failure")]})
    assert batch.main(["--runner", str(runner), "--replay", str(replay), "--out", str(tmp_path / "out")]) == 1
