# api_boolean_split Flat Recipes

Use `flat_recipe` with one supported `target_*` body and one supported
`tool_*` body. A bounded `plane_sheet` is available for the common solid-by-
plane split case.

Required fields are `case_id`, `api`, `modeling_tol`, `target_kind`, and
`tool_kind`, plus the dimensions required by both builders. Optional split
flags are `split_target_add_face`, `split_strict_split`, and
`split_merge_imprint`.

Use `expectations.split_outer_bodies`, `split_inner_bodies`,
`split_wire_bodies`, and `split_total_bodies` for bounded count checks. Each
accepts an integer or an object with `min` and/or `max`.
