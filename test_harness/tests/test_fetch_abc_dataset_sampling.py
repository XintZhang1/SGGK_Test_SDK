from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

from test_harness.tools import fetch_abc_dataset as fetch


def _args(**overrides) -> Namespace:
    base = {
        "extract_mode": "sample",
        "sample_count": 50,
        "sample_strategy": "seeded",
        "sample_seed": fetch.DEFAULT_SAMPLE_SEED,
    }
    base.update(overrides)
    return Namespace(**base)


def _files(count: int = 200) -> list[str]:
    return [f"000{i:05d}/model.step" for i in range(count)]


def _select(files: list[str], **overrides) -> list[str]:
    options = {
        "sample_count": 25,
        "strategy": "seeded",
        "seed": fetch.DEFAULT_SAMPLE_SEED,
        "chunk": 3,
        "fmt": "step",
    }
    options.update(overrides)
    return fetch.select_sample_files(files, **options)


def test_seeded_sampling_is_deterministic_across_runs() -> None:
    files = _files()
    first = _select(files)
    second = _select(files)
    assert first == second
    assert len(first) == 25
    assert len(set(first)) == 25
    assert set(first) <= set(files)


def test_seeded_sampling_differs_from_head_and_varies_with_seed_chunk_format() -> None:
    files = _files()
    seeded = _select(files)
    assert _select(files, strategy="head") == files[:25]
    assert set(seeded) != set(files[:25])
    assert set(_select(files, seed=1)) != set(seeded)
    assert set(_select(files, chunk=4)) != set(seeded)
    assert set(_select(files, fmt="meta")) != set(seeded)


def test_seeded_sampling_preserves_archive_listing_order() -> None:
    files = [f"b/{index}.step" for index in range(60)] + [f"a/{index}.step" for index in range(60)]
    selected = _select(files, sample_count=30, chunk=0)
    order = {name: index for index, name in enumerate(files)}
    positions = [order[name] for name in selected]
    assert positions == sorted(positions)


def test_sampling_returns_everything_when_count_covers_chunk() -> None:
    files = ["a.step", "b.step"]
    assert _select(files, sample_count=50) == files
    assert _select(files, sample_count=2, strategy="head") == files


def test_mode_label_separates_strategies_seeds_and_full_mode() -> None:
    assert fetch.sample_mode_label(_args(sample_strategy="head")) == "sample50"
    assert fetch.sample_mode_label(_args()) == "sample50_seed20260706"
    assert fetch.sample_mode_label(_args(sample_seed=1)) == "sample50_seed1"
    assert fetch.sample_mode_label(_args(extract_mode="full")) == "full"
    archive = Path("abc_0000_step_v00.7z")
    head_marker = fetch.extraction_marker_path(Path("out"), archive, "sample50")
    seeded_marker = fetch.extraction_marker_path(Path("out"), archive, "sample50_seed20260706")
    assert head_marker != seeded_marker


def _write_fixture_manifests(out_root: Path, chunks: list[int]) -> None:
    manifest_root = out_root / "manifests"
    manifest_root.mkdir(parents=True)
    step_names = [f"abc_{chunk:04d}_step_v00.7z" for chunk in chunks]
    meta_names = [f"abc_{chunk:04d}_meta_v00.7z" for chunk in chunks]
    (manifest_root / "step_v00.txt").write_text(
        "\n".join(f"https://example.invalid/{name} {name}" for name in step_names), encoding="utf-8"
    )
    (manifest_root / "meta_v00.txt").write_text(
        "\n".join(f"https://example.invalid/{name} {name}" for name in meta_names), encoding="utf-8"
    )
    all_names = step_names + meta_names
    (manifest_root / "size.yml").write_text(
        "\n".join(f"{name}: {index + 10}" for index, name in enumerate(all_names)), encoding="utf-8"
    )
    (manifest_root / "md5.yml").write_text("\n".join(f"{name}: {'0' * 32}" for name in all_names), encoding="utf-8")


def _run_plan_only(tmp_path: Path, monkeypatch, extra_argv: list[str]) -> Path:
    out_root = tmp_path / "abc"
    _write_fixture_manifests(out_root, [0, 1])
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch_abc_dataset.py", "--out", str(out_root), "--chunk", "0", "--plan-only", *extra_argv],
    )
    assert fetch.main() == 0
    return out_root


def test_plan_progress_and_summary_record_default_seeded_strategy(tmp_path, monkeypatch) -> None:
    out_root = _run_plan_only(tmp_path, monkeypatch, [])

    plan = json.loads((out_root / "abc_fetch_plan.json").read_text(encoding="utf-8"))
    assert plan["sample_strategy"] == "seeded"
    assert plan["sample_seed"] == fetch.DEFAULT_SAMPLE_SEED
    assert plan["archives"]
    for row in plan["archives"]:
        assert row["sample_strategy"] == "seeded"
        assert row["sample_seed"] == fetch.DEFAULT_SAMPLE_SEED

    csv_header = (out_root / "abc_fetch_plan.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "sample_strategy" in csv_header
    assert "sample_seed" in csv_header

    markdown = (out_root / "abc_fetch_plan.md").read_text(encoding="utf-8")
    assert "seeded" in markdown
    assert str(fetch.DEFAULT_SAMPLE_SEED) in markdown

    progress = json.loads((out_root / "abc_fetch_progress.json").read_text(encoding="utf-8"))
    assert progress["sample_strategy"] == "seeded"
    assert progress["sample_seed"] == fetch.DEFAULT_SAMPLE_SEED

    summary = json.loads((out_root / "abc_fetch_summary.json").read_text(encoding="utf-8"))
    assert summary["sample_strategy"] == "seeded"
    assert summary["sample_seed"] == fetch.DEFAULT_SAMPLE_SEED


def test_plan_records_explicit_head_strategy_and_seed(tmp_path, monkeypatch) -> None:
    out_root = _run_plan_only(tmp_path, monkeypatch, ["--sample-strategy", "head", "--sample-seed", "99"])

    plan = json.loads((out_root / "abc_fetch_plan.json").read_text(encoding="utf-8"))
    assert plan["sample_strategy"] == "head"
    assert plan["sample_seed"] == 99
    progress = json.loads((out_root / "abc_fetch_progress.json").read_text(encoding="utf-8"))
    assert progress["sample_strategy"] == "head"
    assert progress["sample_seed"] == 99
