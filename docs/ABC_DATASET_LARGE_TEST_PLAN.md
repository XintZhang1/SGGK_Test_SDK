# ABC Dataset Large Test Plan

This is the current main testing direction for the SGGK SDK harness.

## Message Harness Readiness Gate

Do not fan out a large campaign until the current commit proves all of the
following:

- full pytest, Ruff, compileall, JSON/schema/form validation;
- Release runner build and self-contained API smoke suite;
- compiled adapter registry agrees with every plugin manifest hash/version;
- parallel candidate E2E rejects a bad candidate, promotes an SDK-proven good
  candidate, de-duplicates canonical repeats, and selects deterministically;
- new API discovery/resolution -> `api_plugin_candidate` -> isolated Release build ->
  positive/negative schema -> runtime registry -> three equal semantic hashes;
- known-invalid generated geometry is excluded by deterministic failure
  qualification while SDK status/crash and ambiguous oracles remain candidates;
- eligible candidates complete three-attempt replay, signature-preserving
  reduction, and paired isolated TopoTrack capture/control;
- at least one real GLM-5.2 investigation returns a schema-valid candidate-only
  report with multiple hypotheses, evidence/counter-evidence, possible source
  locations or `source_unavailable`, registered falsification tools, and a
  stable reproduction reference.

External public-interface runs use the single locked SiliconFlow GLM-5.2 profile,
the same profile-bound manifest schema, and the same fixed gates. Protected-source
diagnostics remain an explicitly selected intranet-only workflow. Each run generates
its own immutable manifest instead of replaying an artifact across sessions.

Scale in explicit stages: one Message task, all API smokes, 100 cases, 1,000
cases in one shard, multi-shard merge/resume, then 100k+. Each stage requires
zero harness/infrastructure errors and a passing artifact verifier. SDK/test
failures may remain only if qualification, replay, and candidate reporting
finish successfully. Preflight SDK/license availability, dataset hashes, disk
space, Windows path length, timeout/jobs, and resume metadata before every fan
out.

The Message pipeline's `failure_registry/` contains unconfirmed discovery
candidates. Historical campaign `bug_registry/` outputs remain a separate
known/regression workflow and must not be treated as the same trust level.

## Goal

Use the ABC dataset as a broad and difficult modeling corpus. Prioritize:

- complex imported STEP/IGES/SGT topology
- booleans between imported bodies and generated tools
- generated sweep, support-sweep, extrude, revolve, thicken, and pre-boolean bodies
- exact contact plus `+/- 1e-5` and `+/- 1e-2` tolerance bands
- exchange roundtrip drift through STEP and IGES
- sharded large campaigns with artifact verification, geometry audit, triage, previews, and bug-record promotion

Use a dedicated local Windows workspace containing this repository, the SDK,
and ignored `artifacts/` directories. Keep ABC data and campaign artifacts out
of Git; do not rely on a machine-specific absolute path.

## Required Input

Set one of these before running:

```powershell
$ABC_ROOT = "D:\datasets\abc"
$RUNNER = ".\build\test_harness\Release\sggk_case_runner.exe"
```

If a frozen index already exists:

```powershell
$ABC_INDEX = "D:\datasets\abc\dataset_index.json"
```

## Fetch Helper

Use `test_harness/tools/fetch_abc_dataset.py` to fetch official ABC chunks into local artifacts.
It downloads the official manifests, verifies archive size and MD5, extracts samples or full chunks,
and can immediately run discovery plus CAD feature profiling.
It also writes `abc_fetch_plan.json`, `abc_fetch_plan.csv`, and `abc_fetch_plan.md` before downloading.

For the external UI, `--full-dataset` is the canonical full-v00 mode. It selects every
official STEP and metadata chunk, fully extracts them, verifies archive size and MD5,
and builds `dataset_index.json` for the campaign harness. Plan first, then start the
large transfer after confirming disk capacity:

```powershell
python .\test_harness\tools\fetch_abc_dataset.py `
  --out D:\datasets\abc_v00 `
  --download-root D:\datasets\abc_archive_cache `
  --full-dataset `
  --plan-only

python .\test_harness\tools\fetch_abc_dataset.py `
  --out D:\datasets\abc_v00 `
  --download-root D:\datasets\abc_archive_cache `
  --full-dataset
```

The helper writes polling-safe status to `abc_fetch_progress.json`. Archives are
downloaded as `*.part`, resumed when the server supports byte ranges, verified, and
atomically moved to the final `.7z` name. Completed extraction markers allow a rerun
to skip already verified archive/extraction units. Only one fetch process may mutate a
given download cache at a time.

Full discovery also writes `dataset_index.paths.txt` and the compact
`dataset_index.meta.json`. The metadata binds the index with SHA-256 and lets the UI
inspect very large indexes without loading the complete JSON document into memory.
Each campaign-ready file entry contains its own SHA-256; a missing digest, a changed
STEP file, a shortened selection, or failed triage makes the fixed `abc_step_import`
lane fail closed.

The UI may also select an existing fetch root or `dataset_index.json`. A raw directory
containing STEP files is detected but is not campaign-ready until a dataset index has
been generated; this keeps million-file discovery out of the HTTP request thread.

### Harness STEP import lane

When the UI binds a ready ABC `dataset_index.json`, starting the public function
`step_import` enables the fixed `abc_step_import` campaign profile. The model can
choose only bounded case, shard, job, timeout, and resume values. Host code binds the
actual external index path and expands the request to `run_corpus.py --dataset-list`;
the absolute dataset path is never included in the model prompt. This lane preserves
index order, supports stable shards/resume, verifies input hashes and requested case
coverage immediately before execution, and writes deterministic triage artifacts.
The existing `abc_boolean_mass_recut` profile remains for imported
SGT outputs rather than raw STEP indexes.

Smallest-chunk sample:

```powershell
python .\test_harness\tools\fetch_abc_dataset.py `
  --out .\artifacts\abc_fetch_2chunk_smoke `
  --download-root .\artifacts\abc_fetch_smoke\downloads `
  --smallest-step 2 `
  --sample-count 50 `
  --extract-mode sample `
  --run-discovery `
  --run-feature-profile `
  --fail-on-command
```

Full compressed STEP+meta planning without downloading:

```powershell
python .\test_harness\tools\fetch_abc_dataset.py `
  --out .\artifacts\abc_fetch_full_plan `
  --download-root .\artifacts\abc_fetch_smoke\downloads `
  --all-chunks `
  --plan-only `
  --extract-mode none
```

The 2026-07-06 full v00 plan found 100 STEP chunks and 100 meta chunks:

- STEP+meta selected bytes: 106733727236 bytes, 99.404 GiB
- STEP bytes: 106651993150 bytes, 99.327 GiB
- meta bytes: 81734086 bytes, 0.076 GiB
- current disk free near `artifacts\abc_fetch_smoke\downloads`: 158.25 GiB at planning time

For full extraction of selected chunks, change `--extract-mode full` and pass explicit
`--chunk` or `--chunk-range`. Keep archives and extracted ABC files under `artifacts/`
or another non-Git dataset root.

`fetch_abc_dataset.py` retries interrupted curl downloads. If an endpoint leaves a partial
archive but does not support byte-range resume, the tool falls back to deleting the partial
archive and retrying a clean full download.

## Sample Smoke Helper

Use `test_harness/tools/run_abc_sample_smoke.py` after a fetch/extract pass has produced
`dataset_index.json`, `complex_dataset_index.json`, and `cad_feature_profile.json`.

```powershell
python .\test_harness\tools\run_abc_sample_smoke.py `
  --fetch-root .\artifacts\abc_fetch_10chunk_sample `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --out .\artifacts\abc_fetch_10chunk_sample\sample_smoke `
  --top-import-limit 48 `
  --recut-source-limit 12 `
  --recut-limit 36 `
  --timeout 180 `
  --jobs 1
```

The helper runs dataset audit, preserves `complex_dataset_index.json` complexity order
for top-N corpus import, renders previews, generates exact-bbox corpus recut recipes,
runs recut, triages artifacts, runs geometry audit, and writes
`abc_sample_smoke_summary.json` plus `abc_sample_smoke_report.md`.

## Phase 0: Discover And Audit

Freeze the dataset before any long run:

```powershell
python .\test_harness\tools\discover_corpus.py `
  $ABC_ROOT `
  --out .\artifacts\abc_discovery\dataset_index.json `
  --paths-out .\artifacts\abc_discovery\dataset_index.paths.txt `
  --report .\artifacts\abc_discovery\dataset_index.md `
  --hash-inputs

python .\test_harness\tools\audit_corpus_dataset.py `
  --dataset-list .\artifacts\abc_discovery\dataset_index.json `
  --out .\artifacts\abc_discovery\dataset_audit `
  --require-hashes
```

Profile complex STEP/IGES features so ABC can be split into focused hard subsets:

```powershell
python .\test_harness\tools\profile_cad_features.py `
  --dataset-list .\artifacts\abc_discovery\dataset_index.json `
  --out .\artifacts\abc_feature_profile\cad_feature_profile.json `
  --paths-out .\artifacts\abc_feature_profile\complex_paths.txt `
  --subset-out .\artifacts\abc_feature_profile\complex_dataset_index.json `
  --report .\artifacts\abc_feature_profile\cad_feature_profile.md `
  --min-score 8
```

## Phase 1: Proof-Of-Life Smoke

Use a small but hard sample before any overnight run:

```powershell
python .\test_harness\tools\plan_large_campaign.py `
  --runner $RUNNER `
  --out .\artifacts\abc_plan_smoke `
  --profile smoke `
  --dataset-list .\artifacts\abc_feature_profile\complex_dataset_index.json `
  --shards 2 `
  --jobs 1 `
  --timeout 120 `
  --hash-recipes `
  --dataset-audit-require-hashes `
  --profile-cad-features `
  --cad-feature-min-score 8 `
  --corpus-recut-require-exact-bbox-probe `
  --corpus-preserve-input-order `
  --bundle-zip `
  --promote-bug-records

powershell -ExecutionPolicy Bypass -File .\artifacts\abc_plan_smoke\commands\run_all_with_preflight.ps1
```

Review:

- `artifacts/abc_plan_smoke/merged/campaign_shards_report.md`
- `artifacts/abc_plan_smoke/merged/campaign_verification/campaign_verification.md`
- preview contact sheets under `merged/previews/`
- geometry audit summaries under `merged/geometry_audit/`
- `merged/bug_registry/`
- promoted candidates under `merged/promoted_bug_records/`

## Phase 2: Complex ABC Subset

Run the complex-feature subset with standard breadth:

```powershell
python .\test_harness\tools\plan_large_campaign.py `
  --runner $RUNNER `
  --out .\artifacts\abc_plan_complex_standard `
  --profile standard `
  --dataset-list .\artifacts\abc_discovery\dataset_index.json `
  --use-cad-feature-subset `
  --cad-feature-min-score 8 `
  --shards 8 `
  --jobs 2 `
  --timeout 180 `
  --hash-recipes `
  --dataset-audit-require-hashes `
  --corpus-recut-require-exact-bbox-probe `
  --corpus-preserve-input-order `
  --source-root .\SGK1.4.10\SGGK\include `
  --source-task-write-dsl-seeds `
  --reduce-stable-failures `
  --reduction-limit 5 `
  --reduction-max-trials 80 `
  --bundle-zip `
  --promote-bug-records `
  --replay-promoted-bug-records

powershell -ExecutionPolicy Bypass -File .\artifacts\abc_plan_complex_standard\commands\run_all_with_preflight.ps1
```

This is the main lane for complex booleans and sweep-heavy imported topology.

## Phase 3: Full ABC Stress

After the complex subset is understood, run all frozen ABC entries:

```powershell
python .\test_harness\tools\plan_large_campaign.py `
  --runner $RUNNER `
  --out .\artifacts\abc_plan_full_stress `
  --profile stress `
  --dataset-list .\artifacts\abc_discovery\dataset_index.json `
  --shards 16 `
  --jobs 2 `
  --timeout 240 `
  --hash-recipes `
  --dataset-audit-require-hashes `
  --profile-cad-features `
  --cad-feature-min-score 8 `
  --corpus-recut-require-exact-bbox-probe `
  --corpus-preserve-input-order `
  --source-root .\SGK1.4.10\SGGK\include `
  --source-task-write-dsl-seeds `
  --reduce-stable-failures `
  --reduction-limit 8 `
  --reduction-max-trials 120 `
  --bundle-zip `
  --promote-bug-records `
  --replay-promoted-bug-records

powershell -ExecutionPolicy Bypass -File .\artifacts\abc_plan_full_stress\commands\run_all_with_preflight.ps1
```

## Review Policy

- Treat import failures, roundtrip zero-body results, validation-only boolean failures, crashes, and timeouts separately.
- Do not rely on API success alone. Require `validation.json`, oracle coverage, preview/contact sheets, and geometry audit.
- Prefer exact coordinate-plane bbox probes for corpus recut placement.
- Promote only stable, replayable failures. Keep campaign-local drafts under `artifacts/` until portability audit passes.
- For checked bug records, copy only replay-minimal SGT assets into durable fixtures; never commit ABC dataset files wholesale.

## Current Smoke: Chunk 0027 Sample

Completed on 2026-07-06 under `artifacts/abc_fetch_smoke`.

- Downloaded official ABC manifests: `step_v00.txt`, `meta_v00.txt`, `size.yml`, `md5.yml`.
- Selected smallest STEP chunk `abc_0027_step_v00.7z` from the manifest.
- Downloaded:
  - `abc_0027_step_v00.7z`: 597802299 bytes, MD5 `525d4847fa658c313400bcb418250620`
  - `abc_0027_meta_v00.7z`: 810813 bytes, MD5 `3280bc6e660a81af0efef9d0427ba7d8`
- Extracted first 50 STEP files and matching metadata to `artifacts/abc_fetch_smoke/sample50`.
- Discovery wrote `artifacts/abc_fetch_smoke/dataset_index.json`: 50 STEP files, 62980803 bytes.
- Dataset audit passed with `ok=True`, `errors=0`, `warnings=2`.
- CAD feature profile marked 49/50 files complex. Feature totals included:
  - `advanced_face`: 14714
  - `bspline_surface`: 2184
  - `bspline_curve`: 2207
  - `bounded_surface`: 458
  - `toroidal_surface`: 587
- STEP import smoke over 5 files passed 5/5 at `artifacts/abc_fetch_smoke/corpus_import_smoke`.
- Import preview contact sheet: `artifacts/abc_fetch_smoke/corpus_import_smoke_preview/contact.png`.
- Recut smoke generated 9 exact-bbox boolean recipes from 3 imported SGTs.
- Recut run passed 9/9 at `artifacts/abc_fetch_smoke/recut_run`.
- Recut triage reported `failures=0`, `groups=0`, `command_failures=0`.
- Recut geometry audit reported 9 cases, 0 duplicate input groups, 0 full geometry duplicate groups, 0 tolerance mismatches, exact input bbox enabled.
- Recut preview contact sheet: `artifacts/abc_fetch_smoke/recut_preview/contact.png`.

## Current Smoke: Chunks 0014 And 0027 Sample

Completed on 2026-07-06 under `artifacts/abc_fetch_2chunk_smoke`.

- Used `test_harness/tools/fetch_abc_dataset.py` to select the 2 smallest STEP chunks from official ABC manifests.
- Reused verified chunk 0027 archives and downloaded chunk 0014 into `artifacts/abc_fetch_smoke/downloads`.
- Verified archives:
  - `abc_0027_step_v00.7z`: 597802299 bytes, MD5 `525d4847fa658c313400bcb418250620`
  - `abc_0027_meta_v00.7z`: 810813 bytes, MD5 `3280bc6e660a81af0efef9d0427ba7d8`
  - `abc_0014_step_v00.7z`: 635922658 bytes, MD5 `9dd577d475fa078a7d9314d4a010b2b0`
  - `abc_0014_meta_v00.7z`: 648522 bytes, MD5 `083f470bc4639c1495da2802ddb34462`
- Extracted first 50 STEP files and matching metadata from each chunk.
- Discovery wrote `artifacts/abc_fetch_2chunk_smoke/dataset_index.json`: 100 STEP files, 75228374 bytes.
- Dataset audit passed with `ok=True`, `errors=0`, `warnings=2`, hash coverage 100/100, duplicate content groups 0.
- CAD feature profile marked 96/100 files complex. Feature totals included:
  - `advanced_face`: 18685
  - `bspline_curve`: 4017
  - `bspline_surface`: 2425
  - `bounded_surface`: 578
  - `surface_of_linear_extrusion`: 378
  - `surface_of_revolution`: 223
  - `toroidal_surface`: 801
- Initial path-ordered import smoke over 20 complex files passed 20/20 at `artifacts/abc_fetch_2chunk_smoke/corpus_import_smoke`.
- Top-complex import smoke over 24 files passed 23/24 at `artifacts/abc_fetch_2chunk_smoke/top_complex_import`.
- Stable failure seed:
  - Source: `artifacts/abc_fetch_2chunk_smoke/extracted/chunk_0027_sample50/00270005/00270005_57f1fbc32f8b6410fc60afcd_step_002.step`
  - Size: 44363292 bytes
  - Feature profile: complexity score 420, `advanced_face=9984`, `bspline_surface=1625`, `bounded_surface=220`, `toroidal_surface=422`, `spherical_surface=162`, `conical_surface=341`, `cylindrical_surface=3118`
  - Failure fingerprint: `af7e09945094d4ad`
  - Error: `v is out of srf v range`
  - Validation failure: `result_body_count_below_min actual=0 min=1`
  - Reproduced with default AP203, all B-spline conversion options, and AP242.
- Top-complex import preview contact sheet: `artifacts/abc_fetch_2chunk_smoke/top_complex_import_preview/contact.png`.
- Top-complex recut generated 24 exact-bbox boolean recipes from 8 imported SGTs.
- Top-complex recut run passed 24/24 at `artifacts/abc_fetch_2chunk_smoke/top_complex_recut_run`.
- Top-complex recut triage reported `failures=0`, `groups=0`, `command_failures=0`.
- Top-complex recut geometry audit reported 24 cases, exact input bbox enabled, tolerance mismatches 0, same-boolean duplicate input groups 3.
- Top-complex recut preview contact sheet: `artifacts/abc_fetch_2chunk_smoke/top_complex_recut_preview/contact.png`.

## Current Smoke: Chunks 0014, 0025, And 0027 Sample100

Completed on 2026-07-06 under `artifacts/abc_fetch_3chunk_sample100`.

- Reused verified chunks 0014 and 0027; added chunk 0025.
- Verified new archives:
  - `abc_0025_step_v00.7z`: 680215717 bytes, MD5 `c218ef3c5eb29d059d49341cb9c8fb9f`
  - `abc_0025_meta_v00.7z`: 801206 bytes, MD5 `97b2f220e7e1321705ca9a5356c3d4bd`
- Extracted first 100 STEP files and matching metadata from chunks 0014, 0025, and 0027.
- Discovery wrote `artifacts/abc_fetch_3chunk_sample100/dataset_index.json`: 300 STEP files, 101768141 bytes.
- CAD feature profile marked 281/300 files complex. Feature totals included:
  - `advanced_face`: 28209
  - `bspline_curve`: 9315
  - `bspline_surface`: 2610
  - `bounded_surface`: 657
  - `offset_surface`: 13
  - `surface_of_linear_extrusion`: 1930
  - `surface_of_revolution`: 231
  - `toroidal_surface`: 1060
- `run_abc_sample_smoke.py` ran dataset audit, top-complex import, preview, recut generation, recut run, triage, and geometry audit under `artifacts/abc_fetch_3chunk_sample100/sample_smoke`.
- Dataset audit passed with `ok=True`, `files=300`, `errors=0`, `warnings=2`.
- Top-complex import over the first 48 complexity-ranked files passed 45/48, failed 3, timed out 0.
- Stable failure groups:
  - `af7e09945094d4ad`: `00270005_57f1fbc32f8b6410fc60afcd_step_002.step`, error `v is out of srf v range`
  - `b47a7dce1aac5728`: `00270069_57f2364a93832a109099c263_step_000.step` and `00270070_2ce7e5395c9076e67e35e5cc_step_000.step`, error `Not accepted type!`
- The `b47a7dce1aac5728` failures reproduced with AP242 and with all STEP B-spline conversion options enabled.
- Top-complex import preview contact sheet: `artifacts/abc_fetch_3chunk_sample100/sample_smoke/top_complex_import_preview/contact.png`.
- Recut generated 36 exact-bbox boolean recipes from 12 imported SGTs.
- Recut run passed 36/36, failed 0, timed out 0.
- Recut triage reported `failures=0`, `groups=0`, `command_failures=0`.
- Recut geometry audit reported 36 cases, exact input bbox enabled, tolerance mismatches 0, same-boolean duplicate input groups 3.
- Recut preview contact sheet: `artifacts/abc_fetch_3chunk_sample100/sample_smoke/top_complex_recut_preview/contact.png`.

## Current Smoke: Chunks 0014, 0025, 0027, And 0028 Sample100

Started on 2026-07-06 and completed just after local midnight on 2026-07-07 under `artifacts/abc_fetch_4chunk_sample100`.

- Reused verified chunks 0014, 0025, and 0027; added chunk 0028 while the 10-chunk fetch continued in the background.
- Verified new archives:
  - `abc_0028_step_v00.7z`: 686672816 bytes, MD5 `199e8141fefd510f060257b44bde995d`
  - `abc_0028_meta_v00.7z`: 799609 bytes, MD5 `16a9d02b4f4f97957f92b7bd96aa3d7d`
- Extracted first 100 STEP files and matching metadata from chunks 0014, 0025, 0027, and 0028.
- Discovery wrote `artifacts/abc_fetch_4chunk_sample100/dataset_index.json`: 400 STEP files, 123915096 bytes.
- CAD feature profile marked 369/400 files complex. Feature totals included:
  - `advanced_face`: 34578
  - `bspline_curve`: 12374
  - `bspline_surface`: 3156
  - `bounded_surface`: 916
  - `offset_surface`: 25
  - `surface_of_linear_extrusion`: 2495
  - `surface_of_revolution`: 231
  - `toroidal_surface`: 1494
- `run_abc_sample_smoke.py` ran dataset audit, top-complex import, preview, recut generation, recut run, triage, and geometry audit under `artifacts/abc_fetch_4chunk_sample100/sample_smoke`.
- Dataset audit passed with `ok=True`, `files=400`, `errors=0`, `warnings=2`.
- Top-complex import over the first 64 complexity-ranked files passed 59/64, failed 5, timed out 0.
- Stable failure groups:
  - `af7e09945094d4ad`: `00270005_57f1fbc32f8b6410fc60afcd_step_002.step`, error `v is out of srf v range`
  - `b47a7dce1aac5728`: `00270069_57f2364a93832a109099c263_step_000.step`, `00270070_2ce7e5395c9076e67e35e5cc_step_000.step`, `00280019_42b83b06661b84f368807d96_step_000.step`, and `00280059_4356230064e30f3bd63bdd2e_step_000.step`, error `Not accepted type!`
- New chunk 0028 `b47a7dce1aac5728` members both include `offset_surface`; `00280019` combines offset, B-spline, extrusion, toroidal, conical, and cylindrical surfaces, while `00280059` combines offset, bounded, B-spline, and spherical surfaces.
- Top-complex import triage reported `failures=5`, `groups=2`, `command_failures=0`.
- Top-complex import preview contact sheet: `artifacts/abc_fetch_4chunk_sample100/sample_smoke/top_complex_import_preview/contact.png`.
- Recut generated 48 exact-bbox boolean recipes from 16 imported SGTs.
- Recut run passed 48/48, failed 0, timed out 0.
- Recut triage reported `failures=0`, `groups=0`, `command_failures=0`.
- Recut geometry audit reported 48 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 3, same-boolean duplicate input groups 3.
- Recut preview contact sheet: `artifacts/abc_fetch_4chunk_sample100/sample_smoke/top_complex_recut_preview/contact.png`.

### Expanded 4-Chunk Full-Complex Sweep

Completed on 2026-07-07 under `artifacts/abc_fetch_4chunk_sample100`.

- Ran the full `complex_dataset_index.json` for the 4-chunk sample with `--preserve-input-order`.
- Full complex STEP import covered 369/369 complexity-ranked files:
  - Passed: 363
  - Failed: 6
  - Timed out: 0
  - Triage: `failures=6`, `groups=2`, `command_failures=0`
- Failure groups remained stable:
  - `af7e09945094d4ad`: 1 case, `v is out of srf v range`
  - `b47a7dce1aac5728`: 5 cases, `Not accepted type!`
- The expanded sweep added one lower-score `b47a7dce1aac5728` member:
  - `00270055_e4e25395b0a7059376940e90_step_000.step`
  - Feature profile: complexity score 58, `advanced_face=9`, `bspline_curve=5`, `offset_surface=2`, `surface_of_revolution=4`, `cylindrical_surface=2`
- Generated 192 exact-bbox recut boolean recipes from 64 successfully imported SGTs.
- Expanded recut run passed 192/192, failed 0, timed out 0.
- Expanded recut triage reported `failures=0`, `groups=0`, `command_failures=0`.
- Expanded recut geometry audit reported 192 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 15, same-boolean duplicate input groups 18.
- Expanded recut preview contact sheet: `artifacts/abc_fetch_4chunk_sample100/full_complex_recut_preview/contact.png`.

## Current Smoke: Chunks 0014, 0025, 0027, 0028, And 0061 Sample100

Completed on 2026-07-07 under `artifacts/abc_fetch_5chunk_sample100` while the 10-chunk fetch continued in the background.

- Reused verified chunks 0014, 0025, 0027, and 0028; added chunk 0061.
- Verified new archives:
  - `abc_0061_step_v00.7z`: 712969275 bytes, MD5 `666b3d4a713dd6e223e1a23f71fe41d6`
  - `abc_0061_meta_v00.7z`: 854761 bytes, MD5 `22bd5e989ab7c6f590dd8d6a2680ef14`
- Extracted first 100 STEP files and matching metadata from chunks 0014, 0025, 0027, 0028, and 0061.
- Discovery wrote `artifacts/abc_fetch_5chunk_sample100/dataset_index.json`: 500 STEP files, 162115552 bytes.
- CAD feature profile marked 459/500 files complex. Feature totals included:
  - `advanced_face`: 45370
  - `bspline_curve`: 18119
  - `bspline_surface`: 4013
  - `bounded_surface`: 1274
  - `conical_surface`: 2184
  - `cylindrical_surface`: 12385
  - `offset_surface`: 38
  - `surface_of_linear_extrusion`: 2696
  - `surface_of_revolution`: 695
  - `toroidal_surface`: 1530
- `run_abc_sample_smoke.py` ran dataset audit, top-complex import, preview, recut generation, recut run, triage, and geometry audit under `artifacts/abc_fetch_5chunk_sample100/sample_smoke`.
- Dataset audit passed with `ok=True`, `files=500`, `errors=0`, `warnings=2`.
- Top-complex import over the first 96 complexity-ranked files passed 89/96, failed 7, timed out 0.
- Top-complex import triage reported `failures=7`, `groups=3`, `command_failures=0`.
- Import failure groups:
  - `af7e09945094d4ad`: `00270005_57f1fbc32f8b6410fc60afcd_step_002.step`, error `v is out of srf v range`
  - `b47a7dce1aac5728`: 5 cases, error `Not accepted type!`, including new chunk 0061 member `00610046_7f7c3f19744badf744038471_step_000.step`
  - `b40fa5e755823e04`: `00610051_6d8088deb88c490b4ce3f6f8_step_000.step`, error `the residual legal number of knots is smaller than 1!`
- New chunk 0061 import-failure members:
  - `00610046_7f7c3f19744badf744038471_step_000.step`: complexity score 435, `bspline_surface=76`, `bspline_curve=181`, `bounded_surface=30`, `advanced_face=91`, `offset_surface=12`, `surface_of_linear_extrusion=4`, `cylindrical_surface=7`
  - `00610051_6d8088deb88c490b4ce3f6f8_step_000.step`: complexity score 419, `bspline_surface=194`, `bspline_curve=219`, `bounded_surface=97`, `advanced_face=213`, `toroidal_surface=7`, `conical_surface=9`, `cylindrical_surface=42`
- Top-complex import preview contact sheet: `artifacts/abc_fetch_5chunk_sample100/sample_smoke/top_complex_import_preview/contact.png`.
- Recut generated 96 exact-bbox boolean recipes from 32 imported SGTs.
- Recut run passed 94/96, failed 2, timed out 0.
- Recut triage reported `failures=1`, `groups=1`, `command_failures=1`.
- Stable recut failures reproduced individually:
  - `abc_sample_recut_result_1_b19863b3_cylinder_tangent_x_subtraction_gap_geom_tol_388ce6df5b`: return code 2, API succeeded but validation failed with `result_body_count_below_min actual=0 min=1`; target source `00250080_57dfcdb1f3489110df7cbf30_step_083.step`
  - `abc_sample_recut_result_1_7ceeb4ff_cylinder_tangent_x_subtraction_overlap_geom_tol_1e5e63dd8e`: return code `3221225477` (`0xC0000005` access violation); target source `00250092_67f8b52e4530fe14860418d6_step_000.step`
- Recut geometry audit reported 96 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 3, same-boolean duplicate input groups 3.
- Recut preview contact sheet: `artifacts/abc_fetch_5chunk_sample100/sample_smoke/top_complex_recut_preview/contact.png`.

### Expanded 5-Chunk Full-Complex Sweep

Completed on 2026-07-07 under `artifacts/abc_fetch_5chunk_sample100`.

- Ran the full `complex_dataset_index.json` for the 5-chunk sample with `--preserve-input-order`.
- Full complex STEP import covered 459/459 complexity-ranked files:
  - Passed: 450
  - Failed: 9
  - Timed out: 0
  - Triage: `failures=9`, `groups=3`, `command_failures=0`
- Failure groups remained the same three groups from the 96-case smoke.
- The expanded sweep added two lower-score `b47a7dce1aac5728` members beyond the 96-case smoke:
  - `00270055_e4e25395b0a7059376940e90_step_000.step`: complexity score 58, `advanced_face=9`, `bspline_curve=5`, `offset_surface=2`, `surface_of_revolution=4`, `cylindrical_surface=2`
  - `00610083_c53140d9321926058d5bb2d5_step_000.step`: complexity score 74, `advanced_face=7`, `bounded_surface=2`, `bspline_curve=4`, `bspline_surface=4`, `offset_surface=1`, `cylindrical_surface=2`

## Current Smoke: Chunks 0014, 0025, 0026, 0027, 0028, And 0061 Sample100

Completed on 2026-07-07 under `artifacts/abc_fetch_6chunk_sample100` while the 10-chunk fetch continued in the background.

- Reused verified chunks 0014, 0025, 0027, 0028, and 0061; added chunk 0026.
- Verified new archives:
  - `abc_0026_step_v00.7z`: 713401482 bytes, MD5 `22f2a119ef655abb853e49f5d52d2176`
  - `abc_0026_meta_v00.7z`: 835973 bytes, MD5 `b2f864e5a80e7bec7c5bd2a5e1c5fb85`
- Extracted first 100 STEP files and matching metadata from chunks 0014, 0025, 0026, 0027, 0028, and 0061.
- Discovery wrote `artifacts/abc_fetch_6chunk_sample100/dataset_index.json`: 600 STEP files, 201057884 bytes.
- CAD feature profile marked 550/600 files complex. Feature totals included:
  - `advanced_face`: 56252
  - `bspline_curve`: 21049
  - `bspline_surface`: 4367
  - `bounded_surface`: 1439
  - `conical_surface`: 2420
  - `cylindrical_surface`: 15374
  - `offset_surface`: 46
  - `surface_of_linear_extrusion`: 2853
  - `surface_of_revolution`: 826
  - `toroidal_surface`: 2097
  - `trimmed_curve`: 21
- `run_abc_sample_smoke.py` ran dataset audit, top-complex import, preview, recut generation, recut run, triage, and geometry audit under `artifacts/abc_fetch_6chunk_sample100/sample_smoke`.
- Dataset audit passed with `ok=True`, `files=600`, `errors=0`, `warnings=2`.
- Top-complex import over the first 128 complexity-ranked files passed 119/128, failed 9, timed out 0.
- Top-complex import triage reported `failures=9`, `groups=3`, `command_failures=0`.
- Import failure groups remained the same three groups:
  - `af7e09945094d4ad`: `00270005_57f1fbc32f8b6410fc60afcd_step_002.step`, error `v is out of srf v range`
  - `b47a7dce1aac5728`: 7 cases in the 128-case smoke, error `Not accepted type!`, including new chunk 0026 members `00260002_57e8acbfd91ac010bb7a7b6e_step_000.step` and `00260038_5dbc2c41e927bf10eaacea64_step_000.step`
  - `b40fa5e755823e04`: `00610051_6d8088deb88c490b4ce3f6f8_step_000.step`, error `the residual legal number of knots is smaller than 1!`
- New chunk 0026 `b47a7dce1aac5728` members:
  - `00260002_57e8acbfd91ac010bb7a7b6e_step_000.step`: complexity score 539, `advanced_face=2023`, `bspline_curve=689`, `bspline_surface=41`, `offset_surface=4`, `surface_of_linear_extrusion=108`, `surface_of_revolution=3`, `toroidal_surface=436`, `conical_surface=95`, `cylindrical_surface=656`
  - `00260038_5dbc2c41e927bf10eaacea64_step_000.step`: complexity score 236, `advanced_face=11`, `bounded_surface=8`, `bspline_curve=9`, `bspline_surface=16`, `offset_surface=4`
- Top-complex import preview contact sheet: `artifacts/abc_fetch_6chunk_sample100/sample_smoke/top_complex_import_preview/contact.png`.
- Recut generated 120 exact-bbox boolean recipes from 40 imported SGTs.
- Recut run passed 109/120, failed 11, timed out 0.
- Recut triage reported `failures=10`, `groups=9`, `command_failures=1`.
- Recut failure classes:
  - 8 failures across 3 target SGTs returned `wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now.`
  - 1 failure returned API success but validation failed with `result_body_count_below_min actual=0 min=1`
  - 1 command failure returned `3221225477` (`0xC0000005` access violation)
- Recut targets worth preserving for regression:
  - `00260011_57e8b8e6c3c68e110b35cc53_step_000.step`: complexity score 512, `advanced_face=539`, `bspline_surface=166`, `bspline_curve=790`, `trimmed_curve=8`, `bounded_surface=80`, `toroidal_surface=22`, `conical_surface=27`, `cylindrical_surface=147`
  - `00260006_628bc5759b7b7c65f3494b5f_step_001.step`: complexity score 316, `advanced_face=248`, `bspline_surface=16`, `bspline_curve=40`, `trimmed_curve=4`, `bounded_surface=8`, `cylindrical_surface=118`
  - `00260007_628bc5759b7b7c65f3494b5f_step_002.step`: complexity score 127, `advanced_face=17`, `bspline_surface=4`, `bspline_curve=12`, `trimmed_curve=1`, `bounded_surface=2`, `conical_surface=2`, `cylindrical_surface=2`
- Recut geometry audit reported 120 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 3, same-boolean duplicate input groups 3.
- Recut preview contact sheet: `artifacts/abc_fetch_6chunk_sample100/sample_smoke/top_complex_recut_preview/contact.png`.

### Expanded 6-Chunk Full-Complex Sweep

Completed on 2026-07-07 under `artifacts/abc_fetch_6chunk_sample100`.

- Ran the full `complex_dataset_index.json` for the 6-chunk sample with `--preserve-input-order`.
- Full complex STEP import covered 550/550 complexity-ranked files:
  - Passed: 539
  - Failed: 11
  - Timed out: 0
  - Triage: `failures=11`, `groups=3`, `command_failures=0`
- Failure groups remained the same three groups from the 128-case smoke.
- The expanded sweep added two lower-score members beyond the 128-case smoke:
  - `00270055_e4e25395b0a7059376940e90_step_000.step`: `b47a7dce1aac5728`
  - `00610083_c53140d9321926058d5bb2d5_step_000.step`: `b47a7dce1aac5728`

## Current Smoke: Chunks 0014, 0025, 0026, 0027, 0028, 0031, And 0061 Sample100

Completed on 2026-07-07 under `artifacts/abc_fetch_7chunk_sample100` while the 10-chunk fetch continued in the background.

- Reused verified chunks 0014, 0025, 0026, 0027, 0028, and 0061; added chunk 0031.
- Verified new archives:
  - `abc_0031_step_v00.7z`: 744076956 bytes, MD5 `01c336a2b270d75d09a170ba6ec9185b`
  - `abc_0031_meta_v00.7z`: 801545 bytes, MD5 `9aab07d777fb3030c3865b8c1a2cae9d`
- Extracted first 100 STEP files and matching metadata from chunks 0014, 0025, 0026, 0027, 0028, 0031, and 0061.
- Discovery wrote `artifacts/abc_fetch_7chunk_sample100/dataset_index.json`: 700 STEP files.
- Dataset audit passed with `ok=True`, `files=700`, `errors=0`, `warnings=2`.
- CAD feature profile marked 640/700 files complex. Feature totals included:
  - `advanced_face`: 74386
  - `bspline_curve`: 31357
  - `bspline_surface`: 9168
  - `bounded_surface`: 3329
  - `conical_surface`: 2627
  - `cylindrical_surface`: 18949
  - `offset_surface`: 122
  - `spherical_surface`: 723
  - `surface_of_linear_extrusion`: 3850
  - `surface_of_revolution`: 863
  - `toroidal_surface`: 3085
  - `trimmed_curve`: 23
- `run_abc_sample_smoke.py` ran dataset audit, top-complex import, preview, recut generation, recut run, triage, and geometry audit under `artifacts/abc_fetch_7chunk_sample100/sample_smoke`.
- Top-complex import over the first 160 complexity-ranked files passed 143/160, failed 17, timed out 0.
- Top-complex import triage reported `failures=17`, `groups=3`, `command_failures=0`.
- Import failure groups remained the same three groups:
  - `af7e09945094d4ad`: `00270005_57f1fbc32f8b6410fc60afcd_step_002.step`, error `v is out of srf v range`
  - `b47a7dce1aac5728`: 15 cases in the 160-case smoke, error `Not accepted type!`, including new chunk 0031 members
  - `b40fa5e755823e04`: `00610051_6d8088deb88c490b4ce3f6f8_step_000.step`, error `the residual legal number of knots is smaller than 1!`
- New chunk 0031 `b47a7dce1aac5728` members worth keeping as regression seeds:
  - `00310005_581a7543dc0b9b106144fba3_step_002.step`: complexity score 580, `advanced_face=634`, `bspline_surface=652`, `bspline_curve=794`, `bounded_surface=325`, `offset_surface=22`, `surface_of_linear_extrusion=46`, `toroidal_surface=24`, `cylindrical_surface=114`
  - `00310048_581a7e4adcfe8810d87d2224_step_000.step`: complexity score 539, `advanced_face=2023`, `bspline_curve=689`, `bspline_surface=41`, `bounded_surface=17`, `offset_surface=4`, `surface_of_revolution=3`, `surface_of_linear_extrusion=108`, `toroidal_surface=436`, `spherical_surface=1`, `conical_surface=95`, `cylindrical_surface=656`
  - `00310003_581a7543dc0b9b106144fba3_step_000.step`: complexity score 303, `advanced_face=35`, `bspline_surface=30`, `bspline_curve=80`, `offset_surface=4`, `cylindrical_surface=3`
- Top-complex import preview contact sheet: `artifacts/abc_fetch_7chunk_sample100/sample_smoke/top_complex_import_preview/contact.png`.
- Recut generated and ran 141 exact-bbox boolean recipes from imported SGTs.
- Recut run passed 133/141, failed 8, timed out 0.
- Recut triage reported `failures=7`, `groups=6`, `command_failures=1`; the missing triage case was the command-level crash.
- Recut failure classes:
  - 6 failures across `00260006_628bc5759b7b7c65f3494b5f_step_001.step` and `00260011_57e8b8e6c3c68e110b35cc53_step_000.step` returned `wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now.`
  - 1 failure on `00250080_57dfcdb1f3489110df7cbf30_step_083.step` returned API success but validation failed with `result_body_count_below_min actual=0 min=1`
  - 1 command failure on `00250092_67f8b52e4530fe14860418d6_step_000.step` returned `3221225477` (`0xC0000005` access violation)
- Recut geometry audit reported 141 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 3, same-boolean duplicate input groups 3.
- Recut preview contact sheet: `artifacts/abc_fetch_7chunk_sample100/sample_smoke/top_complex_recut_preview/contact.png`.

### Expanded 7-Chunk Full-Complex Sweep

Completed on 2026-07-07 under `artifacts/abc_fetch_7chunk_sample100`.

- Ran the full `complex_dataset_index.json` for the 7-chunk sample with `--preserve-input-order`.
- Full complex STEP import covered 640/640 complexity-ranked files:
  - Passed: 621
  - Failed: 19
  - Timed out: 0
  - Triage: `failures=19`, `groups=3`, `command_failures=0`
- Failure groups remained the same three groups from the 160-case smoke.
- The expanded sweep added two lower-score `b47a7dce1aac5728` members beyond the 160-case smoke:
  - `00270055_e4e25395b0a7059376940e90_step_000.step`
  - `00610083_c53140d9321926058d5bb2d5_step_000.step`

## Current Smoke: Chunks 0014, 0025, 0026, 0027, 0028, 0031, 0043, And 0061 Sample100

Completed on 2026-07-07 under `artifacts/abc_fetch_8chunk_sample100`.

- Reused verified chunks 0014, 0025, 0026, 0027, 0028, 0031, and 0061; added chunk 0043.
- Verified new archives:
  - `abc_0043_step_v00.7z`: 784854142 bytes, MD5 `5fe2c521a55ae742df6e42b0614232d3`
  - `abc_0043_meta_v00.7z`: 756454 bytes, MD5 `d79a33448fc55e7dd8eba6e5936a05fc`
- Extracted first 100 STEP files and matching metadata from chunks 0014, 0025, 0026, 0027, 0028, 0031, 0043, and 0061.
- Dataset audit passed with `ok=True`, `files=800`, `errors=0`, `warnings=2`.
- CAD feature profile marked 728/800 files complex. Feature totals included:
  - `advanced_face`: 103832
  - `bspline_curve`: 39958
  - `bspline_surface`: 11840
  - `bounded_surface`: 4060
  - `conical_surface`: 3812
  - `cylindrical_surface`: 24353
  - `offset_surface`: 134
  - `spherical_surface`: 805
  - `surface_of_linear_extrusion`: 4848
  - `surface_of_revolution`: 1195
  - `toroidal_surface`: 3770
  - `trimmed_curve`: 31
- Top-complex import over the first 192 complexity-ranked files passed 168/192, failed 24, timed out 0.
- Top-complex import triage reported `failures=24`, `groups=4`, `command_failures=0`.
- Import failure groups:
  - `b47a7dce1aac5728`: 18 cases, error `Not accepted type!`, including new 0043 members `00430007_587131f579772510f11e1417_step_000.step`, `00430027_d37d4a47f38f5e9b04521bd0_step_000.step`, and `00430062_6f552b58d89b616da6dbe45e_step_000.step`
  - `b40fa5e755823e04`: 4 cases, error `the residual legal number of knots is smaller than 1!`, including new 0043 members `00430001_587130f0907944176e33c8df_step_071.step`, `00430002_587130f0907944176e33c8df_step_072.step`, and `00430084_96918a1e34c9aa0f7e2999fc_step_003.step`
  - `0fcdcb769dab1339`: `00430083_96918a1e34c9aa0f7e2999fc_step_002.step`, error `The spline curve is disconnect, please check the data!`
  - `af7e09945094d4ad`: `00270005_57f1fbc32f8b6410fc60afcd_step_002.step`, error `v is out of srf v range`
- New 0043 import-failure members worth keeping as regression seeds:
  - `00430083_96918a1e34c9aa0f7e2999fc_step_002.step`: complexity score 482, `bspline_surface=252`, `bspline_curve=676`, `bounded_surface=125`, `advanced_face=617`, `surface_of_linear_extrusion=148`, `toroidal_surface=27`, `spherical_surface=1`, `cylindrical_surface=131`
  - `00430002_587130f0907944176e33c8df_step_072.step`: complexity score 398, `advanced_face=11855`, `bspline_surface=138`, `bounded_surface=44`, `toroidal_surface=49`, `spherical_surface=24`, `conical_surface=9`, `cylindrical_surface=710`
  - `00430007_587131f579772510f11e1417_step_000.step`: complexity score 330, `bspline_surface=25`, `bspline_curve=75`, `advanced_face=27`, `offset_surface=10`
- Top-complex import preview contact sheet: `artifacts/abc_fetch_8chunk_sample100/sample_smoke/top_complex_import_preview/contact.png`.
- Recut generated and ran 189 exact-bbox boolean recipes from 63 imported SGTs; 1 source was skipped.
- Recut run passed 175/189, failed 14, timed out 0.
- Recut triage reported `failures=13`, `groups=11`, `command_failures=1`; the missing triage case was the command-level crash.
- Recut failure classes:
  - 12 failures across `00260070_a1f8f63711b90dee50a1a6ef_step_001.step`, `00260006_628bc5759b7b7c65f3494b5f_step_001.step`, `00260007_628bc5759b7b7c65f3494b5f_step_002.step`, and `00260011_57e8b8e6c3c68e110b35cc53_step_000.step` returned `wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now.`
  - 1 failure on `00250080_57dfcdb1f3489110df7cbf30_step_083.step` returned API success but validation failed with `result_body_count_below_min actual=0 min=1`
  - 1 command failure on `00250092_67f8b52e4530fe14860418d6_step_000.step` returned `3221225477` (`0xC0000005` access violation)
- Recut geometry audit reported 189 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 3, same-boolean duplicate input groups 3.
- Recut preview contact sheet: `artifacts/abc_fetch_8chunk_sample100/sample_smoke/top_complex_recut_preview/contact.png`.

## Current Smoke: 10 Smallest Chunks Sample50

Completed on 2026-07-07 under `artifacts/abc_fetch_10chunk_sample`.

- Selected chunks 0027, 0014, 0025, 0028, 0061, 0026, 0031, 0043, 0002, and 0010.
- Reused verified chunks 0014, 0025, 0026, 0027, 0028, 0031, 0043, and 0061; added chunks 0002 and 0010.
- Verified new archives:
  - `abc_0002_step_v00.7z`: 785984798 bytes, MD5 `932c177ffe1f9601d2cd163085302278`
  - `abc_0002_meta_v00.7z`: 750073 bytes, MD5 `981a680f12231feca74dc302c4d857ff`
  - `abc_0010_step_v00.7z`: 792732893 bytes, MD5 `d4e41cdee9e422072a75934727f06149`
  - `abc_0010_meta_v00.7z`: 751955 bytes, MD5 `25966159ae491d8821488721d9b6d719`
- Extracted first 50 STEP files and matching metadata from each selected chunk.
- Discovery wrote `artifacts/abc_fetch_10chunk_sample/dataset_index.json`: 500 STEP files, 403106455 bytes.
- Dataset audit passed with `ok=True`, `files=500`, `errors=0`, `warnings=2`.
- CAD feature profile marked 459/500 files complex. Feature totals included:
  - `advanced_face`: 89923
  - `bspline_curve`: 40021
  - `bspline_surface`: 11172
  - `bounded_surface`: 3365
  - `conical_surface`: 5916
  - `cylindrical_surface`: 22096
  - `offset_surface`: 124
  - `spherical_surface`: 623
  - `surface_of_linear_extrusion`: 2487
  - `surface_of_revolution`: 1864
  - `toroidal_surface`: 3319
  - `trimmed_curve`: 28
- Top-complex import over the first 192 complexity-ranked files passed 170/192, failed 22, timed out 0.
- Top-complex import triage reported `failures=22`, `groups=4`, `command_failures=0`.
- Import failure groups:
  - `b47a7dce1aac5728`: 18 cases, error `Not accepted type!`, including new chunk 0010 members `00100000_df6629a908dec75f8a69bda7_step_002.step`, `00100003_35809b42367c59ffb8aca25e_step_001.step`, `00100005_35809b42367c59ffb8aca25e_step_003.step`, and `00100031_98a8073b9515489ebfe8f5b3_step_000.step`
  - `b40fa5e755823e04`: 2 chunk 0043 cases, error `the residual legal number of knots is smaller than 1!`
  - `b25fe88c5a6af58f`: `00100023_88d67c1f3366b3b4025c88aa_step_012.step`, error `暂不支持一个loop中有超过两个uv退化点的情况. Face Id : 699`
  - `af7e09945094d4ad`: `00270005_57f1fbc32f8b6410fc60afcd_step_002.step`, error `v is out of srf v range`
- New chunk 0010 import-failure members worth keeping as regression seeds:
  - `00100023_88d67c1f3366b3b4025c88aa_step_012.step`: complexity score 420, `bspline_surface=136`, `bspline_curve=2435`, `bounded_surface=68`, `advanced_face=547`, `surface_of_revolution=435`
  - `00100000_df6629a908dec75f8a69bda7_step_002.step`: complexity score 468, `bspline_surface=90`, `bspline_curve=225`, `bounded_surface=34`, `advanced_face=164`, `offset_surface=6`, `surface_of_linear_extrusion=6`, `toroidal_surface=12`, `spherical_surface=3`, `conical_surface=2`, `cylindrical_surface=44`
  - `00100003_35809b42367c59ffb8aca25e_step_001.step`: complexity score 195, `bspline_surface=8`, `bspline_curve=16`, `bounded_surface=4`, `advanced_face=11`, `offset_surface=2`, `surface_of_linear_extrusion=4`, `spherical_surface=3`
- Top-complex import preview contact sheet: `artifacts/abc_fetch_10chunk_sample/sample_smoke/top_complex_import_preview/contact.png`.
- Recut generation considered 64 sources, used 62 sources, skipped 2 sources, and generated 186 exact-bbox boolean recipes:
  - `00020027_99f82dfd033e43489c58125d_step_000.step` was skipped because bbox dimensions exceeded `max_model_size`
  - `00100004_35809b42367c59ffb8aca25e_step_002.step` was skipped because exact-bbox probe validation hit a JSON read error on an extremely large serialized bbox
- Recut run passed 179/186, failed 7, timed out 0.
- Recut triage reported `failures=7`, `groups=6`, `command_failures=0`.
- Recut failure classes:
  - 3 failures on `00020036_8091c63e4e9b4249b4d54d23_step_000.step` returned `wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now.`
  - 1 failure on `00020009_7259f13b84f34cce843db1f8_step_052.step` returned API success but validation failed with `result_body_count_below_min actual=0 min=1`
  - 3 failures on `00020049_2d03cdc19bff40769b14da8b_step_000.step` returned `LOOP_COEDGE_NEXT_WRONG`
- Recut geometry audit reported 186 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 3, same-boolean duplicate input groups 3.
- Recut preview contact sheet: `artifacts/abc_fetch_10chunk_sample/sample_smoke/top_complex_recut_preview/contact.png`.

### Expanded 10-Chunk Full-Complex Sweep

Completed on 2026-07-07 under `artifacts/abc_fetch_10chunk_sample`.

- Ran the full `complex_dataset_index.json` for the 10-chunk sample50 with `--preserve-input-order`.
- Full complex STEP import covered 459/459 complexity-ranked files:
  - Passed: 435
  - Failed: 24
  - Timed out: 0
  - Triage: `failures=24`, `groups=4`, `command_failures=0`
- Failure groups remained the same four groups from the 192-case smoke.
- The expanded sweep added two lower-score `b47a7dce1aac5728` members beyond the 192-case smoke:
  - `00020014_2e382f0c54824698a9711cf5_step_000.step`
  - `00100032_eb1691f57c09aaad30b2fd34_step_000.step`

## Current Smoke: 10 Smallest Chunks Sample100

Completed on 2026-07-07 under `artifacts/abc_fetch_10chunk_sample100`.

- Reused the verified 10 smallest chunks and re-extracted the first 100 STEP files plus matching metadata from each chunk.
- Discovery wrote `artifacts/abc_fetch_10chunk_sample100/dataset_index.json`: 1000 STEP files, 574146651 bytes.
- Dataset audit passed with `ok=True`, `files=1000`, `errors=0`, `warnings=2`.
- CAD feature profile marked 918/1000 files complex. Feature totals included:
  - `advanced_face`: 139542
  - `bspline_curve`: 64041
  - `bspline_surface`: 16082
  - `bounded_surface`: 4839
  - `conical_surface`: 7811
  - `cylindrical_surface`: 32601
  - `offset_surface`: 151
  - `spherical_surface`: 1008
  - `surface_of_linear_extrusion`: 5709
  - `surface_of_revolution`: 1979
  - `toroidal_surface`: 5464
  - `trimmed_curve`: 46
- Top-complex import over the first 256 complexity-ranked files passed 228/256, failed 28, timed out 0.
- Top-complex import triage reported `failures=28`, `groups=5`, `command_failures=0`.
- Import failure groups:
  - `b47a7dce1aac5728`: 21 cases, error `Not accepted type!`, including new chunk 0010 member `00100088_56d5fec5e4b03325213b1efc_step_004.step`
  - `b40fa5e755823e04`: 4 cases, error `the residual legal number of knots is smaller than 1!`
  - `0fcdcb769dab1339`: `00430083_96918a1e34c9aa0f7e2999fc_step_002.step`, error `The spline curve is disconnect, please check the data!`
  - `b25fe88c5a6af58f`: `00100023_88d67c1f3366b3b4025c88aa_step_012.step`, error `暂不支持一个loop中有超过两个uv退化点的情况. Face Id : 699`
  - `af7e09945094d4ad`: `00270005_57f1fbc32f8b6410fc60afcd_step_002.step`, error `v is out of srf v range`
- New sample100 targets worth preserving:
  - `00100088_56d5fec5e4b03325213b1efc_step_004.step`: complexity score 460, `bspline_surface=132`, `bspline_curve=142`, `bounded_surface=65`, `advanced_face=403`, `offset_surface=4`, `toroidal_surface=67`, `cylindrical_surface=153`
  - `00100065_5d2a4a51a736e336c76f4d8f_step_027.step`: complexity score 256, `bspline_surface=4`, `bspline_curve=41`, `trimmed_curve=11`, `advanced_face=554`, `toroidal_surface=12`, `conical_surface=2`, `cylindrical_surface=130`
  - `00020055_2d03cdc19bff40769b14da8b_step_006.step`: complexity score 430, `bspline_surface=72`, `bspline_curve=142`, `bounded_surface=28`, `advanced_face=101`, `toroidal_surface=16`, `conical_surface=1`, `cylindrical_surface=25`
  - `00020057_89a80008b1894d1cb78447f6_step_000.step`: complexity score 189, `bspline_surface=6`, `bspline_curve=32`, `bounded_surface=3`, `advanced_face=31`, `cylindrical_surface=9`
- Top-complex import preview contact sheet: `artifacts/abc_fetch_10chunk_sample100/sample_smoke/top_complex_import_preview/contact.png`.
- Recut generation considered 80 sources, used 77 sources, skipped 3 sources, and generated 231 exact-bbox boolean recipes:
  - `00020027_99f82dfd033e43489c58125d_step_000.step` was skipped because bbox dimensions exceeded `max_model_size`
  - `00100004_35809b42367c59ffb8aca25e_step_002.step` was skipped because exact-bbox probe validation hit a JSON read error on an extremely large serialized bbox
  - `00100090_56d5fec5e4b03325213b1efc_step_006.step` was skipped because bbox dimensions exceeded `max_model_size`
- Recut run passed 218/231, failed 13, timed out 1.
- Recut triage reported `failures=10`, `groups=7`, `command_failures=3`.
- Recut failure classes:
  - 6 failures across `00100065_5d2a4a51a736e336c76f4d8f_step_027.step` and `00020036_8091c63e4e9b4249b4d54d23_step_000.step` returned `wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now.`
  - 1 failure on `00020009_7259f13b84f34cce843db1f8_step_052.step` returned API success but validation failed with `result_body_count_below_min actual=0 min=1`
  - 3 failures on `00020049_2d03cdc19bff40769b14da8b_step_000.step` returned `LOOP_COEDGE_NEXT_WRONG`
  - 1 command-level timeout on `00020055_2d03cdc19bff40769b14da8b_step_006.step` returned code 124 after 180 seconds
  - 2 command failures on `00020057_89a80008b1894d1cb78447f6_step_000.step` returned `3221225477` (`0xC0000005` access violation)
- Recut geometry audit reported 231 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 9, same-boolean duplicate input groups 9.
- Recut preview contact sheet: `artifacts/abc_fetch_10chunk_sample100/sample_smoke/top_complex_recut_preview/contact.png`.

### Expanded 10-Chunk Sample100 Full-Complex Sweep

Completed on 2026-07-07 under `artifacts/abc_fetch_10chunk_sample100`.

- Ran the full `complex_dataset_index.json` for the 10-chunk sample100 with `--preserve-input-order`.
- Full complex STEP import covered 918/918 complexity-ranked files:
  - Passed: 883
  - Failed: 35
  - Timed out: 0
  - Triage: `failures=35`, `groups=5`, `command_failures=0`
- Failure groups remained the same five groups from the 256-case smoke.
- The expanded sweep increased `b47a7dce1aac5728` to 28 members, adding lower-score members beyond the 256-case smoke:
  - `00020014_2e382f0c54824698a9711cf5_step_000.step`
  - `00100005_35809b42367c59ffb8aca25e_step_003.step`
  - `00100031_98a8073b9515489ebfe8f5b3_step_000.step`
  - `00100032_eb1691f57c09aaad30b2fd34_step_000.step`
  - `00100077_9df97fb76d2c5074922aec7d_step_003.step`
  - `00270055_e4e25395b0a7059376940e90_step_000.step`
  - `00610083_c53140d9321926058d5bb2d5_step_000.step`

## Current Smoke: 20 Smallest Chunks Sample50

Completed on 2026-07-07 under `artifacts/abc_fetch_20chunk_sample50`.

- Selected chunks 0027, 0014, 0025, 0028, 0061, 0026, 0031, 0043, 0002, 0010, 0050, 0013, 0030, 0011, 0023, 0005, 0051, 0048, 0036, and 0038.
- Plan covered 40 official archives, 15784288091 selected bytes (14.7 GiB compressed); 10 chunks were already cached and 10 new STEP+meta chunk pairs were downloaded and verified.
- Extracted first 50 STEP files and matching metadata from each selected chunk.
- Discovery wrote `artifacts/abc_fetch_20chunk_sample50/dataset_index.json`: 1000 STEP files.
- Dataset audit passed with `ok=True`, `files=1000`, `errors=0`, `warnings=2`.
- CAD feature profile marked 898/1000 files complex. Feature totals included:
  - `advanced_face`: 150460
  - `bspline_curve`: 69108
  - `bspline_surface`: 13677
  - `bounded_surface`: 4382
  - `conical_surface`: 9099
  - `cylindrical_surface`: 37057
  - `offset_surface`: 140
  - `spherical_surface`: 999
  - `surface_of_linear_extrusion`: 7725
  - `surface_of_revolution`: 1891
  - `toroidal_surface`: 5436
  - `trimmed_curve`: 39
- Top-complex import over the first 256 complexity-ranked files passed 229/256, failed 27, timed out 0.
- Top-complex import triage reported `failures=27`, `groups=5`, `command_failures=0`.
- Import failure groups stayed stable versus the 10-chunk sample100 run:
  - `b47a7dce1aac5728`: 22 cases in top-256, error `Not accepted type!`
  - `b40fa5e755823e04`: 2 cases, error `the residual legal number of knots is smaller than 1!`
  - `0fcdcb769dab1339`: `00510012_74f58f6b96630bc4c5bf77f3_step_000.step`, error `the spline curve is disconnect, please check the data!`
  - `af7e09945094d4ad`: `00270005_57f1fbc32f8b6410fc60afcd_step_002.step`, error `v is out of srf v range`
  - `b25fe88c5a6af58f`: `00100023_88d67c1f3366b3b4025c88aa_step_012.step`, UV-degenerate loop unsupported
- Top-complex import preview contact sheet: `artifacts/abc_fetch_20chunk_sample50/sample_smoke/top_complex_import_preview/contact.png`.
- Recut generation considered 80 sources, used 73 sources, skipped 7 sources, and generated 219 exact-bbox boolean recipes. Exact bbox probing had 1 source failure.
- Recut run passed 200/219, failed 19, timed out 0.
- Recut triage reported `failures=19`, `groups=16`, `command_failures=0`.
- Recut failure classes:
  - 9 failures across `00110031_10fe46f4da60f5a9e9723d1f_step_000.step`, `00110013_56e7cebee4b093d14fc66c1e_step_000.step`, and `00020036_8091c63e4e9b4249b4d54d23_step_000.step` returned `wire and face both in the body is not allowed for boolean INTERSECTION or SUBTRACTION now.`
  - 6 failures across `00050012_e408ebe315f24b71a82d3868_step_000.step` and `00050013_e408ebe315f24b71a82d3868_step_001.step` returned `3221225477` (`0xC0000005` access violation)
  - 3 failures on `00020049_2d03cdc19bff40769b14da8b_step_000.step` returned `LOOP_COEDGE_NEXT_WRONG`
  - 1 failure on `00020009_7259f13b84f34cce843db1f8_step_052.step` returned API success but validation failed with `result_body_count_below_min actual=0 min=1`
- New 20-chunk recut targets worth preserving:
  - `00110013_56e7cebee4b093d14fc66c1e_step_000.step`: complexity score 340, `advanced_face=2180`, `cylindrical_surface=1016`, `toroidal_surface=129`, `conical_surface=101`
  - `00110031_10fe46f4da60f5a9e9723d1f_step_000.step`: complexity score 277, `advanced_face=108`, `bspline_curve=33`, `cylindrical_surface=19`, `bspline_surface=10`
  - `00050012_e408ebe315f24b71a82d3868_step_000.step`: complexity score 382, `advanced_face=64`, `bspline_curve=52`, `bspline_surface=23`, `toroidal_surface=16`
  - `00050013_e408ebe315f24b71a82d3868_step_001.step`: complexity score 382, same feature shape as `00050012`
- Recut geometry audit reported 219 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 27, same-boolean duplicate input groups 30.
- Recut preview contact sheet: `artifacts/abc_fetch_20chunk_sample50/sample_smoke/top_complex_recut_preview/contact.png`.

### Expanded 20-Chunk Sample50 Full-Complex Sweep

Completed on 2026-07-07 under `artifacts/abc_fetch_20chunk_sample50`.

- Ran the full `complex_dataset_index.json` for the 20-chunk sample50 with `--preserve-input-order`.
- Full complex STEP import covered 898/898 complexity-ranked files:
  - Passed: 865
  - Failed: 33
  - Timed out: 0
  - Triage: `failures=33`, `groups=5`, `command_failures=0`
- Failure groups remained the same five groups from the 256-case smoke.
- Full-complex group counts:
  - `b47a7dce1aac5728`: 28 cases, `Not accepted type!`
  - `b40fa5e755823e04`: 2 cases, residual legal knots
  - `0fcdcb769dab1339`: 1 case, disconnected spline curve
  - `af7e09945094d4ad`: 1 case, `v is out of srf v range`
  - `b25fe88c5a6af58f`: 1 case, UV-degenerate loop unsupported

## Current Smoke: 40 Smallest Chunks Sample50

Completed on 2026-07-07 under `artifacts/abc_fetch_40chunk_sample50`.

- Selected chunks 0027, 0014, 0025, 0028, 0061, 0026, 0031, 0043, 0002, 0010, 0050, 0013, 0030, 0011, 0023, 0005, 0051, 0048, 0036, 0038, 0072, 0022, 0024, 0076, 0083, 0041, 0003, 0046, 0060, 0093, 0021, 0012, 0073, 0075, 0052, 0032, 0020, 0015, 0085, and 0059.
- Plan covered 80 official archives, 34951791325 selected bytes (32.551 GiB compressed). The fetch initially hit a curl 56 schannel close-notify interruption on `abc_0015_step_v00.7z`; `fetch_abc_dataset.py` now treats curl 56 like curl 18 for resumable retry.
- Extracted first 50 STEP files and matching metadata from each selected chunk.
- Discovery wrote `artifacts/abc_fetch_40chunk_sample50/dataset_index.json`: 2000 STEP files.
- Dataset audit passed with `ok=True`, `files=2000`, `errors=0`, `warnings=2`.
- CAD feature profile marked 1802/2000 files complex. Feature totals included:
  - `advanced_face`: 336509
  - `bspline_curve`: 179175
  - `bspline_surface`: 52963
  - `bounded_surface`: 19965
  - `conical_surface`: 15226
  - `cylindrical_surface`: 78922
  - `offset_surface`: 266
  - `spherical_surface`: 2893
  - `surface_of_linear_extrusion`: 12480
  - `surface_of_revolution`: 2555
  - `toroidal_surface`: 11786
  - `trimmed_curve`: 483
- Top-complex import over the first 320 complexity-ranked files passed 271/320, failed 49, timed out 0.
- Top-complex import triage reported `failures=49`, `groups=7`, `command_failures=0`.
- Import failure groups in the top-320 smoke:
  - `b47a7dce1aac5728`: 35 cases, `Not accepted type!`
  - `b40fa5e755823e04`: 6 cases, residual legal knots
  - `af7e09945094d4ad`: 3 cases, `v is out of srf v range`
  - `90b415c01204001f`: 2 cases, `Seperation loop1 has no uv path!`
  - `0fcdcb769dab1339`: 1 case, disconnected spline curve
  - `9478dbe1f32caf62`: 1 case, exchange invalid topology
  - `b25fe88c5a6af58f`: 1 case, UV-degenerate loop unsupported
- Top-complex preview rendering initially produced many PNGs but returned rc=1 on one case because `render_case_preview.py` attempted to concatenate `None` bbox min/max values. This was a harness preview bug, not an SDK import failure. The preview tool now filters invalid bbox extents and uses robust bbox signature values; a rerun rendered 320/320 previews at `artifacts/abc_fetch_40chunk_sample50/sample_smoke/top_complex_import_preview_rerun/contact.png`.
- Recut generation considered 100 sources, used 91, skipped 9, and generated 273 exact-bbox boolean recipes. Exact bbox probing had 1 source failure.
- Recut run passed 252/273, failed 21, timed out 0.
- Recut triage reported `failures=21`, `groups=17`, `command_failures=0`.
- Recut failures are concentrated in `api_boolean` subtraction with tangent X cylinders against imported bodies, especially exact-contact and `+/- 1e-5` gap/overlap placements. Representative fingerprints include `128cbe430cb3e9ee`, `196045f4db3c4b2e`, `6bbc685565d2d030`, and `b62012eecdcd13a7`.
- Recut geometry audit reported 273 cases, exact input bbox enabled, tolerance mismatches 0, duplicate geometry groups 39, same-boolean duplicate input groups 39.
- Recut preview contact sheet: `artifacts/abc_fetch_40chunk_sample50/sample_smoke/top_complex_recut_preview/contact.png`.

### Expanded 40-Chunk Sample50 Full-Complex Sweep

Completed on 2026-07-07 under `artifacts/abc_fetch_40chunk_sample50`.

- Ran the full `complex_dataset_index.json` for the 40-chunk sample50 with `--preserve-input-order`.
- Full complex STEP import covered 1802/1802 complexity-ranked files:
  - Passed: 1731
  - Failed: 71
  - Timed out: 0
  - Triage: `failures=71`, `groups=8`, `command_failures=0`
- Full-complex group counts:
  - `b47a7dce1aac5728`: 56 cases, `Not accepted type!`
  - `b40fa5e755823e04`: 6 cases, residual legal knots
  - `af7e09945094d4ad`: 3 cases, `v is out of srf v range`
  - `90b415c01204001f`: 2 cases, `Seperation loop1 has no uv path!`
  - `0fcdcb769dab1339`: 1 case, disconnected spline curve
  - `9478dbe1f32caf62`: 1 case, exchange invalid topology
  - `acf66e53e4b74205`: 1 case, `failed in infer loop type`
  - `b25fe88c5a6af58f`: 1 case, UV-degenerate loop unsupported
- The expanded sweep added one new import failure group beyond the 20-chunk run:
  - `00750013_d8733d8a29e62eb77d5f31b7_step_000.step`: fingerprint `acf66e53e4b74205`, error `failed in infer loop type`, complexity score 100, `bspline_curve=3508`, size 14756542 bytes.
- Slowest successful imports are useful performance seeds:
  - `00430008_cebfe4cd2fcdd37a92459aaa_step_000.step`: 45.91 seconds
  - `00930006_da96aa668afc1bb8a81e39f3_step_017.step`: 29.86 seconds
  - `00930003_da96aa668afc1bb8a81e39f3_step_014.step`: 29.44 seconds
  - `00030038_0ef34aa1b15748a5b4ad7c0e_step_028.step`: 27.15 seconds
  - `00030039_0ef34aa1b15748a5b4ad7c0e_step_029.step`: 26.96 seconds

## Next Rolling Commands

The 40 smallest chunks are covered at sample50 breadth, the 20 smallest chunks have a completed full-complex import sweep, and the 10 smallest chunks are covered at sample100 depth. The next useful step is to promote a replayable regression pack from the recurring import and recut targets above, then plan the next breadth jump.

Suggested next commands:

```powershell
python .\test_harness\tools\fetch_abc_dataset.py --out .\artifacts\abc_fetch_80chunk_sample50_plan --download-root .\artifacts\abc_fetch_smoke\downloads --smallest-step 80 --sample-count 50 --extract-mode sample --plan-only
python .\test_harness\tools\fetch_abc_dataset.py --out .\artifacts\abc_fetch_40chunk_sample100_plan --download-root .\artifacts\abc_fetch_smoke\downloads --smallest-step 40 --sample-count 100 --extract-mode sample --plan-only
```

Before expanding beyond 40 chunks, promote a small replayable pack covering import fingerprints `b47a7dce1aac5728`, `b40fa5e755823e04`, `0fcdcb769dab1339`, `af7e09945094d4ad`, `90b415c01204001f`, `9478dbe1f32caf62`, `acf66e53e4b74205`, and `b25fe88c5a6af58f`, plus representative tangent-cylinder subtraction failures from the 40-chunk recut triage.
