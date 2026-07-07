#!/usr/bin/env python3
"""Prepare and optionally execute the interface distillation workflow.

The default mode is SDK-free: read the interface distillation manifest, build
one small-model task per developer form, and write a campaign report. When
reviewed small-model outputs and a Windows runner are available, pass
``--execute`` to normalize, check, compile, run, triage, preview, and report
those outputs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from build_api_test_task import build_task, render_markdown, validate_form


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--forms-dir",
        default="test_harness/forms/interface_distillation",
        help="Directory containing interface distillation form JSON files.",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="Manifest JSON. Defaults to <forms-dir>/00_manifest.json.",
    )
    parser.add_argument("--out", default="artifacts/interface_distillation", help="Workflow output root.")
    parser.add_argument(
        "--model-output-root",
        default="artifacts/model_outputs",
        help="Directory where reviewed small-model JSON outputs are saved.",
    )
    parser.add_argument(
        "--runner",
        default="build/test_harness/Release/sggk_case_runner.exe",
        help="Path to sggk_case_runner.exe when --execute is used.",
    )
    parser.add_argument("--execute", action="store_true", help="Check/compile/run reviewed model outputs when present.")
    parser.add_argument("--require-model-outputs", action="store_true", help="Return non-zero if any model output is missing.")
    parser.add_argument("--api-smoke", action="store_true", help="Run the current API smoke suite after model-output lanes.")
    parser.add_argument("--abc-sample-smoke", action="store_true", help="Run the focused ABC sample smoke lane.")
    parser.add_argument("--abc-fetch-root", default="", help="ABC fetch root; defaults to manifest abc_inputs.preferred_fetch_root.")
    parser.add_argument("--source-root", action="append", default=[], help="SGGK source/include root to scan for source-guided tasks.")
    parser.add_argument("--source-scan-max-findings", type=int, default=120)
    parser.add_argument("--source-scan-max-seeds", type=int, default=30)
    parser.add_argument("--source-task-max-tasks", type=int, default=80)
    parser.add_argument("--source-task-context-lines", type=int, default=12)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--fail-on-failures", action="store_true", help="Return non-zero for SDK/test failures.")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def safe_id(value: str) -> str:
    result = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in value)
    result = result.strip("._-")
    return result or "task"


def run_command(name: str, cmd: list[str], acceptable: set[int] | None = None) -> dict[str, Any]:
    if acceptable is None:
        acceptable = {0}
    print(f"[interface-distillation] {name}")
    print("  " + " ".join(cmd))
    started = time.perf_counter()
    completed = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    elapsed = time.perf_counter() - started
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return {
        "name": name,
        "command": cmd,
        "returncode": completed.returncode,
        "acceptable": sorted(acceptable),
        "ok": completed.returncode in acceptable,
        "elapsed_seconds": elapsed,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def manifest_forms(forms_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    entries = manifest.get("forms")
    if isinstance(entries, list) and entries:
        result: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            form_name = entry.get("form")
            if not isinstance(form_name, str) or not form_name:
                continue
            item = dict(entry)
            item["path"] = str(forms_dir / form_name)
            result.append(item)
        return sorted(result, key=lambda item: int(item.get("order", 999999)))

    result = []
    for path in sorted(forms_dir.glob("*.json")):
        if path.name == "00_manifest.json":
            continue
        result.append({"form": path.name, "path": str(path)})
    return result


def build_tasks(forms_dir: Path, manifest: dict[str, Any], out_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    tasks_dir = out_root / "model_tasks"
    prompts_dir = out_root / "model_prompts"
    for entry in manifest_forms(forms_dir, manifest):
        form_path = Path(str(entry["path"]))
        form = read_json(form_path)
        errors, warnings = validate_form(form)
        request_id = str(form.get("request_id", form_path.stem)) if isinstance(form, dict) else form_path.stem
        task_path = tasks_dir / f"{safe_id(request_id)}.json"
        prompt_path = prompts_dir / f"{safe_id(request_id)}.md"
        record: dict[str, Any] = {
            "request_id": request_id,
            "form": str(form_path),
            "manifest_entry": entry,
            "task_path": str(task_path),
            "prompt_path": str(prompt_path),
            "errors": errors,
            "warnings": warnings,
            "stage": "form_error" if errors else "task_built",
        }
        if not errors:
            task = build_task(form_path.resolve(), form, warnings)
            write_json(task_path, task)
            write_text(prompt_path, render_markdown(task))
            record["target_api"] = form.get("target_api")
            record["geometry_family"] = (form.get("geometry") or {}).get("family")
            record["preferred_output"] = (task.get("api_guidance") or {}).get("preferred_format")
            record["fixed_commands"] = task.get("fixed_commands", [])
        records.append(record)
    return records


def find_model_output(request_id: str, model_output_root: Path) -> tuple[str, Path | None]:
    base = safe_id(request_id)
    candidates = [
        ("raw", model_output_root / f"{base}.json"),
        ("dsl", model_output_root / f"{base}_dsl.json"),
        ("recipe", model_output_root / f"{base}_recipe.json"),
        ("cluster_seed", model_output_root / f"{base}_cluster_seed.json"),
    ]
    for kind, path in candidates:
        if path.is_file():
            return kind, path
    return "missing", None


def normalize_model_output(record: dict[str, Any], model_output_root: Path) -> dict[str, Any]:
    request_id = str(record["request_id"])
    found_kind, found_path = find_model_output(request_id, model_output_root)
    model_record: dict[str, Any] = {
        "found": found_path is not None,
        "input_kind": found_kind,
        "input_path": str(found_path) if found_path else "",
        "kind": "",
        "normalized_path": "",
        "notes": [],
    }
    if found_path is None:
        model_record["kind"] = "missing"
        return model_record

    loaded = read_json(found_path)
    base = safe_id(request_id)
    if found_kind == "dsl":
        model_record["kind"] = "attack_dsl"
        model_record["normalized_path"] = str(found_path)
        return model_record
    if found_kind == "recipe":
        model_record["kind"] = "flat_recipe"
        model_record["normalized_path"] = str(found_path)
        return model_record
    if found_kind == "cluster_seed":
        model_record["kind"] = "cluster_seed"
        model_record["normalized_path"] = str(found_path)
        return model_record

    if not isinstance(loaded, dict):
        model_record["kind"] = "invalid"
        model_record["notes"].append("model output root must be a JSON object")
        return model_record

    kind = loaded.get("kind")
    model_record["kind"] = kind if isinstance(kind, str) else "invalid"
    if kind == "attack_dsl":
        dsl = loaded.get("dsl")
        if not isinstance(dsl, dict):
            model_record["kind"] = "invalid"
            model_record["notes"].append("attack_dsl output must contain object field dsl")
            return model_record
        out_path = model_output_root / f"{base}_dsl.json"
        write_json(out_path, dsl)
        model_record["normalized_path"] = str(out_path)
    elif kind == "flat_recipe":
        recipe = loaded.get("recipe")
        if not isinstance(recipe, dict):
            model_record["kind"] = "invalid"
            model_record["notes"].append("flat_recipe output must contain object field recipe")
            return model_record
        out_path = model_output_root / f"{base}_recipe.json"
        write_json(out_path, recipe)
        model_record["normalized_path"] = str(out_path)
    elif kind == "cluster_seed":
        out_path = model_output_root / f"{base}_cluster_seed.json"
        write_json(out_path, loaded)
        model_record["normalized_path"] = str(out_path)
    elif kind == "needs_harness_extension":
        model_record["normalized_path"] = str(found_path)
    else:
        model_record["kind"] = "invalid"
        model_record["notes"].append("kind must be attack_dsl, flat_recipe, cluster_seed, or needs_harness_extension")
    return model_record


def execute_attack_dsl(
    request_id: str,
    dsl_path: Path,
    out_root: Path,
    runner: Path,
    jobs: int,
    timeout: float,
) -> dict[str, Any]:
    base = safe_id(request_id)
    checks_dir = out_root / "model_checks"
    compiled_dir = out_root / "compiled_model_recipes" / base
    run_dir = out_root / "model_runs" / base
    triage_dir = out_root / "model_triage" / base
    preview_dir = out_root / "model_previews" / base
    audit_dir = out_root / "model_geometry_audit" / base
    check_report = checks_dir / f"{base}_check.json"
    commands: list[dict[str, Any]] = []

    check_cmd = [
        sys.executable,
        "test_harness/tools/compile_attack_dsl.py",
        str(dsl_path),
        "--check",
        "--report",
        str(check_report),
    ]
    commands.append(run_command(f"{request_id}: check DSL", check_cmd))
    if not commands[-1]["ok"]:
        return {"kind": "attack_dsl", "ok": False, "commands": commands, "check_report": str(check_report)}

    compile_cmd = [
        sys.executable,
        "test_harness/tools/compile_attack_dsl.py",
        str(dsl_path),
        "--out",
        str(compiled_dir),
    ]
    commands.append(run_command(f"{request_id}: compile DSL", compile_cmd))
    if not commands[-1]["ok"]:
        return {"kind": "attack_dsl", "ok": False, "commands": commands, "compiled_dir": str(compiled_dir)}

    run_cmd = [
        sys.executable,
        "test_harness/tools/run_recipes.py",
        "--runner",
        str(runner),
        "--recipe",
        str(compiled_dir),
        "--out",
        str(run_dir),
        "--jobs",
        str(jobs),
        "--timeout",
        str(timeout),
        "--triage-out",
        str(triage_dir),
        "--triage-include-passed",
        "--preview-out",
        str(preview_dir),
        "--contact-sheet",
        str(preview_dir / "contact.png"),
        "--geometry-audit-out",
        str(audit_dir),
    ]
    commands.append(run_command(f"{request_id}: run DSL recipes", run_cmd, acceptable={0, 2}))
    return {
        "kind": "attack_dsl",
        "ok": all(command["ok"] for command in commands),
        "commands": commands,
        "check_report": str(check_report),
        "compiled_dir": str(compiled_dir),
        "run_dir": str(run_dir),
        "triage_report": str(triage_dir / "triage_report.md"),
        "contact_sheet": str(preview_dir / "contact.png"),
        "geometry_audit": str(audit_dir / "geometry_audit.md"),
    }


def execute_flat_recipe(
    request_id: str,
    recipe_path: Path,
    out_root: Path,
    runner: Path,
    jobs: int,
    timeout: float,
) -> dict[str, Any]:
    base = safe_id(request_id)
    run_dir = out_root / "model_runs" / base
    triage_dir = out_root / "model_triage" / base
    preview_dir = out_root / "model_previews" / base
    commands: list[dict[str, Any]] = []
    validate_cmd = [sys.executable, "test_harness/tools/validate_recipe.py", str(recipe_path)]
    commands.append(run_command(f"{request_id}: validate recipe", validate_cmd))
    if not commands[-1]["ok"]:
        return {"kind": "flat_recipe", "ok": False, "commands": commands}
    run_cmd = [
        sys.executable,
        "test_harness/tools/run_recipes.py",
        "--runner",
        str(runner),
        "--recipe",
        str(recipe_path),
        "--out",
        str(run_dir),
        "--jobs",
        str(jobs),
        "--timeout",
        str(timeout),
        "--triage-out",
        str(triage_dir),
        "--triage-include-passed",
        "--preview-out",
        str(preview_dir),
        "--contact-sheet",
        str(preview_dir / "contact.png"),
    ]
    commands.append(run_command(f"{request_id}: run recipe", run_cmd, acceptable={0, 2}))
    return {
        "kind": "flat_recipe",
        "ok": all(command["ok"] for command in commands),
        "commands": commands,
        "run_dir": str(run_dir),
        "triage_report": str(triage_dir / "triage_report.md"),
        "contact_sheet": str(preview_dir / "contact.png"),
    }


def execute_cluster_seed(
    request_id: str,
    seed_path: Path,
    out_root: Path,
    runner: Path,
    jobs: int,
    timeout: float,
) -> dict[str, Any]:
    base = safe_id(request_id)
    expanded_dsl = out_root / "expanded_cluster_dsl" / f"{base}.json"
    cmd = [
        sys.executable,
        "test_harness/tools/build_source_guided_cluster.py",
        str(seed_path),
        "--out",
        str(expanded_dsl),
    ]
    commands = [run_command(f"{request_id}: expand cluster seed", cmd)]
    if not commands[-1]["ok"]:
        return {"kind": "cluster_seed", "ok": False, "commands": commands, "expanded_dsl": str(expanded_dsl)}
    dsl_result = execute_attack_dsl(request_id, expanded_dsl, out_root, runner, jobs, timeout)
    dsl_result["kind"] = "cluster_seed"
    dsl_result["expanded_dsl"] = str(expanded_dsl)
    dsl_result["commands"] = commands + dsl_result.get("commands", [])
    dsl_result["ok"] = all(command["ok"] for command in dsl_result["commands"])
    return dsl_result


def execute_model_outputs(records: list[dict[str, Any]], args: argparse.Namespace, out_root: Path) -> None:
    runner = Path(args.runner)
    runner_available = runner.is_file()

    model_output_root = Path(args.model_output_root)
    for record in records:
        if record.get("errors"):
            continue
        model = normalize_model_output(record, model_output_root)
        record["model_output"] = model
        if model["kind"] == "missing":
            record["stage"] = "pending_model_output"
            continue
        if model["kind"] == "invalid":
            record["stage"] = "invalid_model_output"
            continue
        if model["kind"] == "needs_harness_extension":
            record["stage"] = "needs_harness_extension"
            continue
        if not args.execute:
            record["stage"] = "model_output_ready"
            continue
        if not runner_available:
            record["stage"] = "model_output_ready"
            record["execute_result"] = {
                "ok": False,
                "skipped": True,
                "reason": f"runner not found: {runner}",
            }
            continue

        normalized_path = Path(str(model["normalized_path"]))
        if model["kind"] == "attack_dsl":
            result = execute_attack_dsl(str(record["request_id"]), normalized_path, out_root, runner, args.jobs, args.timeout)
        elif model["kind"] == "flat_recipe":
            result = execute_flat_recipe(str(record["request_id"]), normalized_path, out_root, runner, args.jobs, args.timeout)
        elif model["kind"] == "cluster_seed":
            result = execute_cluster_seed(str(record["request_id"]), normalized_path, out_root, runner, args.jobs, args.timeout)
        else:
            result = {"ok": False, "skipped": True, "reason": f"unsupported model kind: {model['kind']}"}
        record["execute_result"] = result
        record["stage"] = "executed"


def run_api_smoke(args: argparse.Namespace, out_root: Path) -> dict[str, Any] | None:
    if not args.api_smoke:
        return None
    runner = Path(args.runner)
    if not runner.is_file():
        return {"ok": False, "skipped": True, "reason": f"runner not found: {runner}"}
    run_dir = out_root / "api_smoke_suite"
    triage_dir = out_root / "api_smoke_suite_triage"
    preview_dir = out_root / "api_smoke_suite_preview"
    cmd = [
        sys.executable,
        "test_harness/tools/run_recipes.py",
        "--runner",
        str(runner),
        "--recipe-list",
        "test_harness/suites/api_smoke_suite.txt",
        "--out",
        str(run_dir),
        "--jobs",
        "1",
        "--timeout",
        str(args.timeout),
        "--triage-out",
        str(triage_dir),
        "--triage-include-passed",
        "--preview-out",
        str(preview_dir),
        "--contact-sheet",
        str(preview_dir / "contact.png"),
    ]
    return run_command("api smoke suite", cmd, acceptable={0, 2})


def run_abc_sample(args: argparse.Namespace, manifest: dict[str, Any], out_root: Path) -> dict[str, Any] | None:
    if not args.abc_sample_smoke:
        return None
    runner = Path(args.runner)
    if not runner.is_file():
        return {"ok": False, "skipped": True, "reason": f"runner not found: {runner}"}
    abc_inputs = manifest.get("abc_inputs") if isinstance(manifest.get("abc_inputs"), dict) else {}
    fetch_root = Path(args.abc_fetch_root or str(abc_inputs.get("preferred_fetch_root") or "artifacts/abc_fetch_smoke"))
    cmd = [
        sys.executable,
        "test_harness/tools/run_abc_sample_smoke.py",
        "--fetch-root",
        str(fetch_root),
        "--runner",
        str(runner),
        "--out",
        str(out_root / "abc_sample_smoke"),
        "--jobs",
        str(args.jobs),
        "--timeout",
        str(args.timeout),
    ]
    return run_command("abc sample smoke", cmd, acceptable={0, 2})


def run_source_tasks(args: argparse.Namespace, out_root: Path) -> dict[str, Any] | None:
    if not args.source_root:
        return None
    scan_out = out_root / "source_scan"
    tasks_out = out_root / "source_attack_tasks"
    commands: list[dict[str, Any]] = []
    scan_cmd = [
        sys.executable,
        "test_harness/tools/scan_source_risks.py",
        *args.source_root,
        "--out",
        str(scan_out),
        "--max-findings",
        str(args.source_scan_max_findings),
        "--max-seeds",
        str(args.source_scan_max_seeds),
    ]
    commands.append(run_command("source risk scan", scan_cmd))
    if commands[-1]["ok"]:
        task_cmd = [
            sys.executable,
            "test_harness/tools/build_source_attack_tasks.py",
            str(scan_out),
            "--out",
            str(tasks_out),
            "--max-tasks",
            str(args.source_task_max_tasks),
            "--context-lines",
            str(args.source_task_context_lines),
            "--write-dsl-seeds",
        ]
        commands.append(run_command("source attack task build", task_cmd))
    return {
        "ok": all(command["ok"] for command in commands),
        "commands": commands,
        "source_scan": str(scan_out),
        "source_attack_tasks": str(tasks_out),
    }


def summarize_records(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        stage = str(record.get("stage") or "unknown")
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Interface Distillation Campaign",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Manifest: `{summary.get('manifest')}`",
        f"- Output root: `{summary.get('out_root')}`",
        f"- Execute enabled: `{summary.get('execute')}`",
        "",
        "## Stage Counts",
        "",
    ]
    for stage, count in sorted((summary.get("stage_counts") or {}).items()):
        lines.append(f"- `{stage}`: `{count}`")
    lines.extend(["", "## Forms", ""])
    for record in summary.get("records", []):
        model = record.get("model_output") or {}
        execute_result = record.get("execute_result") or {}
        lines.append(
            f"- `{record.get('request_id')}` stage=`{record.get('stage')}` api=`{record.get('target_api')}` "
            f"geometry=`{record.get('geometry_family')}` preferred=`{record.get('preferred_output')}` "
            f"model=`{model.get('kind', '')}` run_ok=`{execute_result.get('ok', '')}`"
        )
        if record.get("prompt_path"):
            lines.append(f"  prompt: `{record['prompt_path']}`")
        if model.get("input_path"):
            lines.append(f"  model_output: `{model.get('input_path')}`")
        if execute_result.get("triage_report"):
            lines.append(f"  triage: `{execute_result.get('triage_report')}`")
        if execute_result.get("contact_sheet"):
            lines.append(f"  preview: `{execute_result.get('contact_sheet')}`")
        if record.get("fixed_commands") and record.get("stage") == "pending_model_output":
            lines.append("  next fixed commands:")
            for command in record["fixed_commands"]:
                lines.append(f"  - `{command}`")
    lines.append("")
    if summary.get("api_smoke"):
        lines.extend(["## API Smoke", "", f"- `{summary['api_smoke']}`", ""])
    if summary.get("abc_sample_smoke"):
        lines.extend(["## ABC Sample Smoke", "", f"- `{summary['abc_sample_smoke']}`", ""])
    if summary.get("source_tasks"):
        lines.extend(["## Source Tasks", "", f"- `{summary['source_tasks']}`", ""])
    return "\n".join(lines)


def has_hard_failure(summary: dict[str, Any], require_outputs: bool, fail_on_failures: bool) -> bool:
    stage_counts = summary.get("stage_counts") or {}
    if stage_counts.get("form_error") or stage_counts.get("invalid_model_output"):
        return True
    if require_outputs and stage_counts.get("pending_model_output"):
        return True
    if not fail_on_failures:
        return False
    for key in ("api_smoke", "abc_sample_smoke", "source_tasks"):
        value = summary.get(key)
        if isinstance(value, dict) and value.get("ok") is False:
            return True
    for record in summary.get("records", []):
        result = record.get("execute_result")
        if isinstance(result, dict) and result.get("ok") is False and not result.get("skipped"):
            return True
    return False


def main() -> int:
    args = parse_args()
    forms_dir = Path(args.forms_dir)
    manifest_path = Path(args.manifest) if args.manifest else forms_dir / "00_manifest.json"
    out_root = Path(args.out)
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        print(f"manifest must be a JSON object: {manifest_path}", file=sys.stderr)
        return 1

    records = build_tasks(forms_dir, manifest, out_root)
    execute_model_outputs(records, args, out_root)
    api_smoke = run_api_smoke(args, out_root)
    abc_sample_smoke = run_abc_sample(args, manifest, out_root)
    source_tasks = run_source_tasks(args, out_root)

    summary = {
        "generated_at": now_iso_like(),
        "manifest": str(manifest_path),
        "forms_dir": str(forms_dir),
        "out_root": str(out_root),
        "model_output_root": args.model_output_root,
        "runner": args.runner,
        "execute": args.execute,
        "records": records,
        "stage_counts": summarize_records(records),
        "api_smoke": api_smoke,
        "abc_sample_smoke": abc_sample_smoke,
        "source_tasks": source_tasks,
    }
    summary_path = out_root / "interface_distillation_summary.json"
    report_path = out_root / "interface_distillation_report.md"
    write_json(summary_path, summary)
    write_text(report_path, markdown_report(summary))
    print(f"summary={summary_path}")
    print(f"report={report_path}")
    return 1 if has_hard_failure(summary, args.require_model_outputs, args.fail_on_failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
