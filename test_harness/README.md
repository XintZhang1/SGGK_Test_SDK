# SGGK test harness

This is the first standalone harness layer for SDK-driven tests. It intentionally avoids the vendor sample layout and writes an artifact capsule for each case.

## Build

```powershell
cmake -S .\test_harness -B .\build\test_harness `
  -DSGGK_SDK_DIR="C:/Develop/SGGK_Agent/SGK1.4.10/SGGK" `
  -G "Visual Studio 18 2026" `
  -A x64

cmake --build .\build\test_harness --config Release --parallel
```

The build copies runtime DLLs and `sggk.lic` next to `sggk_case_runner.exe`.
It also builds `sggk_topology_extract.exe`, a small GUI-handoff helper that reopens an input `.sgt` and exports a selected Body/Face/Edge/Vertex/Wire/Shell/Lump/Coedge by topology type plus ID and/or local index.

## Run

```powershell
.\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\test_harness\recipes\boolean_smoke.json `
  --out .\artifacts
```

The runner creates:

```text
artifacts/boolean_smoke/
  manifest.json
  input/target.sgt
  input/tool.sgt
  output/result_*.sgt
  output/error_entity_*.sgt
  output/topo_check_error_*.sgt
  report/status.json
  report/topo_check.json
  report/topo_track.json
  report/input_provenance.json
  report/input_topology_index.json
  report/topo_track_summary.json
  report/properties.json
  report/validation.json
  report/debug_geometry_index.json
  debug_geometry/*.sgt
  report/preview.png
```

The current recipe parser supports a deliberately small flat JSON shape. This keeps the first runner dependency-free while we stabilize the case schema. For `api_boolean`, `target_kind` and `tool_kind` can be direct primitives or operation-built bodies:

- primitives: `solid_cylinder`, `solid_wedge`, `solid_sphere`, `solid_cone`, `solid_torus`
- operation bodies: `extrude_rect`, `thicken_rect_sheet`, `sweep_circle_line`, `support_sweep_bspline_surface`, `revolve_line`, `revolve_rect`, `pre_boolean_cylinder_wedge`
- replay/corpus body reuse: `loaded_sgt` with `{target,tool}_source_file` and optional `{target,tool}_body_index`

The operation body builders let smoke and model-generated cases attack booleans on geometry produced by extrude, sheet thicken, straight sweep, BSpline support-face sweep, revolve, and an earlier boolean, not just clean primitives.

Validate recipes before running generated cases:

```powershell
python .\test_harness\tools\validate_recipe.py .\test_harness\recipes
```

## Result Validation

`ret.Succeeded()` is not treated as the only oracle. Every case writes `report/properties.json`, boolean cases also write `report/input_properties.json`, and every case writes `report/validation.json`; the runner returns a failing exit code when validation fails even if the SDK API and TopoCheck both report success.

Current default scale assumptions for generated SGGK cases:

- topology/modeling tolerance: `1e-2`
- pure geometry tolerance, for example geometry intersection without topology construction: `1e-5`
- maximum intended modeling coordinate/size: `5e5`

Default validation checks:

- at least one result body unless `min_result_bodies` is overridden
- property calculations succeeded
- result length, area, and volume are finite
- result length and area are nonnegative

`CalcBndBox` output from the SDK is a conservative diagnostic bbox, not an exact geometric oracle. The optional `boolean_bbox_relation` check is therefore reported as a skipped/diagnostic check and does not fail a case. Use volume/area/length, point/face/body relation, clash, distance, exact plane-extreme checks, TopoCheck, or roundtrip comparisons for hard real-result validation.

`plane_extreme_checks` compute exact min/max coordinates without trusting SDK bbox values as the oracle. By default the runner creates a coordinate-plane face at `-max_model_size` for `side=min` or `+max_model_size` for `side=max`, calls `api_topo_minimum_distance(plane_face, body)`, and derives `actual_extreme = probe_min + distance` or `probe_max - distance`. The conservative bbox is only used to center and size the finite probe face unless `plane_span` is explicit. Normal oracle mode compares the derived exact coordinate against `expected`; measurement mode sets `compare_expected:false`, omits `expected`, and records the derived coordinate without producing a metric mismatch.

Recipes can add explicit result oracles:

```json
"expectations": {
  "result_bodies": {"min": 1, "max": 1},
  "sample_input_properties": true,
  "boolean_volume_relation": true,
  "total_abs_volume": {"min": 1000.0, "max": 20000000.0, "abs_tol": 0.01},
  "total_area": {"expected": 385184.28459686966, "rel_tol": 1e-6},
  "point_relations": [
    {
      "id": "result_center_inside",
      "role": "result",
      "body_index": 0,
      "point": [0.0, 0.0, 0.0],
      "expected": "Inside",
      "tolerance": 0.01,
      "check_boundary": true
    }
  ],
  "face_point_relations": [
    {
      "id": "target_face_center_inside",
      "role": "target",
      "body_index": 0,
      "face_index": 0,
      "uv_fraction": [0.5, 0.5],
      "expected": "Inside",
      "tolerance": 0.01,
      "check_boundary": true
    }
  ],
  "clash_checks": [
    {
      "id": "result_tool_far_apart",
      "role_a": "result",
      "body_index_a": 0,
      "role_b": "tool",
      "body_index_b": 0,
      "expected": "NoClash",
      "mode": "ClashExistenceOnly",
      "tolerance": 0.01
    }
  ],
  "distance_checks": [
    {
      "id": "result_tool_clearance",
      "role_a": "result",
      "body_index_a": 0,
      "role_b": "tool",
      "body_index_b": 0,
      "kind": "minimum",
      "threshold": 100.0,
      "distance": {"expected": 38.0, "abs_tol": 0.00001}
    }
  ],
  "plane_extreme_checks": [
    {
      "id": "result_x_max",
      "role": "result",
      "body_index": 0,
      "axis": "x",
      "side": "max",
      "expected": 10.0,
      "tolerance": 0.02
    }
  ]
}
```

For boolean cases, input bodies are sampled for bbox by default but not for full length/area/volume; that avoids turning broad batch lanes into input-property crash probes. Set `sample_input_properties: true` in `expectations` for stable solid cases that need a real boolean volume-relation oracle. When sampling is off, `validation.json` records `boolean_volume_relation_skipped_missing_input_properties` under `skipped_checks`; when sampling is requested but input properties are still unavailable, validation fails with `boolean_volume_relation_input_property_unavailable`.

`point_relations` calls `PtBodyRelation` on `result`, `target`, or `tool` bodies. Supported expected values are `Unknown`, `OnVertex`, `OnEdge`, `OnFace`, `Inside`, `Outside`, plus `OnBoundary` and `OnModel` convenience groups. The runner records actual point-relation details in `report/validation.json`.

`face_point_relations` calls `FacePtRelation` on a selected face from a `result`, `target`, or `tool` body. Select faces with `face_index` or `face_id`; provide `uv`, `uv_fraction`, or a 3D `point`. `uv_fraction` samples inside `face.CalcUVBound()`, and the runner records actual UV, computed 3D point, face id, and located edge/vertex in `report/validation.json`.

`clash_checks` calls `api_body_clash` on `result`, `target`, or `tool` body pairs. Supported expected values are exact `ClashType` names plus `NoClash` and `AnyClash`; modes are `ClashExistenceOnly`, `ClashClassify`, and `ClashClassifySubEntities`. The runner records actual clash type and up to eight sub-topology clash pairs in `report/validation.json`.

`distance_checks` calls `api_topo_minimum_distance` or `api_topo_maximum_distance` on `result`, `target`, or `tool` body pairs. Checks support the same numeric `min`, `max`, `expected`, `abs_tol`, and `rel_tol` fields used by total length/area/volume oracles. The runner records actual distance, success state, closest points, and located topology in `report/validation.json`.

`plane_extreme_checks` calls `api_topo_minimum_distance` from a generated coordinate-plane face to a `result`, `target`, or `tool` body and records `compare_expected`, `probe_coordinate`, `probe_coordinate_source`, `max_model_size`, `actual_extreme`, closest points, located topology, and optional debug geometry in `report/validation.json`.

For a standalone exact bbox measurement of SGT inputs, use `probe_exact_bbox.py`. It generates six `compare_expected:false` plane-extreme probes per SGT and writes `exact_bbox.json` / `exact_bbox.md`:

```powershell
python .\test_harness\tools\probe_exact_bbox.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --source .\artifacts\boolean_smoke\output\result_1.sgt `
  --out .\artifacts\exact_bbox_probe_smoke
```

Supported validation failures now write `debug_geometry/*.sgt`, indexed by `report/debug_geometry_index.json`, so the failing body, selected face, clash pair, distance-hit topology, or coordinate probe plane can be opened in the SDK GUI. The negative smoke recipes demonstrate this path: `test_harness/recipes/boolean_oracle_fail_smoke.json` fails on an impossible volume bound, while `test_harness/recipes/plane_extreme_oracle_fail_smoke.json`, `distance_check_oracle_fail_smoke.json`, `clash_check_oracle_fail_smoke.json`, `point_relation_oracle_fail_smoke.json`, and `face_point_relation_oracle_fail_smoke.json` write debug SGT assets for GUI inspection.

For modeling failures that do not produce `debug_geometry`, `build_debug_handoff.py` can use `sggk_topology_extract.exe` to export the primary target/tool contact candidate as `focus/*.sgt` from the copied input bodies. Each pack also writes `visual_index.json` / `visual_index.md` for all copied debug/focus/input SGTs and `focus_index.json` / `focus_index.md` for primary-contact extraction status, tying those files back to the source SGT, topology type/id/local index, locator, check ID, status, and GUI-open path. The helper is auto-detected under `build/test_harness/<config>/`, or can be passed explicitly with `--topology-extractor`.

When a recipe lane crashes only after result/topo-check output and before validation, use `probe_topotrack_crashes.py` to test whether the failure is topotrack-only. The tool reads a `run_recipes.py` summary, copies selected `topo_track=true` recipes with `topo_track=false`, replays them, and classifies cases such as `topotrack_only_modeling_ok` versus `modeling_or_validation_failure_after_topotrack_disabled`:

```powershell
python .\test_harness\tools\probe_topotrack_crashes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --summary .\artifacts\some_recipe_lane\recipe_summary.json `
  --out .\artifacts\topotrack_probe_some_recipe_lane
```

Use `topotrack_only_modeling_ok` as diagnostic evidence, not as a modeling bug by itself.

## Preview Screenshots

Generated tests should be visually checked, especially tolerance sweeps where tiny offsets can accidentally collapse into duplicate cases. Render previews from case artifacts:

```powershell
python .\test_harness\tools\render_case_preview.py `
  .\artifacts\native_chain_run `
  --out-dir .\artifacts\native_chain_previews `
  --contact-sheet .\artifacts\native_chain_previews\contact.png
```

The preview is a deterministic artifact screenshot, not a full SDK GUI render. It draws target/tool/result bounding boxes, selected input edge locators, printed bbox snapshots, property totals, and validation status. For micro-tolerance cases, trust the printed bbox snapshots and signature hash in addition to the visual overlap; use `plane_extreme_checks` when the exact min/max coordinate itself is the oracle.

For generated tolerance families, add a geometry audit after rendering. It hashes input/result geometry, reports same-boolean duplicate inputs, and checks variant names such as `overlap_geom`, `exact`, and `gap_topo` against the actual signed target/tool clearance. When `--exact-bbox-runner` is supplied, the audit probes target/tool SGTs with generated coordinate-plane distance checks and uses those exact extrema instead of conservative SDK bbox snapshots. `run_recipes.py` passes its runner to the audit automatically.

```powershell
python .\test_harness\tools\audit_case_geometry.py `
  .\artifacts\native_chain_run `
  --out .\artifacts\native_chain_geometry_audit `
  --exact-bbox-runner .\build\test_harness\Release\sggk_case_runner.exe `
  --fail-on-duplicates `
  --fail-on-tolerance-mismatch
```

`run_recipes.py` can run this automatically with `--geometry-audit-out <dir>`, optionally combined with `--geometry-audit-fail-on-duplicates` and `--geometry-audit-fail-on-tolerance-mismatch`.

Generate the dedicated tolerance-band smoke previews:

```powershell
.\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\test_harness\dsl\tolerance_band_smoke.json `
  --out .\artifacts\tolerance_band_native_run

python .\test_harness\tools\render_case_preview.py `
  .\artifacts\tolerance_band_native_run `
  --out-dir .\artifacts\preview_tolerance_band `
  --contact-sheet .\artifacts\preview_tolerance_band\contact.png
```

`tolerance_band_smoke.json` sweeps near-tangent primitive, generated-extrude, and large-coordinate boolean cases across exact contact, `+/- 1e-5`, and `+/- 1e-2`. It currently keeps `topo_track=false` because the SDK topo-track query can crash on these near-tangent cases after a successful boolean/topology check. Use these cases as the visual/oracle lane first; rerun a reduced stable failure with topo tracking when localization is needed.

## Attack DSL

Small-model generated attacks should use the JSON DSL first. The runner can execute supported DSL files directly:

```powershell
.\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\test_harness\dsl\operation_chain_smoke.json `
  --out .\artifacts\native_chain_run
```

For debugging or compatibility, the same DSL can still be compiled to flat runner recipes:

```powershell
python .\test_harness\tools\compile_attack_dsl.py `
  .\test_harness\dsl\composite_attack_smoke.json `
  --check `
  --report .\artifacts\dsl_checks\composite_attack_smoke_check.json

python .\test_harness\tools\compile_attack_dsl.py `
  .\test_harness\dsl\composite_attack_smoke.json `
  --out .\artifacts\compiled_dsl_recipes

python .\test_harness\tools\validate_recipe.py .\artifacts\compiled_dsl_recipes
```

Use `--check` as the first gate for model-generated DSL. It expands variants, sweeps, chains, constants, and oracle expressions, then validates the resulting flat recipes without writing them. Keep `--report` outside the compiled recipe directory so `validate_recipe.py <compiled-dir>` sees only recipe JSON files.

The DSL supports:

- numeric constants and simple arithmetic expressions, including built-in `pi` and `tau`
- `defaults` for common runner options
- `target` and `tool` body builders
- `chain` bodies for supported parameterized modeling steps
- stable operation `id` fields for provenance and topology-tracking reports
- `key_points` plus `point_ref` inside point/body or point/face expectations, so reused critical points are named once and compiled to flat `point` coordinates
- `translate` / `secondary_translate` vector shorthand
- `variants` and tolerance `sweeps` that expand one source hypothesis into nearby cases

Supported chain mappings today:

- `rect_profile -> extrude -> transform` compiles to `extrude_rect`
- `rect_profile -> thicken -> transform` compiles to `thicken_rect_sheet`
- `circle_profile -> sweep_line -> transform` compiles to `sweep_circle_line`
- `support_sweep -> transform` compiles to `support_sweep_bspline_surface`
- `line_profile -> revolve -> transform` compiles to `revolve_line`
- `radial_rect_profile -> revolve -> transform` compiles to `revolve_rect`
- `primitive(solid_cylinder) -> boolean(SUBTRACTION, primitive/transform solid_wedge)` compiles to `pre_boolean_cylinder_wedge`
- `load_sgt -> transform` loads an existing `.sgt` corpus or failure body for reuse in an attack

Run compiled DSL recipes the same way as hand-written recipes:

```powershell
.\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\artifacts\compiled_dsl_recipes\dsl_extrude_cap_cut_flush.json `
  --out .\artifacts\compiled_dsl_run
```

Compile and run the operation-chain smoke:

```powershell
python .\test_harness\tools\compile_attack_dsl.py `
  .\test_harness\dsl\operation_chain_smoke.json `
  --out .\artifacts\compiled_chain_recipes
```

Native DSL runs write `report/input_provenance.json` and `report/input_topology_index.json`, then include DSL/provenance metadata in `manifest.json` and `report/topo_track.json`.
Compiled flat recipes also preserve `dsl_source`, `dsl_case_id`, `dsl_variant`, `target_operations`, and `tool_operations`, so process-isolated `run_recipes.py` lanes still retain operation-chain localization.
The topology index records target/tool topology IDs, local indices, operation chains, and geometry locators: vertex points, edge endpoints/lengths, face areas, and bounding boxes where available.
Boolean runs also write `report/topo_track_summary.json` with track-type counts, topology-type counts, resolved ancestor counts, and target/tool ancestor-role counts for quick triage.

Additional smoke recipes:

```powershell
.\build\test_harness\Release\sggk_case_runner.exe --recipe .\test_harness\recipes\boolean_extrude_rect_smoke.json --out .\artifacts\builder_smoke
.\build\test_harness\Release\sggk_case_runner.exe --recipe .\test_harness\recipes\boolean_sweep_smoke.json --out .\artifacts\builder_smoke
.\build\test_harness\Release\sggk_case_runner.exe --recipe .\test_harness\recipes\boolean_support_sweep_bspline_surface_smoke.json --out .\artifacts\builder_smoke
.\build\test_harness\Release\sggk_case_runner.exe --recipe .\test_harness\recipes\boolean_bbox_diagnostic_smoke.json --out .\artifacts\builder_smoke
.\build\test_harness\Release\sggk_case_runner.exe --recipe .\test_harness\recipes\plane_extreme_sphere_smoke.json --out .\artifacts\builder_smoke
.\build\test_harness\Release\sggk_case_runner.exe --recipe .\test_harness\recipes\boolean_revolve_line_smoke.json --out .\artifacts\builder_smoke
.\build\test_harness\Release\sggk_case_runner.exe --recipe .\test_harness\recipes\boolean_revolve_rect_smoke.json --out .\artifacts\builder_smoke
.\build\test_harness\Release\sggk_case_runner.exe --recipe .\test_harness\recipes\boolean_preboolean_smoke.json --out .\artifacts\builder_smoke
```

The thicken operation-chain smoke lives in `test_harness/dsl/thicken_chain_smoke.json`. Compile it with `compile_attack_dsl.py --check` or run it through `run_recipes.py` like the other DSL smoke lanes; it uses `rect_profile -> thicken` to call `api_thicken_body` before the outer boolean and validates the result with property, point/body relation, distance, and exact plane-extreme checks.

## Source Risk Scan

For source-directed attacks, run the deterministic scanner before asking a model to write DSL:

```powershell
python .\test_harness\tools\scan_source_risks.py `
  .\SGK1.4.10\SGGK\include `
  --out .\artifacts\sdk_include_source_risk_scan `
  --max-findings 120 `
  --max-seeds 30
```

It writes:

- `source_risk_report.json`: machine-readable findings with source file/line, risk categories, numeric literals, suggested APIs, and oracles
- `source_risk_report.md`: compact human review report
- `source_risk_files.txt`: unique source files that need inspection
- `attack_seed_drafts.json`: review-required DSL seed drafts for a smaller model or Codex to refine into runnable attacks

Build small-model task packs from a scan report when preparing Qwen/internal-model inference or distillation data:

```powershell
python .\test_harness\tools\build_source_attack_tasks.py `
  .\artifacts\sdk_include_source_risk_scan `
  --out .\artifacts\sdk_include_source_attack_tasks `
  --max-tasks 80 `
  --context-lines 12 `
  --write-dsl-seeds
```

This writes `source_attack_tasks.json`, `source_attack_tasks.jsonl`, `source_attack_task_manifest.md`, `source_attack_task_ids.txt`, and optional `seed_dsl/*.json` files. Each task includes a wider source excerpt, the scanner finding, the model prompt, required output contract, harness constants, suggested post-generation checks, and an optional review-required DSL seed.

The seed drafts are intentionally not bug reports. Read the cited source, adjust exact geometry and expected values, then run `compile_attack_dsl.py --check --report <report.json>` before compiling or running the reviewed DSL. For exact coordinate extrema, add `expectations.plane_extreme_checks` only after choosing a concrete variant and expected coordinate; the runner will derive the actual min/max with coordinate-plane distance probes rather than trusting the conservative SDK bbox.

A checked source-directed smoke lives at `test_harness/dsl/source_directed_scan_smoke.json`. It was derived from SDK header findings in `GeomBase/Toler.h`, `GeomInt/Extrema/*`, and `Topology/Tools/PtFaceRelation.h`, and expands into large-coordinate, fuzzy/exact overlap, and FacePtRelation/point-relation probes:

```powershell
python .\test_harness\tools\compile_attack_dsl.py `
  .\test_harness\dsl\source_directed_scan_smoke.json `
  --out .\artifacts\compiled_source_directed_scan_smoke_v2

python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\artifacts\compiled_source_directed_scan_smoke_v2 `
  --out .\artifacts\source_directed_scan_smoke_run_v2 `
  --timeout 90 `
  --jobs 2 `
  --triage-out .\artifacts\source_directed_scan_smoke_triage_v2 `
  --triage-include-passed `
  --preview-out .\artifacts\source_directed_scan_smoke_preview_v2 `
  --contact-sheet .\artifacts\source_directed_scan_smoke_preview_v2\contact.png `
  --geometry-audit-out .\artifacts\source_directed_scan_smoke_geometry_audit_v2
```

Verified result: 17/17 recipes passed, triage found no failure groups, and geometry audit reported no duplicate inputs or tolerance mismatches. The audit confirmed signed clearances at exact contact, `+/- 1e-5`, and `+/- 1e-2`; the contact sheet is `artifacts/source_directed_scan_smoke_preview_v2/contact.png`.

## Generated Recipe Lanes

For larger generated attacks, compile DSL into one flat recipe per case and run them with process isolation. This prevents one crash or timeout from taking down the whole generated lane.

Generate a baseline OCC-style boolean matrix:

```powershell
python .\test_harness\tools\generate_boolean_matrix.py `
  --out .\artifacts\generated_boolean_matrix_smoke `
  --preset smoke `
  --case-prefix occ_smoke
```

The generator writes flat recipes and a sibling manifest such as `generated_boolean_matrix_smoke_manifest.json`. Presets:

- `smoke`: compact tolerance sweeps over cylinder/cylinder, generated extrude side faces, generated open-profile and closed-profile revolve topology, pre-boolean recuts, large-coordinate cylinders, and a sweep-generated/sweep-generated near-tangent lane.
- `standard`: adds more boolean types, multi-phase sweep/sweep near-tangencies, and additional primitive or generated-body pairs.
- `stress`: adds wider multi-phase sweep/sweep coverage and object-size scale bands.

For topology-building intersections, the generator allows empty results inside the modeling tolerance band. For example, exact tangency, positive gaps, and `-1e-5` micro-overlaps are not treated as non-empty intersection oracles when the topology tolerance is `1e-2`; a `-1e-2` overlap is.

The generated matrix is an attack lane. With the current SDK, the generated closed-profile `revolve_rect` side-cutter smoke can expose a validation-only failure at the `-1e-2` overlap boundary: subtraction reports SDK success and TopoCheck success but returns zero result bodies. Bbox-escape observations from this lane are conservative-bbox diagnostics only unless reproduced with an exact plane-extreme or other hard geometry oracle. Use triage/replay/reduce before filing or suppressing discoveries.

Run the real operation-chain attack lane when you want generated extrusion, sweep, and pre-boolean bodies rather than primitive-only tangencies:

```powershell
python .\test_harness\tools\compile_attack_dsl.py `
  .\test_harness\dsl\real_chain_tolerance_smoke.json `
  --out .\artifacts\compiled_real_chain_tolerance_smoke

python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\artifacts\compiled_real_chain_tolerance_smoke `
  --out .\artifacts\real_chain_tolerance_smoke_run `
  --timeout 90 `
  --jobs 2 `
  --triage-out .\artifacts\real_chain_tolerance_smoke_triage `
  --triage-include-passed `
  --preview-out .\artifacts\preview_real_chain_tolerance_smoke `
  --contact-sheet .\artifacts\preview_real_chain_tolerance_smoke\contact.png
```

This is an attack lane, not a guaranteed-green smoke. Current SDK behavior exposes two reproducible near-tangent `INTERSECTION` failures for `sweep_circle_line` target versus `extrude_rect` tool at `+/- 1e-5`, both with `Coedge has no PCurve to calc nominal curve`. Triage localizes them to the generated sweep-side target topology and generated extrusion-side tool topology via `input_contact_candidates`.

Run the revolve-chain side cutter lane when the source risk involves generated periodic/revolved topology:

```powershell
python .\test_harness\tools\compile_attack_dsl.py `
  .\test_harness\dsl\revolve_chain_smoke.json `
  --out .\artifacts\compiled_revolve_chain_smoke

python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\artifacts\compiled_revolve_chain_smoke `
  --out .\artifacts\revolve_chain_recipe_lane `
  --timeout 90 `
  --jobs 2 `
  --triage-out .\artifacts\revolve_chain_recipe_lane_triage `
  --triage-include-passed `
  --preview-out .\artifacts\preview_revolve_chain_recipe_lane `
  --contact-sheet .\artifacts\preview_revolve_chain_recipe_lane\contact.png
```

This lane is currently kept as coverage for generated open-profile revolve topology and near-boundary contacts. Historical bbox-escape observations from this lane are diagnostic-only because SDK bboxes are conservative; add `plane_extreme_checks` or another hard oracle before filing them as modeling bugs.

Run the closed-profile revolve lane when the source risk involves solid bodies produced by revolve:

```powershell
python .\test_harness\tools\compile_attack_dsl.py `
  .\test_harness\dsl\revolve_rect_chain_smoke.json `
  --out .\artifacts\compiled_revolve_rect_chain_smoke

python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\artifacts\compiled_revolve_rect_chain_smoke `
  --out .\artifacts\revolve_rect_chain_recipe_lane `
  --timeout 90 `
  --jobs 2 `
  --triage-out .\artifacts\revolve_rect_chain_recipe_lane_triage `
  --triage-include-passed `
  --preview-out .\artifacts\preview_revolve_rect_chain_recipe_lane `
  --contact-sheet .\artifacts\preview_revolve_rect_chain_recipe_lane\contact.png
```

This lane currently finds one stable validation-only hard failure at the same `-1e-2` boundary: subtraction returns zero result bodies after reporting success. The historical intersection bbox observation is diagnostic-only unless reproduced through exact plane-extreme or another non-bbox oracle. Triage localizes the actionable failure to generated closed-profile revolve faces such as `target Face#23[2] op=target_revolve_rect_origin` against the cylinder tool side face.

Run generated flat recipes:

```powershell
python .\test_harness\tools\compile_attack_dsl.py `
  .\test_harness\dsl\tolerance_band_smoke.json `
  --out .\artifacts\compiled_tolerance_band_recipes

python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\artifacts\compiled_tolerance_band_recipes `
  --out .\artifacts\tolerance_band_recipe_lane `
  --timeout 60 `
  --jobs 4 `
  --triage-out .\artifacts\tolerance_band_recipe_lane_triage `
  --triage-include-passed `
  --preview-out .\artifacts\preview_tolerance_band_recipe_lane `
  --contact-sheet .\artifacts\preview_tolerance_band_recipe_lane\contact.png
```

`run_recipes.py` supports `--resume`, `--resume-mode passed|completed`, `--shard-count/--shard-index`, `--limit`, `--fail-fast`, `--hash-recipes`, and `--recipe-list <txt>`. It writes `recipe_manifest.json` and `recipe_summary.json`. `triage_artifacts.py` reads `recipe_summary.json`, so runner crashes or timeouts without a case artifact still appear as command failures.

Record known bugs as version-regression assets:

```powershell
python .\test_harness\tools\export_bug_record_drafts.py `
  --triage .\artifacts\some_failed_lane_triage `
  --bundle-index .\artifacts\some_failed_lane_bundles `
  --out .\artifacts\bug_record_drafts\known_bug_drafts.json `
  --bug-prefix sggk_draft

python .\test_harness\tools\record_bug_cases.py `
  --records .\artifacts\bug_record_drafts\known_bug_drafts.json `
  --out .\artifacts\bug_records_reduced_revolve `
  --validate-recipes

python .\test_harness\tools\audit_bug_record_portability.py `
  --records .\test_harness\bug_records `
  --out .\artifacts\bug_record_portability_checked
```

`export_bug_record_drafts.py` turns triage groups and/or failure bundles into editable bug-record JSON. Review the generated `bug_id`, `title`, and notes before checking a record into `test_harness/bug_records`. Bug-record files can contain inline flat recipes, point at existing replay recipes, or provide DSL replay specs with `replay.dsl_path` / `replay.dsl_file` / inline `replay.dsl` plus a selector such as `case_id` or `dsl_case_id` + `dsl_variant`. The materializer compiles DSL replay specs and still writes one flat replay recipe per record, so `registry_replay_recipes.txt` remains directly usable by `run_recipes.py --recipe-list`. The materializer writes `bug_registry.json`, `bug_registry.md`, emitted recipe JSON files, and `registry_replay_recipes.txt`; pass either one JSON file or a directory of `*.json` files with `record_bug_cases.py --records`. See `test_harness/bug_record_examples/dsl_replay_materialize_smoke.json` for DSL-path and inline-DSL examples. Use `audit_bug_record_portability.py --records test_harness/bug_records --out <dir>` before committing reviewed records; it rejects absolute local paths and `artifacts/` dependencies, and allows durable repo paths under `test_harness/fixtures/bug_records`, `test_harness/dsl`, `test_harness/recipes`, and `SGK1.4.10/samples`. Use `validation_failures` for generic validation oracles, `roundtrip_failures` for STEP/IGES source/result comparison oracles, and `topo_track_policy: diagnostic_when_modeling_fails` for modeling-result bugs where missing or skipped topo tracking should be reported as diagnostic context instead of becoming the primary regression oracle. Drafts/registries preserve `localized_inputs` from topo tracking when available, so long-lived records can name the target/tool face, edge, or vertex most tied to the failure. Runner crash records preserve `runner.returncode` and `expected.returncode`, so regression checks can distinguish the same crash from a changed failure. Roundtrip/data-exchange records should keep the original `source_sgt`, `source_step`/`source_stp`, or `source_iges`/`source_igs` path when available, because this is the primary GUI/debug seed. Corpus-derived records should copy the minimal durable SGT inputs under `test_harness/fixtures/bug_records/<id>/` instead of pointing at temporary campaign artifacts. Current checked examples include `bug_records/reduced_revolve_validation.json`, `bug_records/step_roundtrip_bspline_volume_drift.json`, `bug_records/paired_sweep_exact_tangency_crash.json`, and `bug_records/freecad_corpus_recut_boolean_empty_result.json`.

Use `promote_bug_records.py` when campaign-local drafts should become portable review candidates. It loads each replay recipe, copies referenced replay assets such as SGT/STEP/IGES inputs into `test_harness/fixtures/bug_records/<bug-id>/`, rewrites replay paths to repo-relative fixture paths, strips transient GUI launcher paths, and keeps a compact observation that GUI handoff evidence existed. Run portability audit, materialization, replay, and regression classification before moving the promoted JSON/fixtures into the real `test_harness/bug_records` tree. For direct campaign outputs, `run_campaign.py --promote-bug-records --replay-promoted-bug-records` performs that candidate-root materialize/replay/classify loop under `promoted_bug_records/`; for merged shard outputs, use `collect_campaign_shards.py --promote-bug-records --replay-promoted-bug-records --runner <runner>`.

Replay a persistent bug registry as a regression lane:

```powershell
python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe-list .\artifacts\bug_registry_reduced_revolve\registry_replay_recipes.txt `
  --out .\artifacts\bug_registry_replay_lane `
  --triage-out .\artifacts\bug_registry_replay_lane_triage `
  --preview-out .\artifacts\preview_bug_registry_replay_lane `
  --geometry-audit-out .\artifacts\geometry_audit_bug_registry_replay_lane

python .\test_harness\tools\check_bug_registry_regression.py `
  --registry .\artifacts\bug_registry_reduced_revolve `
  --recipe-summary .\artifacts\bug_registry_replay_lane\recipe_summary.json `
  --out .\artifacts\bug_registry_regression_check_reduced_revolve `
  --fail-on-changed `
  --fail-on-unavailable
```

For known-open bug registries this lane is expected to return nonzero because the replay recipes preserve the failing oracle. `check_bug_registry_regression.py` classifies the results as `still_failing`, `fixed_or_not_reproduced`, `changed_failure`, or `unavailable` by comparing registered validation and roundtrip failures with the new artifact reports.

For GUI handoff of known-open registries, pass both the materialized registry and the replay triage to `build_debug_handoff.py`. When both are present, the handoff packs keep the registry fingerprint/bug id/replay recipe as the stable regression identity and merge replay triage artifacts for `case_dir`, previews, input SGTs, and focus Face/Edge/Vertex extraction.

## Corpus Smoke

The runner also supports corpus entry recipes:

- `check_sgt`: deserialize `.sgt`, reserialize bodies, run topology checks and property snapshots.
- `check_sgt` also accepts non-body topology assets such as standalone Face/Edge/Vertex SGTs from `debug_geometry/` or `focus/`; these run generic topology checks, write `output/topology_*.sgt`, and skip body property oracles instead of failing `min_result_bodies`.
- `step_import`: import `.step` / `.stp`, serialize valid result bodies, and write data-exchange diagnostics.
- `iges_import`: import `.iges` / `.igs`, serialize valid result bodies, and write data-exchange diagnostics.
- `step_roundtrip`: load an `.sgt` body, export STEP, import it back, then compare source/result length, area, absolute volume, and bbox with `roundtrip_abs_tol` / `roundtrip_rel_tol`.
- `iges_roundtrip`: load an `.sgt` body, export IGES, import it back, then run the same source/result roundtrip comparison.

Discover local corpus inputs first:

```powershell
python .\test_harness\tools\discover_corpus.py `
  .\SGK1.4.10\samples\Release\Output `
  --out .\artifacts\sdk_sample_discovery\dataset_index.json `
  --hash-inputs
```

The discovery tool writes:

- `dataset_index.json`: machine-readable file list, APIs, sizes, hashes, extension/root summaries, and duplicate content groups.
- `dataset_index.paths.txt`: plain path list that can be fed to `run_corpus.py`.
- `dataset_index.md`: short Markdown inventory report.

By default discovery skips generated directories named `.git`, `.vs`, `__pycache__`, `build`, and `artifacts`. Use `--include-artifacts` when previous harness outputs should become an explicit replay corpus.

Audit a frozen dataset before a long run:

```powershell
python .\test_harness\tools\audit_corpus_dataset.py `
  --dataset-list .\artifacts\sdk_sample_discovery\dataset_index.json `
  --out .\artifacts\sdk_sample_discovery_dataset_audit
```

`audit_corpus_dataset.py` checks that referenced CAD/SGT files still exist and are nonempty, summarizes extension/API/root coverage, reports hash coverage and duplicate content groups when hashes are present, and writes `dataset_audit.json` plus `dataset_audit.md`. Missing or empty referenced files are hard errors; duplicate content, missing hashes, and single-format/API corpora are warnings unless stricter flags such as `--require-hashes` or `--fail-duplicate-ratio` are used.

Profile STEP/IGES features when you want a focused complex curve/surface subset:

```powershell
python .\test_harness\tools\profile_cad_features.py `
  --dataset-list .\artifacts\public_freecad_corpus_smoke_v1\dataset_index.json `
  --out .\artifacts\cad_feature_profile_freecad_smoke\cad_feature_profile.json `
  --paths-out .\artifacts\cad_feature_profile_freecad_smoke\complex_paths.txt `
  --subset-out .\artifacts\cad_feature_profile_freecad_smoke\complex_dataset_index.json `
  --report .\artifacts\cad_feature_profile_freecad_smoke\cad_feature_profile.md `
  --min-score 8
```

The profiler scans STEP keywords and IGES directory-entry type numbers for B-spline curves/surfaces, trimmed or bounded surfaces, pcurves/curve-on-surface entities, offset/swept/revolved surfaces, and common analytic surfaces. It writes `cad_feature_profile.json`, `cad_feature_profile.md`, `complex_paths.txt`, and `complex_dataset_index.json`. Prefer the JSON subset for benchmark or campaign lanes because it preserves source discovery metadata such as SHA1, size, root, extension, and API while adding per-file `feature_profile`; the path list is a lightweight compatibility output. The profile is a heuristic corpus-selection aid for import/roundtrip or post-import recut lanes, not a modeling-validity oracle. `fetch_public_corpus.py --profile-features` runs the same profile immediately after public corpus discovery and records the report and subset paths in `public_corpus_manifest.json`.

Batch scan a directory:

```powershell
python .\test_harness\tools\run_corpus.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --dataset .\artifacts\boolean_smoke `
  --out .\artifacts\corpus_boolean_smoke `
  --timeout 60 `
  --jobs 4 `
  --resume `
  --triage-out .\artifacts\corpus_boolean_smoke_triage `
  --triage-include-passed
```

Run a discovered dataset index or path list:

```powershell
python .\test_harness\tools\run_corpus.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --dataset-list .\artifacts\sdk_sample_discovery\dataset_index.json `
  --out .\artifacts\sdk_sample_discovery_corpus_run `
  --timeout 60 `
  --jobs 2 `
  --hash-inputs `
  --triage-out .\artifacts\sdk_sample_discovery_corpus_triage `
  --triage-include-passed
```

For large runs:

- Use `--jobs N` to run multiple `sggk_case_runner.exe` processes.
- Use `--resume` to skip previously passing source files in the same output directory.
- Use `--shard-count N --shard-index I` to split a stable sorted corpus across machines or runs.
- Use `--limit N` for smoke slices.
- Use `--dataset-list FILE` to run a stable discovered file set rather than rescanning moving directories.
- Use repeated `--sgt-api check_sgt|step_roundtrip|iges_roundtrip` to run multiple checks for each `.sgt` source.
- Use `--step-surface-to-bspline`, `--step-curve-to-bspline`, and `--step-spcurve-in-wire-to-bspline` to stress STEP BSpline export; tune `--roundtrip-abs-tol` and `--roundtrip-rel-tol` for source/result comparisons.
- Use `--triage-out DIR` to run artifact triage immediately after the batch.

The corpus runner writes generated recipes under `_recipes`, a `corpus_manifest.json` with selected inputs and run configuration, and an incrementally updated `corpus_summary.json` with pass/fail, timeout, stdout/stderr, source file, API, source index, skip state, and elapsed time for each case.

## Corpus Recut Attacks

Use `generate_corpus_recut_matrix.py` when an existing `.sgt` corpus should become boolean attack input instead of only being checked or imported. The generator creates `api_boolean` recipes with `target_kind=loaded_sgt` and generated tools placed at exact, `+/- 1e-5`, and `+/- 1e-2` contacts. When a runner is provided or auto-detected, each source SGT is first probed with `check_sgt` plus six coordinate-plane distance extrema, so tool placement uses exact source bounds rather than conservative SDK bboxes. If the exact probe is unavailable, the generator falls back to named SGT points and bbox snapshots; use `--require-exact-bbox-probe` to fail instead of falling back.

```powershell
python .\test_harness\tools\generate_corpus_recut_matrix.py `
  --dataset-list .\artifacts\sdk_sample_discovery\dataset_index.json `
  --out .\artifacts\sdk_sample_recut_recipes `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --preset smoke `
  --case-prefix sdk_sample_recut `
  --source-limit 3

python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\artifacts\sdk_sample_recut_recipes `
  --out .\artifacts\sdk_sample_recut_run `
  --jobs 2 `
  --timeout 90 `
  --triage-out .\artifacts\sdk_sample_recut_triage `
  --preview-out .\artifacts\preview_sdk_sample_recut `
  --geometry-audit-out .\artifacts\geometry_audit_sdk_sample_recut
```

Presets:

- `smoke`: one side-tangent cylinder recut family with `SUBTRACTION` and exact/`+/- geom_tol` variants.
- `standard`: adds `INTERSECTION`, `+/- topo_tol`, a generated `sweep_circle_line` tool, and an `extrude_rect` center slab.
- `stress`: adds `UNION` and more tangent directions.

For STEP/IGES sources, first run the import/check corpus lane, then discover the produced artifact SGTs with `discover_corpus.py --include-artifacts` and feed that dataset index into the recut generator.

## Artifact Triage

After a corpus or DSL run, build a failure index:

```powershell
python .\test_harness\tools\triage_artifacts.py `
  .\artifacts\corpus_boolean_smoke `
  --out .\artifacts\corpus_boolean_smoke_triage `
  --include-passed
```

The triage tool writes:

- `triage_summary.json`: machine-readable failures, warnings, corpus command failures, TopoCheck failures, validation failures, roundtrip-comparison failures, topo-track summaries, and localized input topology.
- `failure_groups` inside `triage_summary.json`: duplicate failures grouped by a stable fingerprint derived from API, reasons, error codes, TopoCheck signature, validation signature, roundtrip signature, topo-track counts, and localized input operation/topology signatures.
- `regression_seeds.json`: one representative seed per failure group, including artifact input files and recipe/source references where available.
- `triage_report.md`: a concise human-readable report.

For boolean/DSL cases, localized input topology is derived from `report/topo_track.json` ancestor `input_ref` entries and `report/input_topology_index.json` locators. This lets failures name concrete target/tool faces, edges, or vertices plus the terminal modeling operation that produced them.
When topo tracking is unavailable or intentionally skipped, triage also reports `input_contact_candidates`: ranked target/tool Body/Face/Edge/Vertex bbox-nearness pairs from `input_topology_index.json`. These candidates are included in failure fingerprints, failure groups, markdown reports, and regression seeds so validation-only failures still point at concrete input topology.

Replay representative seeds before filing or reducing a failure group:

```powershell
python .\test_harness\tools\replay_regression_seeds.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --seeds .\artifacts\corpus_boolean_smoke_triage\regression_seeds.json `
  --out .\artifacts\corpus_boolean_smoke_replay `
  --retries 3 `
  --timeout 120
```

The replay tool writes generated recipes under `_recipes`, then writes `replay_summary.json` and `replay_report.md`. Each seed is classified as `stable_failure`, `flaky`, `not_reproduced`, or `unavailable`. When a seed includes an original recipe path, replay uses that recipe first so expectations and operation metadata are preserved; artifact SGT inputs are the fallback when the original recipe is unavailable.

Reduce stable flat-recipe failures before handoff when the representative recipe is still too large:

```powershell
python .\test_harness\tools\reduce_failure_recipe.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\artifacts\compiled_real_chain_tolerance_smoke\real_chain_sweep_extrude_side_int_gap_geom.json `
  --out .\artifacts\reduced_real_chain_gap_geom `
  --timeout 90 `
  --max-trials 120
```

The reducer runs the original recipe once to learn the failure predicate, then greedily tries smaller legal parameters. API-error baselines preserve the SDK error code by default; validation and TopoCheck baselines preserve their failed oracle class. Positive geometry dimensions are not reduced below `--min-dimension`, which defaults to `0.01` to match the current topology/modeling tolerance. For known near-contact body pairs such as sweep/extrude, sweep/sweep, and revolve/cylinder, reducer mutations preserve the contact offset while shrinking dimensions. Outputs are `reduced_recipe.json`, `reduction_summary.json`, `reduction_report.md`, and per-trial artifacts under `runs`.

Export stable failures into handoff-ready bug bundles:

```powershell
python .\test_harness\tools\export_failure_bundles.py `
  --triage .\artifacts\corpus_boolean_smoke_triage `
  --replay .\artifacts\corpus_boolean_smoke_replay `
  --preview-dir .\artifacts\preview_corpus_boolean_smoke `
  --out .\artifacts\corpus_boolean_smoke_bundles `
  --zip
```

Each bundle contains `bug_report.md`, `bundle_manifest.json`, `localization_summary.json`, `reproduce.ps1`, original and replay recipes when available, copied input files (`source_sgt`, STEP/IGES source inputs, `target_sgt`, and `tool_sgt` when present), key reports such as `data_exchange.json` / `roundtrip_comparison.json` when present, and an optional preview PNG. With `--zip`, a sibling `<bundle>.zip` is written for handoff. The top-level `bundle_report.md` lists one bundle per triage fingerprint.

For a lighter GUI-oriented handoff, build debug SGT packs directly from a registry or triage summary:

```powershell
python .\test_harness\tools\build_debug_handoff.py `
  --registry .\artifacts\some_run\bug_registry `
  --preview-dir .\artifacts\some_run\previews\dsl_lane `
  --out .\artifacts\some_run\debug_handoff
```

Each pack contains `README.md`, `manifest.json`, `visual_index.json`, `visual_index.md`, `focus_index.json`, `focus_index.md`, `sgt_paths.txt`, copied `debug_geometry/*.sgt`, target/tool/source input SGTs when available, selected reports, optional preview PNG, and `open_folder.ps1`. If `SggkGui.exe` is found under the SDK or passed through `--gui`, the pack also writes `open_in_gui.ps1` as a convenience launcher.

The reduced revolve validation-only failures are a current example of this handoff path:

```powershell
python .\test_harness\tools\triage_artifacts.py `
  .\artifacts\reduced_revolve_v3_lane `
  .\artifacts\reduced_revolve_rect_lane `
  --out .\artifacts\reduced_revolve_validation_triage `
  --include-passed

python .\test_harness\tools\replay_regression_seeds.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --seeds .\artifacts\reduced_revolve_validation_triage\regression_seeds.json `
  --out .\artifacts\reduced_revolve_validation_replay `
  --retries 3 `
  --timeout 90

python .\test_harness\tools\export_failure_bundles.py `
  --triage .\artifacts\reduced_revolve_validation_triage `
  --replay .\artifacts\reduced_revolve_validation_replay `
  --preview-dir .\artifacts\preview_reduced_revolve_v3 `
  --preview-dir .\artifacts\preview_reduced_revolve_rect `
  --out .\artifacts\reduced_revolve_validation_bundles `
  --zip
```

This produces three stable failure bundles for open-profile revolve intersection/subtraction and closed-profile revolve subtraction. Each bundle's `reproduce.ps1` currently reproduces with return code `2`, while the SDK status remains success; the failing oracle is recorded in `report/validation.json`.

## Campaign Runner

Use `run_campaign.py` to run the standard end-to-end flow in one command: optional source-risk scanning plus source-attack task packaging, corpus discovery/run, loaded-SGT corpus recut lanes, generated matrix lanes, DSL lanes, aggregate triage, seed replay, preview/contact-sheet rendering, geometry audit, failure-bundle export, bug-registry collection, GUI-ready debug handoff pack generation, editable bug-record draft export, optional promoted bug-record candidate replay, and checked-in known-bug regression.

```powershell
python .\test_harness\tools\run_campaign.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --out .\artifacts\campaign_smoke_real_chain `
  --source-root .\SGK1.4.10\SGGK\include `
  --source-scan-max-findings 120 `
  --source-scan-max-seeds 30 `
  --source-task-max-tasks 80 `
  --source-task-write-dsl-seeds `
  --dataset-root .\SGK1.4.10\samples\Release\Output `
  --corpus-limit 2 `
  --corpus-recut-source-limit 2 `
  --corpus-recut-use auto `
  --matrix-limit 3 `
  --dsl .\test_harness\dsl\real_chain_tolerance_smoke.json `
  --jobs 2 `
  --timeout 90 `
  --hash-inputs `
  --hash-recipes `
  --triage-include-passed `
  --bundle-zip
```

The campaign runner writes `campaign_summary.json` and `campaign_report.md` at the campaign root. Passing `--source-root` runs `scan_source_risks.py` first and records `source_scan/` reports plus `attack_seed_drafts.json`; by default it also builds `source_attack_tasks/` with JSONL/model-task output unless `--skip-source-attack-tasks` is passed. DSL lanes run `compile_attack_dsl.py --check` first and write `dsl_checks/<lane>.json` before compiling recipes under `recipes/<lane>/`; when `--dsl` is omitted, the default DSL set includes tolerance-band, real operation-chain tolerance, and BSpline support-sweep complex-surface smokes. Recipe-lane failures are expected test discoveries, so their return code `2` is recorded and the campaign continues into aggregate triage, replay, bundle export, bug-registry collection, debug handoff pack generation, and editable bug-record draft export. Corpus recut lanes are enabled when SGT inputs are available. Use `--discover-include-artifacts` when `--dataset-root` should scan historical harness outputs and failure bundles instead of only source datasets; use `--discover-limit`, `--discover-include-build`, and `--discover-exclude-dir` to bound or tune discovery. `run_campaign.py` audits original corpus dataset lists and discovered indexes into `dataset_audit/` by default, records a `dataset_audit` block in the summary/report, and lets artifact verification check the audit evidence; use `--skip-dataset-audit`, `--dataset-audit-require-hashes`, and `--dataset-audit-fail-duplicate-ratio <ratio>` to tune this gate. Use repeated `--corpus-sgt-api check_sgt|step_roundtrip|iges_roundtrip` plus the `--corpus-step-*`, `--corpus-iges-*`, and `--corpus-roundtrip-*` options to include SGT exchange roundtrips in the standard corpus lane. By default `--corpus-recut-use auto` recuts `runs/corpus/**/output/result_*.sgt` artifacts when the corpus lane produced them, and falls back to original dataset SGTs otherwise; use `--corpus-recut-use original|artifacts|both` when you need explicit source selection. Recut generation and recipe-lane geometry audit use exact coordinate-plane distance extrema when the runner is available, falling back to diagnostic bbox snapshots only when exact probing cannot run. Use `--corpus-recut-require-exact-bbox-probe` for extent-sensitive recut lanes, or `--corpus-recut-no-exact-bbox-probe` only for exploratory fast lanes. Use `--skip-corpus-recut`, `--skip-corpus-recut-artifacts`, `--corpus-recut-preset smoke|standard|stress`, `--corpus-recut-source-limit`, and `--corpus-recut-limit` to control this lane. Use `--shard-count N --shard-index I` to split corpus and recipe lanes across stable campaign shards; a lane with no work in a shard writes an empty summary with `empty_shard=true` instead of failing the campaign. Recipe lanes run geometry audit by default and report duplicate-input and tolerance-mismatch counts; use `--no-geometry-audit` to skip it, or `--geometry-audit-fail-on-duplicates` / `--geometry-audit-fail-on-tolerance-mismatch` for stricter lanes. The transient registry is written under `bug_registry/` with `bug_registry.md`, `bug_registry.json`, and `registry_replay_recipes.txt`; use `--skip-bug-registry` to skip it. GUI-ready SGT packs are written under `debug_handoff/` with `debug_handoff_report.md`; when `sggk_topology_extract.exe` is available they also include `focus/*.sgt` exports of the primary contact target/tool topology; use `--skip-debug-handoff` to skip them. Editable bug-record drafts are written to `bug_record_drafts/drafts.json`; use `--skip-bug-record-drafts` to skip them and `--bug-record-prefix <prefix>` to control generated draft IDs. Add `--promote-bug-records` when those drafts should be copied/re-written into portable candidates under `promoted_bug_records/`; add `--replay-promoted-bug-records` when the promoted candidate root should also run materialization, replay, and regression classification under `promoted_bug_records/materialized`, `promoted_bug_records/replay`, and `promoted_bug_records/regression`. Checked-in records under `test_harness/bug_records` are materialized and replayed by default under `known_bug_records/`, `known_bug_replay/`, and `known_bug_regression/`; the known-bug replay also writes triage, preview/contact sheet, geometry audit, and `known_bug_debug_handoff/` unless those global outputs are disabled. Use `--skip-known-bug-regression` to skip this lane, `--bug-record <file-or-dir>` to override the record set, and `--known-bug-fail-on-fixed|--known-bug-fail-on-changed|--known-bug-fail-on-unavailable` when a version gate should fail on those states. Pass `--reduce-stable-failures` to run `reduce_failure_recipe.py` after aggregate replay for stable failures with flat recipes; use `--reduction-limit`, `--reduction-max-trials`, `--reduction-timeout`, and `--reduction-min-dimension` to bound the cost. The reduction lane writes `reductions/reduction_index.json` and `reduction_index.md`, and artifact verification checks those paths when present. `run_campaign.py` also summarizes validation/oracle coverage by default under `oracle_coverage/`; passed cases must have `report/validation.json` and at least one classified oracle kind unless `--skip-oracle-coverage` or `--oracle-coverage-min-kinds 0` is used. The coverage report counts property snapshots, metric expectations, point/body and face/point relations, clash checks, distance checks, exact plane extrema, skipped checks, and validation failures so source-directed runs can see whether they exercised real-result validation. `run_campaign.py` runs artifact verification by default at the end, writes `campaign_verification/`, and records an `artifact_verification` block in the summary/report; use `--skip-artifact-verify` for intentionally incomplete fast runs, or `--artifact-verify-allow-duplicate-inputs`, `--artifact-verify-allow-duplicate-geometry`, `--artifact-verify-allow-tolerance-mismatches`, and repeated `--artifact-verify-expect-known-bug-status <status>` to forward strictness controls. `collect_campaign_shards.py` also merges shard reduction indexes into `reductions/reduction_index.json` and rebuilds a merged `debug_handoff/` from the merged registry by default; pass `--skip-debug-handoff` there to skip GUI handoff generation. Use `--bundle-zip` for zipped handoff bundles and `--fail-on-failures` when the campaign should return nonzero after stable newly discovered failures are found.

`run_campaign.py` invokes the artifact verifier automatically. You can rerun it manually for an existing campaign, a merged shard root, or an exploratory pass with relaxed geometry-audit strictness. The verifier also opens debug handoff indexes and checks per-pack manifests, `sgt_paths.txt`, and focus indexes when present:

```powershell
python .\test_harness\tools\verify_campaign_artifacts.py `
  --campaign .\artifacts\campaign `
  --out .\artifacts\campaign\campaign_verification
```

The verifier reads `campaign_summary.json` or merged `campaign_shards_summary.json`, checks advertised lane summaries, DSL check reports, preview contact sheets, geometry-audit counts, dataset-audit evidence, bug-registry/debug-handoff paths, debug handoff `visual_index` / `focus_index` files, and known-bug regression outputs. For merged shard roots it also checks the merged shard report and recursively verifies each referenced shard campaign. It writes `campaign_verification.json` and `campaign_verification.md`, and returns nonzero when required evidence is missing, dataset audit reports missing/empty files, or strict geometry-audit defaults find duplicate inputs, duplicate geometry, or tolerance mismatches. Use `--expect-known-bug-status still_failing` for known-open regression campaigns and `--allow-duplicate-inputs`, `--allow-duplicate-geometry`, or `--allow-tolerance-mismatches` only for exploratory lanes.

For large local or distributed runs, generate a frozen plan first. `plan_large_campaign.py` runs corpus discovery up front, writes a stable `discovery/dataset_index.json`, emits one PowerShell script per shard, and emits `preflight.ps1`, `run_all_sequential.ps1`, `run_all_with_preflight.ps1`, and `collect_shards.ps1`. Profiles choose the default lane breadth: `smoke` keeps tight limits and `check_sgt`, `standard` enables the standard matrix/recut lanes plus STEP/IGES roundtrips, and `stress` adds wider generated coverage. Use `--corpus-recut-require-exact-bbox-probe` on plans when recut lanes should skip any source whose exact coordinate-plane bbox probe cannot run. The generated preflight script runs `preflight_campaign.py` against the plan's runner, frozen dataset list, source roots, DSLs, and bug records before shards are launched; it audits dataset list quality and checked bug-record paths so stale corpus inputs, absolute local bug-record paths, or `artifacts/` bug-record dependencies fail before a long run. Use planner `--skip-dataset-audit`, `--dataset-audit-require-hashes`, and `--dataset-audit-fail-duplicate-ratio <ratio>` to freeze dataset-audit strictness into `commands/preflight.ps1`. Add planner `--profile-cad-features --cad-feature-min-score <N>` when STEP/IGES-heavy plans should also write `preflight/cad_feature_profile/complex_dataset_index.json` and audit that complex subset; add `--require-cad-feature-profile` when an empty or unavailable complex subset should fail preflight. Use `--use-cad-feature-subset` when the shard corpus lane should run directly on the plan-time `cad_feature_profile/complex_dataset_index.json`; the plan preserves the original dataset lists as provenance and records the selected shard dataset list separately. Run `run_all_with_preflight.ps1` when you want that gate in front of the whole batch. Pass `--reduce-stable-failures` with reduction limits on the plan when each shard should attempt bounded recipe reduction after stable replay. Add planner `--replay-reductions` when the collect script should replay canonical merged reduced recipes through the same runner before verification; replay writes `reduction_replay/semantic_check.json` / `.md` to compare the fresh replay against the reducer's saved failure predicate without requiring triage fingerprint equality. Add `--export-reduction-bug-record-drafts` with replay when those reduced replay failures should also become reviewable drafts under `reduction_bug_record_drafts/`; add `--materialize-reduction-bug-records` when the collect step should also turn those drafts into a temporary `reduction_bug_records_materialized/` registry and classify it against the reduced replay in `reduction_bug_regression/`; add `--promote-bug-records` when merged drafts should also be copied/re-written into artifact-local portable candidates under `promoted_bug_records/`; add `--replay-promoted-bug-records` with promotion when those candidates should also be materialized, replayed, and classified from the promoted root before review. The generated collect script runs `collect_campaign_shards.py` and then `verify_campaign_artifacts.py --campaign <merged> --out <merged>\campaign_verification` by default; merged collection also writes `reductions/reduction_index.json` when shard reductions exist, with raw entries plus `fingerprint_groups` for unique-failure review, optional `reduction_replay/`, optional `reduction_bug_record_drafts/`, optional `reduction_bug_records_materialized/`, optional `promoted_bug_records/`, and `oracle_coverage/` unless coverage is skipped. Use planner `--skip-oracle-coverage` for intentionally incomplete merged runs, or `--oracle-coverage-min-kinds <N>` to change the default one-oracle-kind gate. Use `--skip-artifact-verify` to omit the final verifier gate, or `--verify-allow-duplicate-inputs`, `--verify-allow-duplicate-geometry`, and `--verify-allow-tolerance-mismatches` for exploratory plans. When `--source-root` is present, source scanning/task packaging runs in shard 0 by default; other shards receive `--skip-source-scan` unless `--source-scan-each-shard` is used.

Planner skip gates are applied at both levels: shard scripts receive `run_campaign.py` skip/relax flags, while the collect script keeps the merged oracle/verification policy.

```powershell
python .\test_harness\tools\plan_large_campaign.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --out .\artifacts\large_campaign_plan `
  --profile standard `
  --shards 8 `
  --jobs 2 `
  --timeout 120 `
  --dataset-root D:\corpus\sggk `
  --source-root .\SGK1.4.10\SGGK\include `
  --hash-inputs `
  --hash-recipes `
  --source-task-write-dsl-seeds `
  --materialize-bug-records `
  --validate-recipes

powershell -ExecutionPolicy Bypass -File .\artifacts\large_campaign_plan\commands\run_all_sequential.ps1
```

Run only the preflight gate for a plan:

```powershell
powershell -ExecutionPolicy Bypass -File .\artifacts\large_campaign_plan\commands\preflight.ps1
```

Or run the whole plan with preflight first:

```powershell
powershell -ExecutionPolicy Bypass -File .\artifacts\large_campaign_plan\commands\run_all_with_preflight.ps1
```

The plan writes `large_campaign_plan.md`, `large_campaign_plan.json`, `commands\command_list.txt`, `commands\preflight.ps1`, `commands\run_shard_*of*.ps1`, `commands\run_all_with_preflight.ps1`, and `commands\collect_shards.ps1`. After all shards finish, the collect script writes merged `campaign_shards_report.md`, `dataset_audit/dataset_audit_collection.md`, `oracle_coverage/`, `bug_registry/`, `debug_handoff/`, `bug_record_drafts/drafts.json`, optionally `promoted_bug_records/` with materialized/replay/regression subfolders when promoted replay is enabled, optionally `reduction_replay/`, `reduction_bug_record_drafts/drafts.json`, `reduction_bug_records_materialized/`, `reduction_bug_regression/`, optionally `bug_records_materialized/`, and by default `campaign_verification/`.

You can also preflight a non-planner run directly:

```powershell
python .\test_harness\tools\preflight_campaign.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --dataset-root .\SGK1.4.10\samples\Release\Output `
  --source-root .\SGK1.4.10\SGGK\include `
  --out .\artifacts\campaign_preflight
```

For faster large-lane triage, `collect_bug_registry.py --triage <triage-dir>` can also build a registry directly from `triage_summary.json` without first exporting bundles. It preserves the first original recipe path as `original_recipe`, representative case directory, `debug_geometry` / `debug_geometry_index` paths, and writes `registry_replay_recipes.txt` when a replayable recipe path exists. Bundle collection remains the richer handoff path, but direct triage collection is useful for quick version-regression checks after a long run.

When a triage group should graduate into a maintained regression record, run `export_bug_record_drafts.py --triage <triage-dir> --bundle-index <bundle-dir> --debug-handoff <debug_handoff-dir> --out <drafts.json>`, edit the draft, then pass it to `record_bug_cases.py`. The draft preserves original/replay recipe paths, representative case directories, copied debug-geometry SGTs, bundle reports, input SGTs, validation/roundtrip oracle details, topo-track diagnostics, source-task provenance such as `source_task_id`, `source_ref`, and `source_risk_id`, plus optional GUI handoff evidence including `visual_index.md`, `focus_index.md`, and `sgt_paths.txt`. Before checking the edited record into `test_harness/bug_records`, run `audit_bug_record_portability.py`; campaign-local drafts may point at artifacts, but checked records should not.

A compact end-to-end smoke that exercises corpus discovery, corpus `check_sgt`, loaded-SGT recut, generated matrix, DSL oracles, aggregate triage, previews, geometry audit, bug-registry collection, and bug-record draft export:

```powershell
python .\test_harness\tools\run_campaign.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --out .\artifacts\campaign_end_to_end_smoke_current `
  --dataset-root .\SGK1.4.10\samples\Release\Output `
  --corpus-limit 3 `
  --corpus-sgt-api check_sgt `
  --matrix-preset smoke `
  --matrix-limit 5 `
  --corpus-recut-preset smoke `
  --corpus-recut-source-limit 1 `
  --corpus-recut-limit 3 `
  --dsl .\test_harness\dsl\oracle_checks_smoke.json `
  --dsl-limit 2 `
  --jobs 2 `
  --timeout 90 `
  --replay-retries 1 `
  --replay-limit 2
```

Existing triage/replay/bundle outputs can also be collected manually:

```powershell
python .\test_harness\tools\collect_bug_registry.py `
  --triage .\artifacts\reduced_revolve_validation_triage `
  --replay .\artifacts\reduced_revolve_validation_replay `
  --bundle-index .\artifacts\reduced_revolve_validation_bundles `
  --out .\artifacts\bug_registry_reduced_revolve
```

Merge multiple campaign shards after a split run:

```powershell
python .\test_harness\tools\collect_campaign_shards.py `
  --campaign .\artifacts\campaign_shard_0of4 `
  --campaign .\artifacts\campaign_shard_1of4 `
  --campaign .\artifacts\campaign_shard_2of4 `
  --campaign .\artifacts\campaign_shard_3of4 `
  --out .\artifacts\campaign_shards_merged `
  --bug-prefix nightly `
  --materialize-bug-records `
  --validate-recipes
```

The shard collector reads each `campaign_summary.json`, reports source-scan and source-attack-task raw counts, per-lane totals, per-lane DSL check report/recipe/failure counts, empty shards, aggregate sums, dataset-audit raw counts, known-bug regression status counts, and merged reduction fingerprints, then reuses `collect_bug_registry.py` / `export_bug_record_drafts.py` to produce merged `bug_registry/` and `bug_record_drafts/drafts.json`. Direct campaigns and merged shard collection automatically pass the current `debug_handoff/debug_handoff_index.json` into draft export, so the drafts can preserve GUI handoff paths when a matching fingerprint has a pack. It writes `dataset_audit/dataset_audit_collection.json` / `.md` from per-campaign audit evidence without rerunning dataset audit, and fails when audited shards report missing/empty files or `ok=false`. With `--promote-bug-records`, it writes portable review candidates under `promoted_bug_records/`; with `--replay-promoted-bug-records --runner <runner>`, it also materializes, replays, and classifies those candidates under `promoted_bug_records/materialized`, `promoted_bug_records/replay`, and `promoted_bug_records/regression`. With `--replay-reductions --runner <runner>`, it replays one canonical reduced recipe per fingerprint into `reduction_replay/` with triage, preview contact sheet, geometry audit evidence, and a semantic check that classifies each canonical recipe as `stable_same_failure`, `changed_failure`, `not_reproduced`, or `unavailable`. Add `--export-reduction-bug-record-drafts` to generate separate reduced-replay drafts at `reduction_bug_record_drafts/drafts.json`; those drafts point at canonical reduced recipes, include semantic replay evidence, and remain review-only until explicitly recorded. Add `--materialize-reduction-bug-records` to materialize those reduced drafts into `reduction_bug_records_materialized/` and immediately classify them with `check_bug_registry_regression.py` under `reduction_bug_regression/` using the reduced replay summary. With `--materialize-bug-records`, it also writes `bug_records_materialized/` so the merged discoveries can be replayed through `run_recipes.py --recipe-list`.

Known bugs can also be recorded without a fresh campaign:

```powershell
python .\test_harness\tools\export_bug_record_drafts.py `
  --triage .\artifacts\reduced_revolve_validation_triage `
  --bundle-index .\artifacts\reduced_revolve_validation_bundles `
  --debug-handoff .\artifacts\reduced_revolve_debug_handoff `
  --out .\artifacts\bug_record_drafts\reduced_revolve_drafts.json `
  --bug-prefix sggk_reduced_revolve

python .\test_harness\tools\record_bug_cases.py `
  --records .\artifacts\bug_record_drafts\reduced_revolve_drafts.json `
  --out .\artifacts\bug_records_reduced_revolve `
  --validate-recipes
```
