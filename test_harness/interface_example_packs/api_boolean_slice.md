# api_boolean_slice Flat Recipes

Use `flat_recipe` with the same supported `target_*` and `tool_*` builders as
the binary boolean harness. `boolean_type` is optional and defaults to `UNION`
for this API.

Slice outputs are reported as wire-type bodies. Use
`expectations.slice_result_bodies` and `slice_wire_bodies` with integer or
`min`/`max` count bounds. Set `require_property_calculations` to `false` when
the case is intended to test wire topology rather than solid properties.
