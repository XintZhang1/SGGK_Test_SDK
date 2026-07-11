# Message API Form Workflow

The production path is fully automatic:

```text
API/C++ form
  -> provider-neutral prompt manifest
  -> parallel Message API candidate pool
  -> normalize + kind-specific fixed gate + SHA-256 de-duplication
  -> bounded diagnostic repair
  -> independent SDK execution or isolated adapter build
  -> deterministic selection + atomic promotion
  -> qualification + same-failure replay + reduction + TopoTrack probe
  -> candidate-only failure bundle/report
```

## Build And Run

```powershell
python .\test_harness\tools\build_model_prompt_pack.py `
  --out .\artifacts\model_prompt_pack `
  --max-prompt-chars 60000

python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --run-id api_form_batch `
  --execute `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --jobs 1 `
  --timeout 120 `
  .\artifacts\model_prompt_pack\model_task_manifest.json
```

Use `--profile siliconflow-test` only for an explicit external simulation. It
uses the same `choices[0].message.content` JSON contract and is never an
intranet fallback.

Provider endpoint, model, credentials, CA bundle, SDK, data, and source roots
come from environment configuration. Model output may not provide command,
runner, dataset, output, cwd, environment, executable, or shell fields.

## Acceptance Rules

- `attack_dsl` must pass `compile_attack_dsl.py --check` under model asset policy.
- `flat_recipe` must pass strict recipe and materialized-asset validation.
- `cluster_seed` is expanded by fixed code and then passes the DSL gate.
- `campaign_request` selects a registered profile and bounded typed arguments;
  fixed code binds local paths and resolves argv with `shell=False`.
- `api_plugin_candidate` is materialized through a registered fixed C++
  archetype, then must pass isolated Release build/schema/registry/three-replay
  semantic gates.
- `needs_harness_extension` is a non-executing report. It does not apply or
  propose a source patch and is used only when no existing adapter/plugin or
  registered archetype can cover the API.
- The low-level `run_authoring_gateway.py` stages transport-contract candidates
  for diagnostics only. Its provenance has `authoring_accepted=false`.

SDK/oracle failures never trigger model repair and never get relabeled as SDK
bugs. The pipeline keeps the accepted test, writes triage and replay evidence,
and emits artifact-local bug candidates marked as requiring classification.

## Post-promotion Evaluation

`run_interface_distillation.py` may evaluate outputs already promoted by the
Message API pipeline. It is not a model transport and cannot author, seed, or
accept a model output.

Report the pipeline summary, accepted output/provenance, fixed-gate report,
recipe summary, triage report, replay classification, and any failure bundles.
