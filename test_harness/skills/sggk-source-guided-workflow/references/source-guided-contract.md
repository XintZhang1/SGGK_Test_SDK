# Source-Guided Message API Contract

This contract is embedded in a provider-neutral task manifest. It does not
authorize a human, Codex, or a standalone model session to save or execute a
response. Only `run_message_harness_pipeline.py` may accept and promote the
untrusted JSON returned in `message.content`.

## Input

The bounded manifest prompt may provide:

- `source_excerpt`: cited source lines, preferably from SGGK intranet source. Public surrogate tasks may use OCCT links and line ranges only.
- `source_risk`: risk categories, numeric literals, branch predicates, and suggested attack family.
- `developer_form_context`: optional form fields from `sggk-api-form-workflow`.
- `supported_dsl`: body builders, oracles, constants, and known unsupported APIs.
- `cluster_policy`: exact contact, `+/- geom_tol`, `+/- topo_tol`, source literal thresholds, generated-topology sibling, and optional large-coordinate sibling.

## Output

The Message API candidate must be one JSON object with an allowed top-level
`kind`:

- `attack_dsl`: untrusted SGGK attack DSL requiring the pipeline fixed gate.
- `flat_recipe`: one or more direct runner recipes for supported non-DSL APIs.
- `cluster_seed`: a compact seed for `build_source_guided_cluster.py`.
- `campaign_request`: a fixed-profile large campaign request, used when the model should not enumerate every generated case.
- `needs_harness_extension`: unsupported API/body builder with proposed runner fields and validation oracle.

## Required Fields

Every candidate must include:

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
- `extension_summary`
- `proposed_recipe_fields`
- `proposed_artifacts`
- `validation_oracle`
- `minimum_smoke_case`
- `patch_plan` with schema, validator, normalizer, runner, and tests steps

For `campaign_request`, include:

- `profile_id`
- bounded `args`
- optional short `notes`
- `expected_artifacts`

Executable names, commands, runner/data/output paths, cwd, environment, and shell mode are forbidden. Fixed code owns those bindings and resolves argv with `shell=False`.

## Acceptance And Execution

Submit the prompt manifest through the integrated pipeline:

```powershell
python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --run-id source_guided_batch `
  --execute `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  <model_task_manifest.json>
```

The pipeline invokes cluster expansion, `compile_attack_dsl.py`,
`validate_recipe.py`, `validate_harness_extension.py`, and `run_recipes.py` as
host-owned fixed gates. Direct invocation of those tools is limited to
debugging checked-in deterministic fixtures or an existing pipeline gate
artifact. It must never be used to accept, repair, or execute captured model
output.

Report failed `validation.json`, TopoCheck, roundtrip comparison, clash, distance, point relation, face relation, plane-extreme, crash, and timeout separately.
