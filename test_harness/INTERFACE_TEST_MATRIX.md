# SGGK Interface Test Matrix

This matrix is the current inventory for distilling the end-to-end workflow:

```text
developer/source form -> model-authored DSL or recipe -> generated case cluster -> harness run -> triage/report
```

It distinguishes top-level runner APIs from SDK calls that are currently tested as body builders or validation oracles.

## Top-Level Runner APIs

These values are accepted by the flat recipe `api` field and by the DSL compiler where applicable.

| Runner API | SDK surface exercised | Input source | Best ABC/source-guided use | Current examples |
|---|---|---|---|---|
| `api_boolean` | `api_boolean` with `UNION`, `INTERSECTION`, `SUBTRACTION` | Generated bodies or `loaded_sgt` | Main ABC recut lane; source-guided tolerance/contact/topology attacks | `recipes/boolean_smoke.json`, `dsl/tolerance_band_smoke.json`, `dsl/occ_source_guided_surrogate_examples.json` |
| `api_boolean_split` | `api_boolean_split` with classified outer/inner/wire results | Generated or loaded target/tool, including `plane_sheet` | Split classification, imprint, and tolerance-boundary tests | `recipes/boolean_split_plane_smoke.json` |
| `api_boolean_slice` | `api_boolean_slice` returning wire-type bodies | Intersecting generated or loaded bodies | Section-wire count and topology validation | `recipes/boolean_slice_smoke.json` |
| `api_offset2d` | `api_offset2d` with line/arc paths and typed `Offset2DStatus` | Bounded 2D path segments | Success plus expected `CanNotConnect`/`CrvDegenToPoint` diagnostics | `recipes/offset2d_line_smoke.json`, `recipes/offset2d_cannot_connect_smoke.json` |
| `api_offset_body` | `api_offset_body` | Analytic primitive or materialized `loaded_sgt` | Offset sign/extent/property checks and later corpus offsets | `interface_example_packs/api_offset_body.example_recipe.json` |
| `api_topology_section` | `api_topology_section` with distinct Edge/Vertex lists | Intersecting generated or loaded topologies | Heterogeneous section artifact/count validation | `recipes/topology_section_spheres_smoke.json` |
| `check_sgt` | SGT deserialization and TopoCheck/property validation | Existing `.sgt` body or topology asset | Re-open imported ABC outputs, debug/focus SGTs, and reduced failures | `recipes/check_sgt_sample.json` |
| `step_import` | `api_step_import` | ABC `.step` / `.stp` corpus | First pass over ABC samples; source-guided import/heal/topology filtering tests | `run_corpus.py --corpus-step-api step_import` via ABC helper |
| `iges_import` | `api_iges_import` | `.iges` / `.igs` corpus | Exchange corpus and import robustness tests | `run_corpus.py --corpus-iges-api iges_import` |
| `step_roundtrip` | `api_step_export` then `api_step_import` | Existing `.sgt` body | Source-guided drift tests for precision, BSpline conversion, units, dropped topology | `recipes/step_roundtrip_smoke.json`, `dsl/exchange_roundtrip_smoke.json` |
| `iges_roundtrip` | `api_iges_export` then `api_iges_import` | Existing `.sgt` body | Source-guided export precision/tolerance drift tests | `recipes/iges_roundtrip_smoke.json`, `dsl/exchange_roundtrip_smoke.json` |

Use `needs_harness_extension` when source inspection points to an API not represented in this table or in the body-builder/oracle sections below.

## Body Builders Tested Through Boolean

These are not independent top-level runner APIs yet. They are currently exercised by building a body, then running boolean and validation oracles around the result.

| Harness builder | SDK calls | Source-guided attack ideas | ABC usage | Current examples |
|---|---|---|---|---|
| `solid_cylinder` | `api_make_solid_cylinder` | partial angle, seam edge, near-tangent side/cap contacts | generated tool for ABC recut | `recipes/boolean_smoke.json`, `dsl/tolerance_band_smoke.json` |
| `solid_wedge` | `api_make_solid_wedge` | sliver wedge, coplanar face, thin cutter | generated tool or baseline primitive target | `recipes/boolean_smoke.json` |
| `solid_sphere` | `api_make_solid_sphere` | point relation, clash, distance, plane-extreme oracle smoke | oracle calibration; less useful for ABC recut | `recipes/point_relation_smoke.json`, `recipes/distance_check_smoke.json` |
| `solid_cone` | `api_make_solid_cone` | near-zero top radius, partial angle, apex tolerance | generated cutter family | supported by schema; add source-guided example before relying on it heavily |
| `solid_torus` | `api_make_solid_torus` | periodic seam, small tube radius, tangency | generated cutter family | supported by schema; add source-guided example before relying on it heavily |
| `plane_sheet` | planar sheet construction | splitter placement, coplanarity, finite extent | tool for `api_boolean_split` | `recipes/boolean_split_plane_smoke.json` |
| `extrude_rect` | `api_create_rect_sheet_body`, `api_extrude_entity` | generated cap/side faces, side-edge contact, tiny height | generated sibling for source-risk clusters; ABC recut tool/target | `recipes/boolean_extrude_rect_smoke.json`, `dsl/operation_chain_smoke.json` |
| `thicken_rect_sheet` | `api_create_rect_sheet_body`, `api_thicken_body` | offset/thicken side and cap topology, asymmetric min/max distance | generated sibling for offset/tolerance source risks | `recipes/boolean_thicken_rect_sheet_smoke.json`, `dsl/thicken_chain_smoke.json` |
| `sweep_circle_line` | `api_sweep_entity` | generated sweep side topology, near-tangent side contact, PCurve/topology history | high-value generated tool/target against ABC bodies | `recipes/boolean_sweep_smoke.json`, `dsl/real_chain_tolerance_smoke.json` |
| `support_sweep_bspline_surface` | `api_create_face`, `api_sweep_entity` with support-face mode | BSpline support-face sweep, guide/support tolerance, generated complex surface | strong source-guided surrogate for ABC complex surfaces | `recipes/boolean_support_sweep_bspline_surface_smoke.json`, `dsl/complex_surface_sweep_boolean_smoke.json` |
| `revolve_line` | `api_revolve_entity` on open profile | periodic/open revolved side topology, angle boundaries | generated sibling for seam/periodic source risks | `recipes/boolean_revolve_line_smoke.json`, `dsl/revolve_chain_smoke.json` |
| `revolve_rect` | `api_revolve_entity` on radial rectangular face | closed-profile revolved solid, side cutter at tolerance bands | generated sibling for solid periodic topology | `recipes/boolean_revolve_rect_smoke.json`, `dsl/revolve_rect_chain_smoke.json` |
| `pre_boolean_cylinder_wedge` | internal `api_boolean`, then reuse result | operation-chain history, recutting previous boolean results | proxy for ABC imported/previous-result recut when no durable SGT exists | `recipes/boolean_preboolean_smoke.json`, `dsl/operation_chain_smoke.json` |
| `loaded_sgt` | SGT deserialization, then boolean | recut imported ABC bodies, saved failures, reduced bug fixtures | primary ABC corpus recut target | `dsl/load_sgt_attack_smoke.json`, `generate_corpus_recut_matrix.py` |

## DSL Chain Operations

The DSL can preserve modeling provenance more clearly than flat recipes. Prefer DSL for source-guided tasks.

| Chain op | Compiles to | Use when source mentions |
|---|---|---|
| `primitive` | primitive body builders | clean baseline, source threshold reproduction |
| `rect_profile -> extrude` | `extrude_rect` | generated cap/side faces, linear extrusion |
| `rect_profile -> thicken` | `thicken_rect_sheet` | offset/thicken tolerances and side/cap topology |
| `circle_profile -> sweep_line` | `sweep_circle_line` | sweep path/profile tolerance, generated PCurves |
| `support_sweep` | `support_sweep_bspline_surface` | BSpline surface support, guide/support mismatch |
| `line_profile -> revolve` | `revolve_line` | open-profile revolve and periodic side faces |
| `radial_rect_profile -> revolve` | `revolve_rect` | closed revolved solid and side/cap topology |
| `primitive -> boolean` | `pre_boolean_cylinder_wedge` | topology history, prior boolean output recut |
| `load_sgt -> transform` | `loaded_sgt` | ABC imported bodies, saved outputs, reduced repros |
| `transform` | final translate/scale | exact/gap/overlap contact placement |

## Validation Oracles

Every distilled test should include at least one real-result oracle beyond API success.

| Oracle | SDK/check surface | Best use | Current examples |
|---|---|---|---|
| TopoCheck | `TopoCheckTool` | every body-producing case | all body-output runner paths |
| property snapshot | length/area/volume/bbox diagnostics | result sanity, volume drift, empty/invalid output | `recipes/boolean_smoke.json` |
| `result_bodies` | harness validation | empty/non-empty truth, especially boolean/intersection | most smoke recipes |
| `split_*_bodies` | classified split output lists | outer/inner/wire/total split count truth | `recipes/boolean_split_plane_smoke.json` |
| `slice_*_bodies` | slice wire-body output | exact/bounded slice wire counts | `recipes/boolean_slice_smoke.json` |
| `offset2d_status`, `offset2d_result_paths` | typed Offset2D status and output paths | expected success and diagnostic outcomes | `recipes/offset2d_*_smoke.json` |
| `topology_section_edges`, `topology_section_vertices`, `topology_section_total` | heterogeneous section output | preserve Edge/Vertex type and count | `recipes/topology_section_spheres_smoke.json` |
| `boolean_volume_relation` | input/result property relation | stable solid booleans | `recipes/boolean_thicken_rect_sheet_smoke.json` |
| `point_relations` | `PtBodyRelation` | inside/outside/boundary probes | `recipes/point_relation_smoke.json` |
| `face_point_relations` | `FacePtRelation` | UV/face classification and boundary source risks | `recipes/face_point_relation_smoke.json` |
| `clash_checks` | `api_body_clash` | subtraction clearance, no-clash/any-clash classification | `recipes/clash_check_smoke.json` |
| `distance_checks` | `api_topo_minimum_distance`, `api_topo_maximum_distance` | exact contact, gap/overlap, proximity thresholds | `recipes/distance_check_smoke.json` |
| `plane_extreme_checks` | generated coordinate plane + minimum distance | exact min/max coordinate oracle, not conservative bbox | `recipes/plane_extreme_sphere_smoke.json` |
| roundtrip comparison | source/result properties and bbox after exchange | STEP/IGES drift | `recipes/step_roundtrip_smoke.json`, `recipes/iges_roundtrip_smoke.json` |
| topo tracking | SDK ModelingRet topology history | source-guided localization | native DSL boolean runs with `topo_track=true` |

## Distillation Flow Per Interface

Use this exact loop for each interface family:

1. Fill a developer/source form.
   - `target_api`: one of the top-level runner APIs.
   - `geometry.family`: one of `primitive`, `generated_extrude`, `generated_thicken`, `generated_sweep`, `support_sweep_bspline`, `generated_revolve`, `pre_boolean`, `loaded_sgt`, `exchange_file`, or `corpus`.
   - `sdk_source_refs`: intranet source file/function/line, or an OCC surrogate link for public examples.
2. Ask the model to produce one of:
   - `attack_dsl` for boolean/body-builder/source-guided cases.
   - `flat_recipe` for direct import/check/roundtrip cases.
   - `cluster_seed` for `build_source_guided_cluster.py`.
   - `campaign_request` for a registered large-run profile with bounded args.
   - `needs_harness_extension` for unsupported SDK APIs.
3. Expand to a small cluster when source risk involves tolerance/contact:
   - exact contact
   - `+/- geom_tol`
   - `+/- topo_tol`
   - source literal thresholds
   - generated-topology sibling
   - optional large-coordinate sibling under `max_model_size`
4. Run static gates:
   - `compile_attack_dsl.py --check --report` for DSL.
   - `validate_recipe.py` for flat recipes or compiled output.
5. Run Windows SDK tests:
   - `run_recipes.py` for generated recipe folders.
   - `run_corpus.py` for ABC STEP/IGES import.
   - `run_abc_sample_smoke.py` for top-complex import plus exact-bbox recut.
6. Produce report artifacts:
   - `recipe_summary.json` or `corpus_summary.json`
   - `triage_report.md`
   - `validation.json` for failing cases
   - `roundtrip_comparison.json` for exchange failures
   - preview `contact.png`
   - `geometry_audit.md`
   - optional bug records after replay/portability audit

## Recommended Interface Order

1. `api_boolean` with primitives and generated tools.
2. `step_import` over ABC STEP samples.
3. `api_boolean_split`, `api_boolean_slice`, and `api_topology_section` to validate heterogeneous topology adapters.
4. `api_offset2d` typed status semantics and `api_offset_body` analytic properties.
5. `loaded_sgt` ABC recut booleans using `generate_corpus_recut_matrix.py`.
6. Sweep families: `sweep_circle_line`, `support_sweep_bspline_surface`.
7. Extrude/thicken families: `extrude_rect`, `thicken_rect_sheet`.
8. Revolve families: `revolve_line`, `revolve_rect`.
9. Exchange roundtrip and oracle-specific calibration.
10. `check_sgt` over imported outputs, debug SGTs, and reduced repros.

## Known Unsupported Or Extension-Needed Areas

The current harness does not yet expose standalone runner APIs for:

- general offset surface and variable face-specific body offsets beyond the current `api_offset_body` adapter
- heal/repair
- fillet/chamfer
- defeature/remove-feature
- shell/solid builder as first-class operations
- arbitrary curve/surface construction
- HLR/display-only APIs
- meshing/tessellation APIs

When source inspection targets one of these, the model should emit `needs_harness_extension` with proposed recipe fields, required artifacts, and one minimum smoke case.
