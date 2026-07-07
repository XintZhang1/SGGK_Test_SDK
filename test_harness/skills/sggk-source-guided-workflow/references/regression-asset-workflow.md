# Regression Asset Workflow

Use this workflow after any broad or source-guided SGGK test run that should
survive as a version-regression monitor.

The goal is not to commit raw campaign artifacts. The goal is to keep a compact
local asset pack that can replay the same tested surface after an SDK update and
report:

- baseline bugs that are fixed in the new SDK
- baseline bugs that still fail
- baseline bugs whose failure mode changed
- baseline passing cases that became new issues
- explicit unsupported cases that started passing or changed into candidate bugs
- topo-track or contact-candidate localization for each reported issue

## Asset Shape

A regression asset pack contains:

- `asset_manifest.json`: source commit, tool/form references, dataset label, baseline SDK label, campaign paths, preserved cases, and baseline status counts
- `regression_recipe_list.txt`: replay list for the preserved recipes
- `recipes/regression_cases/*.json`: simplified replayable test cases copied from the run
- `bug_registry.json`: baseline candidate bugs in the same shape expected by registry replay tools
- `asset_report.md`: human-readable replay and compare instructions

The asset may still reference local ABC/SGT dataset paths inside recipes. Keep it
under `artifacts/` or intranet storage. Do not commit SDK files, generated
artifacts, ABC data, copied SGTs, build outputs, or binaries.

## Snapshot After A Run

After a campaign or shard finishes, create the asset pack:

```powershell
python .\test_harness\tools\manage_regression_assets.py snapshot `
  --campaign .\artifacts\abc_boolean_mass_recut_100k_shard0000_smoke `
  --out .\artifacts\regression_assets\abc_boolean_mass_recut_100k_shard0000 `
  --asset-id abc_boolean_mass_recut_100k_shard0000 `
  --sdk-version SGK1.4.10 `
  --dataset-label abc_fetch_40chunk_sample50 `
  --max-cases 5000 `
  --pass-sample 2000
```

For a small shard this usually preserves every executed recipe. For a full
100k+ campaign, keep all failures plus a deterministic sample of baseline
passes. Increase `--max-cases` and `--pass-sample` when local storage allows a
larger regression monitor.

Explicit unsupported kernel responses are stored as `known_unsupported`, not as
candidate bugs. Add project-specific messages with `--unsupported-pattern`.

## Replay On A New SDK

After updating/rebuilding the SDK runner, replay the saved cases:

```powershell
python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe-list .\artifacts\regression_assets\abc_boolean_mass_recut_100k_shard0000\regression_recipe_list.txt `
  --out .\artifacts\regression_replay\abc_boolean_mass_recut_100k_shard0000\run `
  --triage-out .\artifacts\regression_replay\abc_boolean_mass_recut_100k_shard0000\triage `
  --jobs 1 `
  --timeout 180
```

If localization matters for a reduced candidate, regenerate that focused recipe
with `topo_track=true` or enable the corresponding campaign option before
snapshotting. Without topo-track, triage still records target/tool bbox contact
candidates as a fallback localization signal.

## Compare Versions

Compare the new replay with the baseline asset:

```powershell
python .\test_harness\tools\manage_regression_assets.py compare `
  --asset .\artifacts\regression_assets\abc_boolean_mass_recut_100k_shard0000 `
  --new-run .\artifacts\regression_replay\abc_boolean_mass_recut_100k_shard0000\run\recipe_summary.json `
  --new-triage .\artifacts\regression_replay\abc_boolean_mass_recut_100k_shard0000\triage\triage_summary.json `
  --out .\artifacts\regression_compare\abc_boolean_mass_recut_100k_shard0000 `
  --new-sdk-version SGK1.4.11
```

The comparison report groups results into fixed, new issues, changed failures,
still failing, unsupported changes, and unavailable cases. For each new or
changed issue, inspect the `new_track` block:

- `source=topo_track` means the SDK topo-track report identified likely input
  ancestors.
- `source=input_bbox_contact_fallback` means topo-track did not localize the
  failure, so triage used input topology bbox contact candidates.
- `source=none` means the run lacks usable localization evidence; rerun a
  reduced/focused case with topo tracking enabled.

## Promotion Rule

Do not promote every failure from a broad campaign. Promote a bug record only
after:

- unsupported/not-allowed responses are filtered out
- the representative recipe reproduces on replay
- the case is reduced or the dataset dependency is explicitly documented
- topo-track/contact localization is attached
- the regression asset can replay the case on the current Windows harness

The persistent GitHub artifact should be the workflow, form, generator code,
skill instructions, and human report. The heavy replay asset stays local.
