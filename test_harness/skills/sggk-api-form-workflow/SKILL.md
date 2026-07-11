---
name: sggk-api-form-workflow
description: Convert trusted SGGK API forms/intakes into parallel Message API candidates, fixed-gate and execute them, adapt supported new APIs through fixed plugins, and summarize candidate-only failure evidence.
---

# SGGK API Form Workflow

## Purpose

Use this skill when the input is a developer test request form rather than a direct source-code finding. The goal is a repeatable lane:

1. Developer fills a compact form.
2. Deterministic code converts the form into a constrained small-model task.
3. Independent Message API roles emit SGGK DSL/recipe/plugin candidates.
4. The harness gates, de-duplicates, executes, deterministically selects, and promotes one result.
5. Failures are qualified, replayed, reduced, probed with TopoTrack, and reported as candidates.

## Required References

Read these before acting:

- `references/form-contract.md`
- `references/small-model-contract.md`
- `references/workflow.md`
- `references/api-smoke-suite.md`

## Guardrails

- Do not ask the small model to call SGGK SDK APIs directly.
- Prefer attack DSL for `api_boolean`; use flat recipes for `check_sgt`, `step_import`, `iges_import`, `step_roundtrip`, and `iges_roundtrip`.
- New APIs matching a registered fixed archetype use `api_plugin_candidate`;
  only APIs outside built-ins, plugins, and archetypes use `needs_harness_extension`.
- Keep generated model outputs and run artifacts under `artifacts/`; do not commit them.
- The integrated Message API pipeline must run `compile_attack_dsl.py --check`
  as its fixed gate before SDK execution. Never invoke the compiler directly on
  captured model output; direct use is limited to checked-in deterministic
  fixtures or diagnosis of a pipeline gate artifact.
- Run the self-contained API smoke suite with `--jobs 1` for deterministic evidence.
- Treat SDK success plus failed TopoCheck, validation, roundtrip comparison, clash, distance, point relation, or plane-extreme oracle as a failure.
