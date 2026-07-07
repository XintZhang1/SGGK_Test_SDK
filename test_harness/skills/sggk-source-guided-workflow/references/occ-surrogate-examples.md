# OCC Surrogate Source Examples

Use these public OCCT anchors as transferable examples when proprietary SGGK source is unavailable. Store links, line ranges, hypotheses, and reviewed harness mappings only; do not copy OCCT source into this repository.

## Anchors

1. `BOPAlgo_Options::SetFuzzyValue`
   - Link: https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/ModelingAlgorithms/TKBO/BOPAlgo/BOPAlgo_Options.cxx#L49-L108
   - Risk: fuzzy tolerance clamped against confusion tolerance.
   - Harness mapping: near-tangent boolean family across `geom_tol`, exact contact, and `topo_tol`.

2. `BRepAlgoAPI_BuilderAlgo`
   - Link: https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/ModelingAlgorithms/TKBO/BRepAlgoAPI/BRepAlgoAPI_BuilderAlgo.cxx#L117-L186
   - Risk: fuzzy value forwarded into pave filler and same-domain unifier.
   - Harness mapping: generated sweep/extrude side-face boolean family with stable operation IDs.

3. `BOPAlgo_PaveFiller`
   - Link: https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/ModelingAlgorithms/TKBO/BOPAlgo/BOPAlgo_PaveFiller_10.cxx#L63-L148
   - Risk: edge/vertex tolerance growth and expanded bounding boxes.
   - Harness mapping: pre-boolean or revolve-generated topology recut near contact with distance and body-count oracles.

4. `Precision.hxx`
   - Link: https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/FoundationClasses/TKernel/Precision/Precision.hxx#L165-L235
   - Risk: confusion, intersection, and approximation tolerances are distinct.
   - Harness mapping: exact-vs-fuzzy tolerance band family and large-coordinate sibling.

5. `IGESControl_Writer`
   - Link: https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/DataExchange/TKDEIGES/IGESControl/IGESControl_Writer.cxx#L141-L165
   - Risk: export precision derived from shape tolerance when fixed precision is not forced.
   - Harness mapping: `iges_roundtrip` and `step_roundtrip` recipes from generated/corpus SGT sources with property drift oracles.

## Reviewed DSL

The reviewed surrogate DSL lives at `test_harness/dsl/occ_source_guided_surrogate_examples.json`.

Run structural checks:

```powershell
python .\test_harness\tools\compile_attack_dsl.py .\test_harness\dsl\occ_source_guided_surrogate_examples.json --check --report .\artifacts\occ_source_guided_surrogate_check.json
```
