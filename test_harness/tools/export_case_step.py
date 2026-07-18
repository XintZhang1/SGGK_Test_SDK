#!/usr/bin/env python3
"""Export one boolean case's SGT bodies to STEP for cross-kernel comparison.

The runner has no dedicated ``step_export`` recipe yet, so this helper reuses
the proven ``step_roundtrip`` lane and keeps only the exported
``output/roundtrip.step``.  The roundtrip import/compare result is ignored on
purpose: an import-side SDK defect must not hide a successful export.  Export
success is defined as a nonempty STEP file with a recorded SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RESULT_KIND = "sggk_case_step_export"
REPO_ROOT = Path(__file__).resolve().parents[2]

EXPORT_ROLES = ("target", "tool")


class ExportError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _case_sgt_inputs(case_dir: Path) -> dict[str, Path]:
    inputs: dict[str, Path] = {}
    for role in EXPORT_ROLES:
        candidate = case_dir / "input" / f"{role}.sgt"
        if candidate.is_file():
            inputs[role] = candidate
    return inputs


def _case_result_sgts(case_dir: Path) -> list[Path]:
    output_dir = case_dir / "output"
    if not output_dir.is_dir():
        return []
    return sorted(
        (path for path in output_dir.glob("result_*.sgt") if path.is_file()),
        key=lambda path: path.name,
    )


def _export_one_sgt(runner: Path, sgt_path: Path, work_root: Path, timeout: float) -> Path:
    """Run step_roundtrip on one SGT and return the exported roundtrip.step."""

    work_root.mkdir(parents=True, exist_ok=True)
    case_id = f"export_{sgt_path.stem}"
    recipe = {
        "case_id": case_id,
        "api": "step_roundtrip",
        "source_file": str(sgt_path),
        "source_body_index": 0,
    }
    recipe_path = work_root / f"{case_id}.json"
    recipe_path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command = [
        str(runner),
        "--recipe",
        str(recipe_path),
        "--out",
        str(work_root / "run"),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
    )
    # The roundtrip import/compare may fail for unrelated SDK import defects;
    # only the exported STEP file matters here.
    candidates = sorted((work_root / "run").rglob("roundtrip.step"))
    exported = next((path for path in candidates if path.is_file() and path.stat().st_size > 0), None)
    if exported is None:
        raise ExportError(
            f"step export produced no STEP for {sgt_path.name} "
            f"(runner rc={completed.returncode}; stderr tail: {completed.stderr[-400:]!r})"
        )
    return exported


def export_case_steps(
    case_dir: Path,
    runner: Path,
    out_dir: Path,
    *,
    timeout: float = 120.0,
    include_results: bool = True,
) -> dict[str, Any]:
    case_dir = case_dir.expanduser().resolve()
    runner = runner.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    if not case_dir.is_dir():
        raise ExportError(f"case directory does not exist: {case_dir}")
    if not runner.is_file():
        raise ExportError(f"runner does not exist: {runner}")
    inputs = _case_sgt_inputs(case_dir)
    missing = [role for role in EXPORT_ROLES if role not in inputs]
    if missing:
        raise ExportError(f"case is missing input SGT for roles: {', '.join(missing)}")
    result_sgts = _case_result_sgts(case_dir) if include_results else []
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "case_dir": str(case_dir),
        "exports": {},
        "ok": True,
        "errors": [],
    }
    with tempfile.TemporaryDirectory(prefix="sggk-step-export-", dir=str(out_dir)) as work_root:
        work = Path(work_root)
        jobs: list[tuple[str, Path, str]] = [(role, path, f"{role}.step") for role, path in inputs.items()]
        jobs.extend((f"result_{index}", path, f"result_{index}.step") for index, path in enumerate(result_sgts, start=1))
        for role, sgt_path, output_name in jobs:
            try:
                exported = _export_one_sgt(runner, sgt_path, work / role, timeout)
                destination = out_dir / output_name
                shutil.copyfile(exported, destination)
                manifest["exports"][role] = {
                    "source_sgt": str(sgt_path),
                    "step": str(destination),
                    "sha256": _sha256_file(destination),
                    "size_bytes": destination.stat().st_size,
                    "ok": True,
                }
            except (ExportError, subprocess.TimeoutExpired, OSError) as exc:
                manifest["ok"] = False
                manifest["errors"].append(f"{role}: {exc}")
                manifest["exports"][role] = {
                    "source_sgt": str(sgt_path),
                    "step": "",
                    "sha256": "",
                    "size_bytes": 0,
                    "ok": False,
                    "error": str(exc),
                }
    _write_json(out_dir / "export_manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, type=Path, help="Boolean case artifact directory")
    parser.add_argument("--runner", required=True, type=Path, help="sggk_case_runner.exe path")
    parser.add_argument("--out", required=True, type=Path, help="Export output directory")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--skip-results", action="store_true", help="Export only target/tool, not result_*.sgt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = export_case_steps(
            args.case,
            args.runner,
            args.out,
            timeout=args.timeout,
            include_results=not args.skip_results,
        )
    except ExportError as exc:
        print(f"export error: {exc}", file=sys.stderr)
        return 1
    print(f"export_manifest={args.out / 'export_manifest.json'}")
    print(f"exported={sum(1 for item in manifest['exports'].values() if item['ok'])}")
    print(f"errors={len(manifest['errors'])}")
    return 0 if manifest["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
