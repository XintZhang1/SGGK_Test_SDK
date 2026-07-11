# check_sgt_loaded_assets

Use this excerpt for `check_sgt` replay forms over saved SGT artifacts.

Rules:
- Return `{"kind":"flat_recipe","recipe":{...}}`.
- Preserve the exact `source_file` path under `artifacts/` or intranet storage.
- Use `source_body_index` only when selecting a specific body from a multi-body
  asset.
- For non-body topology/debug assets, do not invent body property expectations;
  keep the result focused on loadability and TopoCheck.
- `check_sgt` is a replay/check surface, not a place to create new geometry.
