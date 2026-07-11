---
name: sggk-source-attack
description: Convert bounded SGGK source-risk evidence into Message API task manifests, run candidates through fixed harness gates and the SDK, and qualify/replay/localize resulting failures.
---

# SGGK Source Attack

## Purpose

Use source inspection to prepare trusted evidence for automatic test authoring.
Production model output comes only from `choices[0].message.content` through
`run_message_harness_pipeline.py`. A human, Codex, or standalone model session
must not save, edit, compile, run, or promote captured model JSON.

The scanner and task builder produce prompt context, not tests. Low-level DSL
expansion, compilation, validation, and runner commands are host-owned fixed
gates. Direct invocation is limited to diagnosing checked-in deterministic
fixtures or artifacts already produced by a pipeline fixed gate; it cannot
accept a model response or create authoring provenance.

## Production Workflow

1. Inspect bounded source context for fragile predicates: tolerances, special-case branches, disabled checks, degeneracies, topology mutation, seams, periodicity, singularities, merge/split behavior, and exchange conversion.
2. For broad source roots, run `scan_source_risks.py`, then build bounded JSONL tasks with `build_source_attack_tasks.py`. Treat scanner seed DSL as optional prompt context only.
3. Build a provider-neutral `model_task_manifest.json` with `build_model_prompt_pack.py --source-task-dir`.
4. Run the manifest with `run_message_harness_pipeline.py --profile intranet`. The pipeline owns candidate generation, contract repair, cluster expansion, DSL/recipe checks, isolated SDK execution, deterministic selection, provenance, and atomic promotion.
5. Use `--profile siliconflow-test` only for explicit external simulation of the same production protocol. It is never an intranet fallback.
6. Keep deterministic baseline coverage alongside the authored lane: generated boolean matrices, corpus import/roundtrip checks, loaded-SGT recuts, API smoke, and known-bug replay.
7. Qualify failures before calling them SDK defects. Require stable same-signature replay, preserve real-result oracle evidence, reduce only when the signature survives, and use paired isolated TopoTrack capture/control for localization.
8. Keep bug reports candidate-only until deterministic qualification, replay, portability audit, and maintainer review are complete.

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
  --run-id source_attack_batch `
  --execute `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  .\artifacts\source_model_prompt_pack\model_task_manifest.json
```

For large deterministic baselines, use `plan_large_campaign.py` or
`run_campaign.py` after the Message API lane is green. These commands execute
host-authored fixtures, matrices, corpus indexes, and accepted pipeline outputs;
they are not model transports.

## Required References

- `references/attack-heuristics.md`: source-risk and geometry/oracle selection context for the prompt.
- `references/attack-dsl.md`: untrusted `attack_dsl` candidate schema and fixed-gate behavior.
- `references/recipe-schema.md`: fixed flat-recipe schema and supported runner APIs.
- `references/output-contract.md`: pipeline evidence and reporting requirements.

## Guardrails

- Only the integrated Message API pipeline may set `authoring_accepted=true` or promote a formal model output.
- Never copy/paste a response, seed a fixture as a model output, or use a direct compiler/runner command as an acceptance shortcut.
- Model JSON cannot contain commands, executable/runner paths, dataset/output paths, cwd, environment, shell mode, SDK/link flags, URLs, or native tool calls.
- Prefer legal adversarial geometry over impossible input unless the source explicitly handles invalid input.
- Preserve stable case/operation IDs and exact source literals where relevant; use deterministic nearby tolerance bands.
- Require measurable result oracles. API success alone is not a pass.
- `needs_harness_extension` is a non-executing structured backlog report. It cannot propose, generate, review, or apply a source patch.
- Keep proprietary source excerpts and generated artifacts under `artifacts/` or intranet-only storage.
- Before checking in a long-lived regression asset, require portable paths, stable replay evidence, and the existing bug-record audit.
