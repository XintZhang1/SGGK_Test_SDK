#!/usr/bin/env python3
"""Verify that an SGGK campaign kept the artifacts needed for review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import time
from typing import Any


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, help="Campaign root or campaign_summary.json")
    parser.add_argument("--out", help="Output directory for verification JSON/Markdown")
    parser.add_argument("--allow-duplicate-inputs", action="store_true", help="Do not fail on geometry duplicate input groups")
    parser.add_argument("--allow-duplicate-geometry", action="store_true", help="Do not fail on full-geometry duplicate groups")
    parser.add_argument("--allow-tolerance-mismatches", action="store_true", help="Do not fail on geometry tolerance mismatches")
    parser.add_argument(
        "--expect-known-bug-status",
        action="append",
        default=[],
        help="Require known-bug regression status count key, for example still_failing. Can be repeated.",
    )
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Path):
        return str(value)
    return ""


def as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def is_recipe_lane(lane: dict[str, Any]) -> bool:
    return as_str(lane.get("type")) == "recipe"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def summary_path_for(raw: str) -> tuple[str, Path]:
    path = Path(raw).resolve()
    if path.is_dir():
        campaign_summary = path / "campaign_summary.json"
        if campaign_summary.is_file():
            return "campaign", campaign_summary
        return "shards", path / "campaign_shards_summary.json"
    if path.name == "campaign_shards_summary.json":
        return "shards", path
    return "campaign", path


def resolve_path(raw: Any, base: Path) -> Path:
    text = as_str(raw)
    if not text:
        return Path()
    path = Path(text)
    return path if path.is_absolute() else (base / path).resolve()


class Verifier:
    def __init__(self, campaign_root: Path, args: argparse.Namespace) -> None:
        self.campaign_root = campaign_root
        self.args = args
        self.checks: list[dict[str, Any]] = []

    def add(self, severity: str, kind: str, message: str, path: Any = "") -> None:
        self.checks.append(
            {
                "severity": severity,
                "kind": kind,
                "message": message,
                "path": str(path) if path else "",
            }
        )

    def error(self, kind: str, message: str, path: Any = "") -> None:
        self.add("error", kind, message, path)

    def warning(self, kind: str, message: str, path: Any = "") -> None:
        self.add("warning", kind, message, path)

    def ok(self, kind: str, message: str, path: Any = "") -> None:
        self.add("ok", kind, message, path)

    def require_file(self, raw: Any, label: str, base: Path | None = None) -> Path | None:
        path = resolve_path(raw, base or self.campaign_root)
        if not as_str(raw):
            self.error("missing_path", f"{label} path is not recorded")
            return None
        if not path.is_file():
            self.error("missing_file", f"{label} does not exist", path)
            return None
        if path.stat().st_size <= 0:
            self.error("empty_file", f"{label} is empty", path)
            return None
        self.ok("file", f"{label} exists", path)
        return path

    def require_dir(self, raw: Any, label: str, base: Path | None = None) -> Path | None:
        path = resolve_path(raw, base or self.campaign_root)
        if not as_str(raw):
            self.error("missing_path", f"{label} path is not recorded")
            return None
        if not path.is_dir():
            self.error("missing_dir", f"{label} does not exist", path)
            return None
        self.ok("dir", f"{label} exists", path)
        return path

    def read_required_json(self, raw: Any, label: str, base: Path | None = None) -> Any:
        path = self.require_file(raw, label, base)
        if path is None:
            return None
        try:
            value = read_json(path)
        except Exception as exc:  # noqa: BLE001
            self.error("invalid_json", f"{label} is not readable JSON: {exc}", path)
            return None
        self.ok("json", f"{label} is readable JSON", path)
        return value

    def verify_png(self, raw: Any, label: str, base: Path | None = None) -> None:
        path = self.require_file(raw, label, base)
        if path is None:
            return
        try:
            header = path.read_bytes()[:24]
            if len(header) < 24 or not header.startswith(PNG_SIGNATURE):
                self.error("invalid_png", f"{label} is not a PNG", path)
                return
            width, height = struct.unpack(">II", header[16:24])
            if width <= 0 or height <= 0:
                self.error("invalid_png", f"{label} has invalid dimensions {width}x{height}", path)
                return
            self.ok("png", f"{label} PNG dimensions {width}x{height}", path)
        except Exception as exc:  # noqa: BLE001
            self.error("invalid_png", f"{label} PNG header could not be read: {exc}", path)
            return

        try:
            from PIL import Image  # type: ignore
        except Exception:  # noqa: BLE001
            self.warning("png_blank_check_skipped", f"{label} pixel blank check skipped because Pillow is unavailable", path)
            return

        try:
            with Image.open(path) as image:
                extrema = image.convert("RGB").getextrema()
            if all(lo == hi for lo, hi in extrema):
                self.error("blank_png", f"{label} appears to be a single-color image", path)
            else:
                self.ok("png_nonblank", f"{label} has nonblank pixel extrema", path)
        except Exception as exc:  # noqa: BLE001
            self.error("invalid_png", f"{label} could not be inspected with Pillow: {exc}", path)

    def check_geometry_counts(self, prefix: str, source: dict[str, Any]) -> None:
        duplicate_inputs = as_int(source.get("geometry_audit_duplicate_inputs"))
        duplicate_geometry = as_int(source.get("geometry_audit_duplicate_geometry"))
        tolerance_mismatches = as_int(source.get("geometry_audit_tolerance_mismatches"))
        cases = as_int(source.get("geometry_audit_cases"))
        if cases:
            self.ok("geometry_audit_cases", f"{prefix} geometry audit cases={cases}")
        if duplicate_inputs and not self.args.allow_duplicate_inputs:
            self.error("geometry_duplicate_inputs", f"{prefix} duplicate input groups={duplicate_inputs}")
        else:
            self.ok("geometry_duplicate_inputs", f"{prefix} duplicate input groups={duplicate_inputs}")
        if duplicate_geometry and not self.args.allow_duplicate_geometry:
            self.error("geometry_duplicate_geometry", f"{prefix} duplicate geometry groups={duplicate_geometry}")
        else:
            self.ok("geometry_duplicate_geometry", f"{prefix} duplicate geometry groups={duplicate_geometry}")
        if tolerance_mismatches and not self.args.allow_tolerance_mismatches:
            self.error("geometry_tolerance_mismatches", f"{prefix} tolerance mismatches={tolerance_mismatches}")
        else:
            self.ok("geometry_tolerance_mismatches", f"{prefix} tolerance mismatches={tolerance_mismatches}")

    def verify_dsl_check(self, lane: dict[str, Any], label: str) -> None:
        if not lane.get("dsl") and not lane.get("dsl_check_report"):
            return
        report = self.read_required_json(lane.get("dsl_check_report"), f"{label} DSL check report")
        if not isinstance(report, dict):
            return
        if report.get("ok") is not True or lane.get("dsl_check_ok") is not True:
            self.error("dsl_check_failed", f"{label} DSL check is not ok")
        else:
            self.ok("dsl_check", f"{label} DSL check ok")
        compile_failures = as_int(report.get("compile_failure_count") or lane.get("dsl_check_compile_failure_count"))
        validation_failures = as_int(report.get("validation_failure_count") or lane.get("dsl_check_validation_failure_count"))
        if compile_failures or validation_failures:
            self.error(
                "dsl_check_failures",
                f"{label} DSL check failures compile={compile_failures} validation={validation_failures}",
            )
        else:
            self.ok("dsl_check_failures", f"{label} DSL check failures compile=0 validation=0")

    def verify_lane(self, lane: dict[str, Any], no_preview: bool, no_geometry_audit: bool) -> None:
        name = as_str(lane.get("name")) or "<unnamed>"
        label = f"lane {name}"
        if lane.get("empty_shard"):
            self.ok("empty_shard", f"{label} is an empty shard")
            return
        if lane.get("ok") is False:
            self.error("lane_failed", f"{label} ok=false")
        else:
            self.ok("lane_ok", f"{label} ok={lane.get('ok')}")

        if lane.get("summary_path"):
            self.read_required_json(lane.get("summary_path"), f"{label} recipe summary")
        elif lane.get("type") == "recipe":
            self.error("missing_summary", f"{label} is a recipe lane but has no summary_path")

        if as_int(lane.get("total")) > 0 and as_int(lane.get("executed")) <= 0:
            self.error("lane_not_executed", f"{label} total={lane.get('total')} but executed={lane.get('executed')}")

        self.verify_dsl_check(lane, label)

        should_verify_preview = is_recipe_lane(lane) or bool(lane.get("contact_sheet")) or lane.get("preview_returncode") not in (None, "")
        if not no_preview and as_int(lane.get("total")) > 0 and should_verify_preview:
            if lane.get("preview_returncode") not in (None, 0):
                self.error("preview_failed", f"{label} preview returncode={lane.get('preview_returncode')}")
            self.verify_png(lane.get("contact_sheet"), f"{label} contact sheet")

        should_verify_geometry = (
            is_recipe_lane(lane)
            or bool(lane.get("geometry_audit_report"))
            or bool(lane.get("geometry_audit_out"))
            or lane.get("geometry_audit_returncode") not in (None, "")
        )
        if not no_geometry_audit and as_int(lane.get("total")) > 0 and should_verify_geometry:
            if lane.get("geometry_audit_returncode") not in (None, 0):
                self.error("geometry_audit_failed", f"{label} geometry audit returncode={lane.get('geometry_audit_returncode')}")
            if lane.get("geometry_audit_report"):
                self.require_file(lane.get("geometry_audit_report"), f"{label} geometry audit report")
            elif lane.get("geometry_audit_out"):
                self.require_file(Path(as_str(lane.get("geometry_audit_out"))) / "geometry_audit.md", f"{label} geometry audit report")
            else:
                self.error("missing_geometry_audit", f"{label} has no geometry audit path")
            self.check_geometry_counts(label, lane)

    def verify_known_bug_regression(self, known: dict[str, Any], no_preview: bool, no_geometry_audit: bool) -> None:
        if not known:
            return
        label = "known-bug regression"
        for key in ("materialize_ok", "replay_ok", "regression_ok"):
            if known.get(key) is not True:
                self.error("known_bug_failed", f"{label} {key}={known.get(key)}")
            else:
                self.ok("known_bug", f"{label} {key}=true")
        self.read_required_json(known.get("registry_path"), f"{label} registry")
        self.require_file(known.get("registry_report"), f"{label} registry report")
        self.require_file(known.get("replay_recipes"), f"{label} replay recipe list")
        self.read_required_json(known.get("replay_summary"), f"{label} replay summary")
        self.read_required_json(known.get("regression_summary"), f"{label} regression summary")
        self.require_file(known.get("regression_report"), f"{label} regression report")
        if not no_preview:
            self.verify_png(known.get("replay_contact_sheet"), f"{label} replay contact sheet")
        if not no_geometry_audit:
            self.require_file(known.get("replay_geometry_audit_report"), f"{label} replay geometry audit report")
            self.check_geometry_counts(label, {
                "geometry_audit_cases": known.get("replay_geometry_audit_cases"),
                "geometry_audit_duplicate_inputs": known.get("replay_geometry_audit_duplicate_inputs"),
                "geometry_audit_tolerance_mismatches": known.get("replay_geometry_audit_tolerance_mismatches"),
            })
        status_counts = as_dict(known.get("status_counts"))
        for expected in self.args.expect_known_bug_status:
            if expected not in status_counts:
                self.error("known_bug_status_missing", f"{label} missing status {expected}")
            else:
                self.ok("known_bug_status", f"{label} status {expected}={status_counts.get(expected)}")
        if known.get("debug_handoff_out"):
            if known.get("debug_handoff_ok") is not True:
                self.error("known_bug_debug_handoff_failed", f"{label} debug handoff ok={known.get('debug_handoff_ok')}")
            else:
                self.ok("known_bug_debug_handoff", f"{label} debug handoff ok")
            self.verify_debug_handoff_block(
                {
                    "index": known.get("debug_handoff_index"),
                    "report": known.get("debug_handoff_report"),
                },
                f"{label} debug handoff",
            )
            if as_int(known.get("debug_handoff_pack_count")) <= 0:
                self.error("known_bug_debug_handoff_empty", f"{label} debug handoff has no packs")
            else:
                self.ok("known_bug_debug_handoff_packs", f"{label} debug handoff packs={known.get('debug_handoff_pack_count')}")

    def verify_optional_path_block(self, block: dict[str, Any], label: str, keys: list[str]) -> None:
        if not block:
            return
        if block.get("skipped"):
            self.ok("skipped", f"{label} skipped: {block.get('reason', '')}")
            return
        for key in keys:
            if block.get(key):
                self.require_file(block.get(key), f"{label} {key}")

    def verify_focus_index(self, raw_json: Any, raw_markdown: Any, label: str, focus_sgt_count: int, base: Path) -> None:
        focus = self.read_required_json(raw_json, f"{label} focus index JSON", base)
        self.require_file(raw_markdown, f"{label} focus index markdown", base)
        if not isinstance(focus, dict):
            return
        entries = [entry for entry in as_list(focus.get("entries")) if isinstance(entry, dict)]
        if focus_sgt_count and len(entries) < focus_sgt_count:
            self.error("debug_handoff_focus_index_short", f"{label} focus index entries={len(entries)} focus_sgts={focus_sgt_count}")
        else:
            self.ok("debug_handoff_focus_index_entries", f"{label} focus index entries={len(entries)} focus_sgts={focus_sgt_count}")
        extracted = 0
        for entry_index, entry in enumerate(entries):
            entry_label = f"{label} focus entry {entry_index}"
            focus_sgt = as_str(entry.get("focus_sgt"))
            if focus_sgt:
                extracted += 1
                self.require_file(focus_sgt, f"{entry_label} focus SGT", base)
            source_sgt = as_str(entry.get("source_sgt"))
            if source_sgt:
                self.require_file(source_sgt, f"{entry_label} source SGT", base)
            if focus_sgt and as_str(entry.get("status")) != "ok":
                self.error("debug_handoff_focus_status", f"{entry_label} has focus SGT but status={entry.get('status')}")
        if focus_sgt_count and extracted < focus_sgt_count:
            self.error("debug_handoff_focus_index_missing_sgts", f"{label} focus index extracted={extracted} focus_sgts={focus_sgt_count}")
        elif focus_sgt_count:
            self.ok("debug_handoff_focus_index_sgts", f"{label} focus index extracted={extracted}")

    def verify_visual_index(
        self,
        raw_json: Any,
        raw_markdown: Any,
        label: str,
        debug_sgt_count: int,
        focus_sgt_count: int,
        input_sgt_count: int,
        base: Path,
    ) -> None:
        visual = self.read_required_json(raw_json, f"{label} visual index JSON", base)
        self.require_file(raw_markdown, f"{label} visual index markdown", base)
        if not isinstance(visual, dict):
            return
        self.require_file(visual.get("sgt_paths"), f"{label} visual index SGT path list", base)
        entries = [entry for entry in as_list(visual.get("entries")) if isinstance(entry, dict)]
        expected_min = debug_sgt_count + focus_sgt_count + input_sgt_count
        if len(entries) < expected_min:
            self.error(
                "debug_handoff_visual_index_short",
                f"{label} visual index entries={len(entries)} expected_at_least={expected_min}",
            )
        else:
            self.ok("debug_handoff_visual_index_entries", f"{label} visual index entries={len(entries)} expected_at_least={expected_min}")
        copied_count = 0
        for entry_index, entry in enumerate(entries):
            entry_label = f"{label} visual entry {entry_index}"
            copied_path = as_str(entry.get("copied_path"))
            if copied_path:
                copied_count += 1
                self.require_file(copied_path, f"{entry_label} copied SGT", base)
                if as_str(entry.get("status")) != "ok":
                    self.error("debug_handoff_visual_status", f"{entry_label} has copied SGT but status={entry.get('status')}")
            source_path = as_str(entry.get("source_path"))
            if source_path:
                self.require_file(source_path, f"{entry_label} source SGT", base)
        if copied_count < expected_min:
            self.error("debug_handoff_visual_index_missing_sgts", f"{label} visual copied={copied_count} expected_at_least={expected_min}")
        else:
            self.ok("debug_handoff_visual_index_sgts", f"{label} visual copied={copied_count}")

    def verify_debug_handoff_index(self, index: dict[str, Any], label: str, base: Path) -> None:
        pack_count = as_int(index.get("pack_count"))
        packs = [pack for pack in as_list(index.get("packs")) if isinstance(pack, dict)]
        if pack_count and len(packs) != pack_count:
            self.error("debug_handoff_pack_count_mismatch", f"{label} pack_count={pack_count} listed_packs={len(packs)}")
        elif pack_count:
            self.ok("debug_handoff_pack_count", f"{label} pack_count={pack_count}")
        for pack_index, pack in enumerate(packs):
            pack_label = f"{label} pack {pack_index}"
            pack_dir = self.require_dir(pack.get("pack_dir"), pack_label, base)
            pack_base = pack_dir or base
            self.require_file(pack.get("readme") or pack_base / "README.md", f"{pack_label} README", pack_base)
            manifest = self.read_required_json(pack.get("manifest") or pack_base / "manifest.json", f"{pack_label} manifest", pack_base)
            self.require_file(pack_base / "sgt_paths.txt", f"{pack_label} SGT path list", pack_base)
            debug_sgt_count = as_int(pack.get("debug_sgt_count"))
            focus_sgt_count = as_int(pack.get("focus_sgt_count"))
            input_sgt_count = as_int(pack.get("input_sgt_count"))
            visual_index_md = as_str(pack.get("visual_index"))
            visual_index_json = ""
            copied_visual_index = as_dict(as_dict(manifest).get("copied")).get("visual_index") if isinstance(manifest, dict) else {}
            if isinstance(copied_visual_index, dict):
                visual_index_md = visual_index_md or as_str(copied_visual_index.get("markdown"))
                visual_index_json = as_str(copied_visual_index.get("json"))
            if visual_index_md and not visual_index_json:
                candidate = Path(visual_index_md)
                visual_index_json = str(candidate.with_suffix(".json"))
            if visual_index_md or visual_index_json:
                self.verify_visual_index(visual_index_json, visual_index_md, pack_label, debug_sgt_count, focus_sgt_count, input_sgt_count, pack_base)
            elif debug_sgt_count or focus_sgt_count or input_sgt_count:
                self.warning("debug_handoff_visual_index_missing", f"{pack_label} has SGT assets but no visual index path; likely legacy handoff", pack_base)
            focus_index_md = as_str(pack.get("focus_index"))
            focus_index_json = ""
            copied_focus_index = as_dict(as_dict(manifest).get("copied")).get("focus_index") if isinstance(manifest, dict) else {}
            if isinstance(copied_focus_index, dict):
                focus_index_md = focus_index_md or as_str(copied_focus_index.get("markdown"))
                focus_index_json = as_str(copied_focus_index.get("json"))
            if focus_index_md and not focus_index_json:
                candidate = Path(focus_index_md)
                focus_index_json = str(candidate.with_suffix(".json"))
            if focus_index_md or focus_index_json:
                self.verify_focus_index(focus_index_json, focus_index_md, pack_label, focus_sgt_count, pack_base)
            elif focus_sgt_count:
                self.warning("debug_handoff_focus_index_missing", f"{pack_label} has focus SGTs but no focus index path; likely legacy handoff", pack_base)

    def verify_debug_handoff_block(self, block: dict[str, Any], label: str) -> None:
        if not block:
            return
        if block.get("skipped"):
            self.ok("skipped", f"{label} skipped: {block.get('reason', '')}")
            return
        index_raw = block.get("index") or block.get("index_path") or block.get("debug_handoff_index")
        report_raw = block.get("report") or block.get("report_path") or block.get("debug_handoff_report")
        index = self.read_required_json(index_raw, f"{label} index")
        self.require_file(report_raw, f"{label} report")
        if isinstance(index, dict):
            self.verify_debug_handoff_index(index, label, resolve_path(index_raw, self.campaign_root).parent)

    def verify_reductions(self, block: dict[str, Any]) -> None:
        if not block:
            return
        label = "reductions"
        if block.get("skipped"):
            self.ok("skipped", f"{label} skipped: {block.get('reason', '')}")
            return
        index = self.read_required_json(block.get("summary_path"), f"{label} index")
        self.require_file(block.get("report_path"), f"{label} report")
        if not isinstance(index, dict):
            return
        selected = as_int(index.get("selected_count"))
        completed = as_int(index.get("completed_count"))
        if selected and completed <= 0:
            self.error("reductions_incomplete", f"{label} selected={selected} completed={completed}")
        else:
            self.ok("reductions_completed", f"{label} selected={selected} completed={completed}")
        reductions = [item for item in as_list(index.get("reductions")) if isinstance(item, dict)]
        if selected and len(reductions) != selected:
            self.error("reductions_count_mismatch", f"{label} selected={selected} listed={len(reductions)}")
        elif selected:
            self.ok("reductions_count", f"{label} listed={len(reductions)}")
        groups = [item for item in as_list(index.get("fingerprint_groups")) if isinstance(item, dict)]
        if groups:
            fingerprint_counts: dict[str, int] = {}
            for item_index, item in enumerate(reductions):
                fingerprint = as_str(item.get("fingerprint")) or f"missing_fingerprint_{item_index}"
                fingerprint_counts[fingerprint] = fingerprint_counts.get(fingerprint, 0) + 1
            distinct = len(fingerprint_counts)
            duplicate_groups = sum(1 for count in fingerprint_counts.values() if count > 1)
            if as_int(index.get("distinct_fingerprint_count")) != distinct:
                self.error(
                    "reductions_distinct_fingerprint_count",
                    f"{label} declared distinct={index.get('distinct_fingerprint_count')} actual={distinct}",
                )
            elif len(groups) != distinct:
                self.error("reductions_fingerprint_group_count", f"{label} groups={len(groups)} distinct={distinct}")
            else:
                self.ok("reductions_fingerprint_groups", f"{label} distinct fingerprints={distinct}")
            if as_int(index.get("duplicate_fingerprint_group_count")) != duplicate_groups:
                self.error(
                    "reductions_duplicate_fingerprint_count",
                    f"{label} declared duplicate groups={index.get('duplicate_fingerprint_group_count')} actual={duplicate_groups}",
                )
            else:
                self.ok("reductions_duplicate_fingerprint_groups", f"{label} duplicate groups={duplicate_groups}")
        for item_index, item in enumerate(reductions):
            item_label = f"{label} item {item_index}"
            self.read_required_json(item.get("summary_path"), f"{item_label} summary")
            self.require_file(item.get("report_path"), f"{item_label} report")
            if item.get("reduced_recipe"):
                self.read_required_json(item.get("reduced_recipe"), f"{item_label} reduced recipe")
            if item.get("status") != "completed":
                self.error("reduction_failed", f"{item_label} status={item.get('status')}")
            else:
                self.ok("reduction_status", f"{item_label} completed")

    def verify_reduction_replay(self, block: dict[str, Any]) -> None:
        if not block:
            return
        label = "reduction replay"
        if block.get("skipped"):
            self.ok("skipped", f"{label} skipped: {block.get('reason', '')}")
            return
        self.require_file(block.get("recipe_list"), f"{label} recipe list")
        summary = self.read_required_json(block.get("recipe_summary"), f"{label} recipe summary")
        self.read_required_json(block.get("recipe_manifest"), f"{label} recipe manifest")
        self.read_required_json(block.get("triage_summary"), f"{label} triage summary")
        self.require_file(block.get("triage_report"), f"{label} triage report")
        self.verify_png(block.get("contact_sheet"), f"{label} contact sheet")
        audit = self.read_required_json(block.get("geometry_audit_summary"), f"{label} geometry audit summary")
        self.require_file(block.get("geometry_audit_report"), f"{label} geometry audit report")
        semantic = None
        if as_str(block.get("semantic_check_summary")) or as_str(block.get("semantic_check_report")):
            semantic = self.read_required_json(block.get("semantic_check_summary"), f"{label} semantic check summary")
            self.require_file(block.get("semantic_check_report"), f"{label} semantic check report")
        if isinstance(summary, dict):
            total = as_int(summary.get("total"))
            recorded = as_int(block.get("recipe_count"))
            if recorded and total != recorded:
                self.error("reduction_replay_count_mismatch", f"{label} recipe_count={recorded} summary_total={total}")
            else:
                self.ok("reduction_replay_count", f"{label} recipes={total}")
        if isinstance(audit, dict):
            self.check_geometry_counts(label, {
                "geometry_audit_cases": audit.get("case_count"),
                "geometry_audit_duplicate_inputs": audit.get("duplicate_input_group_count"),
                "geometry_audit_duplicate_geometry": audit.get("duplicate_geometry_group_count"),
                "geometry_audit_tolerance_mismatches": audit.get("tolerance_mismatch_count"),
            })
        if isinstance(semantic, dict):
            if semantic.get("ok") is not True:
                self.error(
                    "reduction_replay_semantic_check",
                    f"{label} semantic check failed status_counts={semantic.get('status_counts')}",
                )
            else:
                self.ok(
                    "reduction_replay_semantic_check",
                    f"{label} semantic stable_same_failure={semantic.get('stable_same_failure_count')}",
                )

    def verify_reduction_materialized(self, block: dict[str, Any]) -> None:
        if not block:
            return
        label = "materialized reduced bug records"
        if block.get("skipped"):
            self.ok("skipped", f"{label} skipped: {block.get('reason', '')}")
            return
        registry = self.read_required_json(block.get("summary_path"), f"{label} registry")
        self.require_file(block.get("report_path"), f"{label} registry report")
        self.require_file(block.get("replay_recipes"), f"{label} replay recipes")
        if block.get("ok") is False:
            self.error("reduction_bug_records_materialized_failed", f"{label} ok=false")
        elif isinstance(registry, dict):
            self.ok("reduction_bug_records_materialized", f"{label} total={registry.get('total')}")
        regression = as_dict(block.get("regression"))
        if regression:
            if regression.get("skipped"):
                self.ok("skipped", f"{label} regression skipped: {regression.get('reason', '')}")
            else:
                regression_summary = self.read_required_json(regression.get("summary_path"), f"{label} regression summary")
                self.require_file(regression.get("report_path"), f"{label} regression report")
                if regression.get("ok") is False:
                    self.error("reduction_bug_regression_failed", f"{label} regression ok=false")
                elif isinstance(regression_summary, dict):
                    self.ok("reduction_bug_regression", f"{label} regression status={regression_summary.get('status_counts')}")

    def verify_promoted_replay(self, block: dict[str, Any]) -> None:
        if not block:
            return
        label = "promoted bug-record replay"
        if block.get("skipped"):
            self.ok("skipped", f"{label} skipped: {block.get('reason', '')}")
            return
        registry = self.read_required_json(block.get("registry_path"), f"{label} registry")
        self.require_file(block.get("registry_report"), f"{label} registry report")
        self.require_file(block.get("replay_recipes"), f"{label} replay recipes")
        replay = self.read_required_json(block.get("replay_summary"), f"{label} replay summary")
        regression = self.read_required_json(block.get("regression_summary_path"), f"{label} regression summary")
        self.require_file(block.get("regression_report_path"), f"{label} regression report")
        if block.get("ok") is False:
            self.error("promoted_bug_records_replay_failed", f"{label} ok=false")
        elif isinstance(registry, dict) and isinstance(replay, dict) and isinstance(regression, dict):
            self.ok(
                "promoted_bug_records_replay",
                f"{label} total={registry.get('total')} replay_failed={replay.get('failed')} status={regression.get('status_counts')}",
            )

    def verify_oracle_coverage(self, block: dict[str, Any]) -> None:
        if not block:
            return
        label = "oracle coverage"
        if block.get("skipped"):
            self.ok("skipped", f"{label} skipped: {block.get('reason', '')}")
            return
        coverage = self.read_required_json(block.get("summary_path"), f"{label} summary")
        self.require_file(block.get("report_path"), f"{label} report")
        if not isinstance(coverage, dict):
            return
        if block.get("ok") is not True or coverage.get("ok") is not True:
            self.error("oracle_coverage_failed", f"{label} ok={block.get('ok')} coverage_ok={coverage.get('ok')}")
        else:
            self.ok("oracle_coverage", f"{label} ok")
        total_cases = as_int(coverage.get("total_cases"))
        validation_present = as_int(coverage.get("validation_present"))
        passed_missing = as_int(coverage.get("passed_missing_validation"))
        passed_below_min = as_int(coverage.get("passed_below_min_oracle_kinds"))
        oracle_counts = as_dict(coverage.get("oracle_counts"))
        if total_cases:
            self.ok("oracle_coverage_cases", f"{label} cases={total_cases} validation_present={validation_present}")
        if passed_missing:
            self.error("oracle_coverage_missing_validation", f"{label} passed cases missing validation={passed_missing}")
        else:
            self.ok("oracle_coverage_missing_validation", f"{label} passed cases missing validation=0")
        if passed_below_min:
            self.error("oracle_coverage_below_min", f"{label} passed cases below minimum oracle kinds={passed_below_min}")
        else:
            self.ok("oracle_coverage_below_min", f"{label} passed cases below minimum oracle kinds=0")
        if as_int(coverage.get("passed_cases")) > 0 and not oracle_counts:
            self.error("oracle_coverage_empty", f"{label} has passed cases but no oracle counts")
        elif oracle_counts:
            self.ok("oracle_coverage_counts", f"{label} oracle kinds={len(oracle_counts)}")

    def verify_dataset_audit(self, block: dict[str, Any], expected: bool) -> None:
        label = "dataset audit"
        if block.get("skipped"):
            if expected:
                self.error("dataset_audit_skipped", f"{label} skipped unexpectedly: {block.get('reason', '')}")
            else:
                self.ok("skipped", f"{label} skipped: {block.get('reason', '')}")
            return
        if not block:
            if expected:
                self.error("dataset_audit_missing", f"{label} block is missing")
            else:
                self.warning("dataset_audit_missing", f"{label} block is missing; older campaign summary or no corpus dataset")
            return
        audit = self.read_required_json(block.get("summary_path"), f"{label} summary")
        self.require_file(block.get("report_path"), f"{label} report")
        if not isinstance(audit, dict):
            return
        if block.get("ok") is not True or audit.get("ok") is not True:
            self.error("dataset_audit_failed", f"{label} ok={block.get('ok')} audit_ok={audit.get('ok')}")
        else:
            self.ok("dataset_audit", f"{label} ok")
        total_files = as_int(audit.get("total_files"))
        missing_files = as_int(audit.get("missing_files"))
        empty_files = as_int(audit.get("empty_files"))
        duplicate_groups = as_int(audit.get("duplicate_content_group_count"))
        hash_ratio = audit.get("hash_coverage_ratio")
        if total_files <= 0 and expected:
            self.error("dataset_audit_empty", f"{label} total files={total_files}")
        else:
            self.ok("dataset_audit_files", f"{label} files={total_files}")
        if missing_files:
            self.error("dataset_audit_missing_files", f"{label} missing files={missing_files}")
        else:
            self.ok("dataset_audit_missing_files", f"{label} missing files=0")
        if empty_files:
            self.error("dataset_audit_empty_files", f"{label} empty files={empty_files}")
        else:
            self.ok("dataset_audit_empty_files", f"{label} empty files=0")
        self.ok("dataset_audit_hash_coverage", f"{label} hash coverage={hash_ratio}")
        self.ok("dataset_audit_duplicates", f"{label} duplicate content groups={duplicate_groups}")

    def verify_dataset_audit_collection(self, block: dict[str, Any]) -> None:
        label = "merged dataset audit"
        if not block:
            self.warning("dataset_audit_collection_missing", f"{label} block is missing; older shard collection")
            return
        if block.get("skipped"):
            self.ok("skipped", f"{label} skipped: {block.get('reason', '')}")
            return
        audit = self.read_required_json(block.get("summary_path"), f"{label} summary")
        self.require_file(block.get("report_path"), f"{label} report")
        if not isinstance(audit, dict):
            return
        if block.get("ok") is not True or audit.get("ok") is not True:
            self.error("dataset_audit_collection_failed", f"{label} ok={block.get('ok')} audit_ok={audit.get('ok')}")
        else:
            self.ok("dataset_audit_collection", f"{label} ok")
        audited = as_int(audit.get("audited_campaign_count"))
        failed = as_int(audit.get("failed_campaign_count"))
        missing_files = as_int(audit.get("missing_files"))
        empty_files = as_int(audit.get("empty_files"))
        total_files = as_int(audit.get("total_files"))
        missing_blocks = as_int(audit.get("missing_block_count"))
        if audited <= 0:
            self.error("dataset_audit_collection_empty", f"{label} audited campaigns={audited}")
        else:
            self.ok("dataset_audit_collection_campaigns", f"{label} audited campaigns={audited}")
        if failed:
            self.error("dataset_audit_collection_failed_campaigns", f"{label} failed campaigns={failed}")
        else:
            self.ok("dataset_audit_collection_failed_campaigns", f"{label} failed campaigns=0")
        if missing_files:
            self.error("dataset_audit_collection_missing_files", f"{label} missing files={missing_files}")
        else:
            self.ok("dataset_audit_collection_missing_files", f"{label} missing files=0")
        if empty_files:
            self.error("dataset_audit_collection_empty_files", f"{label} empty files={empty_files}")
        else:
            self.ok("dataset_audit_collection_empty_files", f"{label} empty files=0")
        if missing_blocks:
            self.warning("dataset_audit_collection_missing_blocks", f"{label} missing/legacy audit blocks={missing_blocks}")
        else:
            self.ok("dataset_audit_collection_missing_blocks", f"{label} missing/legacy audit blocks=0")
        self.ok("dataset_audit_collection_files", f"{label} files={total_files}")
        self.ok("dataset_audit_collection_hash_coverage", f"{label} min hash coverage={audit.get('min_hash_coverage_ratio')}")

    def verify_summary(self, summary: dict[str, Any], summary_path: Path) -> dict[str, Any]:
        self.ok("summary", "campaign summary loaded", summary_path)
        report = self.campaign_root / "campaign_report.md"
        self.require_file(report, "campaign report")
        self.require_file(
            self.campaign_root / "campaign_report.zh-CN.md",
            "Chinese campaign report",
        )
        args = as_dict(summary.get("args"))
        no_preview = bool(args.get("no_preview", False))
        no_geometry_audit = bool(args.get("no_geometry_audit", False))
        dataset_audit_expected = (
            "skip_dataset_audit" in args
            and not bool(args.get("skip_dataset_audit", False))
            and bool(args.get("dataset_root") or args.get("dataset_list"))
        )

        for command in as_list(summary.get("commands")):
            if not isinstance(command, dict):
                continue
            name = as_str(command.get("name")) or "<unnamed>"
            if name == "artifact_verification":
                self.ok("command_meta", "ignored prior artifact_verification command record")
                continue
            if command.get("ok") is False:
                self.error("command_failed", f"command {name} ok=false returncode={command.get('returncode')}")
            else:
                self.ok("command_ok", f"command {name} ok={command.get('ok')}")

        for lane in as_list(summary.get("lanes")):
            if isinstance(lane, dict):
                self.verify_lane(lane, no_preview, no_geometry_audit)

        bug_registry = as_dict(summary.get("bug_registry"))
        if bug_registry:
            if bug_registry.get("skipped"):
                self.ok("skipped", f"bug registry skipped: {bug_registry.get('reason', '')}")
            else:
                self.read_required_json(bug_registry.get("summary_path"), "bug registry summary")
                self.require_file(bug_registry.get("report_path"), "bug registry report")
                self.require_file(bug_registry.get("replay_recipes"), "bug registry replay recipes")
        self.verify_debug_handoff_block(as_dict(summary.get("debug_handoff")), "debug handoff")
        self.verify_reductions(as_dict(summary.get("reductions")))
        bug_record_drafts = as_dict(summary.get("bug_record_drafts"))
        if bug_record_drafts:
            if bug_record_drafts.get("skipped"):
                self.ok(
                    "skipped",
                    f"bug record drafts skipped: {bug_record_drafts.get('reason', '')}",
                )
            else:
                self.read_required_json(
                    bug_record_drafts.get("draft_path"),
                    "bug record drafts",
                )
        self.verify_optional_path_block(as_dict(summary.get("bug_records_promoted")), "promoted bug records", [
            "record_path",
            "report_path",
            "portability_summary_path",
            "portability_report_path",
        ])
        self.verify_promoted_replay(as_dict(summary.get("bug_records_promoted_replay")))
        self.verify_known_bug_regression(as_dict(summary.get("known_bug_regression")), no_preview, no_geometry_audit)
        self.verify_dataset_audit(as_dict(summary.get("dataset_audit")), dataset_audit_expected)
        self.verify_oracle_coverage(as_dict(summary.get("oracle_coverage")))

        errors = [item for item in self.checks if item.get("severity") == "error"]
        warnings = [item for item in self.checks if item.get("severity") == "warning"]
        return {
            "generated_at": now_iso_like(),
            "summary_kind": "campaign",
            "campaign_root": str(self.campaign_root),
            "summary_path": str(summary_path),
            "ok": not errors,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "check_count": len(self.checks),
            "checks": self.checks,
        }

    def verify_shard_collection(self, summary: dict[str, Any], summary_path: Path) -> dict[str, Any]:
        self.ok("summary", "campaign shard summary loaded", summary_path)
        self.require_file(self.campaign_root / "campaign_shards_report.md", "campaign shard collection report")
        if as_int(summary.get("campaign_count")) <= 0:
            self.error("empty_shard_collection", "campaign shard collection has no campaigns")
        else:
            self.ok("campaign_count", f"campaign shard collection campaigns={summary.get('campaign_count')}")

        totals = as_dict(summary.get("totals"))
        lane_totals = as_dict(totals.get("lanes"))
        for name, lane in lane_totals.items():
            if not isinstance(lane, dict):
                continue
            prefix = f"merged lane {name}"
            failed_reports = as_int(lane.get("dsl_check_failed_reports"))
            compile_failures = as_int(lane.get("dsl_check_compile_failures"))
            validation_failures = as_int(lane.get("dsl_check_validation_failures"))
            if failed_reports or compile_failures or validation_failures:
                self.error(
                    "merged_dsl_check_failures",
                    f"{prefix} DSL check failures reports={failed_reports} compile={compile_failures} validation={validation_failures}",
                )
            elif as_int(lane.get("dsl_check_reports")):
                self.ok("merged_dsl_check", f"{prefix} DSL checks ok reports={lane.get('dsl_check_reports')}")
            if as_int(lane.get("failed")):
                self.warning("merged_lane_failures", f"{prefix} has failed cases={lane.get('failed')}")

        for index, campaign in enumerate(as_list(summary.get("campaigns"))):
            if not isinstance(campaign, dict):
                continue
            label = f"shard campaign {index}"
            if campaign.get("ok") is False:
                self.error("shard_campaign_failed", f"{label} ok=false root={campaign.get('root')}")
            else:
                self.ok("shard_campaign_ok", f"{label} ok={campaign.get('ok')} root={campaign.get('root')}")
            campaign_summary = self.read_required_json(campaign.get("summary_path"), f"{label} campaign summary")
            if isinstance(campaign_summary, dict):
                sub_root = resolve_path(campaign.get("root"), self.campaign_root)
                if not str(sub_root):
                    sub_root = resolve_path(campaign.get("summary_path"), self.campaign_root).parent
                sub = Verifier(sub_root, self.args)
                sub.verify_summary(campaign_summary, resolve_path(campaign.get("summary_path"), self.campaign_root))
                self.checks.extend(sub.checks)

        self.verify_optional_path_block(as_dict(summary.get("bug_registry")), "merged bug registry", [
            "registry_path",
            "registry_report",
            "summary_path",
            "report_path",
        ])
        self.verify_debug_handoff_block(as_dict(summary.get("debug_handoff")), "merged debug handoff")
        self.verify_dataset_audit_collection(as_dict(summary.get("dataset_audit")))
        self.verify_optional_path_block(as_dict(summary.get("bug_record_drafts")), "merged bug record drafts", [
            "drafts_path",
            "draft_path",
        ])
        self.verify_optional_path_block(as_dict(summary.get("bug_records_promoted")), "merged promoted bug records", [
            "record_path",
            "report_path",
            "portability_summary_path",
            "portability_report_path",
        ])
        self.verify_promoted_replay(as_dict(summary.get("bug_records_promoted_replay")))
        self.verify_optional_path_block(as_dict(summary.get("bug_records_materialized")), "merged materialized bug records", [
            "registry_path",
            "registry_report",
            "replay_recipes",
        ])
        self.verify_reductions(as_dict(summary.get("reductions")))
        self.verify_reduction_replay(as_dict(summary.get("reduction_replay")))
        self.verify_optional_path_block(as_dict(summary.get("reduction_bug_record_drafts")), "reduced bug record drafts", [
            "drafts_path",
            "draft_path",
        ])
        self.verify_reduction_materialized(as_dict(summary.get("reduction_bug_records_materialized")))
        self.verify_oracle_coverage(as_dict(summary.get("oracle_coverage")))

        errors = [item for item in self.checks if item.get("severity") == "error"]
        warnings = [item for item in self.checks if item.get("severity") == "warning"]
        return {
            "generated_at": now_iso_like(),
            "summary_kind": "shards",
            "campaign_root": str(self.campaign_root),
            "summary_path": str(summary_path),
            "ok": not errors,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "check_count": len(self.checks),
            "checks": self.checks,
        }


def markdown_report(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Campaign Artifact Verification")
    lines.append("")
    lines.append(f"- Generated: `{result.get('generated_at')}`")
    lines.append(f"- Campaign: `{result.get('campaign_root')}`")
    lines.append(f"- OK: `{result.get('ok')}`")
    lines.append(f"- Errors: `{result.get('error_count')}`")
    lines.append(f"- Warnings: `{result.get('warning_count')}`")
    lines.append("")
    for title, severity in (("Errors", "error"), ("Warnings", "warning")):
        items = [item for item in as_list(result.get("checks")) if as_dict(item).get("severity") == severity]
        if not items:
            continue
        lines.append(f"## {title}")
        lines.append("")
        for item in items:
            check = as_dict(item)
            path = f" path=`{check.get('path')}`" if check.get("path") else ""
            lines.append(f"- `{check.get('kind')}` {check.get('message')}{path}")
        lines.append("")
    lines.append("## Check Summary")
    lines.append("")
    counts: dict[str, int] = {}
    for item in as_list(result.get("checks")):
        check = as_dict(item)
        key = f"{check.get('severity')}:{check.get('kind')}"
        counts[key] = counts.get(key, 0) + 1
    for key in sorted(counts):
        lines.append(f"- `{key}`: `{counts[key]}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    summary_kind, summary_path = summary_path_for(args.campaign)
    campaign_root = summary_path.parent
    out_dir = Path(args.out).resolve() if args.out else campaign_root / "campaign_verification"
    verifier = Verifier(campaign_root, args)
    if not summary_path.is_file():
        verifier.error("missing_summary", "campaign_summary.json does not exist", summary_path)
        result = {
            "generated_at": now_iso_like(),
            "campaign_root": str(campaign_root),
            "summary_path": str(summary_path),
            "ok": False,
            "error_count": 1,
            "warning_count": 0,
            "check_count": len(verifier.checks),
            "checks": verifier.checks,
        }
    else:
        try:
            summary = read_json(summary_path)
        except Exception as exc:  # noqa: BLE001
            verifier.error("invalid_summary", f"campaign summary is not readable JSON: {exc}", summary_path)
            result = {
                "generated_at": now_iso_like(),
                "campaign_root": str(campaign_root),
                "summary_path": str(summary_path),
                "ok": False,
                "error_count": 1,
                "warning_count": 0,
                "check_count": len(verifier.checks),
                "checks": verifier.checks,
            }
        else:
            if summary_kind == "shards":
                result = verifier.verify_shard_collection(as_dict(summary), summary_path)
            else:
                result = verifier.verify_summary(as_dict(summary), summary_path)

    write_json(out_dir / "campaign_verification.json", result)
    (out_dir / "campaign_verification.md").write_text(markdown_report(result), encoding="utf-8")
    print(f"summary={out_dir / 'campaign_verification.json'}")
    print(f"report={out_dir / 'campaign_verification.md'}")
    print(f"ok={result.get('ok')} errors={result.get('error_count')} warnings={result.get('warning_count')}")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
