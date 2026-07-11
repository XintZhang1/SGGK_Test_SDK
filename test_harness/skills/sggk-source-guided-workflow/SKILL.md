---
name: sggk-source-guided-workflow
description: Compose SGGK source-code risk scanning, developer API forms, OCC surrogate examples, deterministic cluster expansion, DSL review, harness execution, triage, preview, geometry audit, and bug-record promotion. Use when Codex or an intranet small model must turn SGGK/internal source findings into reviewed structured test cases while preserving normal randomized and corpus lanes.
---

# SGGK Source Guided Workflow

## Purpose

Turn source-code findings into reviewed harness attacks without asking the model to write direct SDK code. Use this skill as the orchestrator on top of `sggk-source-attack`, `sggk-api-form-workflow`, and the deterministic matrix/DSL/corpus tools.

## References

- Read `references/source-guided-contract.md` before preparing small-model input or reviewing model output.
- Read `references/occ-surrogate-examples.md` when public surrogate source examples or distillation prompts are needed.
- Read `references/cluster-policy.md` when expanding one source-risk seed into a compact case cluster.
- Read `references/interface-distillation-runbook.md` when replaying the full form-to-report campaign or teaching a small model how to interpret ABC/source-guided run evidence.
- Read `references/model-context-pack.md` when building bounded Message API tasks and selecting the minimum deterministic context for each request.
- Read `references/regression-asset-workflow.md` after a run should become a long-lived version-regression monitor.

## Workflow

1. Pull the latest harness metadata into the Windows SDK workspace. Keep SDK headers, license files, build output, and artifacts out of GitHub.
2. Gather source context:
   - For SGGK source or headers, run `scan_source_risks.py` and `build_source_attack_tasks.py`.
   - For developer-request forms, use `sggk-api-form-workflow` and `build_api_test_task.py`.
   - For public examples, use OCCT anchors from `occ-surrogate-examples.md`; do not vendor OCCT source.
3. Require model output to be JSON only: `attack_dsl`, `flat_recipe`, `cluster_seed`, or `needs_harness_extension`.
4. For `cluster_seed`, run `build_source_guided_cluster.py` to emit reviewed attack DSL before compiling.
5. For DSL, run `compile_attack_dsl.py --check --report` first, then compile/run only after review.
6. Keep randomized and broad lanes active:
   - `generate_boolean_matrix.py` for broad boolean coverage.
   - `generate_corpus_recut_matrix.py` for saved SGT/imported body recuts.
   - `run_campaign.py` or `plan_large_campaign.py` for integrated batches.
7. After execution, triage, render previews, run geometry audit, replay stable seeds, snapshot compact regression assets, and promote bug records only after portability audit.

## Commands

```powershell
python .\test_harness\tools\scan_source_risks.py <source-root> --out .\artifacts\source_risk_scan --max-findings 120 --max-seeds 30
python .\test_harness\tools\build_source_attack_tasks.py .\artifacts\source_risk_scan --out .\artifacts\source_attack_tasks --max-tasks 80 --context-lines 12 --write-dsl-seeds
python .\test_harness\tools\build_source_guided_cluster.py .\test_harness\dsl\source_guided_cluster_seed_smoke.json --out .\artifacts\source_guided_cluster_smoke.json
python .\test_harness\tools\compile_attack_dsl.py .\artifacts\source_guided_cluster_smoke.json --check --report .\artifacts\source_guided_cluster_check.json
python .\test_harness\tools\compile_attack_dsl.py .\artifacts\source_guided_cluster_smoke.json --out .\artifacts\source_guided_cluster_recipes
python .\test_harness\tools\run_recipes.py --runner .\build\test_harness\Release\sggk_case_runner.exe --recipe .\artifacts\source_guided_cluster_recipes --out .\artifacts\source_guided_cluster_run --triage-out .\artifacts\source_guided_cluster_triage --preview-out .\artifacts\source_guided_cluster_preview --geometry-audit-out .\artifacts\source_guided_cluster_audit
```

## Review Rules

- Preserve source references and exact numeric literals when relevant.
- Add nearby variants around `geom_tol=1e-5` and `topo_tol=1e-2`.
- Prefer legal adversarial geometry over impossible inputs.
- Add measurable oracles; API success alone is not enough.
- Emit `needs_harness_extension` for unsupported APIs or body builders.
- Keep proprietary excerpts in artifacts or intranet-only tasks, not in GitHub.
