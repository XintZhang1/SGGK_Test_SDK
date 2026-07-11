# Recipe Schema

Use this reference for the fixed flat-recipe schema. A Message API task may
allow `flat_recipe` or `attack_dsl`; only the integrated pipeline may accept
that untrusted candidate. For DSL, the pipeline invokes
`compile_attack_dsl.py` to translate it into this flat shape.

The pipeline validates candidates automatically. This direct command is only
for host-side diagnosis of checked-in deterministic fixtures or a pipeline gate
artifact; it must not consume captured model output:

```powershell
python .\test_harness\tools\validate_recipe.py .\path\to\recipes
```

## Currently Runnable APIs

### `api_boolean`

The runner supports a flat JSON recipe. `target_*` and `tool_*` fields describe body builders, not only primitives:

```json
{
  "case_id": "extrude_cylinder_subtraction_001",
  "api": "api_boolean",
  "boolean_type": "SUBTRACTION",
  "modeling_tol": 0.01,
  "check_valid": true,
  "topo_track": true,
  "non_destructive": true,
  "target_kind": "extrude_rect",
  "target_length": 260.0,
  "target_width": 180.0,
  "target_height": 220.0,
  "tool_kind": "solid_cylinder",
  "tool_radius": 55.0,
  "tool_height": 280.0,
  "tool_angle": 6.283185307179586,
  "tool_translate_z": -30.0
}
```

Supported values:

- `boolean_type`: `UNION`, `INTERSECTION`, `SUBTRACTION`
- `target_kind` / `tool_kind`: `solid_cylinder`, `solid_wedge`, `solid_sphere`, `solid_cone`, `solid_torus`, `extrude_rect`, `sweep_circle_line`, `support_sweep_bspline_surface`, `revolve_line`, `revolve_rect`, `pre_boolean_cylinder_wedge`, `loaded_sgt`

Common optional fields for both target and tool:

- `{prefix}_translate_x`, `{prefix}_translate_y`, `{prefix}_translate_z`
- `{prefix}_scale`
- `{prefix}_operation_tol`, `{prefix}_g1_tol`
- `{prefix}_create_seam_edge`
- `{prefix}_operations`: array of stable operation IDs copied from compiled DSL chains; used in provenance, input topology indexes, and triage contact candidates.

Body builder fields:

- `solid_cylinder`: `{prefix}_radius`, `{prefix}_height`, optional `{prefix}_angle`
- `solid_wedge`: `{prefix}_length`, `{prefix}_width`, `{prefix}_height`
- `solid_sphere`: `{prefix}_radius`
- `solid_cone`: `{prefix}_bottom_radius`, `{prefix}_height`, optional `{prefix}_top_radius`, `{prefix}_angle`
- `solid_torus`: `{prefix}_long_radius`, `{prefix}_short_radius`, optional `{prefix}_angle`
- `extrude_rect`: `{prefix}_length`, `{prefix}_width`, `{prefix}_height`; creates a rectangular sheet and calls `api_extrude_entity`
- `sweep_circle_line`: `{prefix}_profile_radius`, `{prefix}_height`; creates a circular wire profile and sweeps along a straight line with `api_sweep_entity`
- `support_sweep_bspline_surface`: `{prefix}_path_radius`, `{prefix}_profile_radius`, `{prefix}_height`; creates a BSpline support face, derives a support curve, creates a circular profile face, and calls `api_sweep_entity` in `SupportFace` mode
- `revolve_line`: `{prefix}_bottom_radius`, `{prefix}_top_radius`, `{prefix}_height`; optional `{prefix}_angle`; creates a line profile and calls `api_revolve_entity`, producing generated revolved side topology for boolean attacks
- `revolve_rect`: `{prefix}_inner_radius`, `{prefix}_outer_radius`, `{prefix}_height`; optional `{prefix}_angle`; creates a closed radial rectangular face and calls `api_revolve_entity`, producing a solid-like closed-profile revolved body. Require `outer_radius > inner_radius`.
- `pre_boolean_cylinder_wedge`: `{prefix}_radius`, `{prefix}_height`, `{prefix}_length`, `{prefix}_width`, `{prefix}_secondary_height`; creates a cylinder, subtracts a wedge, then uses that result as the outer boolean body. Optional child-tool placement: `{prefix}_secondary_translate_x/y/z`; optional child boolean: `{prefix}_boolean_type`.
- `loaded_sgt`: `{prefix}_source_file`, optional `{prefix}_body_index`; loads an existing `.sgt` body for corpus replay, failure replay, or follow-up attacks.

Example pre-boolean target:

```json
{
  "case_id": "preboolean_outer_cut_001",
  "api": "api_boolean",
  "boolean_type": "SUBTRACTION",
  "modeling_tol": 0.01,
  "check_valid": true,
  "topo_track": true,
  "non_destructive": true,
  "target_kind": "pre_boolean_cylinder_wedge",
  "target_boolean_type": "SUBTRACTION",
  "target_radius": 180.0,
  "target_height": 360.0,
  "target_angle": 6.283185307179586,
  "target_length": 140.0,
  "target_width": 240.0,
  "target_secondary_height": 200.0,
  "target_secondary_translate_x": 40.0,
  "target_operation_tol": 0.01,
  "tool_kind": "solid_cylinder",
  "tool_radius": 45.0,
  "tool_height": 420.0,
  "tool_angle": 6.283185307179586,
  "tool_translate_x": 225.0,
  "tool_translate_z": -30.0
}
```

Default failure predicates:

- process crash
- non-zero SDK error code
- `TopoCheckTool::CheckBody` failure
- missing result when success was expected
- `report/validation.json` failure, including property calculation errors or violated result-oracle expectations
- unserializable input/output/error topology

## Result Oracles

Add `expectations` when source inspection implies a real-result predicate, not just API success. The runner writes `report/validation.json` and fails the case when these predicates fail.

Supported fields:

- `result_bodies`: `{"min": int, "max": int}`
- `require_property_calculations`, `require_finite_properties`, `require_nonnegative_length_area`, `require_nonnegative_volume`: booleans
- `boolean_bbox_relation`: boolean; conservative SDK bbox diagnostic only. It records bbox relation mismatches under skipped/diagnostic checks and does not fail the case because SDK bboxes are not exact geometric bboxes.
- `sample_input_properties`: boolean; for stable solid boolean cases, sample target/tool length, area, and volume so input-dependent oracles can run
- `boolean_volume_relation`: boolean; requested volume relation check. It runs when input properties are sampled successfully, records a skipped check when sampling is off, and fails with `boolean_volume_relation_input_property_unavailable` when sampling was requested but unavailable.
- `total_length`, `total_area`, `total_volume`, `total_abs_volume`: objects with optional `min`, `max`, `expected`, `abs_tol`, `rel_tol`
- `point_relations`: list of `PtBodyRelation` checks against `result`, `target`, or `tool` bodies. Each item supports `id`, `role`, `body_index`, `point`, `expected`, `tolerance`, `check_boundary`, and `required`. Compiled DSL may also include `point_ref` as provenance, but execution uses the resolved `point`. `expected` can be `Unknown`, `OnVertex`, `OnEdge`, `OnFace`, `Inside`, `Outside`, `OnBoundary`, or `OnModel`.
- `face_point_relations`: list of `FacePtRelation` checks against a selected face from `result`, `target`, or `tool` bodies. Each item supports `id`, `role`, `body_index`, `face_index`, `face_id`, `point`, `uv`, `uv_fraction`, `expected`, `tolerance`, `check_boundary`, and `required`. Compiled DSL may also include `point_ref` as provenance for explicit 3D probes. `expected` can be `Unknown`, `OnVertex`, `OnEdge`, `Inside`, `Outside`, `OnBoundary`, or `OnFace`. Prefer `uv` or `uv_fraction` unless the 3D point is known to lie on the face surface.
- `clash_checks`: list of `api_body_clash` checks between `result`, `target`, or `tool` bodies. Each item supports `id`, `role_a`, `body_index_a`, `role_b`, `body_index_b`, `expected`, `mode`, `tolerance`, and `required`. `expected` can be `Clash_None`, `Clash_Exists`, `Clash_AInB`, `Clash_BInA`, `Clash_Touch`, `Clash_Interfere`, `NoClash`, or `AnyClash`. `mode` can be `ClashExistenceOnly`, `ClashClassify`, or `ClashClassifySubEntities`.
- `distance_checks`: list of `api_topo_minimum_distance` or `api_topo_maximum_distance` checks between `result`, `target`, or `tool` bodies. Each item supports `id`, `role_a`, `body_index_a`, `role_b`, `body_index_b`, `kind`, `threshold`, `distance`, flat numeric expectation fields, and `required`. `kind` can be `minimum` or `maximum`; `distance` and flat fields support `min`, `max`, `expected`, `abs_tol`, and `rel_tol`.
- `plane_extreme_checks`: list of exact coordinate-extreme checks against a `result`, `target`, or `tool` body. Each item supports `id`, `role`, `body_index`, `axis` (`x`, `y`, `z`), `side` (`min`, `max`), `expected`, `compare_expected`, `tolerance`, optional explicit `probe_coordinate`, `plane_span`, `plane_span_scale`, `required`, and `export_debug_geometry`. By default `compare_expected=true` and `expected` is required. Set `compare_expected=false` to measure `actual_extreme` without producing a metric mismatch. By default the runner uses `-max_model_size` for min-side checks and `+max_model_size` for max-side checks, calls `api_topo_minimum_distance` from a coordinate-plane face to the body, and derives `actual_extreme`; conservative SDK bboxes only help center and size the finite probe face.

Example:

```json
{
  "case_id": "boolean_volume_oracle_001",
  "api": "api_boolean",
  "boolean_type": "SUBTRACTION",
  "modeling_tol": 0.01,
  "check_valid": true,
  "topo_track": true,
  "non_destructive": true,
  "target_kind": "solid_cylinder",
  "target_radius": 200.0,
  "target_height": 500.0,
  "target_angle": 6.283185307179586,
  "tool_kind": "solid_wedge",
  "tool_length": 100.0,
  "tool_width": 200.0,
  "tool_height": 150.0,
  "expectations": {
    "result_bodies": {"min": 1, "max": 1},
    "sample_input_properties": true,
    "boolean_volume_relation": true,
    "boolean_bbox_relation": false,
    "total_abs_volume": {"max": 20000000.0, "abs_tol": 0.01},
    "point_relations": [
      {
        "id": "result_probe_inside",
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
        "id": "result_tool_should_be_separate",
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
}
```

Example replay-oriented boolean recipe:

```json
{
  "case_id": "replay_boolean_seed_001",
  "api": "api_boolean",
  "boolean_type": "SUBTRACTION",
  "modeling_tol": 0.01,
  "check_valid": true,
  "topo_track": true,
  "non_destructive": true,
  "target_kind": "loaded_sgt",
  "target_source_file": "C:/path/to/failure/input/target.sgt",
  "target_body_index": 0,
  "tool_kind": "loaded_sgt",
  "tool_source_file": "C:/path/to/failure/input/tool.sgt",
  "tool_body_index": 0
}
```

Generated corpus recut recipes use the same flat `api_boolean` schema. `generate_corpus_recut_matrix.py` writes recipes with `target_kind: "loaded_sgt"`, `target_source_file`, `target_body_index`, and generated tool bodies such as `solid_cylinder`, `sweep_circle_line`, or `extrude_rect`. Pass `--runner` so source bounds come from coordinate-plane distance extrema; serialized bbox estimates are fallback only. Treat this as recipe generation, not a new runner API:

```powershell
python .\test_harness\tools\generate_corpus_recut_matrix.py `
  --dataset-list .\artifacts\dataset_index.json `
  --out .\artifacts\corpus_recut_recipes `
  --preset smoke `
  --runner .\build\test_harness\Release\sggk_case_runner.exe
```

### `check_sgt`

```json
{
  "case_id": "check_saved_case_001",
  "api": "check_sgt",
  "source_file": "C:/path/to/case.sgt"
}
```

### `step_import`

```json
{
  "case_id": "step_import_001",
  "api": "step_import",
  "source_file": "C:/path/to/model.step"
}
```

### `iges_import`

```json
{
  "case_id": "iges_import_001",
  "api": "iges_import",
  "source_file": "C:/path/to/model.iges"
}
```

### `step_roundtrip`

```json
{
  "case_id": "step_roundtrip_001",
  "api": "step_roundtrip",
  "source_file": "C:/path/to/model.sgt",
  "source_body_index": 0,
  "step_app_protocol": "AP203",
  "step_surface_to_bspline": true,
  "step_curve_to_bspline": true,
  "step_spcurve_in_wire_to_bspline": true,
  "roundtrip_abs_tol": 0.01,
  "roundtrip_rel_tol": 1e-5
}
```

The runner exports the selected SGT body to STEP, imports it back, and fails when `report/roundtrip_comparison.json` reports mismatched length, area, absolute volume, or bbox. Use this when source inspection points to export options, BSpline conversion, unit/scale conversion, or data-exchange code paths that can return success while drifting the model.

### `iges_roundtrip`

```json
{
  "case_id": "iges_roundtrip_001",
  "api": "iges_roundtrip",
  "source_file": "C:/path/to/model.sgt",
  "source_body_index": 0,
  "iges_face_only_mode": false,
  "iges_write_sgk_specified_data": false,
  "roundtrip_abs_tol": 0.01,
  "roundtrip_rel_tol": 1e-5
}
```

## Unsupported APIs

When source inspection points to an unsupported operation, output:

```json
{
  "kind": "needs_harness_extension",
  "api": "api_offset_body",
  "why_needed": "Source branch depends on near-zero offset compared with Precision::MinLocalTol.",
  "extension_summary": "Add offset_body recipe with source_file or primitive body plus offset distance.",
  "proposed_recipe_fields": {
    "source_body": "primitive or loaded_sgt body spec",
    "offset": "number or numeric expression",
    "modeling_tol": "positive number"
  },
  "proposed_artifacts": [
    "recipe_summary.json",
    "validation.json",
    "triage_report.md"
  ],
  "validation_oracle": {
    "require_topocheck": true,
    "require_finite_properties": true
  },
  "minimum_smoke_case": {
    "case_id": "offset_near_min_local_tol_001",
    "api": "api_offset_body",
    "source_body": {"kind": "solid_cylinder", "radius": 100.0, "height": 100.0},
    "offset": 0.000025
  },
  "patch_plan": [
    {"layer": "schema", "change": "Add offset recipe fields.", "files": []},
    {"layer": "validator", "change": "Validate source and offset fields.", "files": []},
    {"layer": "normalizer", "change": "Normalize safe aliases only.", "files": []},
    {"layer": "runner", "change": "Add fixed runner route.", "files": []},
    {"layer": "tests", "change": "Add smoke and negative validator cases.", "files": []}
  ]
}
```

## Case IDs

Use lowercase snake case or hyphen-free alphanumeric words separated by underscores. Include the risk and a counter, for example `boolean_near_tangent_001`.
