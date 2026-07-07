# Qoder Context Pack Workflow

Use this when the intranet model is only accessible through Qoder prompts. Do not rely on Qoder's chat history or automatic context compression. Once a Qoder session grows large or feels confused, start a fresh session and paste a deterministic checkpoint plus one task prompt.

## Fixed-Code Builder

Generate the prompt pack from the harness:

```powershell
python .\test_harness\tools\build_qoder_prompt_pack.py `
  --out .\artifacts\qoder_prompt_pack `
  --model-output-root .\artifacts\model_outputs `
  --source-task-dir .\artifacts\interface_distillation_windows_full_40chunk_v2\source_attack_tasks `
  --source-task-limit 80 `
  --max-prompt-chars 60000 `
  --run-tag abc40_v2
```

If source attack tasks are not ready yet, omit `--source-task-dir`; the pack will still include the interface forms.

The builder writes:

- `qoder_session_index.md`: task list and paste order
- `qoder_session_checkpoint.json`: compact machine-readable state
- `qoder_resume_prompt.md`: paste this into a fresh Qoder chat before one task prompt
- `prompts/interface/*.md`: one self-contained prompt per developer/interface form
- `prompts/source/*.md`: one self-contained prompt per source-risk task, when source tasks are provided

Generated files belong under `artifacts/` and should not be committed.

## Paste Protocol

For each task:

1. Start a fresh Qoder session.
2. Paste `qoder_resume_prompt.md`.
3. Paste exactly one task prompt from `prompts/interface/` or `prompts/source/`.
4. Save Qoder's JSON response to the prompt's `expected_output_path`.
5. Run the fixed harness checks from the prompt.
6. Re-run `build_qoder_prompt_pack.py` to refresh output-exists flags and checkpoint state.

Do not paste the entire source tree, all previous reports, old chat history, or many task prompts at once. The pack intentionally trades a little repetition for recoverability.

## Context Compression Policy

The harness owns compression, not Qoder.

- Each prompt contains only one task, the output contract, constants, required source/form context, and fixed commands.
- The checkpoint contains paths and status, not full artifacts.
- Source tasks include one cited source excerpt. If Qoder can see the intranet source tree, it should read the cited file/line itself before finalizing DSL.
- If a prompt exceeds the safe char budget, split the task or reduce source context with `build_source_attack_tasks.py --context-lines <smaller>`.
- If Qoder forgets instructions, discard the session. Do not ask it to summarize itself.

## Expected Outputs

Interface prompts save to:

```text
artifacts/model_outputs/<request_id>.json
```

Source prompts save to:

```text
artifacts/source_model_outputs/<source_task_id>.json
```

After saving interface outputs, run:

```powershell
python .\test_harness\tools\run_interface_distillation.py `
  --out .\artifacts\interface_distillation_qoder_run `
  --model-output-root .\artifacts\model_outputs `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --execute `
  --api-smoke `
  --abc-sample-smoke `
  --abc-fetch-root C:\Develop\SGGK_Agent\artifacts\abc_fetch_40chunk_sample50 `
  --source-root C:\Develop\SGGK_Agent\SGK1.4.10\SGGK\include `
  --jobs 1 `
  --timeout 180
```

Source outputs are review-required. Extract accepted `attack_dsl` or expanded `cluster_seed` outputs, then run `compile_attack_dsl.py --check`, compile, `run_recipes.py`, triage, preview, and geometry audit.

## Failure Mode Rules

- Qoder output with prose outside JSON is invalid; reprompt the same task in a fresh session.
- Qoder output that invents SDK calls is invalid; require `needs_harness_extension`.
- Qoder output that omits real oracles is review-failed.
- Qoder output that depends on previous chat is invalid. The prompt must be runnable from the checkpoint and one task file only.
