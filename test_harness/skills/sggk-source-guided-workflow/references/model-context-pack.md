# Model Prompt Pack Workflow

Use a deterministic prompt pack for Message API authoring. The pack is
provider-neutral: it contains bounded prompts and a machine-readable task
manifest, but no endpoint, model name, credential, retry policy, or transport
implementation.

## Build The Pack

```powershell
python .\test_harness\tools\build_model_prompt_pack.py `
  --out .\artifacts\model_prompt_pack `
  --model-output-root .\artifacts\model_outputs `
  --source-task-dir .\artifacts\source_attack_tasks `
  --source-task-limit 80 `
  --max-prompt-chars 60000 `
  --run-tag abc40_v2
```

The builder writes:

- `model_task_manifest.json`: gateway input with `schema_version` and `tasks`
- `model_task_index.md`: compact task and budget summary
- `prompts/interface/*.md`: one self-contained interface prompt per form
- `prompts/source/*.md`: one self-contained source-risk prompt when source
  tasks are supplied

Each task manifest record contains at least:

- `task_id`
- `prompt_path`
- `expected_output_path`
- `output_contract.allowed_kinds`

All prompt and output paths are repository-relative. Generated packs and model
outputs stay under ignored `artifacts/` paths.

## Pipeline Contract

The Message API pipeline reads exactly one manifest task at a time, loads its
`prompt_path`, calls the configured model, validates the returned JSON against
`output_contract`, runs the kind-specific fixed gate, and performs bounded
diagnostic repair. It atomically writes an authoring-accepted output and
provenance sidecar only after the fixed gate passes. Provider configuration and
credentials do not belong in the prompt pack.

Run the same production path for authoring-only acceptance or add SDK execution:

```powershell
python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --run-id model_context_batch `
  .\artifacts\model_prompt_pack\model_task_manifest.json
```

When the local runner is available, add `--runner ... --execute`. The pipeline
passes source-task output through `compile_attack_dsl.py --check` or strict
`validate_recipe.py` before SDK execution. `run_authoring_gateway.py` is only a
low-level transport/candidate diagnostic and cannot create accepted output.

## Context Policy

- Each prompt contains exactly one task and its output contract.
- Include only the form, relevant schema/skill excerpt, selected example pack,
  and directly cited source excerpt needed for that task.
- Keep raw corpus files, SGT/STEP payloads, full reports, and large triage logs
  outside prompts; use stable paths, hashes, and compact evidence summaries.
- If a prompt exceeds the configured budget, split the task or reduce source
  context deterministically.
- Never rely on prior model conversation state.

## Failure Rules

- Non-JSON or disallowed `kind` output is rejected before acceptance.
- Invented SDK calls must become `needs_harness_extension`.
- Missing real-result oracles fail review.
- Large campaigns must use a typed `campaign_request`, not a command string or generated case enumeration.
- Repair attempts are new gateway tasks linked to their parent output and
  diagnostic context through provenance.
