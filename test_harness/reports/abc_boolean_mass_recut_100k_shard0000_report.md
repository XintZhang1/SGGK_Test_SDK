# ABC Boolean Mass Recut 100k Shard 0000 Report

- Date: 2026-07-07
- Branch: `codex/abc-dataset-harness`
- Commit: `c7e5725`
- Windows artifact root: `C:\Develop\SGGK_Test_SDK\artifacts\abc_boolean_mass_recut_100k_shard0000_smoke`
- Dataset root: `C:\Develop\SGGK_Agent\artifacts\abc_fetch_40chunk_sample50`
- Runner: `build\test_harness\Release\sggk_case_runner.exe`

## Scope

This run exercises the new fixed-code `campaign_command` flow for the `iface_15_boolean_abc_mass_recut` form. The harness generated a 100k-scale ABC loaded-SGT boolean recut corpus, then executed shard `0/1000`.

- Preset: `stress`
- Recipes per usable source: `75`
- Usable SGT sources: `1334`
- Skipped SGT sources during generation: `24`
- Generated recipes: `100000`
- Executed shard size: `100`
- Passed: `99`
- Failed before filtering: `1`
- Known unsupported groups filtered: `0`
- Candidate bug groups: `1`

Generation used `--no-exact-bbox-probe` for this first full-scale smoke so the 100k plan could be produced quickly from serialized SGT bbox estimates. Exact bbox probing should be re-enabled for focused replay/reduction.

## Candidate Bug

Fingerprint: `11cd43e5e404443e`

- Case: `abc_mass_recut_result_1_8b09f370_cylinder_tangent_x_intersection_exact_4fce8e0f85`
- API: `api_boolean`
- Variant: `cylinder_tangent_x_intersection_exact`
- Boolean type: `INTERSECTION`
- Target: `loaded_sgt`
- Tool: `solid_cylinder`
- Runner return code: `2`
- SDK error code: `67108917`
- SDK error message: `迭代求解失败`
- Timed out: `false`
- Unsupported filter: no match
- Single replay: reproduced 1/1 with the same SDK error code `67108917`
- Replay validation/topology reports: no oracle/topology failure; failure is the SDK API status itself

Representative target source:

```text
C:\Develop\SGGK_Agent\artifacts\abc_fetch_40chunk_sample50\full_complex_import\00210047_3d4183fc840fc034ca5a0c1a_step_000_f28c43904e\output\result_1.sgt
```

Representative recipe:

```text
C:\Develop\SGGK_Test_SDK\artifacts\abc_boolean_mass_recut_100k_shard0000_smoke\recipes\abc_mass_recut_result_1_8b09f370_cylinder_tangent_x_intersection_exact_4fce8e0f85.json
```

Representative artifact:

```text
C:\Develop\SGGK_Test_SDK\artifacts\abc_boolean_mass_recut_100k_shard0000_smoke\run\shard_0000_of_1000\abc_mass_recut_result_1_8b09f370_cylinder_tangent_x_intersection_exact_4fce8e0f85
```

## Next Replay

Before promoting a persistent bug record:

1. Replay the representative recipe with exact bbox audit enabled.
2. Run reduction on the cylinder/tool placement and source SGT if the failure is stable.
3. Confirm the same error is not an explicit unsupported/not-allowed kernel response.
4. Preserve raw triage under `artifacts/`; commit only a reduced portable bug record after replay.
