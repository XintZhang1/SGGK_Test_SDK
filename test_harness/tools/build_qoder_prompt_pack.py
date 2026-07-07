#!/usr/bin/env python3
"""Build Qoder paste-in prompt packs with deterministic checkpoints.

Qoder is used as a UI-only model surface in the intranet flow. This tool keeps
the model away from long fragile chat history by generating self-contained
prompts plus a small checkpoint that can be pasted into a fresh Qoder session.
It does not call any model API.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any

from build_api_test_task import build_task, validate_form


DEFAULT_FORMS_DIR = "test_harness/forms/interface_distillation"
DEFAULT_MODEL_OUTPUT_ROOT = "artifacts/model_outputs"
DEFAULT_SOURCE_OUTPUT_ROOT = "artifacts/source_model_outputs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forms-dir", default=DEFAULT_FORMS_DIR)
    parser.add_argument("--manifest", default="", help="Defaults to <forms-dir>/00_manifest.json")
    parser.add_argument("--out", default="artifacts/qoder_prompt_pack")
    parser.add_argument("--model-output-root", default=DEFAULT_MODEL_OUTPUT_ROOT)
    parser.add_argument("--source-output-root", default=DEFAULT_SOURCE_OUTPUT_ROOT)
    parser.add_argument("--source-task-jsonl", default="", help="Optional source_attack_tasks.jsonl")
    parser.add_argument("--source-task-dir", default="", help="Directory containing source_attack_tasks.jsonl")
    parser.add_argument("--source-task-limit", type=int, default=0, help="0 means all source tasks")
    parser.add_argument("--max-prompt-chars", type=int, default=60000)
    parser.add_argument("--qoder-hard-token-limit", type=int, default=200000)
    parser.add_argument("--run-tag", default="", help="Human label stored in generated checkpoint")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def safe_id(value: str) -> str:
    result = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in value)
    return result.strip("._-") or "task"


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def rel(path: Path) -> str:
    try:
        return str(path)
    except OSError:
        return path.as_posix()


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

    return [
        {"form": path.name, "path": str(path)}
        for path in sorted(forms_dir.glob("*.json"))
        if path.name != "00_manifest.json"
    ]


def load_source_tasks(args: argparse.Namespace) -> list[dict[str, Any]]:
    path: Path | None = Path(args.source_task_jsonl) if args.source_task_jsonl else None
    if path is None and args.source_task_dir:
        path = Path(args.source_task_dir) / "source_attack_tasks.jsonl"
    if path is None or not path.is_file():
        return []
    tasks: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            loaded = json.loads(line)
            if isinstance(loaded, dict):
                tasks.append(loaded)
            if args.source_task_limit and len(tasks) >= args.source_task_limit:
                break
    return tasks


def qoder_rules() -> str:
    return """Qoder context rule:
- Do not rely on previous chat messages.
- Treat this prompt as the complete task context.
- If a required file/source line is missing, return needs_harness_extension or a short blocking note inside JSON instead of guessing.
- Return exactly one JSON object. No markdown wrapper, no explanation outside JSON.
- Keep output small enough to save directly into the requested output path.
"""


def output_contract() -> str:
    return """Allowed output kinds:
1. attack_dsl: {"kind":"attack_dsl","dsl":{...},"notes":[],"commands":[]}
2. flat_recipe: {"kind":"flat_recipe","recipe":{...},"notes":[],"commands":[]}
3. cluster_seed: {"kind":"cluster_seed", ...seed fields...}
4. needs_harness_extension: {"kind":"needs_harness_extension","api":"...","why_needed":"...","proposed_recipe_fields":{},"minimum_smoke_case":{}}

Rules:
- Do not write direct SDK code.
- Prefer attack_dsl for api_boolean and source-guided geometry attacks.
- Use flat_recipe for step_import, iges_import, step_roundtrip, iges_roundtrip, and check_sgt.
- Use cluster_seed when one source predicate should expand through build_source_guided_cluster.py.
- Always include a real oracle, not API success only.
"""


def compact_checkpoint(tasks: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": now_iso_like(),
        "run_tag": args.run_tag,
        "qoder_hard_token_limit": args.qoder_hard_token_limit,
        "safe_prompt_char_budget": args.max_prompt_chars,
        "model_output_root": args.model_output_root,
        "source_output_root": args.source_output_root,
        "constants": {
            "topo_tol": 0.01,
            "geom_tol": 0.00001,
            "max_model_size": 500000.0,
        },
        "fixed_loop": [
            "Paste qoder_resume_prompt.md into a fresh Qoder session when context is long or uncertain.",
            "Paste exactly one task prompt from prompts/interface or prompts/source.",
            "Save Qoder's JSON output to expected_output_path.",
            "Run run_interface_distillation.py or the task-specific fixed commands.",
            "Re-run build_qoder_prompt_pack.py to refresh qoder_session_checkpoint.json.",
        ],
        "tasks": tasks,
    }


def interface_prompt(task: dict[str, Any], form: dict[str, Any], expected_output: str, checkpoint: dict[str, Any]) -> str:
    task_context = {
        "task_type": "interface_form",
        "request_id": task.get("request_id"),
        "expected_output_path": expected_output,
        "preferred_output": (task.get("api_guidance") or {}).get("preferred_format"),
        "fixed_commands_after_save": task.get("fixed_commands", []),
        "checkpoint_constants": checkpoint["constants"],
    }
    return f"""# Qoder SGGK Interface Task

{qoder_rules()}

## Compact Checkpoint

```json
{json.dumps(task_context, indent=2, ensure_ascii=False)}
```

## Output Contract

{output_contract()}

## Developer Form

```json
{json.dumps(form, indent=2, ensure_ascii=False)}
```

## Deterministic Harness Prompt

```text
{task.get("prompt", "")}
```
"""


def source_prompt(task: dict[str, Any], expected_output: str, checkpoint: dict[str, Any]) -> str:
    finding = task.get("finding") if isinstance(task.get("finding"), dict) else {}
    source_excerpt = task.get("source_excerpt") if isinstance(task.get("source_excerpt"), dict) else {}
    seed = task.get("seed_draft")
    prompt_context = {
        "task_type": "source_attack",
        "task_id": task.get("task_id"),
        "source_ref": task.get("source_ref"),
        "expected_output_path": expected_output,
        "review_required": True,
        "checkpoint_constants": checkpoint["constants"],
        "post_generation_checks": task.get("post_generation_checks", []),
    }
    seed_text = json.dumps(seed, indent=2, ensure_ascii=False) if isinstance(seed, dict) else "null"
    return f"""# Qoder SGGK Source-Attack Task

{qoder_rules()}

## Compact Checkpoint

```json
{json.dumps(prompt_context, indent=2, ensure_ascii=False)}
```

## Output Contract

{output_contract()}

## Model Prompt

```text
{task.get("model_prompt", "")}
```

## Source Finding

```json
{json.dumps(finding, indent=2, ensure_ascii=False)}
```

## Source Excerpt

Path: `{source_excerpt.get("path", "")}`
Lines: `{source_excerpt.get("start_line", 0)}-{source_excerpt.get("end_line", 0)}`

```text
{source_excerpt.get("text", "")}
```

## Optional Scanner Seed Draft

Use this only as a review-required starting point. You may emit cluster_seed,
attack_dsl, or needs_harness_extension if the seed does not fit the source.

```json
{seed_text}
```
"""


def budget_record(prompt: str, max_chars: int) -> dict[str, Any]:
    chars = len(prompt)
    return {
        "chars": chars,
        "estimated_tokens": estimate_tokens(prompt),
        "over_safe_budget": chars > max_chars,
    }


def build_interface_prompts(args: argparse.Namespace, out_root: Path, checkpoint_tasks: list[dict[str, Any]]) -> None:
    forms_dir = Path(args.forms_dir)
    manifest_path = Path(args.manifest) if args.manifest else forms_dir / "00_manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"manifest must be an object: {manifest_path}")

    checkpoint_shell = {"constants": {"topo_tol": 0.01, "geom_tol": 0.00001, "max_model_size": 500000.0}}
    for order, entry in enumerate(manifest_forms(forms_dir, manifest), start=1):
        form_path = Path(str(entry["path"]))
        form = read_json(form_path)
        if not isinstance(form, dict):
            continue
        errors, warnings = validate_form(form)
        request_id = str(form.get("request_id") or form_path.stem)
        expected_output = str(Path(args.model_output_root) / f"{safe_id(request_id)}.json")
        prompt_path = out_root / "prompts" / "interface" / f"{order:02d}_{safe_id(request_id)}.md"
        task = build_task(form_path.resolve(), form, warnings)
        prompt = interface_prompt(task, form, expected_output, checkpoint_shell)
        write_text(prompt_path, prompt)
        checkpoint_tasks.append(
            {
                "task_type": "interface_form",
                "task_id": request_id,
                "form": str(form_path),
                "prompt_path": str(prompt_path),
                "expected_output_path": expected_output,
                "target_api": form.get("target_api"),
                "geometry_family": (form.get("geometry") or {}).get("family"),
                "preferred_output": (task.get("api_guidance") or {}).get("preferred_format"),
                "form_errors": errors,
                "form_warnings": warnings,
                "output_exists": Path(expected_output).is_file(),
                **budget_record(prompt, args.max_prompt_chars),
            }
        )


def build_source_prompts(args: argparse.Namespace, out_root: Path, checkpoint_tasks: list[dict[str, Any]]) -> None:
    source_tasks = load_source_tasks(args)
    checkpoint_shell = {"constants": {"topo_tol": 0.01, "geom_tol": 0.00001, "max_model_size": 500000.0}}
    for index, task in enumerate(source_tasks, start=1):
        task_id = str(task.get("task_id") or f"source_task_{index:04d}")
        expected_output = str(Path(args.source_output_root) / f"{safe_id(task_id)}.json")
        prompt_path = out_root / "prompts" / "source" / f"{index:04d}_{safe_id(task_id)}.md"
        prompt = source_prompt(task, expected_output, checkpoint_shell)
        write_text(prompt_path, prompt)
        finding = task.get("finding") if isinstance(task.get("finding"), dict) else {}
        checkpoint_tasks.append(
            {
                "task_type": "source_attack",
                "task_id": task_id,
                "source_ref": task.get("source_ref"),
                "prompt_path": str(prompt_path),
                "expected_output_path": expected_output,
                "severity": finding.get("severity"),
                "risk_family": finding.get("suggested_attack_family"),
                "output_exists": Path(expected_output).is_file(),
                **budget_record(prompt, args.max_prompt_chars),
            }
        )


def markdown_index(checkpoint: dict[str, Any]) -> str:
    tasks = checkpoint.get("tasks") if isinstance(checkpoint.get("tasks"), list) else []
    lines = [
        "# Qoder Prompt Pack",
        "",
        f"- Generated: `{checkpoint.get('generated_at')}`",
        f"- Run tag: `{checkpoint.get('run_tag')}`",
        f"- Safe prompt char budget: `{checkpoint.get('safe_prompt_char_budget')}`",
        f"- Qoder hard token limit: `{checkpoint.get('qoder_hard_token_limit')}`",
        "",
        "## How To Use",
        "",
        "1. Start a fresh Qoder session before each task or whenever context feels stale.",
        "2. Paste `qoder_resume_prompt.md`.",
        "3. Paste exactly one task prompt listed below.",
        "4. Save Qoder's JSON response to `expected_output_path`.",
        "5. Run the fixed harness commands from the prompt or `run_interface_distillation.py`.",
        "6. Re-run this pack builder to refresh `qoder_session_checkpoint.json`.",
        "",
        "Do not paste multiple source files, all reports, or old chat history into one Qoder session.",
        "",
        "## Tasks",
        "",
    ]
    for task in tasks:
        budget = "OVER_BUDGET" if task.get("over_safe_budget") else "ok"
        lines.append(
            f"- `{task.get('task_id')}` type=`{task.get('task_type')}` budget=`{budget}` "
            f"tokens~`{task.get('estimated_tokens')}` prompt=`{task.get('prompt_path')}` "
            f"output=`{task.get('expected_output_path')}`"
        )
    lines.append("")
    return "\n".join(lines)


def resume_prompt(checkpoint: dict[str, Any]) -> str:
    compact = dict(checkpoint)
    tasks = checkpoint.get("tasks") if isinstance(checkpoint.get("tasks"), list) else []
    compact["tasks"] = [
        {
            "task_id": task.get("task_id"),
            "task_type": task.get("task_type"),
            "prompt_path": task.get("prompt_path"),
            "expected_output_path": task.get("expected_output_path"),
            "output_exists": task.get("output_exists"),
        }
        for task in tasks
    ]
    return f"""# Qoder Resume Prompt

You are working inside the SGGK test harness. Qoder's automatic context
compression is unsafe for this workflow, so ignore old chat history and use this
checkpoint plus one task prompt only.

Return JSON only for task prompts. Do not call external model APIs. Do not write
direct SDK code. Use the fixed harness DSL/recipe contracts.

```json
{json.dumps(compact, indent=2, ensure_ascii=False)}
```
"""


def main() -> int:
    args = parse_args()
    if args.max_prompt_chars <= 0:
        print("--max-prompt-chars must be positive")
        return 1
    if args.source_task_limit < 0:
        print("--source-task-limit must be >= 0")
        return 1

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    checkpoint_tasks: list[dict[str, Any]] = []
    build_interface_prompts(args, out_root, checkpoint_tasks)
    build_source_prompts(args, out_root, checkpoint_tasks)
    checkpoint = compact_checkpoint(checkpoint_tasks, args)
    write_json(out_root / "qoder_session_checkpoint.json", checkpoint)
    write_text(out_root / "qoder_session_index.md", markdown_index(checkpoint))
    write_text(out_root / "qoder_resume_prompt.md", resume_prompt(checkpoint))
    summary = {
        "out": str(out_root),
        "tasks": len(checkpoint_tasks),
        "over_budget": sum(1 for task in checkpoint_tasks if task.get("over_safe_budget")),
        "index": str(out_root / "qoder_session_index.md"),
        "checkpoint": str(out_root / "qoder_session_checkpoint.json"),
        "resume_prompt": str(out_root / "qoder_resume_prompt.md"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
