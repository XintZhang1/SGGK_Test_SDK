# Interface Distillation Runbook

Use this runbook for the complete automatic intranet Message API loop:

```text
developer/source form -> Message API candidate pool -> fixed gates -> Windows SDK run -> deterministic selection -> triage report
```

Bounded intranet source context may be included in the prompt manifest. The
model still returns one structured candidate in `message.content`, never direct
SDK code, and only the integrated pipeline may accept it.

## Campaign Driver

Build a provider-neutral prompt pack and run it through the integrated Message
API pipeline. There is no fixture-seeding or human-authored model-output path.
Each independent candidate is isolated and must return one JSON object in
`message.content`:

```powershell
python .\test_harness\tools\build_model_prompt_pack.py `
  --out .\artifacts\model_prompt_pack `
  --source-task-dir .\artifacts\interface_distillation_windows_full_40chunk_v2\source_attack_tasks `
  --source-task-limit 80 `
  --max-prompt-chars 60000 `
  --run-tag abc40_v2

python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --run-id abc40_v2 `
  --execute `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  .\artifacts\model_prompt_pack\model_task_manifest.json
```

Use `--profile siliconflow-test` only for the explicit external simulation lane.
It uses the same manifest, candidate contract, gates, selection, and execution
path as `intranet`. The low-level gateway is transport debugging only.

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

## Automatic Candidate Rules

The automatic acceptance path enforces these rules:

- `cluster_seed` is not directly runnable. The pipeline invokes the fixed `build_source_guided_cluster.py` expansion before the DSL gate.
- `attack_dsl` must pass `compile_attack_dsl.py --check --report` before execution.
- `flat_recipe` must pass `validate_recipe.py` before execution.
- API success is not enough. Failed `validation.json`, TopoCheck, roundtrip comparison, point/face relation, clash, distance, plane-extreme, or empty-result oracle is a failure.
- ABC import tasks must receive a concrete trusted asset binding from the task pack. A model may not invent or rebind a filesystem path.
- Keep proprietary source excerpts and generated artifacts under `artifacts/`
  or intranet-only storage. Commit only forms, skills, human-authored
  deterministic fixtures/templates, portable bug records, and human-written
  summary reports; never commit a captured model response as a fixture.

## ABC And Source-Guided Lanes

Run both targeted and broad lanes:

- form/model lane: exercises each known interface family from `forms/interface_distillation/00_manifest.json`
- API smoke lane: checks current stable runner surface
- ABC top-complex import lane: runs the highest-complexity STEP/IGES subset selected by `complex_dataset_index.json`
- ABC recut lane: imports corpus bodies, saves SGT results, then attacks them with exact/gap/overlap generated cutters
- source scan lane: scans SGGK headers/source, writes risk findings, and builds source-attack task JSONL for the intranet model

Source scan output is prioritization and prompt context only. Each Message API
candidate must ground geometry/oracles in the cited branch, and fixed
qualification/replay still decides whether a failure can proceed to a
candidate bug report.

## ABC Boolean 100k Mass Recut Lane

Use this after the `loaded_sgt` ABC recut form has passed fixed gates. The model must not enumerate 100k cases. It emits a typed `campaign_request`; fixed code validates the profile and bounded arguments, binds local runner/data/output paths, and resolves an argv list executed with `shell=False`:

```powershell
python .\test_harness\tools\run_abc_boolean_mass_recut.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --dataset $env:SGGK_DATA_ROOT `
  --out .\artifacts\abc_boolean_mass_recut `
  --target-cases 100000 `
  --preset stress `
  --shard-count 100 `
  --shard-index 0 `
  --jobs 1 `
  --timeout 180 `
  --resume
```

`stress` generates 75 recipes per usable SGT source: 5 tool/contact families, 3 boolean operations, and 5 tolerance bands. A 100k campaign therefore needs about 1,334 usable imported SGTs. Run one shard first, then fan out shard indexes on the Windows machine.

The mass report preserves raw failures, but `abc_boolean_mass_recut_bug_report.md` excludes groups whose normalized error text matches explicit known-unsupported messages such as `Not accepted type!` or `wire and face both ... not allowed ... now`. These are counted as unsupported groups, not bugs. Candidate bug groups still need replay/reduction before promotion.

After each useful shard or full campaign, snapshot a compact regression asset so future SDK versions can replay the same tested surface:

```powershell
python .\test_harness\tools\manage_regression_assets.py snapshot `
  --campaign .\artifacts\abc_boolean_mass_recut `
  --out .\artifacts\regression_assets\abc_boolean_mass_recut `
  --asset-id abc_boolean_mass_recut `
  --sdk-version SGK1.4.10 `
  --dataset-label abc_fetch_40chunk_sample50 `
  --max-cases 5000 `
  --pass-sample 2000
```

The asset keeps copied replay recipes, baseline failure fingerprints, source/tool/form references, and track/contact localization summaries. It does not belong in GitHub; keep it under `artifacts/` or intranet storage.

When the SDK changes, replay and compare:

```powershell
python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe-list .\artifacts\regression_assets\abc_boolean_mass_recut\regression_recipe_list.txt `
  --out .\artifacts\regression_replay\abc_boolean_mass_recut\run `
  --triage-out .\artifacts\regression_replay\abc_boolean_mass_recut\triage `
  --jobs 1 `
  --timeout 180

python .\test_harness\tools\manage_regression_assets.py compare `
  --asset .\artifacts\regression_assets\abc_boolean_mass_recut `
  --new-run .\artifacts\regression_replay\abc_boolean_mass_recut\run\recipe_summary.json `
  --new-triage .\artifacts\regression_replay\abc_boolean_mass_recut\triage\triage_summary.json `
  --out .\artifacts\regression_compare\abc_boolean_mass_recut `
  --new-sdk-version SGK1.4.11
```

The comparison report separates fixed baseline bugs, new issues from baseline-passing cases, changed failures, still failing bugs, and unsupported cases that changed behavior. Use topo-track summaries first for localization; if topo-track is absent or skipped, use the stored target/tool contact candidates as the fallback rough locator.

## Failure Classification

Classify failures before writing a bug:

- SDK/API failure: `status.succeeded=false`, nonzero SDK error code, or process crash.
- Validation-only failure: SDK returned success but harness oracle failed; inspect `validation_failures` before deciding whether the oracle or SDK is wrong.
- Data binding issue: `source_file` is missing, placeholder-only, wrong format, or not present in the selected ABC fetch root.
- Corpus limitation: current dataset has only STEP or only IGES, so the other exchange API needs a different fetch root.
- Harness extension: model requested an SDK surface not supported by the runner.
- Known unsupported: kernel explicitly reports an unsupported/not-allowed case; keep raw evidence but exclude it from candidate bug reports.

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
