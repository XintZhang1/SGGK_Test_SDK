#!/usr/bin/env python3
"""Fetch, verify, and sample official ABC dataset chunks."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Any


BASE_URL = "https://deep-geometry.github.io/abc-dataset/data"
MANIFEST_FILES = ("step_v00.txt", "meta_v00.txt", "size.yml", "md5.yml")
FORMAT_EXTENSIONS = {
    "step": ".step",
    "meta": ".yml",
}
RESUMABLE_CURL_RETURN_CODES = {18, 56}


class FetchError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="artifacts/abc_dataset", help="Output root for manifests, downloads, extracted files, and reports")
    parser.add_argument("--download-root", default="", help="Archive cache directory; defaults to <out>/downloads")
    parser.add_argument("--format", action="append", choices=sorted(FORMAT_EXTENSIONS), default=[], help="ABC format to fetch; default is step plus meta")
    parser.add_argument("--chunk", action="append", default=[], help="Chunk number such as 27 or 0027. Can be repeated")
    parser.add_argument("--chunk-range", action="append", default=[], help="Inclusive chunk range such as 0:4. Can be repeated")
    parser.add_argument("--all-chunks", action="store_true", help="Select all STEP chunks when no explicit chunk is given")
    parser.add_argument("--smallest-step", type=int, default=1, help="Select N smallest STEP chunks when no explicit chunk is given")
    parser.add_argument("--max-step-download-gb", type=float, default=0.0, help="Select smallest STEP chunks up to this total STEP archive budget when no explicit chunk is given")
    parser.add_argument("--plan-only", action="store_true", help="Write fetch plan files and exit without downloads or extraction")
    parser.add_argument("--refresh-manifests", action="store_true", help="Re-download official manifests")
    parser.add_argument("--skip-download", action="store_true", help="Require archives to already exist")
    parser.add_argument("--no-verify", action="store_true", help="Skip size and MD5 verification")
    parser.add_argument("--extract-mode", choices=["none", "sample", "full"], default="sample")
    parser.add_argument("--sample-count", type=int, default=50, help="Files per chunk/format for sample extraction")
    parser.add_argument("--run-discovery", action="store_true", help="Run discover_corpus.py over extracted STEP files")
    parser.add_argument("--run-feature-profile", action="store_true", help="Run profile_cad_features.py after discovery")
    parser.add_argument("--fail-on-command", action="store_true", help="Fail when optional discovery/profile commands fail")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


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


def download_url(url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        resume = out_path.is_file() and out_path.stat().st_size > 0
        last_returncode = 0
        for _attempt in range(6):
            cmd = [
                curl,
                "-L",
                "--fail",
                "--retry",
                "5",
                "--retry-delay",
                "3",
            ]
            if resume:
                cmd.extend(["--continue-at", "-"])
            cmd.extend(["-o", str(out_path), url])
            completed = subprocess.run(cmd)
            last_returncode = completed.returncode
            if completed.returncode == 0:
                return
            if completed.returncode == 33 and out_path.is_file():
                # Some ABC archive endpoints do not support byte ranges. Fall back to a clean full retry.
                out_path.unlink()
                resume = False
                continue
            if completed.returncode in RESUMABLE_CURL_RETURN_CODES and out_path.is_file():
                resume = True
                continue
            break
        raise FetchError(f"curl failed for {url} with return code {last_returncode}")

    with urllib.request.urlopen(url) as response, out_path.open("wb") as out_file:
        shutil.copyfileobj(response, out_file)


def ensure_manifests(out_root: Path, refresh: bool) -> dict[str, Path]:
    manifest_dir = out_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for name in MANIFEST_FILES:
        path = manifest_dir / name
        if refresh or not path.is_file():
            download_url(f"{BASE_URL}/{name}", path)
        result[name] = path
    return result


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


def parse_chunk_text(raw: str) -> int:
    text = str(raw).strip()
    if not text:
        raise FetchError("empty chunk value")
    return int(text)


def selected_chunks(args: argparse.Namespace, step_entries: dict[int, dict[str, str]], sizes: dict[str, int]) -> list[int]:
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
            archive_exists = archive_path.is_file()
            download_size = archive_path.stat().st_size if archive_exists else 0
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
            }
            rows.append(row)
            if not size_ok:
                missing.append({"chunk": chunk, "format": fmt, "archive": name, "expected_size": size, "download_size": download_size})

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
    lines.extend(["", "## Archives", "", "| chunk | format | size GiB | cached | archive |", "| --- | --- | ---: | --- | --- |"])
    for row in plan.get("archives", []):
        if not isinstance(row, dict):
            continue
        lines.append(
            f"| `{row.get('chunk')}` | `{row.get('format')}` | {row.get('size_gib')} | `{row.get('download_size_ok')}` | `{row.get('archive')}` |"
        )
    lines.append("")
    return "\n".join(lines)


def write_fetch_plan(out_root: Path, plan: dict[str, Any]) -> None:
    write_json(out_root / "abc_fetch_plan.json", plan)
    rows = [row for row in plan.get("archives", []) if isinstance(row, dict)]
    write_csv(out_root / "abc_fetch_plan.csv", rows)
    write_text(out_root / "abc_fetch_plan.md", markdown_fetch_plan(plan))


def archive_list(archive_path: Path) -> list[str]:
    cmd = ["tar", "-tf", str(archive_path)]
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise FetchError(f"tar list failed for {archive_path}: {completed.stderr.strip()}")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def extract_archive(archive_path: Path, out_dir: Path, include_files: list[str] | None) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["tar", "-xf", str(archive_path), "-C", str(out_dir)]
    list_path = ""
    if include_files is not None:
        list_path = str(out_dir.parent / f"{archive_path.stem}_include.txt")
        write_text(Path(list_path), "\n".join(include_files) + ("\n" if include_files else ""))
        cmd.extend(["-T", list_path])
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
    formats = args.format or ["step", "meta"]
    try:
        manifests = ensure_manifests(out_root, args.refresh_manifests)
        sizes = parse_name_values(manifests["size.yml"], "int")
        md5s = parse_name_values(manifests["md5.yml"], "str")
        entries_by_format = {
            fmt: parse_archive_manifest(manifests[f"{fmt}_v00.txt"], fmt)
            for fmt in formats
        }
        step_entries = parse_archive_manifest(manifests["step_v00.txt"], "step")
        chunks = selected_chunks(args, step_entries, sizes)
        if not chunks:
            raise FetchError("no chunks selected")

        downloads = Path(args.download_root) if args.download_root else out_root / "downloads"
        extracted_root = out_root / "extracted"
        plan = build_fetch_plan(out_root, downloads, formats, chunks, entries_by_format, sizes, md5s)
        write_fetch_plan(out_root, plan)
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
            }
            write_json(out_root / "abc_fetch_summary.json", summary)
            print(f"summary={out_root / 'abc_fetch_summary.json'}")
            print(f"plan={out_root / 'abc_fetch_plan.md'}")
            print(f"chunks={len(chunks)} formats={','.join(formats)}")
            return 0

        records: list[dict[str, Any]] = []
        command_failures = 0
        for chunk in chunks:
            chunk_record: dict[str, Any] = {"chunk": chunk, "formats": []}
            for fmt in formats:
                item = entries_by_format.get(fmt, {}).get(chunk)
                if not item:
                    raise FetchError(f"missing {fmt} archive manifest entry for chunk {chunk:04d}")
                name = item["name"]
                archive_path = downloads / name
                expected_size = sizes.get(name)
                if args.skip_download and not archive_path.is_file():
                    raise FetchError(f"archive missing under --skip-download: {archive_path}")
                needs_download = not archive_path.is_file()
                if archive_path.is_file() and isinstance(expected_size, int) and archive_path.stat().st_size != expected_size:
                    needs_download = True
                if not args.skip_download and needs_download:
                    download_url(item["url"], archive_path)

                verify = verify_archive(
                    archive_path,
                    expected_size,
                    md5s.get(name),
                )
                if not args.no_verify and not verify["ok"]:
                    raise FetchError(f"verification failed for {archive_path}: {verify}")

                fmt_record: dict[str, Any] = {
                    "format": fmt,
                    "archive": name,
                    "url": item["url"],
                    "verify": verify,
                }
                if args.extract_mode != "none":
                    suffix = FORMAT_EXTENSIONS[fmt]
                    listing = archive_list(archive_path)
                    files = [entry for entry in listing if entry.lower().endswith(suffix)]
                    include_files = files if args.extract_mode == "full" else files[: max(args.sample_count, 0)]
                    mode_label = "full" if args.extract_mode == "full" else f"sample{args.sample_count}"
                    out_dir = extracted_root / f"chunk_{chunk:04d}_{mode_label}"
                    extract = extract_archive(archive_path, out_dir, include_files if args.extract_mode == "sample" else None)
                    if not extract["ok"]:
                        command_failures += 1
                        if args.fail_on_command:
                            raise FetchError(f"extract failed for {archive_path}: {extract['stderr']}")
                    extract["archive_file_count"] = len(files)
                    extract["extracted_file_count"] = count_extracted_files(out_dir, suffix)
                    fmt_record["extract"] = extract
                chunk_record["formats"].append(fmt_record)
            records.append(chunk_record)

        script_dir = Path(__file__).resolve().parent
        optional_commands: dict[str, Any] = {}
        if args.run_discovery:
            dataset_path = out_root / "dataset_index.json"
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
            if not optional_commands["discover_corpus"]["ok"] and args.fail_on_command:
                raise FetchError("discover_corpus failed")
        if args.run_feature_profile:
            dataset_path = out_root / "dataset_index.json"
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
            if not optional_commands["profile_cad_features"]["ok"] and args.fail_on_command:
                raise FetchError("profile_cad_features failed")

        summary = {
            "generated_at": now_iso_like(),
            "out_root": str(out_root),
            "formats": formats,
            "chunks": chunks,
            "extract_mode": args.extract_mode,
            "sample_count": args.sample_count,
            "records": records,
            "optional_commands": optional_commands,
            "command_failures": command_failures,
        }
        write_json(out_root / "abc_fetch_summary.json", summary)
        print(f"summary={out_root / 'abc_fetch_summary.json'}")
        print(f"chunks={len(chunks)} formats={','.join(formats)}")
        return 0 if command_failures == 0 else 2
    except FetchError as exc:
        print(f"fetch_abc_dataset: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
