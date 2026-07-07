# Source-Guided Test Generation TODO

Status: completed in the Windows SDK workspace on 2026-07-06.

Completed artifacts:

- `test_harness/skills/sggk-source-guided-workflow`
- `test_harness/dsl/occ_source_guided_surrogate_examples.json`
- `test_harness/dsl/source_guided_cluster_seed_smoke.json`
- `test_harness/tools/build_source_guided_cluster.py`
- `test_harness/README.md` source-guided workflow commands

This note preserves the work item for review. The goal is to teach the intranet
small model to use source code while writing test cases, then combine those
source-guided cases with normal randomized / matrix-generated case clusters.

## Current Intent

- The small model's advantage is that it can read intranet SGGK source code.
- Source reading should be used during test-case authoring only.
- Harness execution should still use structured DSL / flat recipes, artifact
  validation, triage, previews, geometry audit, and bug-record promotion.
- The model should not write direct SDK code unless the harness explicitly lacks
  the needed API; in that case it should emit `needs_harness_extension`.
- Because the public GitHub checkout does not contain SGGK source, use OCCT
  source as a transferable surrogate for examples and distillation data.
- The current SGGK SDK is runnable from `C:\Develop\SGGK_Agent` on Windows; keep
  SDK execution and generated artifacts there. The Mac workstation can operate
  Codex/GitHub but should not be the SDK runtime until that constraint changes.

## Completed Implementation Tasks

1. Added a source-guided workflow skill that composes:
   - `sggk-source-attack` for source risk scanning and DSL generation.
   - `sggk-api-form-workflow` for developer request forms and fixed runner
     commands.
   - deterministic cluster expansion through `build_source_guided_cluster.py`
     and existing matrix / DSL tools.

2. Added OCC surrogate examples under the harness skills, not OCCT source
   copies. The workflow stores links and line references only. Source anchors:
   - `BOPAlgo_Options::SetFuzzyValue` clamps fuzzy tolerance to
     `Precision::Confusion()`.
     https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/ModelingAlgorithms/TKBO/BOPAlgo/BOPAlgo_Options.cxx#L49-L108
   - `BRepAlgoAPI_BuilderAlgo` forwards fuzzy value to the pave filler and
     same-domain unifier.
     https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/ModelingAlgorithms/TKBO/BRepAlgoAPI/BRepAlgoAPI_BuilderAlgo.cxx#L117-L186
   - `BOPAlgo_PaveFiller` updates edge / vertex tolerances and expands boxes by
     `Precision::Confusion()`.
     https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/ModelingAlgorithms/TKBO/BOPAlgo/BOPAlgo_PaveFiller_10.cxx#L63-L148
   - `Precision.hxx` distinguishes confusion, intersection, and approximation
     tolerances.
     https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/FoundationClasses/TKernel/Precision/Precision.hxx#L165-L235
   - `IGESControl_Writer` derives export precision from shape tolerance when
     fixed precision is not forced.
     https://github.com/Open-Cascade-SAS/OCCT/blob/4f95ecaa3b690e34988d42e2ca7fe882e7a8bc7d/src/DataExchange/TKDEIGES/IGESControl/IGESControl_Writer.cxx#L141-L165

3. Added one reviewed attack DSL example for each source anchor in
   `test_harness/dsl/occ_source_guided_surrogate_examples.json`:
   - fuzzy / confusion near-tangent boolean family
   - generated sweep / extrude side-face boolean family
   - edge / vertex tolerance growth family
   - exact-vs-fuzzy tolerance band family
   - exchange roundtrip tolerance drift family

4. Added deterministic source-guided cluster generator
   `test_harness/tools/build_source_guided_cluster.py`. It takes one reviewed
   source-risk seed and expands it into a small case cluster:
   - exact contact
   - `+/- geom_tol`
   - `+/- topo_tol`
   - source literal threshold, when present
   - generated-topology sibling, such as extrude, sweep, thicken, revolve, or
     pre-boolean
   - optional large-coordinate sibling under `max_model_size`

5. Made the small-model output contract explicit in
   `test_harness/skills/sggk-source-guided-workflow/references/source-guided-contract.md`:
   - input: source excerpt, source risk, developer form context, supported DSL
     builders, supported oracles, and cluster policy
   - output: JSON only, either `attack_dsl`, `flat_recipe`, `cluster_seed`, or
     `needs_harness_extension`
   - post-checks: `compile_attack_dsl.py --check`, `run_recipes.py`, triage,
     preview, and geometry audit

6. Kept the normal randomized / broad coverage lane in the loop:
   - source-guided DSL for targeted hypotheses
   - `generate_boolean_matrix.py` for broad boolean random/matrix coverage
   - `generate_corpus_recut_matrix.py` for saved SGT / imported body recuts
   - `run_campaign.py` or `plan_large_campaign.py` for larger integrated runs

## Windows Continuation Commands

On the Windows machine, after pulling this repo:

```powershell
cd C:\Develop\SGGK_Test_SDK
git pull --ff-only
```

Build if needed:

```powershell
cmake -S .\test_harness -B .\build\test_harness `
  -DSGGK_SDK_DIR="C:/Develop/SGGK_Agent/SGK1.4.10/SGGK" `
  -G "Visual Studio 18 2026" `
  -A x64

cmake --build .\build\test_harness --config Release --parallel
```

Run the current API smoke suite:

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

If SGGK source is available on Windows, start the source-directed lane:

```powershell
python .\test_harness\tools\scan_source_risks.py `
  C:\Develop\SGGK_Agent\SGK1.4.10\SGGK\include `
  --out .\artifacts\sdk_include_source_risk_scan `
  --max-findings 120 `
  --max-seeds 30

python .\test_harness\tools\build_source_attack_tasks.py `
  .\artifacts\sdk_include_source_risk_scan `
  --out .\artifacts\sdk_include_source_attack_tasks `
  --max-tasks 80 `
  --context-lines 12 `
  --write-dsl-seeds
```

Validate the completed source-guided additions:

```powershell
python $env:CODEX_HOME\skills\.system\skill-creator\scripts\quick_validate.py .\test_harness\skills\sggk-source-guided-workflow
python .\test_harness\tools\compile_attack_dsl.py .\test_harness\dsl\occ_source_guided_surrogate_examples.json --check --report .\artifacts\occ_source_guided_surrogate_check.json
python .\test_harness\tools\build_source_guided_cluster.py .\test_harness\dsl\source_guided_cluster_seed_smoke.json --out .\artifacts\source_guided_cluster_smoke.json
python .\test_harness\tools\compile_attack_dsl.py .\artifacts\source_guided_cluster_smoke.json --check --report .\artifacts\source_guided_cluster_check.json
```

## Safety

- Do not commit SGGK SDK source, SDK headers, binaries, licenses, build output,
  or campaign artifacts.
- Keep generated outputs under `artifacts/`.
- Keep proprietary source excerpts inside artifacts or intranet-only model
  tasks; do not add them to GitHub.
