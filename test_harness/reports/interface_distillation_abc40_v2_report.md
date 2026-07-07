# Interface Distillation ABC 40chunk V2 Report

Run date: 2026-07-07

Commit: `3c6d2c9`

Windows output root:

```text
C:\Develop\SGGK_Test_SDK\artifacts\interface_distillation_windows_full_40chunk_v2
```

ABC fetch root:

```text
C:\Develop\SGGK_Agent\artifacts\abc_fetch_40chunk_sample50
```

This report is a human-written distillation sample for small-model training. It records counts, failure fingerprints, error codes, and interpretation rules from one Windows SDK run. It does not include SDK source, SDK binaries, ABC CAD files, or generated artifacts.

## Command

```powershell
python .\test_harness\tools\run_interface_distillation.py `
  --out .\artifacts\interface_distillation_windows_full_40chunk_v2 `
  --model-output-root .\artifacts\model_outputs_full_40chunk_v2 `
  --seed-example-model-outputs `
  --runner C:\Develop\SGGK_Test_SDK\build\test_harness\Release\sggk_case_runner.exe `
  --execute `
  --jobs 1 `
  --timeout 180 `
  --api-smoke `
  --abc-sample-smoke `
  --abc-fetch-root C:\Develop\SGGK_Agent\artifacts\abc_fetch_40chunk_sample50 `
  --source-root C:\Develop\SGGK_Agent\SGK1.4.10\SGGK\include
```

Exit code: `0`. This means the workflow completed and wrote summaries. Individual test failures are expected discoveries and must be read from the triage reports.

## Overall Results

| Lane | Result |
|---|---|
| interface forms | 14/14 executed |
| seeded model examples | 14 seeded |
| API smoke | 16 executed, 16 passed, 0 failed |
| ABC dataset audit | ok=true, 2000 files, 0 errors, 2 warnings |
| ABC feature profile | 2000 profiled, 1802 complex STEP files |
| ABC top-complex import | 48 executed, 29 passed, 19 failed, 0 timed out |
| ABC recut boolean | 24 executed, 21 passed, 3 failed, 0 timed out |
| direct IGES import sanity | 1 executed, 1 passed, 0 failed |
| source scan/task build | 80 source attack tasks, 3 critical / 18 high / 59 medium |

## Per-Form Results

| Request | Passed | Failed | Notes |
|---|---:|---:|---|
| `iface_01_boolean_primitive_source_guided` | 33 | 0 | cluster seed expanded and ran |
| `iface_02_step_import_abc_complex` | 0 | 1 | real STEP was bound from ABC root; same error family as top-complex import |
| `iface_03_loaded_sgt_abc_recut_boolean` | 3 | 0 | loaded SGT recut smoke passed |
| `iface_04_sweep_circle_line_boolean` | 28 | 2 | generated sweep/extrude intersection failures |
| `iface_05_support_sweep_bspline_boolean` | 3 | 0 | BSpline support-sweep cases passed |
| `iface_06_extrude_rect_boolean` | 5 | 0 | generated extrude cases passed |
| `iface_07_thicken_rect_sheet_boolean` | 5 | 0 | generated thicken cases passed |
| `iface_08_revolve_boolean` | 8 | 2 | validation-only revolve-rect failures |
| `iface_09_preboolean_history_recut` | 5 | 0 | prior-result recut smoke passed |
| `iface_10_step_roundtrip_imported_sgt` | 1 | 0 | roundtrip smoke passed |
| `iface_11_iges_roundtrip_imported_sgt` | 1 | 0 | roundtrip smoke passed |
| `iface_12_oracle_calibration_boolean` | 1 | 0 | oracle calibration passed |
| `iface_13_check_sgt_replay` | 1 | 0 | SGT replay passed |
| `iface_14_iges_import_abc_complex` | 0 | 1 | data coverage gap: selected ABC root has no IGES files; direct IGES import sanity passed with a generated roundtrip IGES |

## Candidate Bugs And Gaps

### STEP Import: complex ABC files rejected

- Lane: `abc_sample_smoke/top_complex_import`
- Cases: 48 total, 19 failed
- Fingerprint: `b47a7dce1aac5728`
- Representative: `00050037_d0eaf6069a7c4435b69edcd4_step_005_66704ac597`
- API: `step_import`
- Error code: `553648137`
- Error message: `Not accepted type!`
- Validation failure: `result_body_count_below_min actual=0 min=1`
- Interpretation: SDK/API import failure, not a harness data-binding issue. `iface_02_step_import_abc_complex` also reproduced this family after the seed step rebound the placeholder to a real ABC STEP file.

Next action: replay a representative case, reduce or bundle the input if allowed, and compare with another STEP reader if available before promoting a persistent bug record.

### ABC recut boolean: tangent cylinder subtraction rejected

- Lane: `abc_sample_smoke/top_complex_recut_run`
- Cases: 24 total, 3 failed
- Representative source body: `result_1_e2d323c7`
- API: `api_boolean`
- Failed variants:
  - `abc_sample_recut_result_1_e2d323c7_cylinder_tangent_x_subtraction_exact_125e95b894`
  - `abc_sample_recut_result_1_e2d323c7_cylinder_tangent_x_subtraction_gap_geom_tol_d55755149e`
  - `abc_sample_recut_result_1_e2d323c7_cylinder_tangent_x_subtraction_overlap_geom_tol_ea89274cc4`
- Error code: `301989891`
- Error message: `wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now.`
- Validation failure: `result_body_count_below_min actual=0 min=1`
- Interpretation: SDK/API boolean failure on an imported corpus body plus generated tangent cutter. The exact/gap/overlap trio makes this a good source-guided tolerance candidate.

Next action: export a failure bundle or promote a draft bug record only after copying the minimal durable SGT input into a portable fixture root.

### Sweep/extrude generated-topology intersection: missing PCurve

- Lane: `model_runs/iface_04_sweep_circle_line_boolean`
- Cases: 30 total, 2 failed
- Failed cases:
  - `real_chain_sweep_extrude_side_int_gap_geom`
  - `real_chain_sweep_extrude_side_int_overlap_geom`
- API: `api_boolean`
- Error code: `83886104`
- Error message: `Coedge has no PCurve to calc nominal curve`
- Interpretation: source-guided generated-topology boolean failure near exact/geom tolerance boundary. The target is a sweep-origin face; the tool is an extrude side tolerance placement.

Next action: replay these two DSL-derived flat recipes and run reduction or topology-crash probing if the failure changes with topo tracking.

### Revolve-rect boolean: SDK success with failed result oracle

- Lane: `model_runs/iface_08_revolve_boolean`
- Cases: 10 total, 2 failed
- Failed cases:
  - `revolve_rect_chain_side_cutter_int_overlap_topo`
  - `revolve_rect_chain_side_cutter_sub_overlap_topo`
- API: `api_boolean`
- SDK status: `succeeded=true`, `error_code=0`
- Validation failures:
  - `boolean_intersection_volume_exceeds_input`
  - `result_body_count_below_min actual=0 min=1`
- Interpretation: validation-only candidate. It might be a real modeling bug or an oracle/geometry expectation issue. Do not file as an SDK API failure until replay/reduction confirms the oracle.

Next action: replay with preview and geometry audit, then reduce the flat recipe while preserving the failing oracle.

### IGES import: interface sanity passes, current ABC root lacks IGES files

- Lane: `model_runs/iface_14_iges_import_abc_complex`
- API: `iges_import`
- Error message: `IGES file read error: artifacts/abc_fetch_smoke/examples/top_complex_001.igs`
- Direct sanity evidence: `manual_iges_import_from_roundtrip_v2` imported `api_smoke_suite/iges_roundtrip_smoke/output/roundtrip.iges` with 1 executed, 1 passed, 0 failed.
- Interpretation: data coverage gap. The 40chunk fetch root contains STEP only, so the seeding step kept the placeholder and recorded a note. This should not be treated as an SDK bug or as evidence that `iges_import` itself is broken.

Next action: fetch or point to an IGES-capable corpus, then rerun `iface_14` or an IGES-specific `run_corpus.py` lane.

## Source Task Summary

The source scan over the SDK include tree generated 80 source attack tasks:

- `generated_topology_boolean`: 40
- `boolean_tolerance_band`: 38
- `large_coordinate_tolerance`: 2

Severity counts:

- critical: 3
- high: 18
- medium: 59

These tasks are model inputs, not bug reports. The intranet model should read each cited source excerpt, adjust geometry/oracles, emit `cluster_seed` or `attack_dsl`, and then run the standard static and Windows SDK gates.

## Small-Model Distillation Notes

- A green workflow exit code means reports were generated; it does not mean all recipes passed.
- Prefer `triage_summary.json` and `triage_report.md` over raw logs for bug grouping.
- Separate SDK/API failures, validation-only failures, data-binding gaps, corpus-format gaps, and harness-extension requests.
- Keep ABC/source artifacts local under `artifacts/`; commit only portable recipes, forms, skills, or reviewed bug records after portability audit.
- For future IGES work, do not reuse a STEP-only ABC fetch root.
