from __future__ import annotations

import json
import shutil
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_harness.tools import generate_corpus_recut_matrix as recut
from test_harness.tools.score_case_complexity import DIMENSIONS, MIN_FLAT_SCORE


def _write_sgt(path: Path, minimum=(0.0, 0.0, 0.0), maximum=(10.0, 20.0, 30.0)) -> Path:
    path.write_text(json.dumps({"bndbox": {"min": list(minimum), "max": list(maximum)}}), encoding="utf-8")
    return path


def _fake_validation_payload() -> dict:
    checks = []
    for axis, low, high in (("x", 0.0, 10.0), ("y", 0.0, 20.0), ("z", 0.0, 30.0)):
        checks.append({"axis": axis, "side": "min", "actual_extreme": low, "probe": {"success": True}})
        checks.append({"axis": axis, "side": "max", "actual_extreme": high, "probe": {"success": True}})
    return {"plane_extreme_checks": checks}


class _RunRecorder:
    """Counting subprocess stub that materializes a probe validation report."""

    def __init__(self, payload: dict | None, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.payload = payload
        self.returncode = returncode

    def __call__(self, cmd, **_kwargs):
        self.calls.append([str(part) for part in cmd])
        recipe_path = Path(cmd[cmd.index("--recipe") + 1])
        case_id = json.loads(recipe_path.read_text(encoding="utf-8"))["case_id"]
        out_dir = Path(cmd[cmd.index("--out") + 1])
        if self.payload is not None:
            report = out_dir / case_id / "report"
            report.mkdir(parents=True, exist_ok=True)
            (report / "validation.json").write_text(json.dumps(self.payload), encoding="utf-8")
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


def _probe(runner: Path, source: Path, probe_root: Path, cache: recut.ExactBboxProbeCache | None) -> dict:
    return recut.probe_exact_sgt_bbox(
        runner=runner,
        source=source,
        estimated_bbox=recut.estimate_sgt_bbox(source),
        probe_root=probe_root,
        timeout=1.0,
        cache=cache,
    )


def _cache(tmp_path: Path, runner: Path) -> recut.ExactBboxProbeCache:
    return recut.ExactBboxProbeCache(tmp_path / "exact_bbox_cache.json", runner)


def test_probe_cache_hit_skips_runner_and_reuses_bbox(tmp_path, monkeypatch) -> None:
    source = _write_sgt(tmp_path / "body.sgt")
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"fake-runner")
    probe_root = tmp_path / "probes"
    recorder = _RunRecorder(_fake_validation_payload())
    monkeypatch.setattr(recut.subprocess, "run", recorder)

    first = _probe(runner, source, probe_root, _cache(tmp_path, runner))
    assert first["ok"] is True
    assert first.get("cache_hit") is not True
    assert len(recorder.calls) == 1

    # A fresh cache instance reloads from disk, mirroring a separate CLI run.
    second = _probe(runner, source, probe_root, _cache(tmp_path, runner))
    assert second["ok"] is True
    assert second["cache_hit"] is True
    assert len(recorder.calls) == 1
    assert second["min"] == first["min"]
    assert second["max"] == first["max"]
    assert second["dims"] == first["dims"]
    assert second["center"] == first["center"]
    assert second["source"] == "plane_distance_extrema"
    # The probe artifacts still exist, so evidence pointers are preserved.
    assert second.get("probe_artifact_dir") == first.get("probe_artifact_dir")
    assert second.get("probe_case_id") == first.get("probe_case_id")

    cache_doc = json.loads((tmp_path / "exact_bbox_cache.json").read_text(encoding="utf-8"))
    assert cache_doc["schema_version"] == 1
    assert len(cache_doc["entries"]) == 1
    key, entry = next(iter(cache_doc["entries"].items()))
    source_sha, runner_sha = key.split(":")
    assert source_sha == recut.file_sha256(source)
    assert runner_sha == recut.file_sha256(runner)
    assert entry["bbox"]["ok"] is True
    assert entry["bbox"]["min"] == [0.0, 0.0, 0.0]
    assert entry["bbox"]["max"] == [10.0, 20.0, 30.0]
    assert entry["probe_artifact_dir"] == first["probe_artifact_dir"]


def test_probe_cache_hit_survives_missing_artifacts(tmp_path, monkeypatch) -> None:
    source = _write_sgt(tmp_path / "body.sgt")
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"fake-runner")
    probe_root = tmp_path / "probes"
    recorder = _RunRecorder(_fake_validation_payload())
    monkeypatch.setattr(recut.subprocess, "run", recorder)

    first = _probe(runner, source, probe_root, _cache(tmp_path, runner))
    assert first["ok"] is True
    shutil.rmtree(probe_root / "runs")

    hit = _probe(runner, source, probe_root, _cache(tmp_path, runner))
    assert hit["ok"] is True
    assert hit["cache_hit"] is True
    assert "probe_artifact_dir" not in hit
    assert "probe_case_id" not in hit
    assert len(recorder.calls) == 1


def test_probe_failures_are_never_cached(tmp_path, monkeypatch) -> None:
    source = _write_sgt(tmp_path / "body.sgt")
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"fake-runner")
    probe_root = tmp_path / "probes"
    recorder = _RunRecorder(None, returncode=2)
    monkeypatch.setattr(recut.subprocess, "run", recorder)

    failed = _probe(runner, source, probe_root, _cache(tmp_path, runner))
    assert failed["ok"] is False
    assert not (tmp_path / "exact_bbox_cache.json").exists()

    retry = _probe(runner, source, probe_root, _cache(tmp_path, runner))
    assert retry["ok"] is False
    assert len(recorder.calls) == 2


def test_corrupt_cache_is_ignored_and_rebuilt(tmp_path, monkeypatch) -> None:
    source = _write_sgt(tmp_path / "body.sgt")
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"fake-runner")
    probe_root = tmp_path / "probes"
    recorder = _RunRecorder(_fake_validation_payload())
    monkeypatch.setattr(recut.subprocess, "run", recorder)
    cache_path = tmp_path / "exact_bbox_cache.json"

    cache_path.write_text("{not-json", encoding="utf-8")
    assert _probe(runner, source, probe_root, _cache(tmp_path, runner))["ok"] is True
    assert len(recorder.calls) == 1

    cache_doc = json.loads(cache_path.read_text(encoding="utf-8"))
    entry = next(iter(cache_doc["entries"].values()))
    del entry["bbox"]["center"]
    cache_path.write_text(json.dumps(cache_doc), encoding="utf-8")
    assert _probe(runner, source, probe_root, _cache(tmp_path, runner))["ok"] is True
    assert len(recorder.calls) == 2

    cache_doc = json.loads(cache_path.read_text(encoding="utf-8"))
    cache_doc["schema_version"] = 999
    cache_path.write_text(json.dumps(cache_doc), encoding="utf-8")
    assert _probe(runner, source, probe_root, _cache(tmp_path, runner))["ok"] is True
    assert len(recorder.calls) == 3


def test_runner_or_source_hash_change_invalidates_cache(tmp_path, monkeypatch) -> None:
    source = _write_sgt(tmp_path / "body.sgt")
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"fake-runner-v1")
    probe_root = tmp_path / "probes"
    recorder = _RunRecorder(_fake_validation_payload())
    monkeypatch.setattr(recut.subprocess, "run", recorder)

    assert _probe(runner, source, probe_root, _cache(tmp_path, runner))["ok"] is True
    assert len(recorder.calls) == 1

    runner.write_bytes(b"fake-runner-v2")
    assert _probe(runner, source, probe_root, _cache(tmp_path, runner))["ok"] is True
    assert len(recorder.calls) == 2

    _write_sgt(source, maximum=(11.0, 20.0, 30.0))
    assert _probe(runner, source, probe_root, _cache(tmp_path, runner))["ok"] is True
    assert len(recorder.calls) == 3

    cache_doc = json.loads((tmp_path / "exact_bbox_cache.json").read_text(encoding="utf-8"))
    assert len(cache_doc["entries"]) == 3


def test_disabled_cache_always_runs_the_runner(tmp_path, monkeypatch) -> None:
    source = _write_sgt(tmp_path / "body.sgt")
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"fake-runner")
    recorder = _RunRecorder(_fake_validation_payload())
    monkeypatch.setattr(recut.subprocess, "run", recorder)

    _probe(runner, source, tmp_path / "probes", None)
    _probe(runner, source, tmp_path / "probes", None)
    assert len(recorder.calls) == 2


def test_probe_cache_flags_conflict_is_rejected() -> None:
    args = Namespace(
        dataset=["x"],
        dataset_list=[],
        source_limit=0,
        limit=0,
        body_index=0,
        probe_timeout=60.0,
        probe_cache="cache.json",
        no_probe_cache=True,
        min_complexity_score=0,
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        recut.validate_args(args)


def _run_main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    return recut.main()


def test_main_reuses_probe_cache_across_runs_and_satisfies_require(tmp_path, monkeypatch) -> None:
    source = _write_sgt(tmp_path / "body.sgt")
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"fake-runner")
    recorder = _RunRecorder(_fake_validation_payload())
    monkeypatch.setattr(recut.subprocess, "run", recorder)
    probes = tmp_path / "probes"
    base_argv = [
        "generate_corpus_recut_matrix.py",
        "--dataset",
        str(source),
        "--runner",
        str(runner),
        "--probe-out",
        str(probes),
        "--require-exact-bbox-probe",
        "--no-validate",
    ]

    assert _run_main(monkeypatch, [*base_argv, "--out", str(tmp_path / "m1")]) == 0
    assert len(recorder.calls) == 1

    assert _run_main(monkeypatch, [*base_argv, "--out", str(tmp_path / "m2")]) == 0
    assert len(recorder.calls) == 1
    manifest = json.loads((tmp_path / "m2_manifest.json").read_text(encoding="utf-8"))
    assert manifest["exact_bbox_probe"]["cache_hits"] == 1
    # The cache hit counts as a successful probe under --require-exact-bbox-probe.
    assert manifest["used_source_count"] == 1
    assert manifest["skipped_sources"] == []


def _generate_smoke_lane(tmp_path: Path, monkeypatch, extra_argv: list[str]) -> Path:
    source = _write_sgt(tmp_path / "body.sgt")
    out_dir = tmp_path / "matrix"
    argv = [
        "generate_corpus_recut_matrix.py",
        "--dataset",
        str(source),
        "--out",
        str(out_dir),
        "--no-exact-bbox-probe",
        "--no-validate",
        *extra_argv,
    ]
    return out_dir, _run_main(monkeypatch, argv)


def test_complexity_report_written_with_expected_fields(tmp_path, monkeypatch) -> None:
    out_dir, exit_code = _generate_smoke_lane(tmp_path, monkeypatch, [])
    assert exit_code == 0

    recipe_paths = sorted(out_dir.glob("*.json"))
    assert recipe_paths
    report_path = tmp_path / "matrix_complexity_report.json"
    assert report_path.is_file()
    # The report lives next to the matrix output so batch lanes never pick it up as a recipe.
    assert not (out_dir / "complexity_report.json").exists()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["tool"] == "generate_corpus_recut_matrix.py"
    assert report["model_flat_floor"] == MIN_FLAT_SCORE
    assert report["min_complexity_score"] == 0
    assert report["recipe_count"] == len(recipe_paths)
    assert {entry["case_id"] for entry in report["recipes"]} == {path.stem for path in recipe_paths}
    for entry in report["recipes"]:
        assert set(entry["dimensions"]) == set(DIMENSIONS)
        assert entry["score"] == sum(entry["dimensions"].values())
        assert entry["meets_model_flat_floor"] == (entry["score"] >= MIN_FLAT_SCORE)
        assert entry["oracle_families"]

    aggregate = report["aggregate"]
    assert aggregate["min_score"] == min(entry["score"] for entry in report["recipes"])
    assert aggregate["median_score"] == 2
    assert aggregate["floor_fraction"] == 0.0
    assert aggregate["below_model_floor_count"] == len(recipe_paths)
    assert aggregate["below_min_score_count"] == 0

    histogram = report["dimension_histogram"]
    assert set(histogram) == set(DIMENSIONS)
    assert histogram["oracle_strength"] == len(recipe_paths)
    assert histogram["transform_usage"] == len(recipe_paths)
    assert histogram["generated_topology"] == 0

    manifest = json.loads((tmp_path / "matrix_manifest.json").read_text(encoding="utf-8"))
    assert manifest["complexity"]["report"] == str(report_path)
    assert manifest["complexity"]["aggregate"]["min_score"] == aggregate["min_score"]


def test_min_complexity_score_gate_fails_loudly_but_keeps_recipes(tmp_path, monkeypatch, capsys) -> None:
    out_dir, exit_code = _generate_smoke_lane(tmp_path, monkeypatch, ["--min-complexity-score", "3"])
    assert exit_code == 2
    # Low-scoring recipes are reported, never dropped.
    assert sorted(out_dir.glob("*.json"))
    assert (tmp_path / "matrix_complexity_report.json").is_file()
    captured = capsys.readouterr()
    assert "complexity gate failed" in captured.out


def test_min_complexity_score_gate_passes_at_lane_minimum(tmp_path, monkeypatch) -> None:
    _out_dir, exit_code = _generate_smoke_lane(tmp_path, monkeypatch, ["--min-complexity-score", "2"])
    assert exit_code == 0
