---
name: sggk-api-form-workflow
description: Convert developer-filled SGGK API test forms into small-model tasks, review the model output, run the harness API smoke suite, and summarize reports for intranet import.
---

# SGGK API Form Workflow

## Purpose

Use this skill when the input is a developer test request form rather than a direct source-code finding. The goal is a repeatable lane:

1. Developer fills a compact form.
2. Deterministic code converts the form into a constrained small-model task.
3. The small model emits SGGK attack DSL or a `needs_harness_extension` object.
4. The harness checks, compiles, runs, triages, previews, and reports the result.

## Required References

Read these before acting:

- `references/form-contract.md`
- `references/small-model-contract.md`
- `references/workflow.md`
- `references/api-smoke-suite.md`

## Guardrails

- Do not ask the small model to call SGGK SDK APIs directly.
- Prefer attack DSL for `api_boolean`; use flat recipes for `check_sgt`, `step_import`, `iges_import`, `step_roundtrip`, and `iges_roundtrip`.
- Unsupported APIs must be reported as `needs_harness_extension`.
- Keep generated model outputs and run artifacts under `artifacts/`; do not commit them.
- Run `compile_attack_dsl.py --check` before compiling or running model-generated DSL.
- Run the API smoke suite with `--jobs 1` because later recipes depend on `boolean_smoke` artifacts.
- Treat SDK success plus failed TopoCheck, validation, roundtrip comparison, clash, distance, point relation, or plane-extreme oracle as a failure.
