#!/usr/bin/env python3
"""Build deterministic SGGK model prompts and a gateway task manifest.

The builder is provider-neutral and performs no model call.  It writes one
self-contained prompt per task plus ``model_task_manifest.json``.  A Message
API gateway can consume the manifest, validate each output contract, and write
the formal output and provenance sidecar atomically.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from build_api_test_task import build_task, validate_form

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FORMS_DIR = "test_harness/forms/interface_distillation"
DEFAULT_MODEL_OUTPUT_ROOT = "artifacts/model_outputs"
DEFAULT_SOURCE_OUTPUT_ROOT = "artifacts/source_model_outputs"
ALLOWED_OUTPUT_KINDS = {
    "attack_dsl",
    "flat_recipe",
    "cluster_seed",
    "needs_harness_extension",
    "campaign_request",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forms-dir", default=DEFAULT_FORMS_DIR)
    parser.add_argument("--manifest", default="", help="Defaults to <forms-dir>/00_manifest.json")
    parser.add_argument("--out", default="artifacts/model_prompt_pack")
    parser.add_argument("--model-output-root", default=DEFAULT_MODEL_OUTPUT_ROOT)
    parser.add_argument("--source-output-root", default=DEFAULT_SOURCE_OUTPUT_ROOT)
    parser.add_argument("--source-task-jsonl", default="", help="Optional source_attack_tasks.jsonl")
    parser.add_argument("--source-task-dir", default="", help="Directory containing source_attack_tasks.jsonl")
    parser.add_argument("--source-task-limit", type=int, default=0, help="0 means all source tasks")
    parser.add_argument("--max-prompt-chars", type=int, default=60000)
    parser.add_argument("--max-context-tokens", type=int, default=200000)
    parser.add_argument("--run-tag", default="", help="Stable run label stored in the task manifest")
    return parser.parse_args()


def now_iso_like() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"gateway paths must stay inside the repository: {path}") from exc


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def safe_id(value: str) -> str:
    result = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in value)
    return result.strip("._-") or "task"


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


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
            item["path"] = forms_dir / form_name
            result.append(item)
        return sorted(result, key=lambda item: int(item.get("order", 999999)))
    return [
        {"form": path.name, "path": path}
        for path in sorted(forms_dir.glob("*.json"))
        if path.name != "00_manifest.json"
    ]


def load_source_tasks(args: argparse.Namespace) -> list[dict[str, Any]]:
    path: Path | None = repo_path(args.source_task_jsonl) if args.source_task_jsonl else None
    if path is None and args.source_task_dir:
        path = repo_path(args.source_task_dir) / "source_attack_tasks.jsonl"
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


def model_rules() -> str:
    return """Model output rules:
- Treat this prompt as the complete task context.
- If required source evidence is missing, return needs_harness_extension instead of guessing.
- Return exactly one JSON object with no markdown wrapper or surrounding prose.
- Do not emit credentials, provider configuration, or direct SDK source patches.
- Do not emit command, commands, tool, runner, dataset, or output-path fields for campaigns.
- Keep the response bounded so the gateway can validate and stage it atomically.
"""


def output_contract_text() -> str:
    return """Allowed output kinds:
1. attack_dsl: {"kind":"attack_dsl","dsl":{...},"notes":[]}
2. flat_recipe: {"kind":"flat_recipe","recipe":{...},"notes":[]}
3. cluster_seed: {"kind":"cluster_seed", ...seed fields...}
4. needs_harness_extension:
   {"kind":"needs_harness_extension","api":"...","why_needed":"...","extension_summary":"...",
    "proposed_recipe_fields":{},"proposed_artifacts":[],"validation_oracle":{},
    "minimum_smoke_case":{},"patch_plan":[]}
5. campaign_request: {"kind":"campaign_request","profile_id":"...","args":{...},"notes":[],"expected_artifacts":[]}

Contract policy:
- Prefer attack_dsl for api_boolean and source-guided geometry attacks.
- Use flat_recipe for STEP/IGES import or roundtrip and check_sgt.
- Use cluster_seed only when fixed code should expand one source predicate.
- Use campaign_request for corpus campaigns. Choose only an allowed profile_id and its typed bounded args.
- Include a real result oracle; API success alone is insufficient.
"""


def contract_for(preferred: Any, *, source_task: bool = False) -> dict[str, Any]:
    if source_task:
        allowed = ["attack_dsl", "cluster_seed", "needs_harness_extension"]
    else:
        kind = str(preferred or "").strip()
        allowed = [kind] if kind in ALLOWED_OUTPUT_KINDS else sorted(ALLOWED_OUTPUT_KINDS)
        if "needs_harness_extension" not in allowed:
            allowed.append("needs_harness_extension")
    return {
        "type": "json_object",
        "kind_field": "kind",
        "allowed_kinds": allowed,
    }


def constants() -> dict[str, float]:
    return {"topo_tol": 0.01, "geom_tol": 0.00001, "max_model_size": 500000.0}


def campaign_profiles_for(task: dict[str, Any]) -> dict[str, Any]:
    direct = task.get("allowed_campaign_profiles")
    if isinstance(direct, dict):
        return direct
    harness_contract = task.get("harness_contract")
    if isinstance(harness_contract, dict) and isinstance(harness_contract.get("allowed_campaign_profiles"), dict):
        return dict(harness_contract["allowed_campaign_profiles"])
    return {}


def interface_prompt(task: dict[str, Any], form: dict[str, Any], expected_output: str) -> str:
    preferred = (task.get("api_guidance") or {}).get("preferred_format")
    task_context = {
        "task_type": "interface_form",
        "request_id": task.get("request_id"),
        "expected_output_path": expected_output,
        "output_contract": contract_for(preferred),
        "selected_example_pack": (task.get("harness_contract") or {}).get("selected_example_pack", ""),
        "interface_family": (task.get("harness_contract") or {}).get("interface_family", ""),
        "run_profile": (task.get("harness_contract") or {}).get("run_profile", {}),
        "allowed_campaign_profiles": campaign_profiles_for(task),
        "constants": constants(),
    }
    return f"""# SGGK Model Interface Task

{model_rules()}

## Task Metadata

```json
{json.dumps(task_context, indent=2, ensure_ascii=False)}
```

## Output Contract

{output_contract_text()}

## Developer Form

```json
{json.dumps(form, indent=2, ensure_ascii=False)}
```

## Deterministic Harness Prompt

```text
{task.get("prompt", "")}
```
"""


def source_prompt(task: dict[str, Any], expected_output: str) -> str:
    finding = task.get("finding") if isinstance(task.get("finding"), dict) else {}
    source_excerpt = task.get("source_excerpt") if isinstance(task.get("source_excerpt"), dict) else {}
    seed = task.get("seed_draft")
    task_context = {
        "task_type": "source_attack",
        "task_id": task.get("task_id"),
        "source_ref": task.get("source_ref"),
        "expected_output_path": expected_output,
        "output_contract": contract_for(None, source_task=True),
        "review_required": True,
        "constants": constants(),
        "post_generation_checks": task.get("post_generation_checks", []),
    }
    seed_text = json.dumps(seed, indent=2, ensure_ascii=False) if isinstance(seed, dict) else "null"
    return f"""# SGGK Model Source-Attack Task

{model_rules()}

## Task Metadata

```json
{json.dumps(task_context, indent=2, ensure_ascii=False)}
```

## Output Contract

{output_contract_text()}

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

Use this only as a review-required starting point.  Emit cluster_seed,
attack_dsl, or needs_harness_extension when the seed does not fit the source.

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


def build_interface_prompts(args: argparse.Namespace, out_root: Path, tasks: list[dict[str, Any]]) -> None:
    forms_dir = repo_path(args.forms_dir)
    manifest_path = repo_path(args.manifest) if args.manifest else forms_dir / "00_manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"manifest must be an object: {manifest_path}")

    for order, entry in enumerate(manifest_forms(forms_dir, manifest), start=1):
        form_path = Path(entry["path"])
        form = read_json(form_path)
        if not isinstance(form, dict):
            continue
        errors, warnings = validate_form(form)
        request_id = str(form.get("request_id") or form_path.stem)
        expected_path = repo_path(args.model_output_root) / f"{safe_id(request_id)}.json"
        prompt_path = out_root / "prompts" / "interface" / f"{order:02d}_{safe_id(request_id)}.md"
        task = build_task(form_path, form, warnings)
        preferred = (task.get("api_guidance") or {}).get("preferred_format")
        prompt = interface_prompt(task, form, repo_relative(expected_path))
        example_pack = task.get("example_pack") if isinstance(task.get("example_pack"), dict) else {}
        write_text(prompt_path, prompt)
        tasks.append(
            {
                "task_type": "interface_form",
                "task_id": request_id,
                "request_id": request_id,
                "form_path": repo_relative(form_path),
                "prompt_path": repo_relative(prompt_path),
                "expected_output_path": repo_relative(expected_path),
                "output_contract": contract_for(preferred),
                "target_api": form.get("target_api"),
                "geometry_family": (form.get("geometry") or {}).get("family"),
                "selected_example_pack": (task.get("harness_contract") or {}).get("selected_example_pack", ""),
                "example_pack_manifest_path": example_pack.get("manifest_path", ""),
                "interface_family": task.get("interface_family", ""),
                "run_profile_id": task.get("run_profile_id", ""),
                "allowed_campaign_profiles": campaign_profiles_for(task),
                "form_errors": errors,
                "form_warnings": warnings,
                "output_exists": expected_path.is_file(),
                "status": "completed" if expected_path.is_file() else "ready",
                **budget_record(prompt, args.max_prompt_chars),
            }
        )


def build_source_prompts(args: argparse.Namespace, out_root: Path, tasks: list[dict[str, Any]]) -> None:
    for index, task in enumerate(load_source_tasks(args), start=1):
        task_id = str(task.get("task_id") or f"source_task_{index:04d}")
        expected_path = repo_path(args.source_output_root) / f"{safe_id(task_id)}.json"
        prompt_path = out_root / "prompts" / "source" / f"{index:04d}_{safe_id(task_id)}.md"
        prompt = source_prompt(task, repo_relative(expected_path))
        write_text(prompt_path, prompt)
        finding = task.get("finding") if isinstance(task.get("finding"), dict) else {}
        tasks.append(
            {
                "task_type": "source_attack",
                "task_id": task_id,
                "prompt_path": repo_relative(prompt_path),
                "expected_output_path": repo_relative(expected_path),
                "output_contract": contract_for(None, source_task=True),
                "source_ref": task.get("source_ref"),
                "severity": finding.get("severity"),
                "risk_family": finding.get("suggested_attack_family"),
                "output_exists": expected_path.is_file(),
                "status": "completed" if expected_path.is_file() else "ready",
                **budget_record(prompt, args.max_prompt_chars),
            }
        )


def markdown_index(manifest: dict[str, Any]) -> str:
    lines = [
        "# Model Prompt Pack",
        "",
        f"- Generated: `{manifest.get('generated_at')}`",
        f"- Run tag: `{manifest.get('run_tag')}`",
        f"- Tasks: `{manifest.get('task_count')}`",
        f"- Safe prompt char budget: `{manifest.get('safe_prompt_char_budget')}`",
        "",
        "The Message API gateway consumes `model_task_manifest.json`, reads one",
        "`prompt_path` per task, validates `output_contract`, and stages the",
        "formal JSON plus provenance at `expected_output_path`.",
        "",
        "## Tasks",
        "",
    ]
    for task in manifest.get("tasks", []):
        budget = "OVER_BUDGET" if task.get("over_safe_budget") else "ok"
        lines.append(
            f"- `{task.get('task_id')}` type=`{task.get('task_type')}` status=`{task.get('status')}` "
            f"budget=`{budget}` prompt=`{task.get('prompt_path')}` output=`{task.get('expected_output_path')}`"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.max_prompt_chars <= 0:
        print("--max-prompt-chars must be positive")
        return 1
    if args.max_context_tokens <= 0:
        print("--max-context-tokens must be positive")
        return 1
    if args.source_task_limit < 0:
        print("--source-task-limit must be >= 0")
        return 1

    out_root = repo_path(args.out)
    repo_relative(out_root)
    tasks: list[dict[str, Any]] = []
    build_interface_prompts(args, out_root, tasks)
    build_source_prompts(args, out_root, tasks)
    manifest = {
        "schema_version": 1,
        "generated_at": now_iso_like(),
        "run_tag": args.run_tag,
        "task_count": len(tasks),
        "safe_prompt_char_budget": args.max_prompt_chars,
        "max_context_tokens": args.max_context_tokens,
        "staging": {
            "model_output_root": repo_relative(repo_path(args.model_output_root)),
            "source_output_root": repo_relative(repo_path(args.source_output_root)),
            "write_policy": "gateway_validates_contract_then_atomic_write_with_provenance",
        },
        "constants": constants(),
        "tasks": tasks,
    }
    manifest_path = out_root / "model_task_manifest.json"
    index_path = out_root / "model_task_index.md"
    write_json(manifest_path, manifest)
    write_text(index_path, markdown_index(manifest))
    print(
        json.dumps(
            {
                "out": repo_relative(out_root),
                "tasks": len(tasks),
                "over_budget": sum(1 for task in tasks if task.get("over_safe_budget")),
                "manifest": repo_relative(manifest_path),
                "index": repo_relative(index_path),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
