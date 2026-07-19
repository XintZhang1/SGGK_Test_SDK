# AGENTS.md — SGGK Test SDK Harness

Guidance for AI coding agents working in this repository. Read this before
making any change. When a section conflicts with `test_harness/HARNESS_ARCHITECTURE.md`,
the architecture document wins — it is the project's source of truth.

## Conversation language (fixed requirement)

Always respond to the user in Chinese (Simplified) — this is a standing
requirement from the repository owner and applies to every reply. Code,
identifiers, comments, and repository artifacts keep the existing language
conventions described below (English for code/maintainer docs, Chinese for
user-facing artifacts).

## Project overview

This is a **deterministic authoring and execution test harness for the
proprietary SGGK CAD geometric modeling kernel SDK** (SGK1.4.10). It uses the
SiliconFlow `zai-org/GLM-5.2` Message API to generate test code and test cases
for SGGK public APIs, subjects every model output to fixed host-side gates, and
only executes approved candidates against the real SDK. Failures go through a
deterministic pipeline: qualification, 3x replay, signature-preserving
reduction, failure bundles, bug registries, and tool-bounded model
investigation.

The repository has two halves:

- **Python orchestration layer** (`test_harness/`, stdlib-first, pinned
  `jsonschema` + `Pillow`): session orchestration, Message API gateway, fixed
  gates, campaign tooling, local web UI, pytest suite.
- **C++17 case runner** (`test_harness/src/`, CMake): `sggk_case_runner.exe`
  executes one flat JSON recipe per process against the SGGK SDK and writes an
  artifact capsule per case; `sggk_topology_extract.exe` is a GUI-handoff
  helper that re-exports selected topology from `.sgt` files;
  `sggk_mesh_dump.exe` tessellates `.sgt` bodies into a bounded mesh JSON
  for the failure-showcase real-geometry renderings.

**Windows x64 only.** The end-user entry point is a local web UI
(`SGGK_Harness_UI.cmd`, `http://127.0.0.1:8765`); normal users never touch
PowerShell. `harness.ps1 start|comment|status|show|retry` is a maintainer
compatibility shim over `test_harness/tools/sggk_harness.py`, which forwards to
`test_harness.orchestration`.

## Non-negotiable trust boundaries

These invariants are the core of the design. Do not weaken them in any change:

- Model output is **always an untrusted candidate**. Only fixed host code may
  normalize, validate, gate, compile, execute, select, and atomically promote
  it. There is no human-authored, clipboard, or checked-in-fixture path that
  can publish a model output.
- Model JSON **cannot** provide commands, executable/runner paths, cwd,
  environment variables, shell, SDK/link flags, dataset/output paths, URLs, or
  native tool calls.
- **No SDK execution before explicit approval.** The host binds an approval
  attestation to the exact hash of the latest immutable review round; only that
  hash-bound candidate runs, in an isolated artifact directory.
- No implicit provider/model/endpoint switching or fallback. The external
  build's only production profile is `siliconflow` (base URL
  `https://api.siliconflow.cn/v1`, model `zai-org/GLM-5.2`, both locked).
  A separate `siliconflow_vision` profile (same locked base URL, model
  `Qwen/Qwen3-VL-32B-Instruct`, shared `SILICONFLOW_API_KEY`) exists ONLY
  for advisory visual review of host-rendered geometry previews: its
  `visual_review_report` output is evidence for humans — it never gates,
  approves, executes, or alters any candidate, verdict, or state machine.
- Proprietary SGGK source and source evidence stay on the local machine.
  Source tasks require the explicit legacy `intranet` profile and are never
  sent to SiliconFlow. External tasks must be marked `public_interface`.
- Reasoning text, credentials, and authorization headers are never persisted.
  The API key lives in Windows Credential Manager (UI) or
  `SILICONFLOW_API_KEY` (maintainer CLI) and never enters JSON, logs, prompts,
  provenance, or git.
- Failure evidence is candidate-only: `failure_registry` / investigation
  reports never confirm an SDK defect; any `confirmed_bug` claim is rejected.

## Environment and toolchain

- Python ≥ 3.11. The repo bootstraps a private, git-ignored runtime under
  `.offline_runtime/` (CPython 3.11.9 embeddable + portable CMake 4.3.3 +
  pinned wheels) via `install_offline.cmd`; no network, no system-Python
  changes. **Use `.offline_runtime/python/python.exe` for all Python
  commands** — the system `python` on a dev machine may lack pytest/ruff.
- Visual Studio 2022 or newer with the "Desktop development with C++" workload
  (VS 2026 verified). `test_harness/msvc.py` detects the newest generator CMake
  supports; the Harness UI uses it to auto-select a preset.
- The SGGK SDK, license (`sggk.lic`), and sources are company assets, **not in
  git** (`SGK*/`, `SGGK/`, `*.lic`, `*.dll`, `*.exe` are ignored). Builds and
  real SDK runs require `SGGK_SDK_DIR` pointing at a valid SDK root (CMake
  checks for `include/Foundation/init.h`).
- Runtime deps are pinned: `jsonschema==4.26.0`, `Pillow==12.1.1`; dev deps
  `pytest==9.0.3`, `ruff==0.12.2` (`pyproject.toml`,
  `requirements-offline.txt`). Everything else is Python stdlib. **Do not add
  dependencies casually** — the offline bundle (`offline_bundle/manifest.json`
  + `wheelhouse/`) pins every transitive wheel by SHA-256, so a new dependency
  requires regenerating the bundle (`offline_bundle/build_manifest.py`).

## Build commands

From the repo root (PowerShell syntax shown; adapt paths for Git Bash):

```powershell
$env:SGGK_SDK_DIR = "<local SGGK SDK root>"
Push-Location .\test_harness
cmake --fresh --preset windows-local        # VS 2022; use windows-vs2026 on VS 2026
cmake --build --preset windows-release --parallel
Pop-Location
```

- Presets live in `test_harness/CMakePresets.json`. Build trees:
  `build/test_harness/` (VS 2022) or `build/test_harness-vs2026/` (VS 2026).
- Runner path after a Release build:
  `build/test_harness/Release/sggk_case_runner.exe` (or the `-vs2026` tree).
  Post-build steps copy SDK DLLs and `sggk.lic` next to the executables.
- CMake configure runs `tools/generate_plugin_registry.py` over
  `test_harness/api_plugins/` and generates the C++ dispatch/metadata registry;
  a plugin needs no central C++/CMake edits.

## Test and verification commands

Run everything from the repo root with the offline runtime:

```bash
.offline_runtime/python/python.exe -m pytest -q          # 658 tests, ~45 s, no SDK needed
.offline_runtime/python/python.exe -m ruff check .       # global gate: F rules only
.offline_runtime/python/python.exe -m compileall -q test_harness
.offline_runtime/python/python.exe test_harness/tools/validate_recipe.py test_harness/recipes
```

Verified on this machine (2026-07): pytest 658 passed; `validate_recipe.py`
reports OK for all checked-in recipes. `ruff check .` currently reports **one
pre-existing F401** (`pytest` imported but unused in
`test_harness/tests/test_nx_sggk_boolean_comparison.py`) — do not let your
change add new findings.

- pytest config is in `pyproject.toml` (`testpaths = ["test_harness/tests"]`).
  `test_harness/tests/conftest.py` puts the repo root and `test_harness/tools`
  on `sys.path`; tools modules import each other as top-level modules.
- Unit tests run without the SDK, runner, or network. Real SDK execution
  requires the built runner and a configured `SGGK_SDK_DIR`; the 24-case
  self-contained smoke is `test_harness/suites/api_smoke_suite.txt` via
  `tools/run_recipes.py` (see `test_harness/README.md`).
- JSON contract/schemas are validated by dedicated tools:
  `validate_recipe.py`, `validate_plugin_runtime.py`,
  `validate_interface_capabilities.py`, `validate_harness_extension.py`,
  `validate_diagnostic_catalog.py`, `validate_provenance_metadata.py`, etc.

## Code style and conventions

- Ruff: `line-length = 120`, `target-version = py311`, global `select = ["F"]`.
  Per the `pyproject.toml` comment, the global gate is intentionally minimal
  because inherited campaign tools predate the style baseline; **new or changed
  modules should additionally pass explicit `E/F/I/UP` checks** (e.g.
  `ruff check --select E,F,I,UP <changed files>`).
- Python files start with `from __future__ import annotations`, use type hints,
  stdlib-first design, and `pathlib.Path`. Match the surrounding module's
  structure instead of introducing new frameworks.
- Language convention: code, identifiers, comments, and maintainer docs in
  English; user-facing artifacts (review rounds, `final_report.zh-CN.md`,
  UI text, argparse help for user commands) in Chinese. Root `README.md` and
  `docs/` are Chinese; `test_harness/*.md` are English.
- `test_harness/` and `test_harness/tools/` are PEP 420 namespace packages (no
  `__init__.py`); the subpackages `orchestration/`, `authoring_gateway/`,
  `investigation/`, `nx/`, `ui/` have `__init__.py`.
- Line endings (`.gitattributes`): LF for code/JSON/Markdown, CRLF for
  `*.cmd`/`*.bat`/`*.ps1`; `*.sgt` and CAD exchange files are binary.
- `artifacts/` is the git-ignored output root for every run; UI config lives in
  `artifacts/harness_ui/config.json`. Never commit artifacts, SDK files,
  licenses, or credentials.
- Review rounds, comments, candidates, and approvals are **immutable,
  hash-chained events**. Tools that consume promoted output require
  hash-matching provenance (`authoring_accepted=true`,
  `accepted_by=message_harness_pipeline`, successful fixed gate).
- Checked-in known-bug records (`test_harness/bug_records/`) must pass
  `tools/audit_bug_record_portability.py`: no absolute local paths, no
  `artifacts/` dependencies; durable inputs belong under
  `test_harness/fixtures/bug_records/`, `test_harness/dsl/`,
  `test_harness/recipes/`, or `SGK1.4.10/samples`.

## Code organization

- `test_harness/orchestration/` — immutable review-session workflow
  (`workflow.py`): `start`/`comment`/`status`/`show`/`retry`, approval
  attestation, execution gating. Post-execution (both `completed` and
  `execution_failed`) it also runs the Parasolid comparison, the advisory
  visual review, and a best-effort **failure showcase** hook
  (`_run_failure_showcase`): every case with triage reasons or a nonzero
  runner return code gets its capsule copied to
  `artifacts/<api>/round_<NNNN>_<sessionts>/<case_id>/` (input recipe+`.sgt`,
  output `.sgt`, report JSON, run_state/manifest, comparison evidence; files
  > 32 MiB skipped, **STEP is never copied** — it is NX-only transport) plus
  a fixed-content `reproduce.ps1`, a generated google-test repro TU
  (`<case>_repro.cpp`, full or 裁剪版 depending on `fault_module`), a Chinese
  `analysis.md`, a deterministic `pre_analysis.json` (`fault_domain` +
  `fault_module` + optional Parasolid recheck evidence), and a real shaded
  mesh render as `<case>_mesh.png`/`<case>_analysis.png` (the bbox overlay
  stays as fallback when `sggk_mesh_dump.exe` is unavailable).
  Showcased cases are appended (atomic, capped at 500 records) to the durable
  失败用例数据库 `artifacts/failure_analysis_db.json`. All of this is
  diagnostic evidence only and never confirms an SDK defect.
  `sggk_harness.py` and `harness.ps1` forward
  here. Unknown APIs route through fixed-archetype adaptation
  (`_api_adaptation_binding`: host-local header parse → archetype mapping →
  host-issued adaptation contract → `api_adaptation` task → GLM
  `api_plugin_candidate` → materialize/build/smoke → `promote_api_plugin.py`
  registration); unmappable signatures fall back to the `interface_dsl_design`
  backlog. `public_doc_discovery.py` gives the external profile bounded,
  hash-pinned Doxygen (`docs/html`) public-interface evidence; raw SDK headers
  never enter prompts.
- `test_harness/authoring_gateway/` — Message API client, endpoint profiles
  (`siliconflow` external default, `intranet` for source tasks,
  `siliconflow_vision` for advisory visual review), contracts, comment
  interpretation, bounded source evidence, multimodal (image content-part)
  transport with byte budgets and SHA-256-bound image provenance.
- `test_harness/investigation/` — tool-bounded model bug investigation:
  registered tool IDs (including `comparison.get_verdict` for Parasolid
  evidence), hash-chained evidence ledger, hypothesis reports.
- `test_harness/ui/` — local web UI (`python -m test_harness.ui`, port 8765):
  server, application logic, settings, ABC dataset panel; static frontend in
  `ui/static/`. `ui/state.py` projects parsed summaries so users do not read
  raw JSON: `round_overview` (candidate card), `execution_overview`
  (per-case pass/fail, failure groups, Parasolid attention cases, advisory
  visual-review summary), and `failure_analysis` (失败分析 tab: per failed
  case signature chips, triage reasons, oracle failures, Parasolid
  verdict/cause, deterministic `fault_domain` + `fault_module` with Chinese
  labels, advisory `visual_fault_hint` with a disagreement badge, the mesh
  analysis PNG, and the showcase/repro paths). Raw tokens stay visible in a
  muted 原始标记 line; all Chinese token maps live in
  `tools/oracle_text_zh.py`. `/api/artifact` additionally serves
  session-scoped PNGs (≤ 4 MiB) as base64 image payloads; CSP stays
  `default-src 'self'` with only `img-src 'self' data:` for those previews.
- `test_harness/nx/` — optional Siemens NX Python API integration (static
  detection, runtime probe, allowlisted journal runner). Comparator
  conventions: boolean operation is api-aware (`api_combine_bodies`→unite;
  manifest default `boolean_type` only counts for boolean-family APIs), and
  the free-edge closedness probe is seam-aware (isolated single-face loops on
  periodic surfaces are not boundary edges). `nx_journals/oracle_recheck.py`
  re-measures failed oracle checks (distance, point-vs-body containment via a
  probe-sphere intersect, clash via Intersect volume, plane extreme via
  rectangular-extreme, import integrity) and feeds the deterministic
  three-way `fault_module` attribution.
- `test_harness/tools/` — ~80 maintainer CLI tools: pipeline
  (`run_message_harness_pipeline.py`), gates/normalization
  (`normalize_model_output.py`, `model_fixed_gate_contracts.py`,
  `score_case_complexity.py`), DSL compiler (`compile_attack_dsl.py`), runners
  (`run_recipes.py`, `run_corpus.py`, `run_campaign.py`,
  `plan_large_campaign.py`), failure pipeline (`triage_artifacts.py`,
  `qualify_failures.py`, `replay_regression_seeds.py`,
  `reduce_failure_recipe.py`, `export_failure_bundles.py`,
  `analyze_failure_cases.py` — deterministic per-case fault-domain +
  fault-module pre-analysis (Parasolid oracle recheck when NX is available)
  + annotated overlay + advisory VL fault hints), failure handoff
  (`export_failure_gtest.py` — google-test repro TU generator;
  `render_mesh_views.py` — Pillow-only shaded mesh views;
  `oracle_text_zh.py` — shared Chinese token maps/failure-string
  translator), Parasolid
  divergence analysis (`classify_parasolid_divergence.py`; verdicts flow into
  triage/bundles/investigation/final report), bug registries
  (`record_bug_cases.py`, `check_bug_registry_regression.py`), plugin pipeline
  (`api_archetype_mapping.py`, `materialize_api_plugin_candidate.py`,
  `build_api_plugin_candidate.py`, `plugin_catalog.py`,
  `promote_api_plugin.py`), advisory visual review (`run_visual_review.py`),
  ABC dataset tooling (`fetch_abc_dataset.py` with seeded stratified sampling,
  `generate_corpus_recut_matrix.py` with persistent exact-bbox probe cache and
  host-lane complexity report/gate), and more.
  `test_harness/README.md` documents each.
- `test_harness/src/` — C++ sources of the three executables
  (`sggk_case_runner`, `sggk_topology_extract`, `sggk_mesh_dump` — the latter
  tessellates `.sgt` bodies into the bounded mesh JSON consumed by
  `render_mesh_views.py`).
- `test_harness/api_plugins/` — checked-in compile-time API plugins (current
  real pilot: `api_combine_bodies`; generatable archetypes:
  `body_list_to_body`, `unary_body_to_bodies`; others remain manual).
- `test_harness/recipes/` — hand-written flat JSON smoke recipes;
  `test_harness/dsl/` — checked-in attack-DSL fixtures;
  `test_harness/suites/` — recipe lists; `test_harness/schemas/` — JSON
  schemas for model/plugin/investigation contracts; `test_harness/forms/` —
  intake form schema and distillation inventory;
  `test_harness/interface_example_packs/` — per-API example packs.
- `test_harness/skills/` — prompt/skill packs (`sggk-api-review-workflow`,
  `sggk-source-attack`) used when building model tasks.
- `test_harness/bug_records/` + `test_harness/fixtures/bug_records/` —
  reviewed known-bug registry and its portable replay assets.
- `offline_bundle/` — offline installer payload and scripts
  (`build_manifest.py`, `install_wheels.py`, `verify_offline.py`); every file
  hash-pinned in `manifest.json`.
- `docs/` — Chinese user/deployment guides; `SGK1.4.10/` — git-ignored SDK
  drop (docs/samples/SGGK) present on dev machines only.

## Testing strategy (project-specific)

- Generated cases are JSON **recipes** (flat) or **attack-DSL** files compiled
  to flat recipes; `validate_recipe.py` + `compile_attack_dsl.py --check` are
  fixed gates for any candidate before review.
- One recipe = one runner process (isolation); batch lanes use
  `run_recipes.py` / `run_corpus.py` with `--jobs`, `--timeout`, `--resume`,
  `--shard-count/--shard-index`.
- `ret.Succeeded()` is never the only oracle: every case writes
  `report/validation.json` with property, TopoCheck, point/face-relation,
  clash, distance, and plane-extreme oracles; the runner exits nonzero when
  validation fails even if the SDK reports success. SDK `CalcBndBox` output is
  a conservative diagnostic, never a hard oracle.
- Case capsules use **`.sgt` (RapidTopoJsonSerializer) for every internal
  body exchange** (inputs, outputs, debug geometry, handoffs). STEP is used
  ONLY as the Siemens NX comparison transport and must never newly circulate
  inside the harness — showcase copies, bundles, and previews copy `.sgt`
  files and skip `*.step`/`*.stp`.
- A nonzero SDK/test result is **not automatically a bug**:
  `qualify_failures.py` applies deterministic contradiction rules first; only
  groups whose 3 replay attempts all match the immutable signature
  (`stable_same_failure`) get reduction, failure bundles, registry entries, or
  investigation. Flaky/changed/unverified results stay in inconclusive triage.
- Persistent known bugs are replayed as regression lanes;
  `check_bug_registry_regression.py` classifies `still_failing` /
  `fixed_or_not_reproduced` / `changed_failure` / `unavailable`.
- Large campaigns have a readiness gate (full pytest/Ruff/compileall/schema
  validation, Release runner build, plugin registry validation, all API
  smokes, parallel-candidate and replay/reduction proofs) before staged scale-up
  to 100k+ cases — see `test_harness/HARNESS_ARCHITECTURE.md` and
  `docs/ABC_DATASET_LARGE_TEST_PLAN.md`.

## Security considerations

- Web UI binds loopback only, validates the local Host header, requires a
  random CSRF token for mutations, and previews artifacts only within the
  current session (no path traversal, no non-text previews; model content is
  rendered as text nodes only).
- All subprocess execution is fixed-command with `shell=False`, timeouts, and
  process-tree termination; the NX journal runner adds path allowlists. Never
  introduce code that lets model output or UI input choose commands, paths, or
  environment.
- SSE/model responses are size-bounded (16 MiB candidate, 256 MiB wire) and
  must end with explicit `finish_reason=stop` + `[DONE]`; anything else fails
  closed.
- Do not commit: API keys, `sggk.lic`, SDK trees (`SGK*/`), build output
  (`build/`), run output (`artifacts/`), or the offline runtime
  (`.offline_runtime/`). The single checked-in binary exception is the
  hash-pinned `offline_bundle/archives/vc_redist.x64.exe`.
- TopoTrack crash probes (`probe_topotrack_crashes.py`) run paired isolated
  processes; never run `--capture-flat-topotrack` in a shared process. All
  probe classifications are diagnostic evidence, not causal proof.

## Where to read next

- `test_harness/HARNESS_ARCHITECTURE.md` — architecture and trust-boundary
  source of truth (read first for any pipeline change).
- `test_harness/README.md` — full maintainer CLI reference (runner, DSL,
  corpus, campaigns, bug registries).
- `test_harness/INTERFACE_TEST_MATRIX.md` — supported runner APIs, body
  builders, oracles, extension gaps.
- `test_harness/MESSAGE_API_ENDPOINTS.md` — provider profile, key handling,
  failure semantics.
- Root `README.md` + `docs/` (Chinese) — offline deployment and user guides.
