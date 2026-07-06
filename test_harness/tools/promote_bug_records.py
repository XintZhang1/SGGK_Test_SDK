#!/usr/bin/env python3
"""Promote campaign-local SGGK bug drafts into portable regression records."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any


ASSET_EXTENSIONS = {".sgt", ".step", ".stp", ".iges", ".igs"}
RECORD_ASSET_KEYS = (
    "source_sgt",
    "source_step",
    "source_stp",
    "source_iges",
    "source_igs",
    "target_sgt",
    "tool_sgt",
)
RECIPE_ASSET_KEYS = {
    "source_file",
    "source_sgt",
    "source_step",
    "source_stp",
    "source_iges",
    "source_igs",
    "target_source_file",
    "tool_source_file",
    "target_sgt",
    "tool_sgt",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", action="append", required=True, help="Draft/record JSON file or directory. Can repeat.")
    parser.add_argument("--out", required=True, help="Portable bug-record JSON file to write")
    parser.add_argument("--repo-root", default=".", help="Repository root used for repo-relative emitted paths")
    parser.add_argument("--fixture-root", default="test_harness/fixtures/bug_records", help="Fixture root relative to --repo-root or absolute")
    parser.add_argument("--registry-id", default="", help="registry_id for the output JSON; defaults to output stem")
    parser.add_argument("--description", default="", help="Description for the promoted record set")
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting output JSON and copied fixture assets")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def sanitize_name(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "record")).strip("._")
    return text or "record"


def expand_record_files(values: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in values:
        path = Path(raw).resolve()
        if path.is_dir():
            files.extend(child for child in path.rglob("*.json") if child.is_file())
        else:
            files.append(path)
    return sorted(set(files), key=lambda item: str(item).lower())


def records_from_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = read_json(path)
    if isinstance(root, list):
        return {}, [item for item in root if isinstance(item, dict)]
    if not isinstance(root, dict):
        raise ValueError(f"record file root must be an object or list: {path}")
    records = root.get("records")
    if not isinstance(records, list):
        raise ValueError(f"record file must contain a records array: {path}")
    return as_dict(root.get("defaults")), [item for item in records if isinstance(item, dict)]


def resolve_path(raw: Any, base: Path) -> Path:
    text = as_str(raw)
    if not text:
        return Path()
    path = Path(text)
    return path if path.is_absolute() else (base / path).resolve()


def repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"path is outside repo root: {path} (repo root {repo_root})") from exc


def record_relative(path: Path, record_out: Path) -> str:
    return Path(os.path.relpath(path.resolve(), record_out.parent.resolve())).as_posix()


def portable_record_asset_path(path: Path, record_out: Path) -> str:
    return record_relative(path, record_out)


def load_replay_recipe(record: dict[str, Any], defaults: dict[str, Any], record_file: Path) -> dict[str, Any]:
    replay = as_dict(record.get("replay"))
    if isinstance(replay.get("recipe"), dict):
        return dict(replay["recipe"])
    if isinstance(record.get("recipe"), dict):
        return dict(record["recipe"])
    recipe_path = as_str(replay.get("recipe_path") or record.get("recipe_path") or record.get("replay_recipe") or defaults.get("recipe_path"))
    if not recipe_path:
        raise ValueError(f"{record_file}: record {record.get('bug_id') or record.get('fingerprint')} has no replay recipe")
    path = resolve_path(recipe_path, record_file.parent)
    if not path.is_file():
        raise FileNotFoundError(f"replay recipe not found: {path}")
    recipe = read_json(path)
    if not isinstance(recipe, dict):
        raise ValueError(f"replay recipe root must be an object: {path}")
    return dict(recipe)


def copy_asset(
    raw: str,
    record_file: Path,
    bug_fixture_dir: Path,
    repo_root: Path,
    record_out: Path,
    copied: dict[str, Path],
    overwrite: bool,
) -> tuple[str, str]:
    source = resolve_path(raw, record_file.parent)
    if not source.is_file():
        raise FileNotFoundError(f"asset path not found: {source}")
    key = str(source.resolve()).lower()
    if key in copied:
        dest = copied[key]
    else:
        dest_name = sanitize_name(source.stem) + source.suffix.lower()
        dest = bug_fixture_dir / dest_name
        used_dests = {str(item.resolve()).lower() for item in copied.values()}
        suffix_index = 2
        while str(dest.resolve()).lower() in used_dests or (dest.exists() and not overwrite):
            dest = bug_fixture_dir / f"{sanitize_name(source.stem)}_{suffix_index}{source.suffix.lower()}"
            suffix_index += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite existing fixture asset: {dest}")
        shutil.copy2(source, dest)
        copied[key] = dest
    return repo_relative(dest, repo_root), portable_record_asset_path(dest, record_out)


def collect_fixture_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            refs.update(collect_fixture_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(collect_fixture_refs(child))
    elif isinstance(value, str):
        normalized = value.replace("\\", "/")
        for marker in ("test_harness/fixtures/bug_records/", "fixtures/bug_records/"):
            if marker in normalized:
                refs.add(normalized[normalized.index(marker) + len(marker) :])
                break
    return refs


def is_local_asset_text(value: str) -> bool:
    if not value or value.startswith(("http://", "https://")):
        return False
    return Path(value).suffix.lower() in ASSET_EXTENSIONS


def should_copy_recipe_field(key: str, value: Any) -> bool:
    return isinstance(value, str) and is_local_asset_text(value) and (key in RECIPE_ASSET_KEYS or key.endswith("_source_file"))


def rewrite_recipe_assets(
    value: Any,
    record_file: Path,
    bug_fixture_dir: Path,
    repo_root: Path,
    record_out: Path,
    copied: dict[str, Path],
    overwrite: bool,
) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if should_copy_recipe_field(str(key), child):
                repo_path, _record_path = copy_asset(child, record_file, bug_fixture_dir, repo_root, record_out, copied, overwrite)
                result[key] = repo_path
            else:
                result[key] = rewrite_recipe_assets(child, record_file, bug_fixture_dir, repo_root, record_out, copied, overwrite)
        return result
    if isinstance(value, list):
        return [rewrite_recipe_assets(item, record_file, bug_fixture_dir, repo_root, record_out, copied, overwrite) for item in value]
    return value


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = as_str(value)
        if text:
            return text
    return ""


def handoff_observation(record: dict[str, Any]) -> str:
    handoff = as_dict(record.get("debug_handoff"))
    if not handoff:
        return ""
    parts = [
        "GUI handoff existed in source campaign",
        f"debug_sgt_count={handoff.get('debug_sgt_count', 0)}",
        f"focus_sgt_count={handoff.get('focus_sgt_count', 0)}",
        f"input_sgt_count={handoff.get('input_sgt_count', 0)}",
    ]
    if handoff.get("has_preview") is not None:
        parts.append(f"has_preview={handoff.get('has_preview')}")
    return "; ".join(parts)


def promote_record(
    record: dict[str, Any],
    defaults: dict[str, Any],
    record_file: Path,
    record_out: Path,
    fixture_root: Path,
    repo_root: Path,
    overwrite: bool,
) -> dict[str, Any]:
    bug_id = first_nonempty(record.get("bug_id"), record.get("fingerprint"), record.get("representative_case_id"))
    fixture_name = sanitize_name(bug_id)
    bug_fixture_dir = fixture_root / fixture_name
    copied: dict[str, Path] = {}
    recipe = load_replay_recipe(record, defaults, record_file)
    recipe["case_id"] = first_nonempty(recipe.get("case_id"), record.get("representative_case_id"), bug_id)
    recipe = rewrite_recipe_assets(recipe, record_file, bug_fixture_dir, repo_root, record_out, copied, overwrite)

    promoted: dict[str, Any] = {
        "bug_id": bug_id,
        "fingerprint": first_nonempty(record.get("fingerprint"), bug_id),
        "title": as_str(record.get("title")),
        "representative_case_id": first_nonempty(record.get("representative_case_id"), recipe.get("case_id")),
        "api": first_nonempty(record.get("api"), recipe.get("api"), defaults.get("api")),
        "replay_status": first_nonempty(record.get("replay_status"), defaults.get("replay_status"), "stable_failure"),
        "reasons": as_list(record.get("reasons")) or as_list(defaults.get("reasons")),
        "validation_failures": as_list(record.get("validation_failures")),
        "validation_oracle_details": as_list(record.get("validation_oracle_details")),
        "roundtrip_failures": as_list(record.get("roundtrip_failures")),
        "roundtrip_oracle_details": as_list(record.get("roundtrip_oracle_details")),
        "expected": as_dict(record.get("expected")),
        "topo_track_policy": first_nonempty(record.get("topo_track_policy"), defaults.get("topo_track_policy"), "diagnostic_when_modeling_fails"),
        "topo_track_diagnostic": as_dict(record.get("topo_track_diagnostic")) or as_dict(defaults.get("topo_track_diagnostic")),
        "modeling_failure_required": bool(record.get("modeling_failure_required", defaults.get("modeling_failure_required", True))),
        "primary_contact": as_dict(record.get("primary_contact")),
        "localized_inputs": as_list(record.get("localized_inputs")),
        "replay": {"recipe": recipe},
    }

    for key in RECORD_ASSET_KEYS:
        raw = as_str(record.get(key))
        if raw and is_local_asset_text(raw):
            _repo_path, record_path = copy_asset(raw, record_file, bug_fixture_dir, repo_root, record_out, copied, overwrite)
            promoted[key] = record_path

    observations: list[Any] = []
    observations.extend(as_list(defaults.get("observations")))
    observations.extend(as_list(record.get("observations")))
    if as_str(record.get("notes")):
        observations.append(as_str(record.get("notes")))
    observations.append(f"Promoted from campaign-local bug record {record_file.name} at {now_iso_like()}.")
    handoff_note = handoff_observation(record)
    if handoff_note:
        observations.append(handoff_note)
    promoted["observations"] = observations
    promoted["notes"] = "Portable promoted record. Transient artifact paths and GUI launcher paths were stripped; replay assets are stored under test_harness/fixtures/bug_records."

    return {key: value for key, value in promoted.items() if value not in ("", [], {}, None)}


def markdown_report(root: dict[str, Any], out_path: Path, fixture_root: Path) -> str:
    lines = [
        "# Promoted Bug Records",
        "",
        f"- Generated: `{root.get('generated_at')}`",
        f"- Output: `{out_path}`",
        f"- Fixture root: `{fixture_root}`",
        f"- Records: `{len(as_list(root.get('records')))}`",
        "",
        "| bug id | fingerprint | replay kind | assets |",
        "| --- | --- | --- | ---: |",
    ]
    for record in as_list(root.get("records")):
        if not isinstance(record, dict):
            continue
        recipe = as_dict(as_dict(record.get("replay")).get("recipe"))
        asset_count = len(collect_fixture_refs(record))
        lines.append(
            f"| `{record.get('bug_id')}` | `{record.get('fingerprint')}` | `{recipe.get('api', '')}` | `{asset_count}` |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    fixture_root = Path(args.fixture_root)
    if not fixture_root.is_absolute():
        fixture_root = (repo_root / fixture_root).resolve()
    out_path = Path(args.out).resolve()
    if out_path.exists() and not args.overwrite:
        print(f"refusing to overwrite existing output: {out_path}")
        return 2

    records: list[dict[str, Any]] = []
    for record_file in expand_record_files(args.records):
        defaults, items = records_from_file(record_file)
        for item in items:
            records.append(promote_record(item, defaults, record_file, out_path, fixture_root, repo_root, args.overwrite))

    output = {
        "schema_version": 1,
        "registry_id": args.registry_id or sanitize_name(out_path.stem),
        "description": args.description or "Portable bug records promoted from campaign-local drafts.",
        "generated_at": now_iso_like(),
        "defaults": {
            "replay_status": "stable_failure",
            "modeling_failure_required": True,
            "topo_track_policy": "diagnostic_when_modeling_fails",
        },
        "records": records,
    }
    write_json(out_path, output, args.overwrite)
    report_path = out_path.with_suffix(".md")
    write_text(report_path, markdown_report(output, out_path, fixture_root), args.overwrite)
    print(f"records={out_path}")
    print(f"report={report_path}")
    print(f"fixtures={fixture_root}")
    print(f"count={len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
