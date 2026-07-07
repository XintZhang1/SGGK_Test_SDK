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
  "notes": [],
  "commands": []
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
  "notes": [],
  "commands": []
}
```

Fixed large-campaign output:

```json
{
  "kind": "campaign_command",
  "command": "python .\\test_harness\\tools\\run_abc_boolean_mass_recut.py --target-cases 100000 ...",
  "why_this_fixed_campaign_matches": "The form requests a corpus-scale ABC loaded_sgt recut lane; fixed code expands recipes and reports filtered bug groups.",
  "expected_artifacts": [
    "artifacts/abc_boolean_mass_recut/abc_boolean_mass_recut_summary.json",
    "artifacts/abc_boolean_mass_recut/abc_boolean_mass_recut_bug_report.md"
  ],
  "unsupported_filter_policy": "Explicit kernel unsupported/not-allowed messages are counted separately and not filed as bugs."
}
```

Unsupported output:

```json
{
  "kind": "needs_harness_extension",
  "api": "api_offset_body",
  "why": "The current runner has no recipe for standalone body offset.",
  "minimal_extension": "Add a flat api_offset_body recipe with primitive or loaded_sgt input plus offset distance.",
  "proposed_recipe": {
    "case_id": "offset_near_tol_001",
    "api": "api_offset_body"
  }
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
- For 100k+ corpus campaigns, use `campaign_command`; do not enumerate individual cases in model output.
- For `check_sgt`, `step_import`, `iges_import`, `step_roundtrip`, and `iges_roundtrip`, use flat recipe JSON if no DSL mapping exists.
