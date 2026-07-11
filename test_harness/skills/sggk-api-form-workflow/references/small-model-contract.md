# Small-Model Contract

The small model receives the `prompt` field emitted by `build_api_test_task.py`.

It must return exactly one JSON object.

Runnable DSL output:

```json
{
  "kind": "attack_dsl",
  "dsl": {
    "dsl_version": 1,
    "constants": {
      "topo_tol": 0.01,
      "geom_tol": 0.00001,
      "max_model_size": 500000.0,
      "tau": "2 * pi"
    },
    "defaults": {
      "api": "api_boolean",
      "modeling_tol": "topo_tol",
      "check_valid": true,
      "topo_track": true,
      "non_destructive": true
    },
    "cases": []
  },
  "notes": []
}
```

Runnable flat-recipe output:

```json
{
  "kind": "flat_recipe",
  "recipe": {
    "case_id": "step_roundtrip_dev_case_001",
    "api": "step_roundtrip",
    "source_file": "C:/path/to/source.sgt",
    "source_body_index": 0
  },
  "notes": []
}
```

Typed large-campaign output:

```json
{
  "kind": "campaign_request",
  "profile_id": "abc_boolean_mass_recut",
  "args": {
    "target_cases": 100000,
    "preset": "stress",
    "shard_count": 100,
    "shard_index": 0,
    "jobs": 1,
    "timeout_seconds": 180,
    "resume": true
  },
  "notes": ["The form requests a corpus-scale ABC loaded_sgt recut lane."],
  "expected_artifacts": [
    "artifacts/abc_boolean_mass_recut/abc_boolean_mass_recut_summary.json",
    "artifacts/abc_boolean_mass_recut/abc_boolean_mass_recut_bug_report.md"
  ],
  "expected_artifacts": ["abc_boolean_mass_recut_summary.json", "abc_boolean_mass_recut_bug_report.md"]
}
```

Unsupported output:

```json
{
  "kind": "needs_harness_extension",
  "api": "api_offset_body",
  "why_needed": "The current runner has no recipe for standalone body offset.",
  "extension_summary": "Add a flat api_offset_body recipe with primitive or loaded_sgt input plus offset distance.",
  "proposed_recipe_fields": {
    "source_kind": "solid_cylinder | loaded_sgt",
    "offset_distance": "number or numeric expression",
    "modeling_tol": "positive number"
  },
  "proposed_artifacts": [
    "recipe_summary.json",
    "validation.json",
    "triage_report.md"
  ],
  "validation_oracle": {
    "require_topocheck": true,
    "require_finite_properties": true
  },
  "minimum_smoke_case": {
    "case_id": "offset_near_tol_001",
    "api": "api_offset_body"
  },
  "patch_plan": [
    {"layer": "schema", "change": "Add recipe fields.", "files": []},
    {"layer": "validator", "change": "Validate fields and unsupported combinations.", "files": []},
    {"layer": "normalizer", "change": "Normalize safe aliases only.", "files": []},
    {"layer": "runner", "change": "Route fixed runner support.", "files": []},
    {"layer": "tests", "change": "Add positive and negative smoke tests.", "files": []}
  ]
}
```

Review rules:

- Valid JSON only.
- No direct SDK code.
- No unsupported body builders.
- Use stable operation `id` values in chains.
- Include real oracles. Good defaults are `result_bodies`, property checks, point/body relation, face/point relation, clash, distance, plane-extreme, and roundtrip comparison when applicable.
- For tolerance requests, use `sweeps` or `paired_sweeps` around exact contact, `geom_tol`, and `topo_tol`.
- For `api_boolean`, prefer DSL over flat recipes.
- For 100k+ corpus campaigns, use `campaign_request`; never emit commands, runner/data/output paths, cwd, environment, or shell fields.
- For `check_sgt`, `step_import`, `iges_import`, `step_roundtrip`, and `iges_roundtrip`, use flat recipe JSON if no DSL mapping exists.
