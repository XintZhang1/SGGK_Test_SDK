# Source-Guided Small-Model Contract

## Input

Provide the model with:

- `source_excerpt`: cited source lines, preferably from SGGK intranet source. Public surrogate tasks may use OCCT links and line ranges only.
- `source_risk`: risk categories, numeric literals, branch predicates, and suggested attack family.
- `developer_form_context`: optional form fields from `sggk-api-form-workflow`.
- `supported_dsl`: body builders, oracles, constants, and known unsupported APIs.
- `cluster_policy`: exact contact, `+/- geom_tol`, `+/- topo_tol`, source literal thresholds, generated-topology sibling, and optional large-coordinate sibling.

## Output

Require JSON only, with one of these top-level `kind` values:

- `attack_dsl`: reviewed or draft SGGK attack DSL.
- `flat_recipe`: one or more direct runner recipes for supported non-DSL APIs.
- `cluster_seed`: a compact seed for `build_source_guided_cluster.py`.
- `campaign_command`: a fixed-code large campaign command, used when the model should not enumerate every generated case.
- `needs_harness_extension`: unsupported API/body builder with proposed runner fields and validation oracle.

## Required Fields

Every output must include:

- `source_ref`
- `hypothesis`
- `review_required`
- `expected_oracles`
- `post_checks`

For `attack_dsl`, include `dsl_version`, `constants`, `defaults`, and `cases`.

For `cluster_seed`, include:

- `cluster_id`
- `source_ref`
- `hypothesis`
- `contact_path`
- `contact_value`
- `target`
- `tool`
- optional `source_literal_offsets`
- optional `generated_sibling`
- optional `large_coordinate_sibling`

For `needs_harness_extension`, include:

- `api`
- `why_needed`
- `proposed_recipe_fields`
- `proposed_artifacts`
- `minimum_smoke_case`

For `campaign_command`, include:

- `command`
- `why_this_fixed_campaign_matches`
- `expected_artifacts`
- `unsupported_filter_policy`

## Post-Checks

Run these before trusting generated output:

```powershell
python .\test_harness\tools\compile_attack_dsl.py <dsl.json> --check --report <check.json>
python .\test_harness\tools\compile_attack_dsl.py <dsl.json> --out <recipes-dir>
python .\test_harness\tools\validate_recipe.py <recipes-dir>
python .\test_harness\tools\run_recipes.py --runner .\build\test_harness\Release\sggk_case_runner.exe --recipe <recipes-dir> --out <run-dir> --triage-out <triage-dir> --preview-out <preview-dir> --geometry-audit-out <audit-dir>
```

Report failed `validation.json`, TopoCheck, roundtrip comparison, clash, distance, point relation, face relation, plane-extreme, crash, and timeout separately.
