# Interface Generated Ops And ABC Recut Report

- Date: 2026-07-07
- Branch: `codex/abc-dataset-harness`
- Baseline commit before this report: `5f56bca`
- SDK label: `SGK1.4.10`
- Windows checkout: `C:\Develop\SGGK_Test_SDK`
- Runner: `C:\Develop\SGGK_Test_SDK\build\test_harness\Release\sggk_case_runner.exe`
- ABC root: `C:\Develop\SGGK_Agent\artifacts\abc_fetch_40chunk_sample50`

This report continues the interface-distillation workflow after the ABC boolean
mass-recut run. It covers generated modeling interfaces and validation oracles
through the fixed harness path:

```text
developer/source form -> DSL or generated recipes -> SDK run -> triage -> regression asset
```

No SDK source, SDK binaries, ABC files, generated recipes, or Windows artifacts
are committed here. Those remain under Windows `artifacts\`.

## Runs

| Run | Scope | Executed | Passed | Candidate bugs | Known unsupported | Asset |
|---|---|---:|---:|---:|---:|---|
| `iface_api_smoke_suite_20260707` | All current smoke recipes: booleans, body builders, oracles, check_sgt, STEP/IGES roundtrip | 16 | 16 | 0 | 0 | `artifacts\regression_assets\iface_api_smoke_suite_20260707` |
| `iface_generated_ops_source_guided_20260707` | Source-guided DSL clusters for sweep, support-sweep, extrude, thicken, revolve, and pre-boolean chains | 66 | 62 | 4 | 0 | `artifacts\regression_assets\iface_generated_ops_source_guided_20260707` |
| `iface_abc_recut_generated_tools_standard_20260707` | ABC imported SGT targets recut by cylinder, sweep, and extrude tools | 240 | 236 | 4 | 0 | `artifacts\regression_assets\iface_abc_recut_generated_tools_standard_20260707` |

The smoke suite is a baseline connectivity monitor. The two larger runs are bug
discovery and skill-distillation samples.

## Commands

API smoke suite:

```powershell
python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe-list .\test_harness\suites\api_smoke_suite.txt `
  --out .\artifacts\iface_api_smoke_suite_20260707 `
  --timeout 180 `
  --jobs 1 `
  --resume `
  --resume-mode completed `
  --hash-recipes `
  --triage-out .\artifacts\iface_api_smoke_suite_20260707\triage `
  --triage-include-passed
```

Source-guided generated-operation cluster:

```powershell
$root = '.\artifacts\iface_generated_ops_source_guided_20260707'
$dsls = @(
  '.\test_harness\dsl\paired_sweep_smoke.json',
  '.\test_harness\dsl\complex_surface_sweep_boolean_smoke.json',
  '.\test_harness\dsl\real_chain_tolerance_smoke.json',
  '.\test_harness\dsl\thicken_chain_smoke.json',
  '.\test_harness\dsl\revolve_chain_smoke.json',
  '.\test_harness\dsl\revolve_rect_chain_smoke.json',
  '.\test_harness\dsl\operation_chain_smoke.json'
)
python .\test_harness\tools\compile_attack_dsl.py $dsls --check --report "$root\dsl_check_report.json"
python .\test_harness\tools\compile_attack_dsl.py $dsls --out "$root\recipes" --report "$root\dsl_compile_report.json"
python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe "$root\recipes" `
  --out "$root\run" `
  --timeout 180 `
  --jobs 2 `
  --resume `
  --resume-mode completed `
  --hash-recipes `
  --triage-out "$root\triage" `
  --triage-include-passed
```

ABC imported-SGT generated-tool recut:

```powershell
$root = '.\artifacts\iface_abc_recut_generated_tools_standard_20260707'
$sgtList = '.\artifacts\iface_check_sgt_abc_imported_sgt_top300.paths.txt'
python .\test_harness\tools\generate_corpus_recut_matrix.py `
  --dataset-list $sgtList `
  --out "$root\recipes" `
  --manifest "$root\recipes_manifest.json" `
  --preset standard `
  --source-limit 8 `
  --limit 240 `
  --case-prefix iface_abc_recut_generated_tools `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --probe-out "$root\exact_bbox_probes"
python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe "$root\recipes" `
  --out "$root\run" `
  --timeout 180 `
  --jobs 2 `
  --resume `
  --resume-mode completed `
  --hash-recipes `
  --triage-out "$root\triage" `
  --triage-include-passed
```

Regression assets were created from the campaign root, not the `run` subfolder,
so `manage_regression_assets.py` could see both `run\recipe_summary.json` and
`triage\triage_summary.json`.

## Source-Guided Generated Ops

Compilation checked and emitted 66 flat recipes from seven DSL files. The cases
cover:

- `support_sweep_bspline_surface`: 3/3 passed
- thicken chain: 5/5 passed
- pre-boolean chain: 6/6 passed
- extrude/sweep and sweep/extrude tolerance chains: 23/25 passed
- revolve line: 10/10 passed
- revolve rect: 8/10 passed
- operation-chain smoke cases: 7/7 passed

Candidate bug groups:

| Fingerprint | Case | Family | Failure |
|---|---|---|---|
| `2ca03389cdd0aa4c` | `real_chain_sweep_extrude_side_int_overlap_geom` | sweep target, extrude tool | SDK API failed, error code `83886104`, `coedge has no pcurve to calc nominal curve` |
| `96ab3b9a767f9847` | `real_chain_sweep_extrude_side_int_gap_geom` | sweep target, extrude tool | SDK API failed, error code `83886104`, `coedge has no pcurve to calc nominal curve` |
| `e429e37b2b2cb8ca` | `revolve_rect_chain_side_cutter_int_overlap_topo` | closed-profile revolve | SDK returned success, validation failed: `boolean_intersection_volume_exceeds_input` |
| `fb878dadf7126e4e` | `revolve_rect_chain_side_cutter_sub_overlap_topo` | closed-profile revolve | SDK returned success, validation failed: `result_body_count_below_min actual=0 min=1` |

Interpretation:

- The two sweep/extrude failures are API-status candidates. They should be
  replayed and reduced before promotion.
- The two revolve-rect failures are validation-only candidates. They may be real
  modeling bugs or expectation/oracle issues; replay with preview and geometry
  audit before filing an SDK bug.
- No generated-operation failure was classified as explicit unsupported.

## ABC Recut Generated Tools

Generation used eight imported ABC SGT bodies from the frozen SGT-only list
`artifacts\iface_check_sgt_abc_imported_sgt_top300.paths.txt`. Exact bbox probes
ran through the SDK runner for all eight sources, with zero probe failures.

Recipe families:

| Tool family | Executed | Passed | Candidate bugs |
|---|---:|---:|---:|
| `cylinder_tangent_x` | 80 | 77 | 3 |
| `sweep_tangent_x` | 80 | 79 | 1 |
| `extrude_center_slab` | 80 | 80 | 0 |

Candidate bug groups:

| Fingerprint | Case | Tool family | Failure |
|---|---|---|---|
| `1804da8b44c9f606` | `iface_abc_recut_generated_tools_result_1_dfcdc08f_sweep_tangent_x_intersection_overlap_topo_tol_2945b84a1a` | sweep | validation failed: `result_body_count_below_min actual=0 min=1` |
| `c07ffa6f32f34957` | `iface_abc_recut_generated_tools_result_1_0996b0f1_cylinder_tangent_x_subtraction_gap_geom_tol_60c7dc158d` | cylinder | validation failed: `result_body_count_below_min actual=0 min=1` |
| `db37e25aa33fb97c` | `iface_abc_recut_generated_tools_result_1_dfcdc08f_cylinder_tangent_x_intersection_overlap_topo_tol_718bee4511` | cylinder | validation failed: `result_body_count_below_min actual=0 min=1` |
| `e1ee2d50162de9bc` | `iface_abc_recut_generated_tools_result_1_0996b0f1_cylinder_tangent_x_subtraction_gap_topo_tol_b588fd36bf` | cylinder | validation failed: `result_body_count_below_min actual=0 min=1` |

Interpretation:

- The generated sweep tool found one ABC-body candidate where an expected
  non-empty overlap produced zero result bodies.
- The generated extrude slab lane passed all 80 cases and is a good stable
  regression monitor for this subset.
- These failures are not explicit unsupported responses. They need focused
  replay/reduction to determine whether the oracle is too strict or the boolean
  result is incorrectly empty.

## Related Direct-API Evidence

The same Windows checkout also holds direct corpus assets from the previous
interface pass:

| Run | Executed | Passed | Candidate bugs | Known unsupported | Notes |
|---|---:|---:|---:|---:|---|
| `iface_step_import_abc_complex_top200` | 200 | 161 | 10 | 29 | `Not accepted type!` is treated as unsupported, not a bug |
| `iface_check_sgt_abc_imported_sgt_top300` | 300 | 300 | 0 | 0 | Use an explicit SGT-only path list; do not point `check_sgt` at mixed import directories |
| `iface_step_roundtrip_abc_imported_sgt_top100` | 100 | 98 | 2 | 0 | Both failures are roundtrip area/volume drift candidates |

This means the current reusable asset set now covers:

- API smoke connectivity
- STEP import over top-complex ABC files
- imported SGT replay/check
- STEP roundtrip drift
- ABC boolean recut
- generated sweep/support-sweep/extrude/thicken/revolve/pre-boolean source-risk clusters
- ABC imported-body recut with generated cylinder, sweep, and extrude tools

## Distillation Notes

- For a Message API small-model workflow, keep generated DSL compact and rely on fixed
  code for expansion. This run compiled 7 DSL files into 66 recipes without the
  model seeing the whole repository or artifacts.
- For ABC recut, freeze path lists. The pure `check_sgt` lane must use an
  SGT-only list; mixed directories can accidentally include `input\source.step`
  and pollute the interface conclusion.
- For regression snapshots, pass the campaign root containing both `run\` and
  `triage\`. Passing only `run\` preserves failures but loses candidate/known
  unsupported classification.
- Treat validation-only failures as candidates, not final SDK bugs, until replay,
  geometry audit, and reduction confirm that the expectation is correct.
- Keep the heavy recipe and SGT assets local under `artifacts\`; commit only the
  workflow, forms, skill docs, and reports.

## Next Actions

1. Replay the four source-guided generated-operation candidates with preview and
   geometry audit; reduce stable sweep/extrude PCurve failures first.
2. Replay the four ABC generated-tool recut candidates and check whether
   `result_body_count_below_min` is a true empty-result modeling bug or an
   expectation that should permit zero bodies for the specific contact band.
3. Increase ABC generated-tool recut from `source-limit 8` to a shardable larger
   plan after the above oracle audit, especially for `sweep_tangent_x`.
4. Add a dedicated IGES corpus before treating IGES import as covered beyond the
   current smoke/roundtrip sanity lane.
