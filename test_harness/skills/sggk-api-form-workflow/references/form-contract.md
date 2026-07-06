# Developer Form Contract

The developer form is JSON and should match `test_harness/forms/api_test_form.schema.json`.

Required fields:

- `request_id`: stable id for generated task and artifacts.
- `owner`: developer or team responsible for the request.
- `target_api`: one of `api_boolean`, `check_sgt`, `step_import`, `iges_import`, `step_roundtrip`, `iges_roundtrip`, or `needs_harness_extension`.
- `test_goal`: what behavior should be exercised.
- `risk_summary`: why normal API success may hide a bug.
- `geometry.family`: input family such as `primitive`, `generated_extrude`, `generated_thicken`, `generated_sweep`, `support_sweep_bspline`, `generated_revolve`, `pre_boolean`, `loaded_sgt`, `exchange_file`, or `corpus`.
- `oracles`: requested oracle families.
- `run_profile`: `single_case`, `smoke`, `matrix`, `corpus`, or `known_bug_regression`.

Recommended fields:

- `sdk_source_refs`: files, functions, or headers that motivated the request.
- `tolerance_focus`: `exact_contact`, `geom_tol`, `topo_tol`, `large_coordinate`, `seam_periodic`, `generated_topology`, `roundtrip_drift`, or `import_topology`.
- `expected_behavior`: one sentence describing the real result, not just return code.
- `case_count`: target number of cases or variants.
- `input_assets`: source SGT, STEP, IGES, dataset root, or dataset index.

Convert the form to a small-model task:

```powershell
python .\test_harness\tools\build_api_test_task.py `
  .\test_harness\forms\api_test_form.example.json `
  --out .\artifacts\model_tasks\boolean_thicken_generated_sheet_001.json
```

Use `--format markdown` when the intranet model expects a plain prompt.
