from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import run_campaign


def test_campaign_delegates_reduction_to_hardened_replay_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    replay_path = tmp_path / "replay_summary.json"
    replay_path.write_text(json.dumps({"results": []}), encoding="utf-8")
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"runner")
    out_root = tmp_path / "campaign"
    records: list[dict[str, object]] = []
    observed: list[str] = []

    def fake_run_command(
        name: str,
        command: list[str],
        *,
        acceptable: set[int],
        **_kwargs,
    ) -> dict[str, object]:
        observed.extend(command)
        assert name == "reduce_stable_replay_failures"
        assert acceptable == {0, 2}
        reduction_out = Path(command[command.index("--out") + 1])
        reduction_out.mkdir(parents=True, exist_ok=True)
        (reduction_out / "reduction_index.json").write_text(
            json.dumps(
                {
                    "generated_at": "test",
                    "candidate_count": 0,
                    "selected_count": 0,
                    "completed_count": 0,
                    "accepted_reduction_count": 0,
                    "reductions": [],
                }
            ),
            encoding="utf-8",
        )
        return {"name": name, "returncode": 0, "ok": True}

    monkeypatch.setattr(run_campaign, "run_command", fake_run_command)
    args = SimpleNamespace(
        reduce_stable_failures=True,
        reduction_limit=3,
        reduction_timeout=90.0,
        timeout=120.0,
        reduction_max_trials=40,
        reduction_min_dimension=0.01,
    )

    result = run_campaign.run_reductions(
        args,
        Path(run_campaign.__file__).resolve().parent,
        runner,
        out_root,
        {"summary_path": str(replay_path)},
        records,
    )

    assert result is not None
    assert result["candidate_count"] == 0
    assert any(path.endswith("reduce_replay_failures.py") for path in observed)
    assert not any(path.endswith("reduce_failure_recipe.py") for path in observed)
    assert records[0]["ok"] is True


def test_campaign_bundle_export_forwards_merged_topotrack_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    out_root = tmp_path / "campaign"
    triage = tmp_path / "triage"
    replay = tmp_path / "replay"
    probe = tmp_path / "topotrack_probe_index.json"
    triage.mkdir()
    replay.mkdir()
    probe.write_text(json.dumps({"results": []}), encoding="utf-8")
    observed: list[str] = []

    def fake_run_command(name: str, command: list[str], **_kwargs) -> dict[str, object]:
        observed.extend(command)
        assert name == "export_failure_bundles"
        bundle_out = Path(command[command.index("--out") + 1])
        bundle_out.mkdir(parents=True, exist_ok=True)
        (bundle_out / "bundle_index.json").write_text(
            json.dumps({"bundles": []}),
            encoding="utf-8",
        )
        return {"name": name, "returncode": 0, "ok": True}

    monkeypatch.setattr(run_campaign, "run_command", fake_run_command)
    args = SimpleNamespace(skip_bundles=False, bundle_zip=False)

    result = run_campaign.run_bundle_export(
        args,
        Path(run_campaign.__file__).resolve().parent,
        out_root,
        {"out": str(triage)},
        {"out": str(replay)},
        None,
        {"summary_path": str(probe), "skipped": False},
        [],
        [],
    )

    assert result is not None
    assert "--topotrack-probe" in observed
    assert observed[observed.index("--topotrack-probe") + 1] == str(probe)
