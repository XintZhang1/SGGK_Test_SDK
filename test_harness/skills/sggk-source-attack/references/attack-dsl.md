# Attack DSL

This reference defines the untrusted `attack_dsl` candidate shape supplied to a
Message API task. The integrated pipeline is the only production consumer of a
model candidate: it reads `message.content`, runs the fixed DSL gate, executes
in isolation, selects, and promotes with provenance. Use
`generate_boolean_matrix.py` for host-generated broad baseline coverage rather
than asking a candidate to enumerate a matrix. Treat the DSL as a compact
parameterized-modeling IR with stable operation IDs.

For checked-in deterministic fixtures only, a developer may run supported DSL
directly to diagnose the runner:

```powershell
.\build\test_harness\Release\sggk_case_runner.exe --recipe .\path\to\attack.json --out .\artifacts
```

Likewise, these compiler commands are fixed-gate debugging aids for checked-in
fixtures or pipeline gate artifacts, not model-output acceptance commands:

```powershell
python .\test_harness\tools\compile_attack_dsl.py .\path\to\attack.json --check --report .\artifacts\dsl_checks\attack_check.json
python .\test_harness\tools\compile_attack_dsl.py .\path\to\attack.json --out .\artifacts\compiled_recipes
```

The pipeline invokes `--check` first for every model candidate. It expands
variants/sweeps/chains and validates the compiled flat recipes without writing
them, then runs eligible recipes in an isolated candidate directory. Never
copy/paste a response into the direct commands above. Compiled recipes keep
`dsl_source`, `dsl_case_id`, `dsl_variant`, `target_operations`, and
`tool_operations` so artifacts remain localizable to the original DSL
operation IDs.

## Shape

```json
{
  "dsl_version": 1,
  "constants": {
    "topo_tol": 0.01,
    "geom_tol": 0.00001,
    "max_model_size": 500000.0,
    "tau": "2 * pi"
  },
  "key_points": {
    "target_center": [0.0, 0.0, 0.0]
  },
  "defaults": {
    "api": "api_boolean",
    "boolean_type": "SUBTRACTION",
    "modeling_tol": "topo_tol",
    "check_valid": true,
    "topo_track": true,
    "non_destructive": true
  },
  "cases": [
    {
      "case_id": "boolean_generated_face_tol_001",
      "hypothesis": "Generated extrusion cap is cut just below/at/above the source tolerance.",
      "target": {
        "kind": "extrude_rect",
        "length": 260.0,
        "width": 180.0,
        "height": 220.0
      },
      "tool": {
        "kind": "solid_cylinder",
        "radius": 55.0,
        "height": 280.0,
        "angle": "tau",
        "translate": [0.0, 0.0, -30.0]
      },
      "expectations": {
        "result_bodies": {"min": 1},
        "sample_input_properties": true,
        "boolean_volume_relation": true,
        "total_abs_volume": {"max": 20000000.0, "abs_tol": "topo_tol"},
        "point_relations": [
          {
            "id": "result_center_inside",
            "role": "result",
            "body_index": 0,
            "point_ref": "target_center",
            "expected": "Inside",
            "tolerance": "topo_tol",
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
            "tolerance": "topo_tol",
            "check_boundary": true
          }
        ],
        "distance_checks": [
          {
            "id": "target_tool_clearance",
            "role_a": "target",
            "role_b": "tool",
            "kind": "minimum",
            "distance": {"min": "geom_tol", "abs_tol": "geom_tol"}
          }
        ],
        "plane_extreme_checks": [
          {
            "id": "result_x_max",
            "role": "result",
            "axis": "x",
            "side": "max",
            "expected": "10.0",
            "tolerance": "2 * topo_tol"
          }
        ]
      },
      "sweeps": [
        {
          "path": "tool.translate_z",
          "values": [
            {"suffix": "below_tol", "value": "-30.0 - topo_tol"},
            {"suffix": "flush", "value": "-30.0"},
            {"suffix": "above_tol", "value": "-30.0 + topo_tol"}
          ]
        }
      ]
    }
  ]
}
```

## Fields

- `constants`: numeric symbols. Built-ins include `pi` and `tau`. Values may be numbers or arithmetic expressions using `+`, `-`, `*`, `/`, `**`, and constants.
- Standard SGGK constants should be emitted unless source inspection overrides them: `topo_tol = 0.01`, `geom_tol = 0.00001`, and `max_model_size = 500000.0`.
- `key_points`: named reusable 3D points at the DSL root or inside a case. Values may be `[x, y, z]` arrays or objects with a `point` array. Use `point_ref` in `point_relations` or explicit 3D `face_point_relations`; the compiler expands it to flat `point` coordinates and keeps the reference name as provenance.
- `defaults`: common runner options. Use strings for numeric expressions such as `"topo_tol"` or `"2 * pi"`. `max_model_size` may be set in constants or options; compiled flat recipes preserve it for exact coordinate-plane probes.
- `cases`: attack cases. Use one case per source hypothesis.
- `target` and `tool`: body builders. Supported `kind` values match the compiled recipe schema: primitives, `extrude_rect`, `thicken_rect_sheet`, `sweep_circle_line`, `support_sweep_bspline_surface`, `revolve_line`, `revolve_rect`, and `pre_boolean_cylinder_wedge`.
- `translate`: short form for `[x, y, z]`; compiler emits `{prefix}_translate_x/y/z`.
- `secondary_translate`: short form for the cutter inside `pre_boolean_cylinder_wedge`.
- `variants`: named direct patches, each as `{"suffix": "...", "set": {"path.to.field": value}}`.
- `sweeps`: nearby parameter families. Use them for tolerance boundaries instead of copy-pasting cases.
- `paired_sweeps`: paired parameter families where each row mutates multiple paths together. Use them when boolean type, offset, and oracle expectations must stay aligned; unlike multiple `sweeps`, values in one `paired_sweeps` object are zipped by row rather than expanded as a cartesian product.
- `expectations`: real-result oracles. Use result body count, optional sampled boolean volume relation, total length/area/volume bounds, `point_relations`, `face_point_relations`, `clash_checks`, `distance_checks`, and `plane_extreme_checks` when source inspection implies a measurable predicate. Numeric expressions and `point_ref` entries inside these oracle lists are resolved by `compile_attack_dsl.py`. For plane extrema, keep the default `compare_expected=true` when checking a known coordinate and use `compare_expected=false` for measurement-only exact bbox probes. Treat `boolean_bbox_relation` as conservative bbox diagnostic only.
- `chain`: parameterized modeling steps for a body. Use this when the source risk is tied to generated topology rather than a single primitive.
- `id`: optional stable operation ID on body or chain steps. Use descriptive IDs because runner artifacts record them for failure localization.

Patch paths address the DSL case before compilation, for example:

- `tool.translate_z`
- `tool.translate.2`
- `target.secondary_translate_x`
- `options.modeling_tol`

Paired sweep example:

```json
{
  "paired_sweeps": [
    {
      "paths": ["options.boolean_type", "tool.chain.2.translate_x", "expectations.result_bodies.min"],
      "values": [
        {"suffix": "sub_overlap_topo", "values": ["SUBTRACTION", "170.0 - topo_tol", 1]},
        {"suffix": "sub_exact", "values": ["SUBTRACTION", 170.0, 1]},
        {"suffix": "int_gap_topo", "values": ["INTERSECTION", "170.0 + topo_tol", 0]}
      ]
    }
  ]
}
```

## Operation Chains

Use `target.chain` or `tool.chain` to describe simple parameterized modeling history. The compiler maps supported chains to the current flat runner body builders.

Supported ops:

- `primitive`: create a primitive body, for example `{"op": "primitive", "kind": "solid_cylinder", "radius": 100.0, "height": 200.0}`.
- `load_sgt`: load a body from an existing `.sgt` file, for example `{"id": "load_seed", "op": "load_sgt", "source_file": "artifacts/case/output/result_1.sgt", "body_index": 0}`.
- `rect_profile` then `extrude`: compile to `extrude_rect`.
- `rect_profile` then `thicken`: compile to `thicken_rect_sheet`; use `min_dist`, `max_dist`, optional `operation_tol`, `g1_tol`, and `allow_partial_success` when source risk points at offset/thicken-generated side or cap topology.
- `circle_profile` then `sweep_line`: compile to `sweep_circle_line`.
- `support_sweep`: compile to `support_sweep_bspline_surface`; use `path_radius`, `profile_radius`, `height`, `operation_tol`, and `g1_tol` for BSpline support-face sweep and generated swept-solid topology.
- `line_profile` then `revolve`: compile to `revolve_line`; use `bottom_radius`, `top_radius`, `height`, optional `angle`, and stable IDs for generated revolved side topology.
- `radial_rect_profile` then `revolve`: compile to `revolve_rect`; use `inner_radius`, `outer_radius`, `height`, optional `angle`, and stable IDs for closed-profile revolved solid topology.
- `boolean`: currently supports `solid_cylinder SUBTRACTION solid_wedge`, compiling to `pre_boolean_cylinder_wedge`.
- `transform`: fold `translate`, `translate_x/y/z`, and uniform `scale` into the final body builder.

Example:

```json
{
  "target": {
    "chain": [
      {"id": "target_profile", "op": "rect_profile", "length": 260.0, "width": 180.0},
      {"id": "target_extrude", "op": "extrude", "height": 220.0},
      {"id": "target_place", "op": "transform", "translate": [0.0, 0.0, 0.0]}
    ]
  },
  "tool": {
    "chain": [
      {"id": "tool_cylinder", "op": "primitive", "kind": "solid_cylinder", "radius": 55.0, "height": 280.0, "angle": "tau"},
      {"id": "tool_cap_offset", "op": "transform", "translate": [0.0, 0.0, "-30.0 + topo_tol"]}
    ]
  }
}
```

Unsupported chain patterns should be emitted as `needs_harness_extension`, not forced into an approximate recipe.

## Provenance Artifacts

Native DSL runs and compiled flat recipe lanes preserve modeling provenance in:

- `manifest.json`: DSL source, DSL case ID, variant, hypothesis, source-task provenance, normalized body specs, and operation IDs.
- `report/input_provenance.json`: target/tool summaries plus operation IDs and source-task provenance such as `source_task_id`, `source_ref`, and `source_risk_id`.
- `report/input_topology_index.json`: target/tool topology IDs, local indices, operation chains, and locators for failure triage. Locators include vertex point/tolerance, edge endpoints/length/tolerance, face area/sense, and bounding boxes where available.
- `report/topo_track.json`: raw SDK topology tracking enriched with DSL metadata and resolved `input_ref` objects on ancestors when they map back to target/tool topology.
- `report/topo_track_summary.json`: counts by track type, descendent topology type, ancestor topology type, resolved/unresolved/ambiguous ancestors, and target/tool ancestor roles.
- `report/properties.json`, `report/input_properties.json`, and `report/validation.json`: result length, area, volume, boolean input bboxes, optional sampled input volumes, and real-result oracle outcomes. `validation_ok=false` is a failure even when the API status is success.
- `report/debug_geometry_index.json` and `debug_geometry/*.sgt`: generated on supported validation oracle failures; open these SGTs in the GUI to inspect failing bodies, selected faces, clash/distance hit topology, or coordinate probe planes.
- `report/preview.png` and contact sheets: generated by `test_harness/tools/render_case_preview.py`. Use them to screenshot-check generated variants, especially tiny tolerance offsets that look identical unless printed bbox snapshots are checked.
- `oracle_coverage/oracle_coverage.md`: generated by full campaigns or `summarize_oracle_coverage.py`; use it to confirm generated DSL exercised real-result validation rather than only API return status.
- `dataset_audit/dataset_audit.md`: generated by `audit_corpus_dataset.py`, `run_campaign.py`, or campaign preflight for frozen corpus inputs. It reports missing/empty files, extension/API counts, hash coverage, duplicate content groups, and warnings for narrow corpus coverage.
- `triage_summary.json`, `triage_report.md`, and `regression_seeds.json`: generated by `test_harness/tools/triage_artifacts.py` from one or more artifact roots after batch runs. Use `failure_groups` for deduplication before filing issues or reducing cases.
- `replay_summary.json` and `replay_report.md`: generated by `test_harness/tools/replay_regression_seeds.py` from representative seeds. Confirm stable failures before reduction or bug filing.
- `reduced_recipe.json`, `reduction_summary.json`, and `reduction_report.md`: generated by `test_harness/tools/reduce_failure_recipe.py` for stable flat-recipe failures that need smaller handoff cases while preserving the baseline failure predicate.
- `reductions/reduction_index.json` and `reductions/reduction_index.md`: generated by `test_harness/tools/run_campaign.py --reduce-stable-failures` when campaign-level aggregate replay finds stable flat-recipe failures selected for bounded reduction, and by `collect_campaign_shards.py` when merging shard reduction indexes. Merged indexes keep raw reduction entries and add `fingerprint_groups` for unique-failure review.
- `reduction_replay/canonical_reduced_recipes.txt`, `reduction_replay/runs/recipe_summary.json`, `reduction_replay/triage/triage_report.md`, `reduction_replay/previews/contact.png`, `reduction_replay/geometry_audit/geometry_audit.md`, and `reduction_replay/semantic_check.md`: generated by `collect_campaign_shards.py --replay-reductions --runner <runner>` when canonical merged reduced recipes need fresh replay evidence before bug-record promotion. The semantic check maps by canonical recipe path and reducer predicate, not by triage fingerprint equality.
- `reduction_bug_record_drafts/drafts.json`: generated by `collect_campaign_shards.py --replay-reductions --export-reduction-bug-record-drafts` when canonical reduced replay failures should become reviewable bug-record drafts. Drafts point at reduced recipes, mark `replay_status=stable_failure`, tag `replay.is_reduced_recipe=true`, and carry `reduction_replay_evidence` including semantic-check paths/counts.
- `reduction_bug_records_materialized/bug_registry.md` and `reduction_bug_regression/registry_regression.md`: generated by `collect_campaign_shards.py --materialize-reduction-bug-records` when reduced replay drafts should become a temporary replay registry and be classified against the fresh reduced replay summary before manual check-in.
- `bug_report.md`, `bundle_manifest.json`, `localization_summary.json`, and `reproduce.ps1`: generated by `test_harness/tools/export_failure_bundles.py` for stable failure handoff packages. Bundles copy recipes, key reports, input files, optional previews, and optional zip archives per failure fingerprint.
- Draft bug-record JSON from `test_harness/tools/export_bug_record_drafts.py`: generated from triage and/or bundle outputs when a failure should become a maintained version-regression record.
- `bug_record_portability/bug_record_portability.md`: generated by `test_harness/tools/audit_bug_record_portability.py` before reviewed bug records are checked in. It rejects absolute local paths and `artifacts/` dependencies; use durable fixture, DSL, recipe, or SDK sample paths instead.
- Bug registries and materialized bug records preserve source-task provenance, replay recipes, input SGTs, and debug-geometry SGT paths so source-generated failures can be replayed and opened in the SDK GUI.
- `debug_handoff/debug_handoff_report.md` plus per-fingerprint pack directories: generated by `build_debug_handoff.py` or by `run_campaign.py` after bug-registry collection. Packs include copied debug/input/focus SGTs, `visual_index.json`, `visual_index.md`, `focus_index.json`, `focus_index.md`, `sgt_paths.txt`, selected reports, recipes, optional preview PNGs, and folder/GUI helper scripts.

## Guidance

- The task contract may select `attack_dsl` when the attack is runnable with current body builders.
- Use `sweeps` for below/exactly/above threshold families. For topology-building APIs, prefer exact contact plus `+/- geom_tol` and `+/- topo_tol`; for pure geometry APIs, focus on `geom_tol`.
- Remember the current SGGK scale contract: topology/modeling APIs use `topo_tol = 1e-2`; pure geometry queries use `geom_tol = 1e-5`; generated large-coordinate cases should remain within `max_model_size = 5e5`.
- Use `hypothesis` to capture the source-level suspicion.
- Add `expectations` when the source branch implies measurable truth: expected empty/non-empty result, monotonic volume, bounded area growth, nonzero length, known analytic range, exact coordinate extrema, critical point/body relation, point/face relation on a selected face, expected clash/no-clash relation, or minimum clearance around `geom_tol`/`topo_tol`.
- Set `sample_input_properties: true` only for stable solid boolean cases where the target/tool volume relation is part of the oracle. Leave it off for broad fuzz lanes unless intentionally probing input-property robustness.
- Name important chain steps with stable `id` values, especially operations that create, split, merge, trim, or transform topology.
- Use `load_sgt` when a source risk should be attacked on a previously found bug, corpus import, or operation-generated body.
- For oscillating near-tangent families, sweep both the distance band and the contact phase/direction; `generate_boolean_matrix.py --preset standard` already emits sweep/sweep multi-phase examples.
- For open revolved/periodic topology, use `line_profile -> revolve -> transform` and attack the generated side face near exact contact, `+/- geom_tol`, and `+/- topo_tol`.
- For solid-like revolved topology, use `radial_rect_profile -> revolve -> transform` with `outer_radius > inner_radius`, then attack the outer side face near exact contact, `+/- geom_tol`, and `+/- topo_tol`.
- For complex curve/surface paths, use `support_sweep -> transform` and then boolean or exchange variants. When the support sweep should be solid, add `sample_input_properties: true`, nonzero `total_abs_volume`, and relation tolerances at `topo_tol`.
- For offset/thicken paths, start with `rect_profile -> thicken -> transform` before moving to broader offset bodies. Keep `min_dist < max_dist`, prefer signed distances with thickness comfortably above `topo_tol` for regression candidates, and add exact plane-extreme checks when the intended min/max coordinate is part of the bug predicate.
- After running generated attacks, render a contact sheet and run `audit_case_geometry.py --exact-bbox-runner <runner>` or `run_recipes.py --geometry-audit-out`. Verify variants are visually/numerically distinct and that `overlap_geom`, `exact`, and `gap_topo` style names match the signed target/tool clearance. Matching same-boolean geometry hashes usually mean duplicate geometry unless intentionally testing different oracles on the same shape.
- For campaign runs, inspect `oracle_coverage/oracle_coverage.md`. A useful source-directed batch should report validation present for passed cases and nonzero oracle counts for the intended signal family, such as point relation, face-point relation, clash, distance, plane extrema, or metric expectations.
- For corpus-driven runs, inspect `dataset_audit/dataset_audit.md` before trusting a long run. Missing or empty inputs invalidate the dataset; duplicate groups and single-format/API warnings should be reported as coverage signals rather than ignored.
- When a run fails, inspect `topo_track_summary.json` first, then use `topo_track.json` `input_ref` fields and `input_topology_index.json` locators to name the concrete target/tool face, edge, or vertex involved.
- After triage, replay `regression_seeds.json` and separate stable failures from flaky, unavailable, or not-reproduced seeds.
- After stable replay, reduce oversized flat recipes with `reduce_failure_recipe.py`, export zipped failure bundles for handoff, and use `export_bug_record_drafts.py` when the failure should graduate into a checked-in regression record. For merged shard reductions, use `--replay-reductions --export-reduction-bug-record-drafts` to draft from canonical reduced replay evidence instead of the larger original triage. Before check-in, run `audit_bug_record_portability.py`; move any corpus-derived SGTs out of `artifacts/` into `test_harness/fixtures/bug_records/<id>/`.
- Use `generate_boolean_matrix.py` to expand broad boolean coverage, then run its flat recipes with `run_recipes.py`; use DSL when provenance-rich operation IDs or source-specific chain hypotheses matter more than sheer matrix breadth.
- Use `needs_harness_extension` when the source requires an unsupported API or a body builder that does not exist yet.
