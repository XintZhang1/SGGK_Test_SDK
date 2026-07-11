# `api_topology_section` flat recipes

Use two supported `target_*` / `tool_*` body builders. The fixed adapter passes their topology handles to `api_topology_section` and preserves returned edges and standalone vertices as separate `.sgt` artifacts.

Use `topology_section_edges`, `topology_section_vertices`, and `topology_section_total` inside `expectations`. Each accepts an exact nonnegative integer or `{ "min": N, "max": M }` bounds. These results are topologies, not bodies, so body property/volume oracles do not apply.

The calibrated sphere/sphere example expects the SDK's two-edge representation of the circular section and no standalone vertex. The negative example intentionally misspells both a body field and an expectation field; strict validation must reject both instead of allowing the runner to use defaults.
