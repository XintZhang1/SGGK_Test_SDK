# Output Contract

Use this reference when reporting a source-attack run performed through the
integrated Message API pipeline. It is a host evidence/reporting contract, not
an instruction for Codex, a human, or a standalone model to author runnable
JSON.

## Required Output Shape

Return concise Markdown with three sections:

1. `Inputs`: source references, risk findings, task manifest, profile, and trusted local bindings.
2. `Authoring`: pipeline summary, candidate count, selected candidate, accepted output/provenance, fixed-gate status, and any bounded repair diagnostics.
3. `Execution`: SDK/semantic result, triage, replay, reduction, TopoTrack, and candidate-only bug-report evidence.

Production authoring and approval are limited to the public-function
`harness.ps1 start` / natural-language `harness.ps1 comment` session.
Low-level DSL/compiler/validator/runner commands may appear only as pipeline
fixed-gate evidence or as explicit debugging of checked-in deterministic
fixtures. They cannot accept captured model output.

## Finding Format

Each finding should include:

- `source`: file path and line or function name
- `risk`: one sentence
- `trigger`: threshold, branch, geometry condition, or topology condition
- `recipe_ids`: IDs of recipes that exercise it

## Source Scan Format

When using the deterministic source scanner, report:

- `source_risk_report.md` and `source_risk_report.json`
- `attack_seed_drafts.json` count and representative `seed_id` values
- `source_attack_tasks.jsonl` and `source_attack_task_manifest.md` when `build_source_attack_tasks.py` was used
- top severity/category counts
- confirmation that generated `dsl_seed` files were used only as prompt context and were not compiled, edited, or executed as model output
- source references included in the bounded task context

## Recipe Rules

- The Message API candidate must be valid JSON, not pseudo-JSON.
- Prefer attack DSL over flat recipes when the manifest contract allows both.
- Use exact numeric literals where possible.
- Include deterministic IDs.
- Prefer one recipe per hypothesis plus nearby variants for threshold issues.
- Prefer `chain` bodies when the source risk mentions generated faces, swept side topology, BSpline support-face sweep, previous boolean output, topology history, or operation sequencing.
- Include stable `id` fields on important chain steps so provenance appears in runner artifacts.
- Use `load_sgt` for previously found artifacts or corpus bodies that should become new attack seeds.
- Add `expectations` for measurable source-level predicates. Prefer result body count, optional sampled boolean volume relation, point/body relation checks for critical points, clash/distance checks, and total length/area/volume bounds over prose-only expected behavior.
- Mark unsupported operations as `needs_harness_extension` rather than inventing runner support.

## Failure Triage Rules

- Inspect `report/topo_track_summary.json` first. Use `resolved_ancestor_count`, `unresolved_ancestor_count`, and `ancestor_input_role_counts` to see whether the SDK topo track maps the failure back to target/tool inputs.
- Use `report/topo_track.json` `items[*].ancestors[*].input_ref` to report concrete input topology: role, type, ID, local index, terminal operation, and operation chain.
- Use `report/input_topology_index.json` to turn an `input_ref` into a geometry locator. Vertex locators include point/tolerance, edge locators include endpoints/length/tolerance, face locators include area/sense, and most topologies include bounding boxes.
- If topo tracking is skipped or unresolved, use `triage_summary.json` `input_contact_candidates` to report ranked target/tool Body/Face/Edge/Vertex bbox-nearness pairs. Include operation IDs such as `target_operations` or `tool_operations` when present.
- Preserve and report source-task provenance when present: `source_task_id`, `source_task_path`, `source_ref`, `source_risk_id`, `source_risk_family`, and `source_risk_categories` from manifest, triage groups, bug registries, or bug records.
- Inspect `report/validation.json` whenever `validation_ok=false` or triage reason includes `validation_failed`; report the failed oracle, skipped checks, result totals, and any `debug_geometry` assets. For boolean cases, use `report/input_properties.json` for input bbox snapshots and sampled input volumes when `sample_input_properties=true`.
- For critical-point checks, use `expectations.point_relations` with `role` set to `result`, `target`, or `tool`; expected values include `Inside`, `Outside`, `OnFace`, `OnEdge`, `OnVertex`, `OnBoundary`, and `OnModel`. For DSL output, define reused probes under `key_points` and reference them with `point_ref` so the compiled recipe carries both the resolved point and the probe name. Report actual relations from `report/validation.json` `point_relations`.
- For point/face checks, use `expectations.face_point_relations` with `role` set to `result`, `target`, or `tool`; select faces by `face_index` for generated probes or `face_id` for reduced bug records. Prefer `uv_fraction` or explicit `uv`; report actual face id, UV bound, actual UV, computed point, and relation from `report/validation.json` `face_point_relations`.
- For collision checks, use `expectations.clash_checks` with `role_a`/`role_b` set to `result`, `target`, or `tool`; expected values include exact `ClashType` names plus `NoClash` and `AnyClash`. Report actual clash types and sub-topology pairs from `report/validation.json` `clash_checks`.
- For distance checks, use `expectations.distance_checks` with `role_a`/`role_b` set to `result`, `target`, or `tool`; set `kind` to `minimum` for clearance/tangency hypotheses and provide `expected`, `min`, or `max` with explicit `abs_tol`. Report actual distance, closest points, and located topology from `report/validation.json` `distance_checks`.
- For exact coordinate extrema, use `expectations.plane_extreme_checks` with `role`, `axis`, `side`, `expected`, and `tolerance` when it is a hard oracle. Use `compare_expected=false` or `probe_exact_bbox.py` when the task is pure measurement. Report `compare_expected`, `actual_extreme`, `probe_coordinate`, `probe_coordinate_source`, `max_model_size`, closest points, located body topology, and any `debug_geometry` SGT paths from `report/validation.json` / `report/debug_geometry_index.json`.
- For any validation oracle failure, include `report/debug_geometry_index.json` and copied `debug_geometry/*.sgt` paths when present. These are GUI-ready assets for the failing body, selected face, clash pair, distance-hit topology, or coordinate probe plane.
- For GUI handoff, use `build_debug_handoff.py --registry <bug_registry> --out <debug_handoff>` or the campaign's `debug_handoff/` output. For known-bug replay lanes, use `build_debug_handoff.py --registry <known_bug_records> --triage <known_bug_replay_triage> --out <debug_handoff>` so pack names keep registry fingerprints and bug ids while focus/input SGTs come from the latest replay artifacts. When available, `sggk_topology_extract.exe` exports primary-contact target/tool topology to `focus/*.sgt`; each pack also writes `visual_index.json` / `visual_index.md` for all copied debug/focus/input SGTs and `focus_index.json` / `focus_index.md` for extraction status. Report `debug_handoff_report.md`, each pack directory, `visual_index.md`, `focus_index.md`, `sgt_paths.txt`, copied debug SGT count, copied focus-topology SGT count, copied input SGT count, and the topology extractor path.
- Render screenshots with `render_case_preview.py <artifact-root> --out-dir <preview-dir> --contact-sheet <preview-dir>/contact.png`. For tolerance families, run `audit_case_geometry.py <artifact-root> --out <audit-dir> --exact-bbox-runner <runner>` and check signed clearances before claiming variants are distinct.
- Summarize oracle coverage with `summarize_oracle_coverage.py --campaign <campaign-root> --out <coverage-dir>` for existing runs, or use the default `run_campaign.py` `oracle_coverage/` output. Report passed cases missing validation, passed cases below the minimum oracle-kind count, and oracle counts for property snapshots, metric expectations, point/body relation, face/point relation, clash, distance, and exact plane-extreme checks.
- For a model-authored DSL candidate, report the fixed `compile_attack_dsl.py --check` evidence produced by `run_message_harness_pipeline.py`, including `mode`, `file_count`, `recipe_count`, `compile_failure_count`, and `validation_failure_count`. Do not run the compiler manually on captured model output. Direct compiler use is limited to checked-in deterministic fixtures or diagnosis of an existing pipeline gate artifact.
- Use `triage_artifacts.py <artifact-root> --out <triage-dir>` after large corpus or DSL batches to generate `triage_summary.json` and `triage_report.md`.
- For generated flat recipe batches, prefer `run_recipes.py --runner <runner> --recipe <recipe-dir> --out <artifact-root> --jobs N --resume --triage-out <triage-dir> --preview-out <preview-dir> --geometry-audit-out <audit-dir>`.
- For SGT corpus recut batches, run `generate_corpus_recut_matrix.py --dataset-list <dataset_index.json> --out <recipe-dir> --preset smoke|standard|stress --runner <runner>`, then run the generated recipes with `run_recipes.py` plus preview and geometry audit. Report the source count, skipped source count, recipe count, exact-bbox probe status, bbox source counts, contact sheet, and signed-clearance audit. Use `--require-exact-bbox-probe` for extent-sensitive bug-filing lanes.
- For persistent bug-registry regressions, use `run_recipes.py --runner <runner> --recipe-list <registry_replay_recipes.txt> --out <artifact-root> --triage-out <triage-dir> --preview-out <preview-dir> --geometry-audit-out <audit-dir>`, then `check_bug_registry_regression.py --registry <registry-dir> --recipe-summary <artifact-root>/recipe_summary.json --out <regression-dir>`. A nonzero lane return can be expected for known-open failures; report `still_failing`, `fixed_or_not_reproduced`, `changed_failure`, and `unavailable`.
- For runner crashes or nonzero exits without validation reports, report `runner.returncode`, `timed_out`, inferred `artifact_dir`, primary contact topology, and copied target/tool/source SGTs. If status/topo-check output exists and the original recipe has `topo_track=true`, run `probe_topotrack_crashes.py`; report `topotrack_probe_report.md`, selected count, and classifications such as `topotrack_only_modeling_ok`. Persistent records should preserve `expected.returncode` only when disabling topo tracking still leaves a modeling, validation, or crash failure.
- For large local or distributed campaigns, prefer `plan_large_campaign.py --runner <runner> --out <plan-root> --profile smoke|standard|stress --shards N --dataset-root <root> --source-root <source-root>`. Report `large_campaign_plan.md`, frozen `discovery/dataset_index.json`, `commands/preflight.ps1`, shard scripts, `commands/run_all_sequential.ps1`, `commands/run_all_with_preflight.ps1`, `commands/collect_shards.ps1`, whether source scan runs only in shard 0 or every shard, selected profile limits, and whether oracle/artifact verification gates apply to shards, merged collection, or both. Use planner `--dataset-audit-require-hashes` or `--dataset-audit-fail-duplicate-ratio <ratio>` for benchmark-quality frozen corpus plans; use `--profile-cad-features --cad-feature-min-score <N>` for STEP/IGES-heavy plans, `--require-cad-feature-profile` when an empty complex subset should fail preflight, and `--use-cad-feature-subset` when shard corpus lanes should run directly on the plan-time `cad_feature_profile/complex_dataset_index.json`; use `--skip-dataset-audit` only for legacy path lists that cannot be audited yet. When a plan uses the subset, report `original_dataset_lists`, `shard_dataset_lists`, `cad_feature_profile/cad_feature_profile.md`, `cad_feature_profile/complex_dataset_index.json`, and the complex-file count before showing shard commands. Use planner `--replay-reductions` only when collection should replay canonical reduced recipes before verification; add planner `--export-reduction-bug-record-drafts` only when those reduced replay failures should produce reviewable draft records; add planner `--materialize-reduction-bug-records` only when reduced drafts should become a temporary registry and regression report; add planner `--promote-bug-records` when merged drafts should also become artifact-local portable candidates; add planner `--replay-promoted-bug-records` when promoted candidates should also be materialized, replayed, and classified from their candidate root before review. Run or recommend the generated preflight before long shard runs; report `preflight/preflight_report.md`, ok/error/warning counts, DSL recipe count, bug-record count, bug-record portability audit status, dataset audit status, CAD feature profile complex-file count, `complex_dataset_index.json`, subset-audit status/hash coverage when enabled, and dataset input evidence. After execution, report merged `campaign_shards_report.md`, lane totals, source finding/task counts, merged `dataset_audit/dataset_audit_collection.md` with audited/failed/missing counts, merged `reductions/reduction_index.md`, optional `reduction_replay/` with `semantic_check.md` and stable/changed/not-reproduced/unavailable counts, optional `reduction_bug_record_drafts/drafts.json` and draft count, optional `reduction_bug_records_materialized/bug_registry.md` plus `reduction_bug_regression/registry_regression.md` status counts, optional `promoted_bug_records/` with portability audit status and promoted replay/regression status when enabled, merged `oracle_coverage/oracle_coverage.md`, merged `bug_registry/bug_registry.md`, merged `debug_handoff/debug_handoff_report.md`, visual/focus-index availability, and merged `bug_record_drafts/drafts.json` with GUI handoff evidence when matching packs exist.
- For full local campaigns, use `run_campaign.py --runner <runner> --out <campaign-root> --dataset-root <root> --matrix-preset smoke --corpus-recut-preset smoke --corpus-recut-use auto --dsl <dsl> --jobs N`. Before a long run, use `preflight_campaign.py --runner <runner> --dataset-root <root> --source-root <source-root> --out <preflight-root>` to validate runner/extractor, DSLs, checked-in bug records, bug-record portability, dataset audit, and dataset inputs. Add `--discover-include-artifacts` when previous harness outputs should become corpus seeds, and `--shard-count N --shard-index I` for large split runs. Use `--dataset-audit-require-hashes` and `--dataset-audit-fail-duplicate-ratio <ratio>` when a direct campaign should enforce benchmark-quality corpus inputs. Use `--reduce-stable-failures` with `--reduction-limit` and `--reduction-max-trials` only when stable replay failures should be minimized during the campaign. Add `--promote-bug-records` when generated drafts should become artifact-local portable candidates; add `--replay-promoted-bug-records` when those promoted candidates should also be materialized, replayed, and classified from `promoted_bug_records/`. By default the campaign also audits corpus datasets, materializes and replays checked-in records from `test_harness/bug_records`, summarizes oracle coverage, and runs artifact verification; use `--skip-known-bug-regression` only for pure exploration. Report `campaign_report.md`, `dataset_audit/dataset_audit.md` with missing/empty/hash/duplicate counts, DSL check reports under `dsl_checks/` with compile/validation failure counts, aggregate triage, replay status, reduction index and accepted reduction count when enabled, whether corpus recut used original inputs or corpus output artifacts, corpus-recut source/recipe counts, geometry-audit duplicate/mismatch counts, contact-sheet paths, `oracle_coverage/oracle_coverage.md` with validation-present and oracle-kind counts, bundle paths, `bug_registry/bug_registry.md`, `debug_handoff/debug_handoff_report.md`, `bug_record_drafts/drafts.json` and whether drafts include `debug_handoff.visual_index` / `focus_index` evidence, optional `promoted_bug_records/test_harness/bug_records/*.json`, portability audit status, optional `promoted_bug_records/materialized/bug_registry.md`, `promoted_bug_records/replay/recipe_summary.json`, and `promoted_bug_records/regression/registry_regression.md`, known-bug regression status counts, `campaign_verification/campaign_verification.md`, and any `empty_shard=true` lanes.
- For an existing campaign or merged shard collection, run `verify_campaign_artifacts.py --campaign <campaign-or-merged-root> --out <root>/campaign_verification`; add `--expect-known-bug-status still_failing` for known-open regression runs. Report `campaign_verification.md`, `ok`, error/warning counts, DSL-check status, oracle-coverage status, contact-sheet verification, geometry-audit duplicate/tolerance counts, and known-bug/debug-handoff evidence.
- For split campaign collection, use `collect_campaign_shards.py --campaign <campaign-root>... --out <merged-root> --materialize-bug-records --validate-recipes`. Add `--promote-bug-records` when merged drafts should also be copied/re-written into `promoted_bug_records/` as portable review candidates with a portability audit; add `--replay-promoted-bug-records --runner <runner>` when those promoted candidates should also be materialized, replayed, and classified before review. Add `--replay-reductions --runner <runner>` when canonical reduced recipes should be replayed before review; add `--export-reduction-bug-record-drafts` when reduced replay triage should also generate editable reduced repro drafts; add `--materialize-reduction-bug-records` when those reduced drafts should be checked as a temporary registry against the reduced replay summary. Report `campaign_shards_report.md`, raw aggregate sums, per-lane DSL check report/recipe/failure counts, merged `dataset_audit/dataset_audit_collection.md` with audited/failed/missing-file/hash coverage counts, known-bug regression raw status counts, merged `reductions/reduction_index.md` when shard reductions exist, distinct fingerprint count, duplicate fingerprint group count, canonical reduced recipe paths from `fingerprint_groups`, optional `reduction_replay/canonical_reduced_recipes.txt`, replay `recipe_summary.json`, triage report, preview contact sheet, geometry audit report, `reduction_replay/semantic_check.md` with `stable_same_failure`, `changed_failure`, `not_reproduced`, and `unavailable` counts, optional `reduction_bug_record_drafts/drafts.json` with draft count and `reduction_replay_evidence`, optional `reduction_bug_records_materialized/bug_registry.md`, optional `reduction_bug_regression/registry_regression.md` with `still_failing`/changed/fixed/unavailable counts, optional `promoted_bug_records/test_harness/bug_records/*.json` plus `promoted_bug_records/portability_audit/bug_record_portability.md`, optional `promoted_bug_records/materialized/bug_registry.md`, `promoted_bug_records/replay/recipe_summary.json`, and `promoted_bug_records/regression/registry_regression.md`, merged `oracle_coverage/oracle_coverage.md`, merged `bug_registry/bug_registry.md`, merged `debug_handoff/debug_handoff_report.md`, focus-topology SGT counts and per-pack `visual_index.md` / `focus_index.md` paths when `sggk_topology_extract.exe` was auto-detected, merged `bug_record_drafts/drafts.json` with copied GUI handoff evidence when available, and materialized `bug_records_materialized/bug_registry.md`.
- Use `triage_summary.json` `failure_groups` to deduplicate failures by fingerprint before writing bug reports or selecting reducers.
- Use `regression_seeds.json` to select one representative artifact/recipe/source seed per failure group.
- Use `replay_regression_seeds.py --runner <runner> --seeds <regression_seeds.json> --out <replay-dir> --retries 3` to confirm representative seeds. Report `stable_failure`, `flaky`, `not_reproduced`, and `unavailable` separately.
- Use `reduce_failure_recipe.py --runner <runner> --recipe <stable-flat-recipe.json> --out <reduced-dir> --max-trials N` for stable flat-recipe failures that need minimization. Report `reduced_recipe.json`, `reduction_summary.json`, accepted reductions, final error code or validation failures, whether dimensions stayed above `1e-2`, and whether the reduced case preserves the intended contact offset such as `+/- geom_tol` or `+/- topo_tol`.
- Use `export_failure_bundles.py --triage <triage-dir> --replay <replay-dir> --preview-dir <preview-dir> --out <bundle-dir> --zip` for stable failures that need a handoff package. Report the generated `bug_report.md`, `bundle_manifest.json`, `localization_summary.json`, `reproduce.ps1`, debug geometry directory when present, and zip paths.
- Use `collect_bug_registry.py --triage <triage-dir> --replay <replay-dir> --bundle-index <bundle-dir-or-index> --out <registry-dir>` when multiple campaign or bundle outputs need a persistent bug index. A triage-only registry preserves original recipe paths, representative case dirs, debug geometry, and source/target/tool input paths when available; bundle indices add richer handoff reports. Report `bug_registry.md`, replay status counts, primary contact topology, debug-geometry paths, roundtrip `source_sgt`/STEP/IGES inputs when present, and `registry_replay_recipes.txt`.
- Use `export_bug_record_drafts.py --triage <triage-dir> --bundle-index <bundle-dir-or-index> --debug-handoff <debug_handoff-dir-or-index> --out <drafts.json> --bug-prefix <prefix>` when a triage/bundle failure should become a maintained regression asset. Omit `--debug-handoff` only when no GUI pack was generated. For merged reduced replay cases, prefer collector-generated `reduction_bug_record_drafts/drafts.json` so the draft points at the canonical reduced recipe and includes `reduction_replay_evidence`. Review generated `bug_id`, `title`, and notes before check-in. Report the draft path, replay recipe path, debug-geometry paths, GUI handoff `visual_index.md` / `focus_index.md` / `sgt_paths.txt` when present, source input paths for exchange/roundtrip failures, and topo-track diagnostic status.
- Use `promote_bug_records.py --records <drafts.json-or-dir> --out <portable-json> --repo-root <candidate-root> --fixture-root test_harness/fixtures/bug_records` when campaign-local drafts still point at `artifacts/` but should become checked-in candidates. Report the promoted JSON, fixture root, copied asset count, and whether GUI handoff evidence was preserved only as a summary observation. For direct campaign outputs, prefer `run_campaign.py --promote-bug-records --replay-promoted-bug-records`; for merged shard outputs, prefer `collect_campaign_shards.py --promote-bug-records --replay-promoted-bug-records --runner <runner>` when the candidate-root portability, materialization, replay, and regression classification should all be produced together.
- Use `audit_bug_record_portability.py --records <bug-records.json-or-dir> --out <audit-dir>` before checking a known bug into `test_harness/bug_records`; report `bug_record_portability.md`, `ok`, error count, warning count, and any rejected absolute local or `artifacts/` paths.
- Use `record_bug_cases.py --records <bug-records.json-or-dir> --out <registry-dir> --validate-recipes` when a known bug should be checked in as a version-regression record. Each record should include `bug_id`, `fingerprint` when known, `representative_case_id`, `validation_failures` or `roundtrip_failures`, `topo_track_policy`, `localized_inputs` when topo tracking resolved concrete input topology, and replay material as either `replay.recipe`, `replay.recipe_path`, or DSL replay (`replay.dsl_path` / inline `replay.dsl` plus `case_id` or `dsl_case_id` + `dsl_variant`). Campaign-local records may include a `debug_handoff` block with visual/focus indexes and SGT path lists as review evidence, but checked-in portable records should normally omit transient `artifacts/` paths after the GUI evidence has been reviewed. Include `source_sgt`, `source_step`/`source_stp`, or `source_iges`/`source_igs` for data-exchange and roundtrip bugs when available. For corpus-derived records, copy the minimal durable SGT inputs under `test_harness/fixtures/bug_records/<id>/` and point replay/input-asset paths there instead of temporary campaign artifacts. Prefer `topo_track_policy: diagnostic_when_modeling_fails` when the modeling result is bad; report missing/skipped topo tracking as localization context, not as the primary failure.
- For local STEP/IGES/SGT corpora, run `discover_corpus.py <roots> --out <dataset_index.json> --hash-inputs`, then `audit_corpus_dataset.py --dataset-list <dataset_index.json> --out <audit-dir>`, then `run_corpus.py --dataset-list <dataset_index.json>`. For STEP/IGES-heavy corpora, run `profile_cad_features.py --dataset-list <dataset_index.json> --out <profile.json> --paths-out <complex_paths.txt> --subset-out <complex_dataset_index.json>` and report the feature profile before selecting a focused complex curve/surface subset. Prefer `complex_dataset_index.json` for benchmark/campaign lanes because it preserves hash and discovery metadata; audit it before long runs when source hashes are available. Report `dataset_audit.md`, total files, missing/empty counts, hash coverage, duplicate content groups, extension/API counts, `cad_feature_profile.md`, `complex_dataset_index.json`, profile complex-file count, top tags/scores, subset audit status, and any warnings before claiming the corpus is ready for a long run. `check_sgt` accepts both body SGTs and standalone debug/focus topology SGTs; for non-body topology assets report `result_topology_count`, topology type counts, and skipped body-property oracles. For SGT exchange roundtrips, repeat `--sgt-api check_sgt --sgt-api step_roundtrip --sgt-api iges_roundtrip`; add STEP BSpline flags when source inspection points to curve/surface conversion.
- For STEP/IGES recut attacks, first run import/check, then discover serialized artifact SGTs with `discover_corpus.py --include-artifacts` and pass that new dataset index to `generate_corpus_recut_matrix.py`.
- For large corpus runs, prefer `run_corpus.py --jobs N --resume --shard-count N --shard-index I --triage-out <triage-dir>` so batches can be split, resumed, and triaged.
- When summarizing a failure, name both the source-level hypothesis and the concrete topology locator that reproduces it. For roundtrip failures, also report the source input path, `report/roundtrip_comparison.json` metric deltas, tolerances, and exchange diagnostics.

## Example Content

Finding:

- `src/Boolean/Foo.cpp:123`: near-tangent branch compares distance with `Precision::MinLocalTol`; risk is unstable split classification on generated extrusion side faces. Recipes: `boolean_min_local_tol_001`.

Illustrative candidate contract shape (prompt/reference context only; do not
save or run this as a captured model response):

```json
{
  "dsl_version": 1,
  "constants": {"tol": 0.01, "tau": "2 * pi"},
  "defaults": {
    "api": "api_boolean",
    "boolean_type": "SUBTRACTION",
    "modeling_tol": "tol",
    "check_valid": true,
    "topo_track": true,
    "non_destructive": true
  },
  "cases": [
    {
      "case_id": "boolean_min_local_tol_001",
      "hypothesis": "Generated extrusion cap is cut around MinLocalTol.",
      "target": {"kind": "extrude_rect", "length": 260.0, "width": 180.0, "height": 220.0},
      "tool": {"kind": "solid_cylinder", "radius": 55.0, "height": 280.0, "angle": "tau", "translate": [0.0, 0.0, -30.0]},
      "sweeps": [
        {
          "path": "tool.translate_z",
          "values": [
            {"suffix": "below_tol", "value": "-30.0 - tol"},
            {"suffix": "flush", "value": "-30.0"},
            {"suffix": "above_tol", "value": "-30.0 + tol"}
          ]
        }
      ]
    }
  ]
}
```

Run commands:

```powershell
python .\test_harness\tools\scan_source_risks.py .\SGK1.4.10\SGGK\include --out .\artifacts\sdk_include_source_risk_scan --max-findings 120 --max-seeds 30
python .\test_harness\tools\build_source_attack_tasks.py .\artifacts\sdk_include_source_risk_scan --out .\artifacts\sdk_include_source_attack_tasks --max-tasks 80 --context-lines 12 --write-dsl-seeds
python .\test_harness\tools\build_model_prompt_pack.py --source-task-dir .\artifacts\sdk_include_source_attack_tasks --out .\artifacts\source_model_prompt_pack
.\harness.ps1 start api_boolean
.\harness.ps1 comment "增加源码分支对应的容差两侧和可观测 Oracle。"
.\harness.ps1 comment "明确同意当前方案，可以开始执行真实测试。"
python .\test_harness\tools\generate_corpus_recut_matrix.py --dataset-list .\artifacts\dataset_index.json --out .\artifacts\corpus_recut_recipes --preset smoke --case-prefix corpus_recut --runner .\build\test_harness\Release\sggk_case_runner.exe
python .\test_harness\tools\audit_corpus_dataset.py --dataset-list .\artifacts\dataset_index.json --out .\artifacts\dataset_audit
python .\test_harness\tools\triage_artifacts.py .\artifacts --out .\artifacts\triage
python .\test_harness\tools\render_case_preview.py .\artifacts --out-dir .\artifacts\preview --contact-sheet .\artifacts\preview\contact.png
python .\test_harness\tools\audit_case_geometry.py .\artifacts --out .\artifacts\geometry_audit --exact-bbox-runner .\build\test_harness\Release\sggk_case_runner.exe --fail-on-tolerance-mismatch
python .\test_harness\tools\replay_regression_seeds.py --runner .\build\test_harness\Release\sggk_case_runner.exe --seeds .\artifacts\triage\regression_seeds.json --out .\artifacts\replay --retries 3
python .\test_harness\tools\reduce_failure_recipe.py --runner .\build\test_harness\Release\sggk_case_runner.exe --recipe .\artifacts\compiled_attacks\failing_case.json --out .\artifacts\reduced_failing_case --max-trials 120
python .\test_harness\tools\export_failure_bundles.py --triage .\artifacts\triage --replay .\artifacts\replay --preview-dir .\artifacts\preview --out .\artifacts\failure_bundles --zip
python .\test_harness\tools\collect_bug_registry.py --triage .\artifacts\triage --replay .\artifacts\replay --bundle-index .\artifacts\failure_bundles --out .\artifacts\bug_registry
python .\test_harness\tools\export_bug_record_drafts.py --triage .\artifacts\triage --bundle-index .\artifacts\failure_bundles --out .\artifacts\bug_record_drafts\known_bug_drafts.json --bug-prefix sggk_draft
python .\test_harness\tools\audit_bug_record_portability.py --records .\test_harness\bug_records --out .\artifacts\bug_record_portability_checked
python .\test_harness\tools\record_bug_cases.py --records .\artifacts\bug_record_drafts\known_bug_drafts.json --out .\artifacts\bug_records --validate-recipes
python .\test_harness\tools\record_bug_cases.py --records .\test_harness\bug_records --out .\artifacts\checked_bug_records --validate-recipes
python .\test_harness\tools\run_recipes.py --runner .\build\test_harness\Release\sggk_case_runner.exe --recipe-list .\artifacts\bug_registry\registry_replay_recipes.txt --out .\artifacts\bug_registry_replay --triage-out .\artifacts\bug_registry_replay_triage --preview-out .\artifacts\bug_registry_replay_preview --geometry-audit-out .\artifacts\bug_registry_replay_geometry_audit
python .\test_harness\tools\check_bug_registry_regression.py --registry .\artifacts\bug_registry --recipe-summary .\artifacts\bug_registry_replay\recipe_summary.json --out .\artifacts\bug_registry_regression --fail-on-changed --fail-on-unavailable
python .\test_harness\tools\plan_large_campaign.py --runner .\build\test_harness\Release\sggk_case_runner.exe --out .\artifacts\large_campaign_plan --profile standard --shards 8 --jobs 2 --timeout 120 --dataset-root .\SGK1.4.10\samples\Release\Output --source-root .\SGK1.4.10\SGGK\include --hash-inputs --dataset-audit-require-hashes --hash-recipes --source-task-write-dsl-seeds --materialize-bug-records --validate-recipes
powershell -ExecutionPolicy Bypass -File .\artifacts\large_campaign_plan\commands\run_all_sequential.ps1
python .\test_harness\tools\run_campaign.py --runner .\build\test_harness\Release\sggk_case_runner.exe --out .\artifacts\campaign --dataset-root .\SGK1.4.10\samples\Release\Output --corpus-sgt-api check_sgt --corpus-sgt-api step_roundtrip --corpus-sgt-api iges_roundtrip --corpus-recut-preset smoke --corpus-recut-use auto --jobs 4 --triage-include-passed --bundle-zip
python .\test_harness\tools\collect_campaign_shards.py --campaign .\artifacts\campaign_shard_0of2 --campaign .\artifacts\campaign_shard_1of2 --out .\artifacts\campaign_shards_merged --materialize-bug-records --validate-recipes
```
