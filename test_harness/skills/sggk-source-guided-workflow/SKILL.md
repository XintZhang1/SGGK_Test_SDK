---
name: sggk-source-guided-workflow
description: Prepare bounded SGGK source-risk and API-form context, author candidates through the integrated Message API pipeline, and inspect deterministic gate, SDK, triage, replay, and regression evidence.
---

# SGGK Source Guided Workflow

## Purpose

Turn source-code findings and developer API forms into trusted prompt manifests
for automatic Message API authoring. `run_message_harness_pipeline.py` is the
only production model-output acceptance path. Codex, a human, and a standalone
small-model session must not paste, edit, promote, compile, or execute captured
model JSON.

This skill orchestrates `sggk-source-attack`, `sggk-api-form-workflow`, and the
deterministic matrix/DSL/corpus tools. Low-level cluster, DSL compiler,
validator, and runner commands are host fixed gates. Invoke them directly only
to diagnose checked-in deterministic fixtures or pipeline gate artifacts; they
cannot establish authoring acceptance.

## References

- Read `references/source-guided-contract.md` for the untrusted candidate contract and production acceptance boundary.
- Read `references/occ-surrogate-examples.md` for bounded public prompt context; do not vendor OCCT source.
- Read `references/cluster-policy.md` for the host-owned deterministic cluster expansion policy.
- Read `references/interface-distillation-runbook.md` for the complete Message API form-to-report run.
- Read `references/model-context-pack.md` for prompt budgets, manifests, profiles, and pipeline commands.
- Read `references/regression-asset-workflow.md` after a qualified run should become a long-lived version-regression monitor.

## Workflow

1. Pull the latest harness metadata into the Windows SDK workspace. Keep SDK headers, license files, build output, and artifacts out of GitHub.
2. Build trusted context:
   - For SGGK source or headers, run `scan_source_risks.py` and `build_source_attack_tasks.py`.
   - For developer requests, validate the form/intake through `sggk-api-form-workflow`.
   - For public examples, use only the bounded OCCT anchors in `occ-surrogate-examples.md`.
3. Build `model_task_manifest.json` with `build_model_prompt_pack.py`. Scanner seed DSL is prompt context only; it is never a runnable or accepted output.
4. Submit the manifest through `run_message_harness_pipeline.py --profile intranet`. The pipeline alone reads `message.content`, normalizes candidates, expands `cluster_seed`, runs kind-specific fixed gates, executes isolated SDK cases, selects deterministically, and atomically promotes JSON plus provenance.
5. Require accepted provenance with `authoring_accepted=true`, `accepted_by=message_harness_pipeline`, a matching candidate hash, and a successful fixed gate before any post-promotion consumer runs.
6. Keep randomized and broad deterministic lanes active with `generate_boolean_matrix.py`, `generate_corpus_recut_matrix.py`, `run_campaign.py`, or `plan_large_campaign.py`.
7. After execution, inspect triage, previews, geometry audit, qualification, replay, reduction, TopoTrack evidence, and candidate-only bug reports. Promote durable bug records only after portability audit and stable replay.

## Production Commands

```powershell
python .\test_harness\tools\scan_source_risks.py <source-root> `
  --out .\artifacts\source_risk_scan `
  --max-findings 120 `
  --max-seeds 30

python .\test_harness\tools\build_source_attack_tasks.py `
  .\artifacts\source_risk_scan `
  --out .\artifacts\source_attack_tasks `
  --max-tasks 80 `
  --context-lines 12 `
  --write-dsl-seeds

python .\test_harness\tools\build_model_prompt_pack.py `
  --source-task-dir .\artifacts\source_attack_tasks `
  --out .\artifacts\source_model_prompt_pack

python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --run-id source_guided_batch `
  --execute `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  .\artifacts\source_model_prompt_pack\model_task_manifest.json
```

Use `--profile siliconflow-test` only for explicit external protocol/model
simulation. It follows the same manifest, candidate, fixed-gate, selection, and
promotion path and is never an intranet fallback.

## Candidate Rules

- Candidate kinds are limited by the manifest contract: `attack_dsl`, `flat_recipe`, `cluster_seed`, `campaign_request`, `api_plugin_candidate`, or non-executing `needs_harness_extension` as appropriate.
- Preserve cited source references and exact numeric literals when relevant.
- Prefer legal adversarial geometry, stable case IDs, and measurable semantic oracles.
- A model cannot provide commands, paths, environment, cwd, executables, shell mode, SDK flags, or native tool calls.
- Unsupported APIs outside existing adapters and registered archetypes remain `needs_harness_extension`; a model cannot propose or apply a source patch.
- Keep proprietary excerpts and all generated candidates under `artifacts/` or intranet-only storage.
