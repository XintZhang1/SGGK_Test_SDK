# step_import_iges_import

Use this excerpt for `step_import` and `iges_import` forms that ask the model to
author a single flat recipe or identify when a fixed corpus campaign should be
used instead.

Rules:
- Return `{"kind":"flat_recipe","recipe":{...}}` for one selected file.
- Use `api: "step_import"` for `.step` or `.stp`.
- Use `api: "iges_import"` for `.iges` or `.igs`.
- Preserve the concrete `source_file` path. Do not invent corpus paths.
- For broad ABC/corpus sweeps, prefer a typed `campaign_request` or fixed corpus tools
  instead of enumerating every source file.
- Include `result_bodies`, property, or TopoCheck expectations; API success
  alone is not enough.
- If the requested exchange mode is not supported by the runner, return
  `needs_harness_extension`.
