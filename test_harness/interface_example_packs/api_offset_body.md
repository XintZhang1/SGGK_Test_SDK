# api_offset_body Flat Recipes

Use `flat_recipe` when the requested interface is exactly `api_offset_body`
and the source body can be created by the harness or loaded from SGT.

Required fields:

- `case_id`: stable id.
- `api`: literal `api_offset_body`.
- `source_kind`: `solid_cylinder`, `solid_sphere`, or `loaded_sgt`.
- `offset_distance`: non-zero numeric offset distance.
- `modeling_tol`: positive modeling tolerance.
- `expectations`: property/topocheck/result-body checks.

Primitive source bodies use `source_*` dimensions, for example
`source_radius` and `source_height`. Loaded sources use `source_file` and
optional `source_body_index`.

Do not claim support for sheet offsets, face offsets, mixed offsets, or custom
oracles through this recipe. Return `needs_harness_extension` for those shapes.
