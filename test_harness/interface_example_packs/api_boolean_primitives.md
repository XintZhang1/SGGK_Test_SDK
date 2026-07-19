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
- Simple primitive pairs are only smoke anchors: the candidate as a whole must
  also combine generated-topology chains, large-coordinate placements, and at
  least one degenerate or empty-result case. The fixed complexity gate rejects
  candidates that stay simple.
- For mass coverage, do not enumerate cases: declare `cluster_bases` plus
  `parameter_clusters` (at most 50 cases per cluster) and let fixed code expand
  them deterministically.
