# Windows Qoder SiliconFlow Handoff

Date: 2026-07-07

This handoff captures the new plan: reproduce the intranet Qoder + Qwen model
environment on the Windows SDK machine before moving the workflow into the real
intranet.

## Goal

Build a local simulation of the intranet workflow on Windows:

```text
Qoder prompt/form -> SiliconFlow Qwen model -> fixed harness code -> SDK run -> triage/report -> regression asset
```

The intranet target model is described as `qwen3.6-35b-a3b`. On SiliconFlow,
verify the exact public model id before wiring code. The official chat API docs
show an OpenAI-compatible endpoint and list Qwen reasoning-model names such as
`Qwen/Qwen3.5-35B-A3B`; use the actual model id returned by SiliconFlow model
listing or a successful dry-run request.

Official API doc:

```text
https://api-docs.siliconflow.cn/docs/api/chat-completions-post
```

## Safety Rules

- Never commit API keys, Qoder config secrets, Windows user config, SDK files,
  build outputs, or `artifacts\`.
- Store the SiliconFlow key in a Windows user environment variable or a local
  ignored secret file only.
- Keep prompt packs and run outputs under `artifacts\`.
- Commit only reusable harness code, docs, forms, skills, and reviewed reports.

## Windows Environment Variables

Set these on the Windows machine:

```powershell
[Environment]::SetEnvironmentVariable("SILICONFLOW_API_KEY", "<paste-key-here>", "User")
[Environment]::SetEnvironmentVariable("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1", "User")
[Environment]::SetEnvironmentVariable("QODER_SIM_MODEL", "<exact-siliconflow-qwen-model-id>", "User")
```

Open a new terminal after setting user variables, or set the same variables in
the current PowerShell session:

```powershell
$env:SILICONFLOW_API_KEY = "<paste-key-here>"
$env:SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
$env:QODER_SIM_MODEL = "<exact-siliconflow-qwen-model-id>"
```

## Minimal API Probe

Use this probe before touching the harness:

```powershell
$headers = @{
  "Authorization" = "Bearer $env:SILICONFLOW_API_KEY"
  "Content-Type" = "application/json"
}

$body = @{
  model = $env:QODER_SIM_MODEL
  messages = @(
    @{ role = "system"; content = "You are a JSON-only test harness assistant." },
    @{ role = "user"; content = "Return exactly {`"ok`":true,`"model`":`"probe`"} as JSON." }
  )
  temperature = 0
  max_tokens = 128
  response_format = @{ type = "json_object" }
} | ConvertTo-Json -Depth 20

Invoke-RestMethod `
  -Uri "$env:SILICONFLOW_BASE_URL/chat/completions" `
  -Method Post `
  -Headers $headers `
  -Body $body
```

Expected: a chat completion with JSON content in
`choices[0].message.content`.

## Simulation Workflow

1. Use `test_harness\ui\qoder_prompt_workbench\index.html` or
   `test_harness\tools\build_qoder_prompt_pack.py` to create a compact Qoder
   prompt pack from a form.
2. Send the compact prompt pack to SiliconFlow using the Qwen model id.
3. Require JSON-only model output in one of the existing contracts:
   - `attack_dsl`
   - `flat_recipe`
   - `cluster_seed`
   - `needs_harness_extension`
4. Feed the model output into fixed harness tools:
   - `build_source_guided_cluster.py` for `cluster_seed`
   - `compile_attack_dsl.py --check` before compiling DSL
   - `validate_recipe.py` before running flat recipes
   - `run_recipes.py` or `run_corpus.py` for SDK execution
   - `triage_artifacts.py` and `manage_regression_assets.py snapshot` after
     each run
5. Compare the model output against the known reports:
   - `test_harness\reports\interface_generated_ops_abc_recut_20260707_report.md`
   - `test_harness\reports\abc_boolean_mass_recut_100k_shard0000_report.md`
   - `test_harness\reports\interface_distillation_abc40_v2_report.md`

## Context-Compression Strategy

Qoder's built-in long-context compression is unsafe for this project. Simulate
the intranet constraint by never giving Qoder/SiliconFlow a raw 200k-token repo
context. Use a deterministic context pack instead:

- Always include only one current task form.
- Include the relevant skill excerpt, not every skill.
- Include the relevant runner contract and schema excerpts.
- Include at most one previous report section as an example.
- Include source excerpts or OCC surrogate notes only for the active interface.
- Keep generated artifacts, raw SGT/STEP paths, and huge triage logs out of the
  model prompt; pass only summarized fingerprints/counts.
- Ask the model for JSON only, then let fixed code expand, validate, and run.

Recommended initial budgets:

- prompt pack target: below 80k tokens
- absolute maximum before manual summarization: 150k tokens
- reserve at least 10k tokens for model output and API/system overhead

## First Windows Codex Tasks

1. Configure the three environment variables without committing secrets.
2. Run the minimal API probe.
3. Add a small local-only SiliconFlow caller under `artifacts\qoder_sim\` or an
   ignored local script if needed.
4. Pick one form, preferably:
   `test_harness\forms\interface_distillation\04_sweep_circle_line_boolean.json`
5. Build a Qoder prompt pack and send it to the SiliconFlow model.
6. Verify that the model returns one of the accepted JSON contracts.
7. Run fixed harness validation/compile gates on the model output.
8. Execute a tiny SDK run and generate triage.
9. Write a short comparison: did simulated Qwen rediscover the expected
   sweep/extrude PCurve risk from
   `interface_generated_ops_abc_recut_20260707_report.md`?

## Notes For Porting Back To Intranet

- If the public SiliconFlow model behaves differently from the intranet model,
  keep the same fixed code and only swap model endpoint/model id.
- The value of this simulation is not exact answer matching; it is validating
  prompt shape, context packing, JSON contracts, failure recovery, and report
  generation before the workflow enters the intranet.
- Any stable prompt pack or caller code that is safe and generic can later be
  committed. Any API keys, raw model logs with sensitive source, and local run
  artifacts must remain untracked.
