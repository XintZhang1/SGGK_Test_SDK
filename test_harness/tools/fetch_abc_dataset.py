#!/usr/bin/env python3
"""Fetch, verify, and sample official ABC dataset chunks.

Sample extraction defaults to a deterministic seeded per-chunk selection
(``--sample-strategy seeded``) so ABC testing is not biased toward the head of
each archive; ``--sample-strategy head`` reproduces the legacy first-N
selection for rerunning historical lanes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

BASE_URL = "https://deep-geometry.github.io/abc-dataset/data"
MANIFEST_FILES = ("step_v00.txt", "meta_v00.txt", "size.yml", "md5.yml")
ABC_V00_EXPECTED_CHUNKS = frozenset(range(100))
FORMAT_EXTENSIONS = {
    "step": ".step",
    "meta": ".yml",
}
RESUMABLE_CURL_RETURN_CODES = {18, 56}
DOWNLOAD_BUFFER_BYTES = 1024 * 1024
PROGRESS_SCHEMA_VERSION = 1
DEFAULT_SAMPLE_SEED = 20260706
SAMPLE_STRATEGIES = ("head", "seeded")


class FetchError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="artifacts/abc_dataset",
        help="Output root for manifests, downloads, extracted files, and reports",
    )
    parser.add_argument("--download-root", default="", help="Archive cache directory; defaults to <out>/downloads")
    parser.add_argument(
        "--full-dataset",
        action="store_true",
        help="Fetch every official STEP+meta chunk, fully extract it, and generate dataset_index.json",
    )
    parser.add_argument(
        "--format",
        action="append",
        choices=sorted(FORMAT_EXTENSIONS),
        default=[],
        help="ABC format to fetch; default is step plus meta",
    )
    parser.add_argument("--chunk", action="append", default=[], help="Chunk number such as 27 or 0027. Can be repeated")
    parser.add_argument(
        "--chunk-range", action="append", default=[], help="Inclusive chunk range such as 0:4. Can be repeated"
    )
    parser.add_argument(
        "--all-chunks", action="store_true", help="Select all STEP chunks when no explicit chunk is given"
    )
    parser.add_argument(
        "--smallest-step", type=int, default=1, help="Select N smallest STEP chunks when no explicit chunk is given"
    )
    parser.add_argument(
        "--max-step-download-gb",
        type=float,
        default=0.0,
        help="Select smallest STEP chunks up to this total STEP archive budget when no explicit chunk is given",
    )
    parser.add_argument(
        "--plan-only", action="store_true", help="Write fetch plan files and exit without downloads or extraction"
    )
    parser.add_argument("--refresh-manifests", action="store_true", help="Re-download official manifests")
    parser.add_argument("--skip-download", action="store_true", help="Require archives to already exist")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip MD5 verification; archive size is still enforced for resumable-download safety",
    )
    parser.add_argument("--extract-mode", choices=["none", "sample", "full"], default="sample")
    parser.add_argument("--sample-count", type=int, default=50, help="Files per chunk/format for sample extraction")
    parser.add_argument(
        "--sample-strategy",
        choices=list(SAMPLE_STRATEGIES),
        default="seeded",
        help="Sample selection per chunk: 'seeded' draws a deterministic seeded sample across the archive; "
        "'head' takes the first N files (legacy behavior)",
    )
    parser.add_argument(
        "--sample-seed",
        type=int,
        default=DEFAULT_SAMPLE_SEED,
        help="Seed for --sample-strategy seeded; recorded in the plan, progress, and extraction markers",
    )
    parser.add_argument("--run-discovery", action="store_true", help="Run discover_corpus.py over extracted STEP files")
    parser.add_argument(
        "--run-feature-profile", action="store_true", help="Run profile_cad_features.py after discovery"
    )
    parser.add_argument(
        "--fail-on-command", action="store_true", help="Fail when optional discovery/profile commands fail"
    )
    parser.add_argument(
        "--progress-file", default="", help="Machine-readable progress JSON; defaults to <out>/abc_fetch_progress.json"
    )
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_json_atomic(path: Path, value: Any) -> None:
    """Write polling state without exposing a partially written JSON document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class ProgressReporter:
    """Persist a compact status contract that can be polled by the local UI."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._last_write = 0.0
        self._download_started = time.monotonic()
        self._state: dict[str, Any] = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "status": "preparing",
            "phase": "starting",
            "started_at": now_utc(),
            "updated_at": now_utc(),
            "finished_at": "",
            "message": "Preparing ABC dataset fetch",
            "error": "",
            "out_root": "",
            "download_root": "",
            "plan_path": "",
            "summary_path": "",
            "dataset_index": "",
            "sample_strategy": "",
            "sample_seed": None,
            "download": {
                "total_bytes": 0,
                "completed_bytes": 0,
                "downloaded_bytes_this_run": 0,
                "percent": 0.0,
                "bytes_per_second": 0.0,
                "archives_total": 0,
                "archives_completed": 0,
                "archives_reused": 0,
                "current": None,
            },
        }
        self._write(force=True)

    def _write(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < 0.2:
            return
        self._state["updated_at"] = now_utc()
        write_json_atomic(self.path, self._state)
        self._last_write = now

    def phase(self, phase: str, message: str, *, force: bool = True) -> None:
        self._state["phase"] = phase
        self._state["message"] = message
        if self._state["status"] == "preparing":
            self._state["status"] = "running"
        self._write(force=force)

    def configure(self, out_root: Path, downloads: Path, plan: dict[str, Any]) -> None:
        archive_rows = [row for row in plan.get("archives", []) if isinstance(row, dict)]
        download = self._state["download"]
        download.update(
            {
                "total_bytes": int(plan.get("total_bytes") or 0),
                "completed_bytes": 0,
                "archives_total": len(archive_rows),
                "archives_completed": 0,
                "archives_reused": 0,
            }
        )
        self._state["out_root"] = str(out_root.resolve())
        self._state["download_root"] = str(downloads.resolve())
        self._state["plan_path"] = str((out_root / "abc_fetch_plan.json").resolve())
        self._state["sample_strategy"] = str(plan.get("sample_strategy") or "")
        self._state["sample_seed"] = plan.get("sample_seed")
        self._refresh_download_metrics()
        self._write(force=True)

    def archive_started(
        self,
        *,
        chunk: int,
        fmt: str,
        archive: str,
        expected_bytes: int,
        initial_bytes: int,
    ) -> None:
        self._state["download"]["current"] = {
            "chunk": f"{chunk:04d}",
            "format": fmt,
            "archive": archive,
            "expected_bytes": expected_bytes,
            "downloaded_bytes": initial_bytes,
        }
        self._write(force=True)

    def archive_progress(self, downloaded_bytes: int, _total_bytes: int | None = None) -> None:
        current = self._state["download"].get("current")
        if not isinstance(current, dict):
            return
        previous = int(current.get("downloaded_bytes") or 0)
        current["downloaded_bytes"] = downloaded_bytes
        self._state["download"]["downloaded_bytes_this_run"] += max(downloaded_bytes - previous, 0)
        self._refresh_download_metrics()
        self._write()

    def archive_completed(self, expected_bytes: int, *, reused: bool) -> None:
        download = self._state["download"]
        current = download.get("current")
        current_bytes = int(current.get("downloaded_bytes") or 0) if isinstance(current, dict) else 0
        download["completed_bytes"] += expected_bytes
        download["archives_completed"] += 1
        if reused:
            download["archives_reused"] += 1
        elif current_bytes > expected_bytes:
            download["downloaded_bytes_this_run"] -= current_bytes - expected_bytes
        download["current"] = None
        self._refresh_download_metrics()
        self._write(force=True)

    def _refresh_download_metrics(self) -> None:
        download = self._state["download"]
        current = download.get("current")
        in_progress = int(current.get("downloaded_bytes") or 0) if isinstance(current, dict) else 0
        total = int(download.get("total_bytes") or 0)
        completed = min(int(download.get("completed_bytes") or 0) + in_progress, total) if total else 0
        download["percent"] = round((completed * 100.0 / total), 3) if total else 0.0
        elapsed = max(time.monotonic() - self._download_started, 0.001)
        download["bytes_per_second"] = round(int(download.get("downloaded_bytes_this_run") or 0) / elapsed, 1)

    def completed(self, *, summary: Path, dataset_index: Path | None = None, plan_only: bool = False) -> None:
        self._state.update(
            {
                "status": "completed",
                "phase": "planned" if plan_only else "completed",
                "finished_at": now_utc(),
                "message": "ABC fetch plan is ready" if plan_only else "ABC dataset fetch completed",
                "summary_path": str(summary.resolve()),
                "dataset_index": str(dataset_index.resolve()) if dataset_index and dataset_index.is_file() else "",
            }
        )
        if not plan_only:
            self._state["download"]["percent"] = 100.0
        self._state["download"]["current"] = None
        self._write(force=True)

    def failed(self, error: str) -> None:
        self._state.update(
            {
                "status": "failed",
                "phase": "failed",
                "finished_at": now_utc(),
                "message": "ABC dataset fetch failed",
                "error": error,
            }
        )
        self._write(force=True)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "chunk",
        "format",
        "archive",
        "url",
        "size_bytes",
        "size_gib",
        "md5",
        "download_path",
        "download_exists",
        "download_size_bytes",
        "download_size_ok",
        "partial_path",
        "partial_size_bytes",
        "remaining_bytes",
        "sample_strategy",
        "sample_seed",
    ]
    with path.open("w", newline="", encoding="utf-8") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _partial_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.name}.part")


def _validated_download(path: Path, expected_size: int | None, expected_md5: str | None) -> bool:
    if not path.is_file():
        return False
    if expected_size is not None and path.stat().st_size != expected_size:
        return False
    if expected_md5 and file_md5(path).lower() != expected_md5.lower():
        return False
    return True


def _prepare_partial(out_path: Path, expected_size: int | None, expected_md5: str | None) -> Path:
    part_path = _partial_path(out_path)
    if not out_path.is_file() or _validated_download(out_path, expected_size, expected_md5):
        return part_path

    final_size = out_path.stat().st_size
    partial_size = part_path.stat().st_size if part_path.is_file() else -1
    can_resume_final = expected_size is not None and 0 < final_size < expected_size
    if can_resume_final and final_size > partial_size:
        os.replace(out_path, part_path)
    else:
        out_path.unlink(missing_ok=True)
    return part_path


def _curl_attempt(
    curl: str,
    url: str,
    part_path: Path,
    expected_size: int | None,
    progress: Callable[[int, int | None], None] | None,
) -> int:
    resume = part_path.is_file() and part_path.stat().st_size > 0
    cmd = [
        curl,
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--retry",
        "5",
        "--retry-delay",
        "3",
    ]
    if resume:
        cmd.extend(["--continue-at", "-"])
    cmd.extend(["-o", str(part_path), url])
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    while process.poll() is None:
        if progress is not None:
            progress(part_path.stat().st_size if part_path.is_file() else 0, expected_size)
        time.sleep(0.2)
    _stdout, stderr = process.communicate()
    if process.returncode and stderr:
        message = stderr.decode("utf-8", errors="replace").strip()
        if message:
            print(message, file=sys.stderr)
    if progress is not None:
        progress(part_path.stat().st_size if part_path.is_file() else 0, expected_size)
    return int(process.returncode or 0)


def _urllib_attempt(
    url: str,
    part_path: Path,
    expected_size: int | None,
    progress: Callable[[int, int | None], None] | None,
) -> None:
    offset = part_path.stat().st_size if part_path.is_file() else 0
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        status = getattr(response, "status", None)
        if status is None and hasattr(response, "getcode"):
            status = response.getcode()
        if offset and status != 206:
            # The endpoint ignored Range. Keep the final file untouched and restart only the .part file.
            offset = 0
        mode = "ab" if offset else "wb"
        downloaded = offset
        with part_path.open(mode) as out_file:
            while True:
                chunk = response.read(DOWNLOAD_BUFFER_BYTES)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)
                if progress is not None:
                    progress(downloaded, expected_size)
    if progress is not None:
        progress(part_path.stat().st_size, expected_size)


def download_url(
    url: str,
    out_path: Path,
    *,
    expected_size: int | None = None,
    expected_md5: str | None = None,
    progress: Callable[[int, int | None], None] | None = None,
    max_attempts: int = 6,
    force: bool = False,
) -> dict[str, Any]:
    """Download to ``.part`` and atomically publish only verified content."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not force and _validated_download(out_path, expected_size, expected_md5):
        size = out_path.stat().st_size
        if progress is not None:
            progress(size, expected_size)
        return {"path": str(out_path), "bytes": size, "reused": True, "resumed": False}

    part_path = _partial_path(out_path) if force else _prepare_partial(out_path, expected_size, expected_md5)
    if force:
        part_path.unlink(missing_ok=True)
    initial_size = part_path.stat().st_size if part_path.is_file() else 0
    curl = shutil.which("curl.exe") or shutil.which("curl")
    last_error = "download did not complete"
    for attempt in range(max(max_attempts, 1)):
        has_verification = expected_size is not None or bool(expected_md5)
        if has_verification and _validated_download(part_path, expected_size, expected_md5):
            os.replace(part_path, out_path)
            size = out_path.stat().st_size
            return {"path": str(out_path), "bytes": size, "reused": False, "resumed": initial_size > 0}
        try:
            if curl:
                returncode = _curl_attempt(curl, url, part_path, expected_size, progress)
                if returncode == 33 and part_path.is_file():
                    part_path.unlink()
                    last_error = "server rejected byte-range resume"
                    continue
                if returncode != 0 and returncode not in RESUMABLE_CURL_RETURN_CODES:
                    raise FetchError(f"curl failed with return code {returncode}")
                if returncode != 0:
                    last_error = f"curl interrupted with return code {returncode}"
                    continue
            else:
                _urllib_attempt(url, part_path, expected_size, progress)
        except (OSError, urllib.error.URLError, FetchError) as exc:
            last_error = str(exc)
            if attempt + 1 < max(max_attempts, 1):
                time.sleep(min(attempt + 1, 3))
                continue
            break

        if part_path.is_file() and (
            _validated_download(part_path, expected_size, expected_md5) if has_verification else True
        ):
            os.replace(part_path, out_path)
            size = out_path.stat().st_size
            return {"path": str(out_path), "bytes": size, "reused": False, "resumed": initial_size > 0}

        actual_size = part_path.stat().st_size if part_path.is_file() else 0
        if expected_size is not None and actual_size < expected_size:
            last_error = f"partial response ({actual_size}/{expected_size} bytes)"
            continue
        last_error = "downloaded content failed size or MD5 verification"
        part_path.unlink(missing_ok=True)

    raise FetchError(f"download failed for {url}: {last_error}; partial={part_path}")


def ensure_manifests(out_root: Path, refresh: bool) -> dict[str, Path]:
    manifest_dir = out_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for name in MANIFEST_FILES:
        path = manifest_dir / name
        if refresh or not path.is_file():
            download_url(f"{BASE_URL}/{name}", path, force=refresh)
        result[name] = path
    return result


def apply_mode_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Expand the UI-facing full mode into the deterministic CLI settings."""

    if args.sample_count < 0:
        raise FetchError("--sample-count must be >= 0")
    if args.smallest_step < 0:
        raise FetchError("--smallest-step must be >= 0")
    if args.max_step_download_gb < 0:
        raise FetchError("--max-step-download-gb must be >= 0")
    if args.full_dataset:
        if args.chunk or args.chunk_range or args.max_step_download_gb > 0:
            raise FetchError("--full-dataset cannot be combined with explicit chunk or budget selection")
        if args.format and set(args.format) != set(FORMAT_EXTENSIONS):
            raise FetchError("--full-dataset always requires both STEP and meta formats")
        args.format = ["step", "meta"]
        args.all_chunks = True
        args.extract_mode = "full"
        args.run_discovery = not args.plan_only
        args.fail_on_command = not args.plan_only
    if args.run_feature_profile:
        args.run_discovery = True
    return args


@contextmanager
def exclusive_fetch_lock(download_root: Path) -> Iterator[None]:
    """Prevent two fetchers from mutating the same archive cache concurrently."""

    download_root.mkdir(parents=True, exist_ok=True)
    lock_path = download_root / ".abc_fetch.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise FetchError(f"another ABC fetch is using download root: {download_root}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def parse_name_values(path: Path, value_kind: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        name, raw_value = line.split(":", 1)
        key = name.strip()
        value = raw_value.strip()
        if not key or not value:
            continue
        if value_kind == "int":
            try:
                values[key] = int(value)
            except ValueError:
                continue
        else:
            values[key] = value
    return values


def parse_archive_manifest(path: Path, fmt: str) -> dict[int, dict[str, str]]:
    result: dict[int, dict[str, str]] = {}
    pattern = re.compile(rf"^abc_(\d{{4}})_{re.escape(fmt)}_v00\.7z$")
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        url, name = parts[0], parts[1]
        match = pattern.match(name)
        if match:
            result[int(match.group(1))] = {"name": name, "url": url}
    return result


def validate_selected_archive_metadata(
    formats: list[str],
    chunks: list[int],
    entries_by_format: dict[str, dict[int, dict[str, str]]],
    sizes: dict[str, int],
    md5s: dict[str, str],
    *,
    require_md5: bool,
) -> None:
    """Fail closed when official integrity metadata is missing or malformed."""

    issues: list[str] = []
    for chunk in chunks:
        for fmt in formats:
            item = entries_by_format.get(fmt, {}).get(chunk)
            if not item:
                issues.append(f"missing {fmt} manifest entry for chunk {chunk:04d}")
                continue
            name = item["name"]
            size = sizes.get(name)
            if not isinstance(size, int) or size <= 0:
                issues.append(f"missing or invalid size for {name}")
            digest = str(md5s.get(name) or "")
            if require_md5 and re.fullmatch(r"[0-9A-Fa-f]{32}", digest) is None:
                issues.append(f"missing or invalid MD5 for {name}")
    if issues:
        preview = "; ".join(issues[:8])
        suffix = f"; plus {len(issues) - 8} more" if len(issues) > 8 else ""
        raise FetchError(f"ABC integrity manifest is incomplete: {preview}{suffix}")


def validate_full_dataset_manifests(
    entries_by_format: dict[str, dict[int, dict[str, str]]],
) -> None:
    """Require the complete, immutable ABC v00 STEP and metadata chunk sets."""

    issues: list[str] = []
    for fmt in FORMAT_EXTENSIONS:
        actual = set(entries_by_format.get(fmt, {}))
        missing = sorted(ABC_V00_EXPECTED_CHUNKS - actual)
        unexpected = sorted(actual - ABC_V00_EXPECTED_CHUNKS)
        if missing:
            preview = ", ".join(f"{chunk:04d}" for chunk in missing[:8])
            issues.append(f"{fmt} manifest is missing v00 chunks: {preview}")
        if unexpected:
            preview = ", ".join(f"{chunk:04d}" for chunk in unexpected[:8])
            issues.append(f"{fmt} manifest has unexpected v00 chunks: {preview}")
    if issues:
        raise FetchError("ABC full-dataset manifests are incomplete: " + "; ".join(issues))


def parse_chunk_text(raw: str) -> int:
    text = str(raw).strip()
    if not text:
        raise FetchError("empty chunk value")
    return int(text)


def selected_chunks(
    args: argparse.Namespace, step_entries: dict[int, dict[str, str]], sizes: dict[str, int]
) -> list[int]:
    explicit: set[int] = set()
    for raw in args.chunk:
        explicit.add(parse_chunk_text(raw))
    for raw_range in args.chunk_range:
        parts = raw_range.split(":", 1)
        if len(parts) != 2:
            raise FetchError(f"invalid --chunk-range {raw_range!r}; expected START:END")
        start = parse_chunk_text(parts[0])
        end = parse_chunk_text(parts[1])
        if end < start:
            raise FetchError(f"invalid --chunk-range {raw_range!r}; END must be >= START")
        explicit.update(range(start, end + 1))
    if explicit:
        return sorted(explicit)

    if args.all_chunks:
        return sorted(step_entries)

    rows: list[tuple[int, int]] = []
    for chunk, item in step_entries.items():
        size = sizes.get(item["name"])
        if isinstance(size, int):
            rows.append((size, chunk))
    rows = sorted(rows)
    if args.max_step_download_gb > 0:
        budget_bytes = int(args.max_step_download_gb * 1024 * 1024 * 1024)
        total = 0
        selected: list[int] = []
        for size, chunk in rows:
            if selected and total + size > budget_bytes:
                break
            if size > budget_bytes and not selected:
                break
            selected.append(chunk)
            total += size
        return sorted(selected)
    return [chunk for _, chunk in rows[: max(args.smallest_step, 0)]]


def existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def disk_free_bytes(path: Path) -> int:
    try:
        return int(shutil.disk_usage(existing_parent(path)).free)
    except OSError:
        return 0


def build_fetch_plan(
    out_root: Path,
    downloads: Path,
    formats: list[str],
    chunks: list[int],
    entries_by_format: dict[str, dict[int, dict[str, str]]],
    sizes: dict[str, int],
    md5s: dict[str, str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    bytes_by_format: dict[str, int] = {}
    existing_by_format: dict[str, int] = {}
    existing_bytes_by_format: dict[str, int] = {}
    missing: list[dict[str, Any]] = []

    for chunk in chunks:
        for fmt in formats:
            item = entries_by_format.get(fmt, {}).get(chunk)
            if not item:
                missing.append({"chunk": chunk, "format": fmt, "reason": "missing_manifest_entry"})
                continue
            name = item["name"]
            size = int(sizes.get(name, 0) or 0)
            archive_path = downloads / name
            partial_path = _partial_path(archive_path)
            archive_exists = archive_path.is_file()
            download_size = archive_path.stat().st_size if archive_exists else 0
            partial_size = partial_path.stat().st_size if partial_path.is_file() else 0
            size_ok = archive_exists and (size == 0 or download_size == size)
            bytes_by_format[fmt] = bytes_by_format.get(fmt, 0) + size
            if archive_exists:
                existing_by_format[fmt] = existing_by_format.get(fmt, 0) + 1
                existing_bytes_by_format[fmt] = existing_bytes_by_format.get(fmt, 0) + download_size
            row = {
                "chunk": f"{chunk:04d}",
                "format": fmt,
                "archive": name,
                "url": item["url"],
                "size_bytes": size,
                "size_gib": round(size / (1024**3), 4),
                "md5": md5s.get(name, ""),
                "download_path": str(archive_path),
                "download_exists": archive_exists,
                "download_size_bytes": download_size,
                "download_size_ok": size_ok,
                "partial_path": str(partial_path),
                "partial_size_bytes": partial_size,
                "remaining_bytes": max(size - (size if size_ok else min(partial_size, size)), 0),
            }
            rows.append(row)
            if not size_ok:
                missing.append(
                    {
                        "chunk": chunk,
                        "format": fmt,
                        "archive": name,
                        "expected_size": size,
                        "download_size": download_size,
                    }
                )

    total_bytes = sum(bytes_by_format.values())
    existing_bytes = sum(existing_bytes_by_format.values())
    return {
        "generated_at": now_iso_like(),
        "out_root": str(out_root),
        "download_root": str(downloads),
        "formats": formats,
        "selected_chunks": chunks,
        "selected_chunk_count": len(chunks),
        "selected_archive_count": len(rows),
        "bytes_by_format": dict(sorted(bytes_by_format.items())),
        "total_bytes": total_bytes,
        "total_gib": round(total_bytes / (1024**3), 3),
        "existing_archives_by_format": dict(sorted(existing_by_format.items())),
        "existing_bytes_by_format": dict(sorted(existing_bytes_by_format.items())),
        "existing_bytes": existing_bytes,
        "existing_gib": round(existing_bytes / (1024**3), 3),
        "missing_or_incomplete": missing,
        "missing_or_incomplete_count": len(missing),
        "disk_free_bytes": disk_free_bytes(downloads),
        "disk_free_gib": round(disk_free_bytes(downloads) / (1024**3), 3),
        "archives": rows,
    }


def markdown_fetch_plan(plan: dict[str, Any]) -> str:
    lines = [
        "# ABC Fetch Plan",
        "",
        f"- Generated: `{plan.get('generated_at')}`",
        f"- Output root: `{plan.get('out_root')}`",
        f"- Download root: `{plan.get('download_root')}`",
        f"- Formats: `{', '.join(plan.get('formats', []))}`",
        f"- Chunks: `{plan.get('selected_chunk_count')}`",
        f"- Sample extraction: strategy `{plan.get('sample_strategy')}` seed `{plan.get('sample_seed')}`",
        f"- Archives: `{plan.get('selected_archive_count')}`",
        f"- Total selected bytes: `{plan.get('total_bytes')}` ({plan.get('total_gib')} GiB)",
        f"- Existing archive bytes: `{plan.get('existing_bytes')}` ({plan.get('existing_gib')} GiB)",
        f"- Missing or incomplete archives: `{plan.get('missing_or_incomplete_count')}`",
        f"- Disk free near download root: `{plan.get('disk_free_bytes')}` ({plan.get('disk_free_gib')} GiB)",
        "",
        "## Bytes By Format",
        "",
    ]
    for fmt, value in dict(plan.get("bytes_by_format", {})).items():
        lines.append(f"- `{fmt}`: `{value}` ({round(int(value) / (1024**3), 3)} GiB)")
    lines.extend(
        ["", "## Archives", "", "| chunk | format | size GiB | cached | archive |", "| --- | --- | ---: | --- | --- |"]
    )
    for row in plan.get("archives", []):
        if not isinstance(row, dict):
            continue
        lines.append(
            f"| `{row.get('chunk')}` | `{row.get('format')}` | {row.get('size_gib')} | "
            f"`{row.get('download_size_ok')}` | `{row.get('archive')}` |"
        )
    lines.append("")
    return "\n".join(lines)


def write_fetch_plan(out_root: Path, plan: dict[str, Any]) -> None:
    write_json(out_root / "abc_fetch_plan.json", plan)
    rows = [row for row in plan.get("archives", []) if isinstance(row, dict)]
    write_csv(out_root / "abc_fetch_plan.csv", rows)
    write_text(out_root / "abc_fetch_plan.md", markdown_fetch_plan(plan))


def select_sample_files(
    files: list[str],
    *,
    sample_count: int,
    strategy: str,
    seed: int,
    chunk: int,
    fmt: str,
) -> list[str]:
    """Choose the deterministic per-chunk sample, preserving the archive listing order.

    ``head`` reproduces the legacy first-N selection. ``seeded`` draws from the
    sorted member names with a seed bound to the chunk and format, so every
    chunk/format is sampled independently and repeated runs select the same
    files.
    """

    if sample_count >= len(files):
        return list(files)
    if strategy == "head":
        return files[:sample_count]
    chosen = set(random.Random(f"{seed}:{chunk}:{fmt}").sample(sorted(files), sample_count))
    return [entry for entry in files if entry in chosen]


def sample_mode_label(args: argparse.Namespace) -> str:
    """Extraction identity used for marker paths and output directories.

    ``head`` keeps the legacy ``sample<N>`` label so historical extractions are
    reused; seeded samples include the seed so different strategies or seeds of
    the same count never alias.
    """

    if args.extract_mode == "full":
        return "full"
    if args.sample_strategy == "head":
        return f"sample{args.sample_count}"
    return f"sample{args.sample_count}_seed{args.sample_seed}"


def archive_list(archive_path: Path) -> list[str]:
    detailed = subprocess.run(
        ["tar", "-tvf", str(archive_path)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if detailed.returncode != 0:
        raise FetchError(f"tar verbose list failed for {archive_path}: {detailed.stderr.strip()}")
    validate_archive_entry_types(detailed.stdout.splitlines())
    cmd = ["tar", "-tf", str(archive_path)]
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if completed.returncode != 0:
        raise FetchError(f"tar list failed for {archive_path}: {completed.stderr.strip()}")
    entries = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    validate_archive_members(entries)
    return entries


def validate_archive_entry_types(lines: list[str]) -> None:
    """Reject archive links whose targets are not constrained by member names."""

    for line in lines:
        stripped = line.lstrip()
        if stripped and stripped[0].lower() in {"l", "h"}:
            raise FetchError("archive contains a symbolic or hard link")


def validate_archive_members(entries: list[str]) -> None:
    """Reject archive names that could escape the extraction directory."""

    for entry in entries:
        normalized = entry.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or normalized.startswith("/")
            or normalized.startswith("-")
            or re.match(r"^[A-Za-z]:", normalized)
            or ".." in path.parts
            or "\x00" in normalized
            or "\r" in normalized
            or "\n" in normalized
        ):
            raise FetchError(f"unsafe archive member: {entry!r}")


def extract_archive(archive_path: Path, out_dir: Path, include_files: list[str] | None) -> dict[str, Any]:
    if include_files is not None:
        validate_archive_members(include_files)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["tar", "-xf", str(archive_path), "-C", str(out_dir)]
    list_path = ""
    if include_files is not None:
        list_path = str(out_dir.parent / f"{archive_path.stem}_include.txt")
        write_text(Path(list_path), "\n".join(include_files) + ("\n" if include_files else ""))
        cmd.extend(["-T", list_path])
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True)
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "include_file_count": len(include_files) if include_files is not None else None,
        "include_list": list_path,
        "out": str(out_dir),
    }


def extraction_marker_path(out_root: Path, archive_path: Path, mode_label: str) -> Path:
    return out_root / "extract_state" / f"{archive_path.name}.{mode_label}.json"


def reusable_extraction(
    marker_path: Path,
    *,
    archive_path: Path,
    archive_md5: str,
    expected_files: int,
    out_dir: Path,
    suffix: str,
) -> dict[str, Any] | None:
    if not marker_path.is_file():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict):
        return None
    if marker.get("archive") != archive_path.name or marker.get("archive_md5") != archive_md5:
        return None
    extracted_count = count_extracted_files(out_dir, suffix)
    if extracted_count < expected_files:
        return None
    return {
        "command": [],
        "returncode": 0,
        "ok": True,
        "stdout": "",
        "stderr": "",
        "include_file_count": marker.get("include_file_count"),
        "include_list": marker.get("include_list", ""),
        "out": str(out_dir),
        "archive_file_count": int(marker.get("archive_file_count") or expected_files),
        "extracted_file_count": extracted_count,
        "reused": True,
    }


def verify_archive(path: Path, expected_size: int | None, expected_md5: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "expected_size": expected_size,
        "size_ok": None,
        "md5": "",
        "expected_md5": expected_md5,
        "md5_ok": None,
        "ok": False,
    }
    if not path.is_file():
        return record
    if expected_size is not None:
        record["size_ok"] = record["size"] == expected_size
    if expected_md5:
        digest = file_md5(path)
        record["md5"] = digest
        record["md5_ok"] = digest.lower() == expected_md5.lower()
    record["ok"] = all(value is not False for value in (record["size_ok"], record["md5_ok"]))
    return record


def count_extracted_files(path: Path, suffix: str) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for item in path.rglob(f"*{suffix}") if item.is_file())


def run_tool(cmd: list[str]) -> dict[str, Any]:
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True)
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def main() -> int:
    args = parse_args()
    out_root = Path(args.out)
    progress_path = Path(args.progress_file) if args.progress_file else out_root / "abc_fetch_progress.json"
    reporter = ProgressReporter(progress_path)
    try:
        args = apply_mode_defaults(args)
        formats = args.format or ["step", "meta"]
        downloads = Path(args.download_root) if args.download_root else out_root / "downloads"
        with exclusive_fetch_lock(downloads):
            reporter.phase("manifests", "Fetching official ABC manifests")
            manifests = ensure_manifests(out_root, args.refresh_manifests)
            sizes = parse_name_values(manifests["size.yml"], "int")
            md5s = parse_name_values(manifests["md5.yml"], "str")
            entries_by_format = {fmt: parse_archive_manifest(manifests[f"{fmt}_v00.txt"], fmt) for fmt in formats}
            step_entries = parse_archive_manifest(manifests["step_v00.txt"], "step")
            if args.full_dataset:
                validate_full_dataset_manifests(entries_by_format)
            chunks = selected_chunks(args, step_entries, sizes)
            if not chunks:
                raise FetchError("no chunks selected")
            validate_selected_archive_metadata(
                formats,
                chunks,
                entries_by_format,
                sizes,
                md5s,
                require_md5=not args.no_verify,
            )

            extracted_root = out_root / "extracted"
            reporter.phase("planning", "Building ABC fetch plan")
            plan = build_fetch_plan(out_root, downloads, formats, chunks, entries_by_format, sizes, md5s)
            plan["sample_strategy"] = args.sample_strategy
            plan["sample_seed"] = args.sample_seed
            for row in plan["archives"]:
                row["sample_strategy"] = args.sample_strategy
                row["sample_seed"] = args.sample_seed
            write_fetch_plan(out_root, plan)
            reporter.configure(out_root, downloads, plan)
            summary_path = out_root / "abc_fetch_summary.json"
            if args.plan_only:
                summary = {
                    "generated_at": now_iso_like(),
                    "out_root": str(out_root),
                    "formats": formats,
                    "chunks": chunks,
                    "plan_only": True,
                    "plan": {
                        "json": str(out_root / "abc_fetch_plan.json"),
                        "csv": str(out_root / "abc_fetch_plan.csv"),
                        "markdown": str(out_root / "abc_fetch_plan.md"),
                        "total_bytes": plan["total_bytes"],
                        "total_gib": plan["total_gib"],
                        "existing_bytes": plan["existing_bytes"],
                        "existing_gib": plan["existing_gib"],
                        "missing_or_incomplete_count": plan["missing_or_incomplete_count"],
                    },
                    "sample_strategy": args.sample_strategy,
                    "sample_seed": args.sample_seed,
                }
                write_json(summary_path, summary)
                reporter.completed(summary=summary_path, plan_only=True)
                print(f"summary={summary_path}")
                print(f"plan={out_root / 'abc_fetch_plan.md'}")
                print(f"chunks={len(chunks)} formats={','.join(formats)}")
                return 0

            records: list[dict[str, Any]] = []
            command_failures = 0
            reporter.phase("downloading", "Downloading and verifying ABC archives")
            for chunk in chunks:
                chunk_record: dict[str, Any] = {"chunk": chunk, "formats": []}
                for fmt in formats:
                    item = entries_by_format.get(fmt, {}).get(chunk)
                    if not item:
                        raise FetchError(f"missing {fmt} archive manifest entry for chunk {chunk:04d}")
                    name = item["name"]
                    archive_path = downloads / name
                    expected_size_raw = sizes.get(name)
                    expected_size = int(expected_size_raw) if isinstance(expected_size_raw, int) else None
                    expected_md5 = None if args.no_verify else md5s.get(name)
                    initial_verify = verify_archive(archive_path, expected_size, expected_md5)
                    archive_ok = bool(initial_verify["ok"])
                    if args.skip_download and not archive_ok:
                        raise FetchError(f"archive missing or invalid under --skip-download: {archive_path}")
                    if not args.skip_download and not archive_ok:
                        initial_bytes = 0
                        for candidate in (archive_path, _partial_path(archive_path)):
                            if candidate.is_file():
                                initial_bytes = max(initial_bytes, candidate.stat().st_size)
                        reporter.archive_started(
                            chunk=chunk,
                            fmt=fmt,
                            archive=name,
                            expected_bytes=expected_size or 0,
                            initial_bytes=initial_bytes,
                        )
                        download_url(
                            item["url"],
                            archive_path,
                            expected_size=expected_size,
                            expected_md5=expected_md5,
                            progress=reporter.archive_progress,
                        )
                        reporter.archive_completed(expected_size or archive_path.stat().st_size, reused=False)
                    else:
                        reporter.archive_completed(expected_size or archive_path.stat().st_size, reused=True)

                    reporter.phase("verifying", f"Verifying {name}")
                    verify = verify_archive(archive_path, expected_size, expected_md5)
                    if not verify["ok"]:
                        raise FetchError(f"verification failed for {archive_path}: {verify}")

                    fmt_record: dict[str, Any] = {
                        "format": fmt,
                        "archive": name,
                        "url": item["url"],
                        "verify": verify,
                    }
                    if args.extract_mode != "none":
                        reporter.phase("extracting", f"Extracting {name}")
                        suffix = FORMAT_EXTENSIONS[fmt]
                        listing = archive_list(archive_path)
                        files = [entry for entry in listing if entry.lower().endswith(suffix)]
                        include_files = (
                            files
                            if args.extract_mode == "full"
                            else select_sample_files(
                                files,
                                sample_count=args.sample_count,
                                strategy=args.sample_strategy,
                                seed=args.sample_seed,
                                chunk=chunk,
                                fmt=fmt,
                            )
                        )
                        mode_label = sample_mode_label(args)
                        out_dir = extracted_root / f"chunk_{chunk:04d}_{mode_label}"
                        marker_path = extraction_marker_path(out_root, archive_path, mode_label)
                        extract = reusable_extraction(
                            marker_path,
                            archive_path=archive_path,
                            archive_md5=str(verify.get("md5") or expected_md5 or ""),
                            expected_files=len(include_files),
                            out_dir=out_dir,
                            suffix=suffix,
                        )
                        if extract is None:
                            extract = extract_archive(
                                archive_path,
                                out_dir,
                                include_files if args.extract_mode == "sample" else None,
                            )
                            extract["reused"] = False
                            extract["archive_file_count"] = len(files)
                            extract["extracted_file_count"] = count_extracted_files(out_dir, suffix)
                            if extract["ok"] and extract["extracted_file_count"] >= len(include_files):
                                marker: dict[str, Any] = {
                                    "completed_at": now_utc(),
                                    "archive": archive_path.name,
                                    "archive_md5": str(verify.get("md5") or expected_md5 or ""),
                                    "archive_file_count": len(files),
                                    "include_file_count": len(include_files),
                                    "include_list": extract.get("include_list", ""),
                                    "out": str(out_dir.resolve()),
                                }
                                if args.extract_mode == "sample":
                                    marker["sample_strategy"] = args.sample_strategy
                                    marker["sample_seed"] = args.sample_seed
                                write_json_atomic(marker_path, marker)
                        if not extract["ok"]:
                            command_failures += 1
                            if args.fail_on_command:
                                raise FetchError(f"extract failed for {archive_path}: {extract['stderr']}")
                        fmt_record["extract"] = extract
                    chunk_record["formats"].append(fmt_record)
                    reporter.phase("downloading", "Downloading and verifying ABC archives")
                records.append(chunk_record)

            script_dir = Path(__file__).resolve().parent
            optional_commands: dict[str, Any] = {}
            dataset_path = out_root / "dataset_index.json"
            if args.run_discovery:
                reporter.phase("discovering", "Building the ABC dataset index")
                cmd = [
                    sys.executable,
                    str(script_dir / "discover_corpus.py"),
                    str(extracted_root),
                    "--out",
                    str(dataset_path),
                    "--paths-out",
                    str(out_root / "dataset_index.paths.txt"),
                    "--report",
                    str(out_root / "dataset_index.md"),
                    "--hash-inputs",
                    "--include-artifacts",
                ]
                optional_commands["discover_corpus"] = run_tool(cmd)
                if not optional_commands["discover_corpus"]["ok"]:
                    command_failures += 1
                    if args.fail_on_command:
                        raise FetchError("discover_corpus failed")
                else:
                    paths_path = out_root / "dataset_index.paths.txt"
                    total_files = 0
                    with paths_path.open("r", encoding="utf-8-sig") as path_file:
                        total_files = sum(1 for line in path_file if line.strip())
                    write_json_atomic(
                        out_root / "dataset_index.meta.json",
                        {
                            "schema_version": 1,
                            "dataset_index": str(dataset_path.resolve()),
                            "dataset_index_sha256": file_sha256(dataset_path),
                            "paths_file": str(paths_path.resolve()),
                            "total_files": total_files,
                            "entry_content_hash": "sha256",
                        },
                    )
            if args.run_feature_profile:
                reporter.phase("profiling", "Profiling ABC CAD features")
                cmd = [
                    sys.executable,
                    str(script_dir / "profile_cad_features.py"),
                    "--dataset-list",
                    str(dataset_path),
                    "--out",
                    str(out_root / "cad_feature_profile.json"),
                    "--paths-out",
                    str(out_root / "complex_paths.txt"),
                    "--subset-out",
                    str(out_root / "complex_dataset_index.json"),
                    "--report",
                    str(out_root / "cad_feature_profile.md"),
                    "--min-score",
                    "8",
                ]
                optional_commands["profile_cad_features"] = run_tool(cmd)
                if not optional_commands["profile_cad_features"]["ok"]:
                    command_failures += 1
                    if args.fail_on_command:
                        raise FetchError("profile_cad_features failed")

            summary = {
                "generated_at": now_iso_like(),
                "out_root": str(out_root),
                "formats": formats,
                "chunks": chunks,
                "full_dataset": bool(args.full_dataset),
                "extract_mode": args.extract_mode,
                "sample_count": args.sample_count,
                "sample_strategy": args.sample_strategy,
                "sample_seed": args.sample_seed,
                "records": records,
                "optional_commands": optional_commands,
                "command_failures": command_failures,
            }
            write_json(summary_path, summary)
            if command_failures:
                reporter.failed(f"{command_failures} extraction or post-processing command(s) failed")
            else:
                reporter.completed(summary=summary_path, dataset_index=dataset_path)
            print(f"summary={summary_path}")
            print(f"chunks={len(chunks)} formats={','.join(formats)}")
            return 0 if command_failures == 0 else 2
    except (FetchError, OSError, ValueError, json.JSONDecodeError) as exc:
        reporter.failed(str(exc))
        print(f"fetch_abc_dataset: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
