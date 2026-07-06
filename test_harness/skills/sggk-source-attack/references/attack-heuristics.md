# Attack Heuristics

Use this reference when translating suspicious source into test cases.

For large source roots, run `scan_source_risks.py` before hand-authoring DSL. The scanner emits `source_risk_report.json`, `source_risk_report.md`, `source_risk_files.txt`, and `attack_seed_drafts.json`; use those findings to choose files and hypotheses, not as final proof that a bug exists. For small-model batches, run `build_source_attack_tasks.py` on the scan output so each task carries a wider source excerpt, the finding, the review-required seed DSL, and the required output contract.

## Source Patterns To Search

- Numeric thresholds: `1e-`, `0.01`, `0.00001`, `0.0`, `0`, `PI`, `PI2`, `Precision::DistTol`, `DefModelingTol`, `MinLocalTol`, `LocalTolThreshold`, `UpperCoord`
- Fragile comparisons: `< tol`, `<= tol`, `> tol`, `fabs`, `abs`, `IsZero`, `IsEqual`, `near`, `parallel`, `coincident`
- Topology mutation: `merge`, `split`, `imprint`, `delete`, `purge`, `clone`, `owner`, `partner`, `coedge`, `loop`, `shell`, `lump`
- Geometry boundaries: seam, periodic, singular, pole, tangent, trim boundary, UV boundary, degenerate edge, zero length
- Risk comments: `TODO`, `FIXME`, `temporary`, `hack`, `magic`, `strict`, `assume`, `unreachable`, `skip`, `disabled`

## SGGK Scale Constants

Unless source code or the user gives a more specific value, generate cases with:

- `topo_tol = 1e-2` for modeling and topology-building APIs.
- `geom_tol = 1e-5` for pure geometry queries such as geometry intersection without topology construction.
- `max_model_size = 5e5` for large-coordinate stress cases.

For topology-generating APIs, sweep the geometric relation around exact contact with `+/- geom_tol` and `+/- topo_tol`. For pure geometry APIs, center the sweep around `geom_tol` and use smaller perturbations only when the source comparison exposes them. Add a large-coordinate sibling near but below `max_model_size` whenever the branch may depend on absolute coordinate magnitude.

## Geometry Attacks

Prefer families of 3 to 7 nearby cases:

- exactly at the threshold
- just below: `threshold - geom_tol`, `threshold - topo_tol`, or source-specific `threshold * (1 - 1e-6)`
- just above: `threshold + geom_tol`, `threshold + topo_tol`, or source-specific `threshold * (1 + 1e-6)`
- sign-flipped if direction matters
- large-coordinate version near but below `max_model_size`

Represent nearby families with DSL `sweeps` instead of copy-pasting flat recipes.

Examples:

- Near tangent: cylinder/wedge or cylinder/cylinder with center distance `R + r + {-topo_tol, -geom_tol, 0, geom_tol, topo_tol}`.
- Boolean intersection oracle: for topology-building booleans at `topo_tol = 1e-2`, allow empty results for exact tangency, positive gaps, and `-geom_tol` micro-overlap; require a non-empty intersection only once the overlap reaches the `-topo_tol` band or the source-specific tolerance under test.
- Oscillating near tangent: repeat the same distance sweep from several XY directions or phases, especially for `sweep_circle_line` bodies, so the generated topology is not always attacked from the same bbox face.
- Coincident faces: tool face exactly coplanar with target face, then shifted by `DistTol`.
- Seam boundary: partial cylinder/sphere/torus angles around `0`, `PI/2`, `PI`, `PI2`.
- Post-operation bodies: make the target or tool with `extrude_rect`, `sweep_circle_line`, `revolve_line`, `revolve_rect`, or `pre_boolean_cylinder_wedge` when the source branch depends on faces/edges created by boolean, extrusion, sweep, or revolve rather than clean primitive topology.
- Corpus/failure reuse: use `load_sgt` when a previous artifact, imported model, or reduced failure should become the seed for another boolean/transform attack.
- Corpus recut matrix: use `generate_corpus_recut_matrix.py` when many saved SGT bodies should be attacked uniformly. Pass `--runner` so it derives source bounds from coordinate-plane distance extrema, loads each source as `target_kind=loaded_sgt`, and places generated cylinder/sweep/extrude tools at exact, `+/- geom_tol`, and `+/- topo_tol` contacts. Treat fallback serialized bbox estimates as exploratory only.
- Sweep path boundary: use `sweep_circle_line` with profile radius or path height close to a tolerance threshold, then attack its side faces with an outer boolean.
- Open revolve side boundary: use `revolve_line` or `line_profile -> revolve` with bottom/top radii close to the target condition, then attack the generated side face with a cylinder at exact contact, `+/- geom_tol`, and `+/- topo_tol`.
- Closed revolve solid boundary: use `revolve_rect` or `radial_rect_profile -> revolve` with `outer_radius > inner_radius`, then attack the outer side face with a cylinder at exact contact, `+/- geom_tol`, and `+/- topo_tol`.
- Extrude cap/side boundary: use `extrude_rect` with a cutter exactly aligned to a cap, side face, or generated vertical edge, then offset by `modeling_tol`.
- Large coordinate: place near-tangent bodies so their bboxes approach but do not exceed `5e5`; for example target max-x `400000` and tool min-x swept through `399999.99`, `399999.99999`, `400000`, `400000.00001`, and `400000.01`.
- Degenerate primitive: wedge top nearly collapsed, cone top radius near zero, tiny height.
- Topology ownership: operations that clone or mutate input should run with `non_destructive=true` and `topo_track=true`.

## Result Checks

Always ask the harness to preserve:

- serialized input before mutation
- serialized result bodies
- SDK error code/message
- serialized error entities when present
- TopoCheck result and failed topology when present
- topology property snapshot
- `report/validation.json` with real-result checks, especially length/area/volume, sampled boolean volume-relation, point/face/body relation, clash, distance, and exact plane-extreme oracles when API success may hide a bad result. Treat SDK bbox relation output as conservative diagnostic context only.
- `expectations.point_relations` for critical point/body relation probes on `result`, `target`, or `tool`, especially points that should remain `Inside`, become `Outside`, or lie on `OnBoundary` near tolerance-sized cuts
- `expectations.face_point_relations` for point/face relation probes on selected faces, especially source branches involving face UV boxes, pcurves, trimming loops, seams, or boundary classification
- `expectations.clash_checks` for body/body collision probes on `result`, `target`, or `tool`, especially cases where a subtraction result should no longer interfere with the tool or distant inputs should remain `NoClash`
- `expectations.distance_checks` for minimum-clearance probes on `result`, `target`, or `tool`, especially near-tangent branches, small gaps around `1e-5`, and topology-building overlaps around `1e-2`
- `expectations.plane_extreme_checks` for exact min/max coordinate predicates. The runner uses `-max_model_size` / `+max_model_size` coordinate-plane distance probes by default, then derives `actual_extreme` from `api_topo_minimum_distance`; conservative bboxes only help center and size the finite probe face. Use this instead of bbox relation when filing boundary/extent bugs. Use `compare_expected=false` only for measurement/probe recipes; use an `expected` value for a hard oracle.
- `report/input_properties.json` for boolean input bbox snapshots and optional input volume snapshots; treat skipped input-volume checks as explicit signal, not as a passed volume relation
- topology tracking for ModelingRet APIs
- DSL provenance reports: `input_provenance.json` and `topo_track_summary.json`
- preview screenshots/contact sheets with printed bbox snapshots and signature hashes for tolerance sweeps
- `geometry_audit.json`/`.md` for same-boolean duplicate input detection and signed-clearance confirmation of `+/- geom_tol`, exact, and `+/- topo_tol` variants
- corpus recut manifests from `generate_corpus_recut_matrix.py`, including source paths, exact-bbox probe status, bbox source counts, skipped-source reasons, and generated recipe counts
- `bug_registry.json`/`.md` for cross-run failure deduplication by fingerprint, replay status, primary contact topology, and handoff/replay asset paths
- `bug_records/*.json` for hand-maintained known-bug regression records with inline recipes or replay recipe paths; preserve source-task provenance and debug-geometry SGTs when the bug came from a source-directed attack task
- durable fixture SGTs under `test_harness/fixtures/bug_records/<id>/` for corpus-derived known bugs, so persistent records do not depend on temporary campaign artifacts
- `report/debug_geometry_index.json` and `debug_geometry/*.sgt` for GUI handoff of failing probe planes, hit faces/edges/vertices, or whole bodies
- `registry_regression.json`/`.md` for open-bug replay status, including `still_failing`, `fixed_or_not_reproduced`, `changed_failure`, and `unavailable`

## Prioritize

1. Branches tied to tolerances and topology mutation.
2. Boolean/intersection/imprint/split code paths.
3. Import/heal paths where invalid topology can be filtered or silently dropped.
4. Corpus/failure reuse paths where real imported topology is re-cut by generated tools.
5. Performance shortcuts or parallel branches.
6. Pure cosmetic/UI code only when it can hide failed geometry.
