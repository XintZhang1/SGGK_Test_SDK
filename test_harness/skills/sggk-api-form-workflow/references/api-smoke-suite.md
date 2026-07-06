# API Smoke Suite

The current suite is `test_harness/suites/api_smoke_suite.txt`.

Coverage:

- `api_boolean` with primitive target/tool bodies.
- Generated body builders before boolean: `extrude_rect`, `thicken_rect_sheet`, `sweep_circle_line`, `support_sweep_bspline_surface`, `revolve_line`, `revolve_rect`, and `pre_boolean_cylinder_wedge`.
- Result validation oracles: result body count, property metrics, point/body relation, face/point relation, clash, distance, and exact plane-extreme checks.
- Serialized SGT reload through `check_sgt`.
- Data exchange roundtrip through `step_roundtrip` and `iges_roundtrip`.

Run command:

```powershell
python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe-list .\test_harness\suites\api_smoke_suite.txt `
  --out .\artifacts\api_smoke_suite `
  --jobs 1 `
  --timeout 120 `
  --triage-out .\artifacts\api_smoke_suite_triage `
  --preview-out .\artifacts\api_smoke_suite_preview `
  --contact-sheet .\artifacts\api_smoke_suite_preview\contact.png
```

Known limits:

- `step_import` and `iges_import` require external STEP/IGES source files, so they are covered by corpus lanes rather than the self-contained smoke list.
- The suite must run after the harness is built on Windows with the local SDK.
- The suite writes artifacts only under `artifacts/`, which is ignored by git.
