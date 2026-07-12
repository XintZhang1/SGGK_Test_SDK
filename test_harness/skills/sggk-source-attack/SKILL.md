---
name: sggk-source-attack
description: Prepare bounded intranet SGGK source-risk evidence for the reviewed Message API session, then qualify/replay/localize approved execution failures.
---

# SGGK Source Attack

## Purpose

Use source inspection to prepare trusted evidence for automatic test authoring.
Production model output comes only from `choices[0].message.content` through
`run_message_harness_pipeline.py`. A human, Codex, or standalone model session
must not save, edit, compile, run, or promote captured model JSON.

The scanner and task builder produce prompt context, not tests. Low-level DSL
expansion, compilation, validation, and runner commands are host-owned fixed
gates. Direct invocation is limited to diagnosing checked-in deterministic
fixtures or artifacts already produced by a pipeline fixed gate; it cannot
accept a model response or create authoring provenance.

## Production Workflow

1. Configure the approved intranet source root and start the ordinary public-function review session. Definition discovery, namespace/overload binding, prompt construction, fixed gates, review rounds, and approval are host-owned.
2. Use `scan_source_risks.py` and `build_source_attack_tasks.py` only as optional bounded diagnostics for broad source roots. Their seed DSL remains prompt context, never executable model output.
3. Review and revise only with natural-language `harness.ps1 comment` commands. Source-derived candidates never move to an external endpoint and never execute through a raw pipeline command.
4. Keep deterministic baseline coverage alongside the reviewed lane: generated boolean matrices, corpus import/roundtrip checks, loaded-SGT recuts, API smoke, and known-bug replay.
5. Qualify failures before calling them SDK defects. Require stable same-signature replay, preserve real-result oracle evidence, reduce only when the signature survives, and use paired isolated TopoTrack capture/control for localization.
6. Keep bug reports candidate-only until deterministic qualification, replay, portability audit, and maintainer review are complete.

## Production Commands

```powershell
$env:SGGK_HARNESS_PROFILE = "intranet"
$env:SGGK_SOURCE_ROOT = "<approved-intranet-source-root>"
.\harness.ps1 start api_boolean
.\harness.ps1 comment "增加源码分支对应的容差两侧、退化输入和可观测 Oracle。"
.\harness.ps1 comment "明确同意当前方案，可以开始执行真实测试。"
```

For large deterministic baselines, use `plan_large_campaign.py` or
`run_campaign.py` after the Message API lane is green. These commands execute
host-authored fixtures, matrices, corpus indexes, and accepted pipeline outputs;
they are not model transports.

## Required References

- `references/attack-heuristics.md`: source-risk and geometry/oracle selection context for the prompt.
- `references/attack-dsl.md`: untrusted `attack_dsl` candidate schema and fixed-gate behavior.
- `references/recipe-schema.md`: fixed flat-recipe schema and supported runner APIs.
- `references/output-contract.md`: pipeline evidence and reporting requirements.

## Guardrails

- Only the integrated Message API pipeline may set `authoring_accepted=true` or promote a formal model output.
- Never copy/paste a response, seed a fixture as a model output, or use a direct compiler/runner command as an acceptance shortcut.
- Model JSON cannot contain commands, executable/runner paths, dataset/output paths, cwd, environment, shell mode, SDK/link flags, URLs, or native tool calls.
- Prefer legal adversarial geometry over impossible input unless the source explicitly handles invalid input.
- Preserve stable case/operation IDs and exact source literals where relevant; use deterministic nearby tolerance bands.
- Require measurable result oracles. API success alone is not a pass.
- `needs_harness_extension` is a non-executing structured backlog report. It cannot propose, generate, review, or apply a source patch.
- Keep proprietary source excerpts and generated artifacts under `artifacts/` or intranet-only storage.
- Before checking in a long-lived regression asset, require portable paths, stable replay evidence, and the existing bug-record audit.
