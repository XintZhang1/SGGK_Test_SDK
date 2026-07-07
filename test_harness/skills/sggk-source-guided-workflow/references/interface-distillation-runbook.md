# Interface Distillation Runbook

Use this runbook when the goal is to teach a smaller intranet model the full loop:

```text
developer/source form -> constrained model output -> generated tests -> Windows SDK run -> triage report
```

The model may use intranet source code during test-case authoring. It must still emit structured harness JSON, not direct SDK code.

## Campaign Driver

Prefer the integrated driver for repeatable distillation runs:

```powershell
python .\test_harness\tools\run_interface_distillation.py `
  --out .\artifacts\interface_distillation_windows_full_40chunk `
  --model-output-root .\artifacts\model_outputs_full_40chunk `
  --seed-example-model-outputs `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --execute `
  --api-smoke `
  --abc-sample-smoke `
  --abc-fetch-root C:\Develop\SGGK_Agent\artifacts\abc_fetch_40chunk_sample50 `
  --source-root C:\Develop\SGGK_Agent\SGK1.4.10\SGGK\include `
  --jobs 1 `
  --timeout 180
```

Use the wrapper for a Windows one-command replay:

```powershell
.\test_harness\scripts\run_interface_distillation_windows.ps1 `
  -SdkDir C:\Develop\SGGK_Agent\SGK1.4.10\SGGK `
  -AbcFetchRoot C:\Develop\SGGK_Agent\artifacts\abc_fetch_40chunk_sample50 `
  -SourceRoot C:\Develop\SGGK_Agent\SGK1.4.10\SGGK\include `
  -SeedExampleModelOutputs `
  -Jobs 1 `
  -Timeout 180
```

Omit `--seed-example-model-outputs` / `-SeedExampleModelOutputs` when `--model-output-root` already contains real intranet model responses.

When the intranet model is accessed through Qoder only, first build a paste-in prompt pack instead of trying to keep one long chat alive:

```powershell
python .\test_harness\tools\build_qoder_prompt_pack.py `
  --out .\artifacts\qoder_prompt_pack `
  --model-output-root .\artifacts\model_outputs `
  --source-task-dir .\artifacts\interface_distillation_windows_full_40chunk_v2\source_attack_tasks `
  --source-task-limit 80 `
  --max-prompt-chars 60000 `
  --run-tag abc40_v2
```

Paste `qoder_resume_prompt.md` plus exactly one task prompt into a fresh Qoder session. Save the JSON response to the prompt's `expected_output_path`, then run the fixed harness commands. Do not rely on Qoder's automatic context compression.

## Evidence To Inspect

Every run should produce:

- `interface_distillation_summary.json` and `interface_distillation_report.md`
- `model_tasks/` and `model_prompts/` for form-to-model input
- `model_checks/` and `compiled_model_recipes/` for DSL static gates
- `model_runs/<request_id>/recipe_summary.json`
- `model_triage/<request_id>/triage_report.md`
- `api_smoke_suite/recipe_summary.json`
- `abc_sample_smoke/abc_sample_smoke_summary.json`
- `abc_sample_smoke/top_complex_import_triage/triage_report.md`
- `abc_sample_smoke/top_complex_recut_triage/triage_report.md`
- `source_scan/source_risk_report.json`
- `source_attack_tasks/source_attack_task_manifest.md`

Treat command return code 0 as "workflow completed", not as "all test cases passed". `run_recipes.py` and `run_corpus.py` can return accepted nonzero status for discovered failures so triage, preview, and report generation can continue.

## Small-Model Review Rules

When reviewing generated model output:

- `cluster_seed` is not directly runnable. Expand it with `build_source_guided_cluster.py`, then check and compile the emitted DSL.
- `attack_dsl` must pass `compile_attack_dsl.py --check --report` before execution.
- `flat_recipe` must pass `validate_recipe.py` before execution.
- API success is not enough. Failed `validation.json`, TopoCheck, roundtrip comparison, point/face relation, clash, distance, plane-extreme, or empty-result oracle is a failure.
- ABC import templates with placeholder `source_file` values are rebound during seeding when `--abc-fetch-root` is provided. If the dataset lacks the requested format, keep the placeholder and write a note instead of inventing a path.
- Keep proprietary source excerpts and generated artifacts under `artifacts/` or intranet-only storage. Commit only forms, skills, reviewed DSL/templates, portable bug records, and human-written summary reports.

## ABC And Source-Guided Lanes

Run both targeted and broad lanes:

- form/model lane: exercises each known interface family from `forms/interface_distillation/00_manifest.json`
- API smoke lane: checks current stable runner surface
- ABC top-complex import lane: runs the highest-complexity STEP/IGES subset selected by `complex_dataset_index.json`
- ABC recut lane: imports corpus bodies, saves SGT results, then attacks them with exact/gap/overlap generated cutters
- source scan lane: scans SGGK headers/source, writes risk findings, and builds source-attack task JSONL for the intranet model

The source scan output is a prioritization aid. The small model must still read the cited source branch and adjust geometry/oracles before final bug filing.

## Failure Classification

Classify failures before writing a bug:

- SDK/API failure: `status.succeeded=false`, nonzero SDK error code, or process crash.
- Validation-only failure: SDK returned success but harness oracle failed; inspect `validation_failures` before deciding whether the oracle or SDK is wrong.
- Data binding issue: `source_file` is missing, placeholder-only, wrong format, or not present in the selected ABC fetch root.
- Corpus limitation: current dataset has only STEP or only IGES, so the other exchange API needs a different fetch root.
- Harness extension: model requested an SDK surface not supported by the runner.

Use `triage_summary.json` `failure_groups` to deduplicate by fingerprint, then pick one representative case for replay/reduction.

## 2026-07-07 ABC 40chunk Replay Lessons

Reference report: `test_harness/reports/interface_distillation_abc40_v2_report.md`.

Important lessons from that run:

- The end-to-end flow completed even though individual test cases failed.
- API smoke was green, so failures came from broader ABC/source-guided lanes rather than baseline runner breakage.
- A single STEP import fingerprint covered 19/48 top-complex import failures with error `Not accepted type!`.
- The ABC recut lane found three tangent-cylinder subtraction failures on one imported SGT result with error `wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now.`
- Sweep/extrude generated-topology intersection produced `Coedge has no PCurve to calc nominal curve` in two tolerance-band cases.
- Revolve-rect boolean produced SDK success with failed oracles; these are validation-only candidates that need replay/reduction before becoming persistent bug records.
- The 40chunk ABC fetch root contained STEP only. Direct `iges_import` sanity passed when pointed at a generated `iges_roundtrip` output, so ABC IGES failures from placeholder paths should be classified as corpus coverage gaps, not SDK import bugs.
