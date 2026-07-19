# api_boolean_generated_ops

Use this excerpt for `api_boolean` forms involving generated bodies such as
extrude, sweep, thicken, revolve, or pre-boolean recut chains.

Rules:
- Return `{"kind":"attack_dsl","dsl":{...}}`.
- Every case must contain direct `target` and `tool` objects.
- Put builder chains inside `case.target.chain` and `case.tool.chain`.
- Do not emit top-level `chains` plus `case.inputs` or `case.operations`.
- Use stable `id` values for profiles, generated bodies, transforms, and nested booleans.
- Put tolerance sweeps in `variants[].set`, for example `tool.chain.2.translate_x`.
- Include real expectations such as `result_bodies`, finite-property fields, distance, clash, or plane extremes.
- Do not emit `expectations.properties`; property oracles use direct fields such as
  `require_finite_properties`, `total_volume`, or `total_abs_volume`.
- `boolean_volume_relation` and `boolean_bbox_relation` are booleans only.
- Valid chain patterns include `rect_profile -> extrude`,
  `circle_profile -> extrude`, `rect_profile -> thicken`,
  `circle_profile -> sweep_line`, and `line_profile/radial_rect_profile -> revolve`.
- Chain-op field requirements (the fixed compiler rejects anything else):
  - `rect_profile`: numeric `length` and `width`; `extrude` step then requires numeric `height`.
  - `circle_profile`: numeric `radius`; `extrude` or `sweep_line` step then requires numeric `height`.
  - `thicken` after `rect_profile`: use `thickness` (thickens `[0, thickness]`) or explicit
    `min_dist`/`max_dist`.
  - `line_profile -> revolve`: profile requires numeric `bottom_radius`, `top_radius`, and
    `height`; do not use a free-form `points` array. `revolve` takes optional `angle` (default `"tau"`).
  - `radial_rect_profile -> revolve`: profile requires numeric `inner_radius`, `outer_radius`
    (`outer_radius > inner_radius`), and `height`; do NOT emit `radius`/`length`/`width` there.
    `revolve` takes optional `angle` (default `"tau"`).
- For `support_sweep` / `support_sweep_bspline_surface`, provide numeric
  `path_radius`, `profile_radius`, and `height`.
- Use `distance_checks`, not `expectations.distance`.
- Do not invent chain ops such as `boolean_subtract`; use supported `boolean`
  chain patterns or return `needs_harness_extension`.
- For nested booleans, either put a `tool` object in the `op:"boolean"` step,
  or place base body then tool body/transform immediately before the boolean step.
- Prefer `sweeps` or `paired_sweeps` for multi-value tolerance boundaries.
- If the needed builder or oracle is not supported, return `needs_harness_extension`.

The attached mini DSL is intentionally small but valid. Copy its shape, not its
exact dimensions.
