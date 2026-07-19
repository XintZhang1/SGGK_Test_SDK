"""ABC dataset process control and bounded existing-dataset inspection for the UI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_ALLOWED_REQUEST_FIELDS = {
    "mode",
    "out_root",
    "download_root",
    "refresh_manifests",
    "smallest_step",
    "sample_count",
}
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
MAX_INLINE_INDEX_BYTES = 32 * 1024 * 1024
MAX_INDEX_METADATA_BYTES = 1024 * 1024


class AbcDatasetError(ValueError):
    """An ABC dataset UI request is invalid or unsafe."""


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_output_root(repo_root: Path, raw: Any, *, field: str, default: Path | None = None) -> Path:
    text = str(raw or "").strip()
    if not text:
        if default is None:
            raise AbcDatasetError(f"{field} is required")
        candidate = default
    else:
        expanded = Path(text).expanduser()
        candidate = expanded if expanded.is_absolute() else repo_root / expanded
    result = candidate.resolve()
    if result == Path(result.anchor):
        raise AbcDatasetError(f"{field} cannot be a filesystem root")
    if result == repo_root or result == repo_root / ".git":
        raise AbcDatasetError(f"{field} cannot overwrite the repository")
    try:
        relative = result.relative_to(repo_root)
    except ValueError:
        relative = None
    if relative is not None and (not relative.parts or relative.parts[0].lower() != "artifacts"):
        raise AbcDatasetError(f"{field} must be outside the repository or under artifacts/")
    if result.exists() and not result.is_dir():
        raise AbcDatasetError(f"{field} must be a directory")
    return result


def _positive_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise AbcDatasetError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise AbcDatasetError(f"{field} must be an integer") from exc
    if result < minimum or result > maximum:
        raise AbcDatasetError(f"{field} must be between {minimum} and {maximum}")
    return result


def normalize_fetch_request(repo_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AbcDatasetError("ABC fetch request must be an object")
    unknown = sorted(set(payload) - _ALLOWED_REQUEST_FIELDS)
    if unknown:
        raise AbcDatasetError(f"unsupported ABC fetch fields: {unknown}")
    mode = str(payload.get("mode") or "plan").strip().lower()
    if mode not in {"plan", "full", "sample"}:
        raise AbcDatasetError("mode must be plan, full, or sample")
    out_root = _resolve_output_root(repo_root, payload.get("out_root"), field="out_root")
    download_root = _resolve_output_root(
        repo_root,
        payload.get("download_root"),
        field="download_root",
        default=out_root / "downloads",
    )
    refresh = payload.get("refresh_manifests", False)
    if not isinstance(refresh, bool):
        raise AbcDatasetError("refresh_manifests must be a boolean")
    smallest_step = _positive_int(payload.get("smallest_step", 1), field="smallest_step", minimum=1, maximum=100)
    sample_count = _positive_int(payload.get("sample_count", 50), field="sample_count", minimum=0, maximum=10_000)
    if mode != "sample" and ("smallest_step" in payload or "sample_count" in payload):
        raise AbcDatasetError("smallest_step and sample_count are only valid in sample mode")
    return {
        "mode": mode,
        "out_root": str(out_root),
        "download_root": str(download_root),
        "refresh_manifests": refresh,
        "smallest_step": smallest_step,
        "sample_count": sample_count,
    }


def build_fetch_command(repo_root: Path, request: dict[str, Any]) -> tuple[list[str], Path, Path]:
    script = repo_root / "test_harness" / "tools" / "fetch_abc_dataset.py"
    if not script.is_file():
        raise AbcDatasetError(f"ABC fetch helper is missing: {script}")
    out_root = Path(request["out_root"])
    progress_path = out_root / "abc_fetch_progress.json"
    log_path = out_root / "abc_fetch_ui.log"
    command = [
        sys.executable,
        str(script),
        "--out",
        str(out_root),
        "--download-root",
        request["download_root"],
        "--progress-file",
        str(progress_path),
    ]
    mode = request["mode"]
    if mode in {"plan", "full"}:
        command.append("--full-dataset")
        if mode == "plan":
            command.append("--plan-only")
    else:
        command.extend(
            [
                "--smallest-step",
                str(request["smallest_step"]),
                "--sample-count",
                str(request["sample_count"]),
                "--extract-mode",
                "sample",
                "--run-discovery",
                "--fail-on-command",
            ]
        )
    if request["refresh_manifests"]:
        command.append("--refresh-manifests")
    return command, progress_path, log_path


def _sample_indices(length: int, maximum: int) -> list[int]:
    if length <= maximum:
        return list(range(length))
    return sorted({round(index * (length - 1) / (maximum - 1)) for index in range(maximum)})


def _inspect_index(path: Path, *, maximum_checks: int) -> dict[str, Any]:
    if path.stat().st_size > MAX_INLINE_INDEX_BYTES:
        return _inspect_large_index(path, maximum_checks=maximum_checks)
    errors: list[str] = []
    warnings: list[str] = []
    value = _read_object(path)
    files = value.get("files")
    if not isinstance(files, list) or not files:
        return {
            "valid": False,
            "ready": False,
            "total_files": 0,
            "checked_files": 0,
            "missing_files": [],
            "errors": ["dataset index must contain a non-empty files array"],
            "warnings": [],
        }
    missing: list[str] = []
    malformed = 0
    missing_hashes = 0
    indices = _sample_indices(len(files), max(maximum_checks, 2))
    for index in indices:
        item = files[index]
        raw = item.get("path") or item.get("source_file") if isinstance(item, dict) else ""
        if not isinstance(raw, str) or not raw.strip():
            malformed += 1
            continue
        if re.fullmatch(r"[0-9A-Fa-f]{64}", str(item.get("sha256") or "")) is None:
            missing_hashes += 1
        candidate = Path(raw).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (path.parent / candidate).resolve()
        if candidate.suffix.lower() not in {".step", ".stp"}:
            malformed += 1
        elif not candidate.is_file() and len(missing) < 20:
            missing.append(str(candidate))
    if malformed:
        errors.append(f"{malformed} sampled index entries are malformed or not STEP files")
    if missing:
        errors.append(f"{len(missing)} sampled STEP files are missing")
    if missing_hashes:
        errors.append(f"{missing_hashes} sampled index entries have no SHA-256 content binding")
    if len(indices) < len(files):
        warnings.append(f"validated {len(indices)} evenly distributed entries out of {len(files)}")
    declared = value.get("total_files")
    if isinstance(declared, int) and declared != len(files):
        warnings.append(f"index total_files={declared} does not match files length={len(files)}")
    return {
        "valid": not errors,
        "ready": not errors,
        "total_files": len(files),
        "checked_files": len(indices),
        "missing_files": missing,
        "errors": errors,
        "warnings": warnings,
    }


def _inspect_large_index(path: Path, *, maximum_checks: int) -> dict[str, Any]:
    """Validate a generated full index with bounded memory via its sidecars."""

    meta_path = path.with_name("dataset_index.meta.json")
    paths_path = path.with_name("dataset_index.paths.txt")
    errors: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []
    malformed = 0
    if not meta_path.is_file() or meta_path.stat().st_size > MAX_INDEX_METADATA_BYTES:
        errors.append("large dataset index requires the generated dataset_index.meta.json sidecar")
    if not paths_path.is_file():
        errors.append("large dataset index requires the generated dataset_index.paths.txt sidecar")
    if errors:
        return {
            "valid": False,
            "ready": False,
            "total_files": 0,
            "checked_files": 0,
            "missing_files": [],
            "errors": errors,
            "warnings": warnings,
        }
    meta = _read_object(meta_path)
    expected_index_hash = str(meta.get("dataset_index_sha256") or "").lower()
    total_files = meta.get("total_files")
    if re.fullmatch(r"[0-9a-f]{64}", expected_index_hash) is None:
        errors.append("large dataset metadata has no valid index SHA-256")
    elif _file_sha256(path) != expected_index_hash:
        errors.append("large dataset index does not match its metadata SHA-256")
    if not isinstance(total_files, int) or total_files <= 0:
        errors.append("large dataset metadata has no positive total_files")
        total_files = 0
    if meta.get("entry_content_hash") != "sha256":
        errors.append("large dataset metadata does not require SHA-256 entry bindings")

    checked = 0
    with paths_path.open("r", encoding="utf-8-sig") as path_file:
        for raw_line in path_file:
            raw = raw_line.strip()
            if not raw:
                continue
            candidate = Path(raw).expanduser()
            candidate = candidate.resolve() if candidate.is_absolute() else (path.parent / candidate).resolve()
            checked += 1
            if candidate.suffix.lower() not in {".step", ".stp"}:
                malformed += 1
            elif not candidate.is_file() and len(missing) < 20:
                missing.append(str(candidate))
            if checked >= max(maximum_checks, 2):
                break
    if malformed:
        errors.append(f"{malformed} sampled index paths are not STEP files")
    if missing:
        errors.append(f"{len(missing)} sampled STEP files are missing")
    if checked == 0:
        errors.append("large dataset paths sidecar is empty")
    elif total_files > checked:
        warnings.append(f"validated the first {checked} paths out of {total_files}")
    return {
        "valid": not errors,
        "ready": not errors,
        "total_files": total_files,
        "checked_files": checked,
        "missing_files": missing,
        "errors": errors,
        "warnings": warnings,
    }


def _bounded_step_scan(
    root: Path, *, maximum_entries: int = 20_000, maximum_results: int = 20
) -> tuple[list[str], bool]:
    found: list[str] = []
    visited = 0
    pending = [root]
    while pending and visited < maximum_entries and len(found) < maximum_results:
        current = pending.pop()
        try:
            children = list(os.scandir(current))
        except OSError:
            continue
        for child in children:
            visited += 1
            if visited >= maximum_entries:
                break
            try:
                if child.is_dir(follow_symlinks=False):
                    pending.append(Path(child.path))
                elif child.is_file(follow_symlinks=False) and Path(child.name).suffix.lower() in {".step", ".stp"}:
                    found.append(str(Path(child.path).resolve()))
                    if len(found) >= maximum_results:
                        break
            except OSError:
                continue
    return found, bool(pending or visited >= maximum_entries)


def inspect_existing_abc_dataset(raw_path: str | Path, *, maximum_index_checks: int = 128) -> dict[str, Any]:
    raw_text = str(raw_path).strip()
    if not raw_text:
        raise AbcDatasetError("select an ABC directory or dataset index file")
    requested = Path(raw_text).expanduser()
    path = requested.resolve()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "valid": False,
        "ready": False,
        "kind": "invalid",
        "requested_path": str(requested),
        "root": str(path.parent if path.is_file() else path),
        "dataset_index": "",
        "campaign_dataset": "",
        "total_files": 0,
        "checked_files": 0,
        "missing_files": [],
        "archive_count": 0,
        "partial_archive_count": 0,
        "can_resume_download": False,
        "needs_index": False,
        "full_dataset": False,
        "errors": [],
        "warnings": [],
    }
    if not path.exists():
        result["errors"].append("selected ABC path does not exist")
        return result
    if path.is_file() and path.suffix.lower() != ".json":
        result["errors"].append("select an ABC directory or dataset_index.json")
        return result

    root = path.parent if path.is_file() else path
    index_path = path if path.is_file() else root / "dataset_index.json"
    archive_roots = [root]
    if (root / "downloads").is_dir():
        archive_roots.append(root / "downloads")
    archives: set[Path] = set()
    partials: set[Path] = set()
    for archive_root in archive_roots:
        for fmt in ("step", "meta"):
            archives.update(archive_root.glob(f"abc_[0-9][0-9][0-9][0-9]_{fmt}_v00.7z"))
            partials.update(archive_root.glob(f"abc_[0-9][0-9][0-9][0-9]_{fmt}_v00.7z.part"))
    result["archive_count"] = len(archives)
    result["partial_archive_count"] = len(partials)
    result["can_resume_download"] = bool(archives or partials)

    if index_path.is_file():
        checked = _inspect_index(index_path, maximum_checks=maximum_index_checks)
        result.update(checked)
        result["kind"] = "dataset_index" if path.is_file() else "fetch_root"
        result["dataset_index"] = str(index_path)
        result["campaign_dataset"] = str(index_path) if checked["ready"] else ""
        summary = _read_object(root / "abc_fetch_summary.json")
        chunks = summary.get("chunks") if isinstance(summary.get("chunks"), list) else []
        result["full_dataset"] = bool(
            summary.get("full_dataset") is True
            and summary.get("extract_mode") == "full"
            and int(summary.get("command_failures") or 0) == 0
            and len(chunks) == 100
        )
        return result

    step_sample, scan_truncated = _bounded_step_scan(root)
    if step_sample:
        result.update(
            {
                "valid": True,
                "kind": "raw_step_directory",
                "needs_index": True,
                "checked_files": len(step_sample),
                "warnings": [
                    "STEP files were found, but dataset_index.json must be generated before campaign use",
                    *(["directory scan was intentionally bounded"] if scan_truncated else []),
                ],
            }
        )
        return result
    if archives or partials:
        result.update(
            {
                "valid": True,
                "kind": "archive_cache",
                "warnings": [
                    "ABC archives can be resumed or reused, but extracted STEP data and an index are not ready"
                ],
            }
        )
        return result
    result["errors"].append("no dataset_index.json, STEP files, or ABC archives were found")
    return result


class AbcDatasetBackend:
    """Own one local ABC fetch process and expose its persisted progress snapshot."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.control_path = self.repo_root / "artifacts" / "harness_ui" / "abc_dataset_job.json"
        self._lock = threading.RLock()
        self._process: subprocess.Popen[Any] | None = None
        self._log_handle: Any = None
        self._state = self._load_state()

    def _idle_state(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "idle",
            "operation": "",
            "pid": 0,
            "started_at": "",
            "finished_at": "",
            "error": "",
            "request": {},
            "progress_path": "",
            "log_path": "",
        }

    def _load_state(self) -> dict[str, Any]:
        value = _read_object(self.control_path)
        if value.get("schema_version") != SCHEMA_VERSION:
            return self._idle_state()
        if value.get("status") in {"running", "cancelling"}:
            value["status"] = "failed"
            value["finished_at"] = _now_utc()
            value["error"] = "ABC fetch controller restarted before the prior process completed"
        return value

    def _save(self) -> None:
        _atomic_json(self.control_path, self._state)

    def start_fetch(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = normalize_fetch_request(self.repo_root, payload)
        command, progress_path, log_path = build_fetch_command(self.repo_root, request)
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("an ABC dataset operation is already running")
            if self._state.get("status") in {"running", "cancelling"}:
                raise RuntimeError("an ABC dataset operation is already running")
            Path(request["out_root"]).mkdir(parents=True, exist_ok=True)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            progress_path.unlink(missing_ok=True)
            self._log_handle = log_path.open("w", encoding="utf-8", newline="\n")
            popen_kwargs: dict[str, Any] = {
                "cwd": self.repo_root,
                "stdout": self._log_handle,
                "stderr": subprocess.STDOUT,
                "text": True,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(command, **popen_kwargs)
            except Exception:
                self._log_handle.close()
                self._log_handle = None
                raise
            self._process = process
            self._state = {
                "schema_version": SCHEMA_VERSION,
                "status": "running",
                "operation": "abc_plan" if request["mode"] == "plan" else "abc_fetch",
                "pid": process.pid,
                "started_at": _now_utc(),
                "finished_at": "",
                "error": "",
                "request": request,
                "progress_path": str(progress_path),
                "log_path": str(log_path),
            }
            self._save()
            threading.Thread(target=self._watch, args=(process,), name="abc-dataset-fetch", daemon=True).start()
            return self.snapshot()

    def _watch(self, process: subprocess.Popen[Any]) -> None:
        returncode = process.wait()
        with self._lock:
            if process is not self._process:
                return
            if self._log_handle is not None:
                self._log_handle.close()
                self._log_handle = None
            previous = str(self._state.get("status") or "")
            progress = _read_object(Path(str(self._state.get("progress_path") or "")))
            if previous == "cancelling":
                status = "cancelled"
                error = ""
            elif returncode == 0:
                status = "completed"
                error = ""
            else:
                status = "failed"
                error = str(progress.get("error") or f"ABC fetch exited with code {returncode}")
            self._state.update(
                {
                    "status": status,
                    "pid": 0,
                    "finished_at": _now_utc(),
                    "error": error,
                }
            )
            self._save()
            self._process = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = deepcopy(self._state)
            raw_progress = str(result.get("progress_path") or "")
            result["progress"] = _read_object(Path(raw_progress)) if raw_progress else {}
            return result

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise RuntimeError("there is no running ABC dataset operation")
            self._state["status"] = "cancelling"
            self._save()
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return self.snapshot()

    def inspect_existing(self, path: str | Path) -> dict[str, Any]:
        return inspect_existing_abc_dataset(path)


__all__ = [
    "AbcDatasetBackend",
    "AbcDatasetError",
    "build_fetch_command",
    "inspect_existing_abc_dataset",
    "normalize_fetch_request",
]
