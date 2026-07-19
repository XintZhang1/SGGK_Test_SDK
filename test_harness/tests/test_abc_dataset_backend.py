from __future__ import annotations

import io
import json
import sys
import time
from argparse import Namespace
from hashlib import md5, sha256
from pathlib import Path

import pytest

from test_harness.tools import fetch_abc_dataset as fetch
from test_harness.ui import abc_dataset
from test_harness.ui.abc_dataset import AbcDatasetBackend, AbcDatasetError, inspect_existing_abc_dataset


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, status: int) -> None:
        super().__init__(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_download_is_resumed_into_part_and_atomically_published(tmp_path, monkeypatch) -> None:
    payload = b"abcdefghij"
    destination = tmp_path / "abc_0000_step_v00.7z"
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(payload[:4])
    requests = []

    def fake_urlopen(request, timeout):
        assert timeout == 60
        requests.append(request)
        assert destination.exists() is False
        return FakeResponse(payload[4:], status=206)

    monkeypatch.setattr(fetch.shutil, "which", lambda _name: None)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake_urlopen)
    progress = []
    result = fetch.download_url(
        "https://example.invalid/archive",
        destination,
        expected_size=len(payload),
        expected_md5=md5(payload).hexdigest(),
        progress=lambda current, total: progress.append((current, total)),
    )

    assert requests[0].get_header("Range") == "bytes=4-"
    assert destination.read_bytes() == payload
    assert not part.exists()
    assert result["resumed"] is True
    assert progress[-1] == (len(payload), len(payload))


def test_download_does_not_publish_bad_checksum(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "archive.7z"
    monkeypatch.setattr(fetch.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        fetch.urllib.request,
        "urlopen",
        lambda _request, timeout: FakeResponse(b"wrong", status=200),
    )

    with pytest.raises(fetch.FetchError, match="failed size or MD5"):
        fetch.download_url(
            "https://example.invalid/archive",
            destination,
            expected_size=5,
            expected_md5=md5(b"right").hexdigest(),
            max_attempts=1,
        )

    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


def test_archive_member_validation_blocks_path_escape() -> None:
    fetch.validate_archive_members(["00000001/model.step", "metadata/model.yml"])
    for unsafe in (
        "../outside.step",
        "/absolute.step",
        "C:/outside.step",
        "safe/../../outside.step",
        "-C",
    ):
        with pytest.raises(fetch.FetchError, match="unsafe archive member"):
            fetch.validate_archive_members([unsafe])


def test_archive_type_validation_rejects_links() -> None:
    fetch.validate_archive_entry_types(["-rw-r--r-- user/group 12 file.step"])
    for entry in (
        "lrwxrwxrwx user/group 0 link.step -> ../../outside.step",
        "hrw-r--r-- user/group 0 hard.step link to outside.step",
    ):
        with pytest.raises(fetch.FetchError, match="symbolic or hard link"):
            fetch.validate_archive_entry_types([entry])


def test_selected_archives_require_complete_integrity_metadata() -> None:
    entries = {
        "step": {0: {"name": "abc_0000_step_v00.7z", "url": "https://example.invalid/step"}},
        "meta": {0: {"name": "abc_0000_meta_v00.7z", "url": "https://example.invalid/meta"}},
    }
    with pytest.raises(fetch.FetchError, match="invalid size"):
        fetch.validate_selected_archive_metadata(
            ["step", "meta"],
            [0],
            entries,
            {"abc_0000_step_v00.7z": 10},
            {
                "abc_0000_step_v00.7z": "0" * 32,
                "abc_0000_meta_v00.7z": "0" * 32,
            },
            require_md5=True,
        )
    with pytest.raises(fetch.FetchError, match="invalid MD5"):
        fetch.validate_selected_archive_metadata(
            ["step", "meta"],
            [0],
            entries,
            {"abc_0000_step_v00.7z": 10, "abc_0000_meta_v00.7z": 11},
            {"abc_0000_step_v00.7z": "0" * 32},
            require_md5=True,
        )


def test_full_dataset_mode_has_one_unambiguous_configuration() -> None:
    args = Namespace(
        sample_count=50,
        smallest_step=1,
        max_step_download_gb=0.0,
        full_dataset=True,
        chunk=[],
        chunk_range=[],
        format=[],
        all_chunks=False,
        extract_mode="sample",
        run_discovery=False,
        run_feature_profile=False,
        fail_on_command=False,
        plan_only=False,
    )
    result = fetch.apply_mode_defaults(args)
    assert result.format == ["step", "meta"]
    assert result.all_chunks is True
    assert result.extract_mode == "full"
    assert result.run_discovery is True
    assert result.fail_on_command is True


def test_full_plan_uses_fixture_manifests_without_downloading(tmp_path, monkeypatch) -> None:
    out_root = tmp_path / "abc"
    manifest_root = out_root / "manifests"
    manifest_root.mkdir(parents=True)
    step_names = [f"abc_{chunk:04d}_step_v00.7z" for chunk in range(100)]
    meta_names = [f"abc_{chunk:04d}_meta_v00.7z" for chunk in range(100)]
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
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch_abc_dataset.py", "--out", str(out_root), "--full-dataset", "--plan-only"],
    )

    assert fetch.main() == 0
    plan = json.loads((out_root / "abc_fetch_plan.json").read_text(encoding="utf-8"))
    progress = json.loads((out_root / "abc_fetch_progress.json").read_text(encoding="utf-8"))
    assert plan["selected_chunk_count"] == 100
    assert plan["selected_archive_count"] == 200
    assert progress["status"] == "completed"
    assert progress["phase"] == "planned"


def test_full_dataset_rejects_truncated_archive_manifest() -> None:
    entries = {
        "step": {
            chunk: {
                "name": f"abc_{chunk:04d}_step_v00.7z",
                "url": "https://example.invalid/step",
            }
            for chunk in range(99)
        },
        "meta": {
            chunk: {
                "name": f"abc_{chunk:04d}_meta_v00.7z",
                "url": "https://example.invalid/meta",
            }
            for chunk in range(100)
        },
    }

    with pytest.raises(fetch.FetchError, match="step manifest is missing v00 chunks: 0099"):
        fetch.validate_full_dataset_manifests(entries)


def _write_dataset_index(root: Path, count: int = 3) -> Path:
    files = []
    for index in range(count):
        path = root / "extracted" / f"{index:08d}.step"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ISO-10303-21;", encoding="utf-8")
        files.append({"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()})
    index_path = root / "dataset_index.json"
    index_path.write_text(json.dumps({"total_files": count, "files": files}), encoding="utf-8")
    return index_path


def test_existing_fetch_root_returns_campaign_dataset(tmp_path) -> None:
    index_path = _write_dataset_index(tmp_path)
    report = inspect_existing_abc_dataset(tmp_path)
    assert report["valid"] is True
    assert report["ready"] is True
    assert report["kind"] == "fetch_root"
    assert report["campaign_dataset"] == str(index_path)
    assert report["total_files"] == 3


def test_large_index_uses_bounded_sidecar_validation(tmp_path, monkeypatch) -> None:
    index_path = _write_dataset_index(tmp_path, count=3)
    paths_path = tmp_path / "dataset_index.paths.txt"
    index_value = json.loads(index_path.read_text(encoding="utf-8"))
    paths_path.write_text(
        "\n".join(item["path"] for item in index_value["files"]) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "dataset_index.meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_index_sha256": sha256(index_path.read_bytes()).hexdigest(),
                "total_files": 3,
                "entry_content_hash": "sha256",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(abc_dataset, "MAX_INLINE_INDEX_BYTES", 1)

    report = inspect_existing_abc_dataset(tmp_path, maximum_index_checks=2)

    assert report["ready"] is True
    assert report["checked_files"] == 2
    assert report["total_files"] == 3


def test_large_index_without_sidecars_fails_without_loading_json(tmp_path, monkeypatch) -> None:
    index_path = tmp_path / "dataset_index.json"
    index_path.write_text("{not-json-but-large-enough}", encoding="utf-8")
    monkeypatch.setattr(abc_dataset, "MAX_INLINE_INDEX_BYTES", 1)

    report = inspect_existing_abc_dataset(index_path)

    assert report["ready"] is False
    assert "meta.json" in report["errors"][0]


def test_existing_raw_step_directory_requires_index(tmp_path) -> None:
    (tmp_path / "abc.step").write_text("ISO-10303-21;", encoding="utf-8")
    report = inspect_existing_abc_dataset(tmp_path)
    assert report["valid"] is True
    assert report["ready"] is False
    assert report["kind"] == "raw_step_directory"
    assert report["needs_index"] is True


def test_existing_dataset_inspection_rejects_empty_path() -> None:
    with pytest.raises(AbcDatasetError, match="select an ABC"):
        inspect_existing_abc_dataset("")


def test_fetch_request_rejects_repository_source_directory(tmp_path) -> None:
    repo = tmp_path / "repo"
    (repo / "test_harness" / "tools").mkdir(parents=True)
    with pytest.raises(AbcDatasetError, match="outside the repository or under artifacts"):
        abc_dataset.normalize_fetch_request(repo, {"mode": "full", "out_root": str(repo / "dataset")})


def test_backend_can_cancel_fetch_process(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    (repo / "test_harness" / "tools").mkdir(parents=True)
    output = tmp_path / "dataset"
    progress = output / "abc_fetch_progress.json"
    log = output / "abc_fetch_ui.log"
    monkeypatch.setattr(
        abc_dataset,
        "build_fetch_command",
        lambda _repo, _request: ([sys.executable, "-c", "import time; time.sleep(30)"], progress, log),
    )
    backend = AbcDatasetBackend(repo)
    started = backend.start_fetch({"mode": "full", "out_root": str(output)})
    assert started["status"] == "running"
    backend.cancel()
    deadline = time.monotonic() + 3
    while backend.snapshot()["status"] not in {"cancelled", "failed"} and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend.snapshot()["status"] == "cancelled"
