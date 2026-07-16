"""Authoritative, model-facing summaries of deterministic fixed-gate contracts."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]


API_BOOLEAN_EXPECTATION_SHAPE: dict[str, Any] = {
    "result_bodies": {"min": "int >= 0", "max": "int >= min"},
    "require_finite_properties": True,
    "point_relations": [
        {
            "id": "point_check_id",
            "role": "result|target|tool",
            "body_index": "int >= 0",
            "point": ["x", "y", "z"],
            "expected": "Inside|Outside|OnBoundary|OnModel|OnVertex|OnEdge|OnFace|Unknown",
            "tolerance": "number > 0",
        }
    ],
    "face_point_relations": [
        {
            "id": "face_point_check_id",
            "role": "result|target|tool",
            "body_index": "int >= 0",
            "face_index": "int >= 0",
            "uv_fraction": [0.5, 0.5],
            "expected": "Inside|Outside|OnBoundary|OnFace|OnVertex|OnEdge|Unknown",
            "tolerance": "number > 0",
        }
    ],
    "clash_checks": [
        {
            "id": "clash_check_id",
            "role_a": "target|tool|result",
            "role_b": "target|tool|result",
            "expected": "NoClash|AnyClash|Clash_None|Clash_Exists|Clash_AInB|Clash_BInA|Clash_Touch|Clash_Interfere",
            "mode": "ClashExistenceOnly|ClashClassify|ClashClassifySubEntities",
            "tolerance": "number > 0",
        }
    ],
    "distance_checks": [
        {
            "id": "distance_check_id",
            "role_a": "target|tool|result",
            "role_b": "target|tool|result",
            "kind": "minimum|maximum",
            "threshold": "number > 0",
            "distance": {
                "min": "number",
                "max": "number",
                "expected": "number",
                "abs_tol": "number",
                "rel_tol": "number",
            },
        }
    ],
    "plane_extreme_checks": [
        {
            "id": "plane_extreme_check_id",
            "role": "result|target|tool",
            "body_index": "int >= 0",
            "axis": "x|y|z",
            "side": "min|max",
            "expected": "number",
            "tolerance": "number > 0",
        }
    ],
}


@lru_cache(maxsize=1)
def api_boolean_fixed_gate_example() -> dict[str, Any]:
    """Return a checked-in DSL that exercises every complex api_boolean oracle."""

    path = REPO_ROOT / "test_harness" / "dsl" / "oracle_checks_smoke.json"
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"fixed-gate example must be a JSON object: {path}")
    return value


@lru_cache(maxsize=1)
def api_boolean_fixed_gate_contract() -> str:
    """Return the exact api_boolean field mapping plus a compiler-checked example."""

    example = {
        "kind": "attack_dsl",
        "dsl": api_boolean_fixed_gate_example(),
        "notes": [],
    }
    return f"""## Deterministic api_boolean fixed-gate contract

This block is authoritative for model-authored `attack_dsl`. Capability-family labels are
not JSON field names. Use these exact mappings:

- `result_bodies` -> `expectations.result_bodies`
- `properties` -> direct fields such as `require_finite_properties`, `total_volume`, or `total_abs_volume`
- `boolean_volume_relation` -> `expectations.boolean_volume_relation` (literal boolean)
- `point_relation` -> `expectations.point_relations` (array)
- `face_point_relation` -> `expectations.face_point_relations` (array)
- `clash` -> `expectations.clash_checks` (array)
- `distance` -> `expectations.distance_checks` (array)
- `plane_extreme` -> `expectations.plane_extreme_checks` (array)
- `topocheck` -> no expectation field; use `check_valid: true` and concrete semantic expectations

Never emit `expectations.topocheck`, `expectations.runner_topocheck`,
`expectations.properties`, singular `point_relation` / `face_point_relation`,
`expectations.clash`, `expectations.distance`, `expectations.plane_extreme`, or a
free-form `oracles` array.

Exact complex-oracle item shapes:

- `point_relations[]`: `id`, one `role`, `body_index`, exactly one 3D `point` or declared
  `point_ref`, `expected`, `tolerance`, optional `check_boundary` / `required`. It never uses
  `points`, `role_a`, or `role_b`.
- `face_point_relations[]`: `id`, one `role`, `body_index`, face selector, one `point` /
  `point_ref` / `uv` / `uv_fraction`, `expected`, `tolerance`, optional booleans. It never
  uses `points`, `role_a`, or `role_b`.
- `clash_checks[]`: `id`, `role_a`, `role_b`, optional body indexes, `expected` (not
  `expect`), `mode`, `tolerance`, optional `required`.
- `distance_checks[]`: `id`, `role_a`, `role_b`, optional body indexes, `kind`, positive
  `threshold`, and `distance: {{min|max|expected|abs_tol|rel_tol}}`.
- `plane_extreme_checks[]`: `id`, one `role`, `body_index`, `axis`, `side` (not
  `direction`), `expected`, `tolerance` (not `tol`), and optional fixed-schema fields.

`result_bodies` is `{{"min": int, "max": int}}`. Metric expectations are objects using
`min`, `max`, `expected`, `abs_tol`, and/or `rel_tol`. All literal geometry dimensions
(`radius`, `height`, `length`, `width`, profile radii, and similar required sizes) must be
strictly greater than zero. Model a degeneracy with a positive `geom_tol` / `topo_tol`
epsilon; if the fixed builders cannot express it, return `needs_harness_extension`.

Compiler- and recipe-validator-checked reference output (copy its field shapes, not
necessarily its geometry):

```json
{json.dumps(example, indent=2, ensure_ascii=False)}
```
"""


def fixed_gate_contract_for_api(target_api: str) -> str:
    """Return a model-facing fixed-gate contract for a supported target API."""

    return api_boolean_fixed_gate_contract() if target_api == "api_boolean" else ""


__all__ = [
    "API_BOOLEAN_EXPECTATION_SHAPE",
    "api_boolean_fixed_gate_contract",
    "api_boolean_fixed_gate_example",
    "fixed_gate_contract_for_api",
]
