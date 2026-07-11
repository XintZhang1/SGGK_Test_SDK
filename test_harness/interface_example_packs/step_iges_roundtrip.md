# step_iges_roundtrip

Use this excerpt for `step_roundtrip` and `iges_roundtrip` forms over an already
saved `.sgt` body.

Rules:
- Return `{"kind":"flat_recipe","recipe":{...}}`.
- Use an existing `.sgt` `source_file`; do not point roundtrip recipes directly
  at STEP/IGES corpus files.
- Keep `source_body_index` explicit.
- For STEP, set `step_app_protocol` and conversion flags when the source risk
  mentions AP203/AP214/AP242 or BSpline conversion.
- For IGES, set writer/import flags explicitly when testing option behavior.
- Roundtrip drift is the oracle; API success alone is not enough.
- Use `roundtrip_abs_tol` and `roundtrip_rel_tol` when the prompt asks for
  tighter or looser drift bounds.
