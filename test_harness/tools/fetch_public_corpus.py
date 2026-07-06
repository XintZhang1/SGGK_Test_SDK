#!/usr/bin/env python3
"""Fetch small, reproducible public CAD corpus slices without cloning huge repositories."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


PRESETS = {
    "freecad-library-step": {
        "name": "FreeCAD Parts Library STEP slice",
        "repo": "FreeCAD/FreeCAD-library",
        "extensions": [".step", ".stp"],
        "license": "CC-BY 3.0; each part should be attributed to its respective author(s)",
        "license_url": "https://github.com/FreeCAD/FreeCAD-library#license",
        "homepage": "https://github.com/FreeCAD/FreeCAD-library",
        "notes": [
            "The upstream library is around 5 GB, so this tool downloads a selected slice instead of cloning the full repository.",
            "README says parts should be available in .FcStd and .stp formats where possible.",
        ],
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=sorted(PRESETS), default="freecad-library-step")
    parser.add_argument("--out", required=True, help="Directory for downloaded CAD files and reports")
    parser.add_argument("--limit", type=int, default=8, help="Maximum files to download; 0 means all candidates after filters")
    parser.add_argument("--max-size", type=int, default=400_000, help="Maximum blob size in bytes; 0 disables")
    parser.add_argument("--min-size", type=int, default=0, help="Minimum blob size in bytes")
    parser.add_argument("--path-regex", action="append", default=[], help="Require path to match this regex; may repeat")
    parser.add_argument("--exclude-regex", action="append", default=[], help="Exclude paths matching this regex; may repeat")
    parser.add_argument("--selection", choices=["first", "spread"], default="spread")
    parser.add_argument("--force", action="store_true", help="Redownload existing files")
    parser.add_argument("--dry-run", action="store_true", help="Write manifests but skip downloads and discovery")
    parser.add_argument("--hash-inputs", action="store_true", help="Hash downloaded files in dataset discovery")
    parser.add_argument("--profile-features", action="store_true", help="Run profile_cad_features.py after discovery")
    parser.add_argument("--profile-min-score", type=int, default=8, help="Minimum feature score for complex outputs")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def fetch_json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": "SGGK-test-harness-public-corpus"})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def download_file(url: str, path: Path) -> int:
    request = Request(url, headers={"User-Agent": "SGGK-test-harness-public-corpus"})
    path.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(request, timeout=120) as response:
        data = response.read()
    path.write_bytes(data)
    return len(data)


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as in_file:
        for chunk in iter(lambda: in_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def raw_github_url(repo: str, branch: str, path: str) -> str:
    quoted = "/".join(quote(part) for part in path.split("/"))
    return f"https://raw.githubusercontent.com/{repo}/{quote(branch)}/{quoted}"


def path_matches(path: str, includes: list[re.Pattern[str]], excludes: list[re.Pattern[str]]) -> bool:
    if includes and not any(pattern.search(path) for pattern in includes):
        return False
    return not any(pattern.search(path) for pattern in excludes)


def select_candidates(candidates: list[dict[str, Any]], limit: int, selection: str) -> list[dict[str, Any]]:
    if limit <= 0 or limit >= len(candidates):
        return list(candidates)
    if selection == "first":
        return list(candidates[:limit])
    if limit == 1:
        return [candidates[0]]
    selected: list[dict[str, Any]] = []
    last = len(candidates) - 1
    used: set[int] = set()
    for i in range(limit):
        index = round(i * last / (limit - 1))
        while index in used and index + 1 <= last:
            index += 1
        while index in used and index > 0:
            index -= 1
        used.add(index)
        selected.append(candidates[index])
    return selected


def safe_local_path(path: str) -> Path:
    parts = []
    for part in path.split("/"):
        clean = re.sub(r"[^A-Za-z0-9_. -]+", "_", part).strip(" .")
        parts.append(clean or "item")
    return Path(*parts)


def run_discovery(out_dir: Path, download_dir: Path, hash_inputs: bool) -> dict[str, Any]:
    script_dir = Path(__file__).resolve().parent
    index_path = out_dir / "dataset_index.json"
    cmd = [
        sys.executable,
        str(script_dir / "discover_corpus.py"),
        str(download_dir),
        "--out",
        str(index_path),
        "--paths-out",
        str(out_dir / "dataset_index.paths.txt"),
        "--report",
        str(out_dir / "dataset_index.md"),
        "--include-artifacts",
    ]
    if hash_inputs:
        cmd.append("--hash-inputs")
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "index_path": str(index_path),
        "paths_path": str(out_dir / "dataset_index.paths.txt"),
        "report_path": str(out_dir / "dataset_index.md"),
    }


def run_feature_profile(out_dir: Path, dataset_index: Path, min_score: int) -> dict[str, Any]:
    script_dir = Path(__file__).resolve().parent
    profile_path = out_dir / "cad_feature_profile.json"
    cmd = [
        sys.executable,
        str(script_dir / "profile_cad_features.py"),
        "--dataset-list",
        str(dataset_index),
        "--out",
        str(profile_path),
        "--paths-out",
        str(out_dir / "complex_paths.txt"),
        "--subset-out",
        str(out_dir / "complex_dataset_index.json"),
        "--report",
        str(out_dir / "cad_feature_profile.md"),
        "--min-score",
        str(min_score),
    ]
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "profile_path": str(profile_path),
        "paths_path": str(out_dir / "complex_paths.txt"),
        "subset_path": str(out_dir / "complex_dataset_index.json"),
        "report_path": str(out_dir / "cad_feature_profile.md"),
    }


def write_report(path: Path, manifest: dict[str, Any]) -> None:
    lines = [
        "# SGGK Public Corpus Fetch",
        "",
        f"- Generated: `{manifest['generated_at']}`",
        f"- Preset: `{manifest['preset']}`",
        f"- Source: [{manifest['source']['repo']}]({manifest['source']['homepage']})",
        f"- License note: `{manifest['source']['license']}`",
        f"- Branch: `{manifest['source']['branch']}`",
        f"- Candidates after filters: `{manifest['candidate_count']}`",
        f"- Selected: `{manifest['selected_count']}`",
        f"- Downloaded: `{manifest['downloaded_count']}`",
        "",
        "## Files",
        "",
        "| status | size | path | local |",
        "| --- | ---: | --- | --- |",
    ]
    for item in manifest["files"]:
        lines.append(
            f"| `{item.get('status')}` | {item.get('size', '')} | `{item.get('path')}` | `{item.get('local_path', '')}` |"
        )
    if manifest.get("discovery"):
        lines.extend(
            [
                "",
                "## Discovery",
                "",
                f"- Dataset index: `{manifest['discovery'].get('index_path')}`",
                f"- Report: `{manifest['discovery'].get('report_path')}`",
                f"- Return code: `{manifest['discovery'].get('returncode')}`",
            ]
        )
    if manifest.get("feature_profile"):
        profile = manifest["feature_profile"]
        lines.extend(
            [
                "",
                "## Feature Profile",
                "",
                f"- Profile: `{profile.get('profile_path')}`",
                f"- Complex paths: `{profile.get('paths_path')}`",
                f"- Complex dataset index: `{profile.get('subset_path')}`",
                f"- Report: `{profile.get('report_path')}`",
                f"- Return code: `{profile.get('returncode')}`",
            ]
        )
    write_text(path, "\n".join(lines) + "\n")


def main() -> int:
    args = parse_args()
    if args.limit < 0 or args.max_size < 0 or args.min_size < 0 or args.profile_min_score < 0:
        print("limits, size filters, and profile score must be >= 0", file=sys.stderr)
        return 2
    preset = PRESETS[args.preset]
    out_dir = Path(args.out).resolve()
    download_dir = out_dir / "files"
    out_dir.mkdir(parents=True, exist_ok=True)

    repo = preset["repo"]
    repo_info = fetch_json(f"https://api.github.com/repos/{repo}")
    branch = repo_info.get("default_branch", "master")
    tree = fetch_json(f"https://api.github.com/repos/{repo}/git/trees/{quote(branch)}?recursive=1")
    extensions = {extension.lower() for extension in preset["extensions"]}
    includes = [re.compile(pattern, re.IGNORECASE) for pattern in args.path_regex]
    excludes = [re.compile(pattern, re.IGNORECASE) for pattern in args.exclude_regex]

    candidates = []
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = str(item.get("path", ""))
        suffix = Path(path).suffix.lower()
        size = int(item.get("size") or 0)
        if suffix not in extensions:
            continue
        if args.min_size and size < args.min_size:
            continue
        if args.max_size and size > args.max_size:
            continue
        if not path_matches(path, includes, excludes):
            continue
        candidates.append(
            {
                "path": path,
                "size": size,
                "sha": item.get("sha"),
                "raw_url": raw_github_url(repo, branch, path),
            }
        )
    candidates.sort(key=lambda value: value["path"].lower())
    selected = select_candidates(candidates, args.limit, args.selection)

    records: list[dict[str, Any]] = []
    downloaded = 0
    for item in selected:
        local_path = download_dir / safe_local_path(item["path"])
        record = {
            **item,
            "local_path": str(local_path),
            "status": "dry_run" if args.dry_run else "pending",
        }
        if args.dry_run:
            records.append(record)
            continue
        if local_path.is_file() and not args.force:
            record["status"] = "exists"
            record["downloaded_bytes"] = local_path.stat().st_size
        else:
            record["downloaded_bytes"] = download_file(item["raw_url"], local_path)
            record["status"] = "downloaded"
        record["sha1"] = file_sha1(local_path)
        downloaded += 1
        records.append(record)

    discovery = None
    feature_profile = None
    if not args.dry_run:
        discovery = run_discovery(out_dir, download_dir, args.hash_inputs)
        if args.profile_features and discovery.get("returncode") == 0:
            feature_profile = run_feature_profile(out_dir, Path(discovery["index_path"]), args.profile_min_score)

    manifest = {
        "generated_at": now_iso_like(),
        "preset": args.preset,
        "source": {
            "name": preset["name"],
            "repo": repo,
            "homepage": preset["homepage"],
            "license": preset["license"],
            "license_url": preset["license_url"],
            "branch": branch,
            "default_branch": repo_info.get("default_branch"),
            "notes": preset.get("notes", []),
        },
        "filters": {
            "limit": args.limit,
            "min_size": args.min_size,
            "max_size": args.max_size,
            "path_regex": args.path_regex,
            "exclude_regex": args.exclude_regex,
            "selection": args.selection,
            "profile_features": args.profile_features,
            "profile_min_score": args.profile_min_score,
        },
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "downloaded_count": downloaded,
        "files": records,
        "discovery": discovery,
        "feature_profile": feature_profile,
    }
    write_json(out_dir / "public_corpus_manifest.json", manifest)
    write_report(out_dir / "public_corpus_report.md", manifest)
    print(f"manifest={out_dir / 'public_corpus_manifest.json'}")
    print(f"report={out_dir / 'public_corpus_report.md'}")
    print(f"dataset_index={out_dir / 'dataset_index.json'}")
    if feature_profile:
        print(f"cad_feature_profile={out_dir / 'cad_feature_profile.json'}")
        print(f"complex_paths={out_dir / 'complex_paths.txt'}")
        print(f"complex_dataset_index={out_dir / 'complex_dataset_index.json'}")
    print(f"candidates={len(candidates)} selected={len(selected)} downloaded={downloaded}")
    if discovery and discovery.get("returncode") != 0:
        print(discovery.get("stderr", ""), file=sys.stderr)
        return int(discovery.get("returncode") or 1)
    if feature_profile and feature_profile.get("returncode") != 0:
        print(feature_profile.get("stderr", ""), file=sys.stderr)
        return int(feature_profile.get("returncode") or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
