# api_boolean_primitives

Use this excerpt for `api_boolean` forms over primitive solids such as
cylinders, wedges, spheres, cones, and tori.

Rules:
- Return `{"kind":"attack_dsl","dsl":{...}}`.
- Every runnable case must contain direct `target` and `tool` objects.
- Use direct body specs for simple primitive pairs; use chains only when a
  transform, nested boolean, or generated sibling is needed.
- Sweep contact through `variants[].set` with exact, `geom_tol`, and `topo_tol`
  offsets.
- Use `SUBTRACTION` and `INTERSECTION` variants when the source branch is about
  tolerance/contact classification.
- Include property/result-body expectations and at least one metric oracle when
  the geometry has an analytic relation.
