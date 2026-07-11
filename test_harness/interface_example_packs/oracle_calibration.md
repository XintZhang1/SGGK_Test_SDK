# oracle_calibration

Use this excerpt for `api_boolean` forms whose goal is to add measurable oracles
instead of only checking SDK return status.

Rules:
- Return `{"kind":"attack_dsl","dsl":{...}}`.
- Prefer small stable primitive bodies when calibrating oracle behavior.
- Use named `key_points` when point probes should be reused.
- Use `point_relations`, `face_point_relations`, `clash_checks`,
  `distance_checks`, and `plane_extreme_checks` only when the expected geometry
  is analytically clear.
- Set tolerances explicitly. Use `geom_tol` for metric checks and `topo_tol` for
  topology/boundary checks unless the form says otherwise.
- If an oracle family is not supported, return `needs_harness_extension`.
