---
name: sggk-api-review-workflow
description: Start an SGGK Message API test session from one public-function name, process natural-language review comments, and execute only a host-approved immutable round.
---

# SGGK API Review Workflow

## User contract

The ordinary user supplies exactly two kinds of business input:

1. one API or namespace-qualified public-function name;
2. one natural-language comment at a time after the current Chinese review report exists.

Never ask the user to fill a form, choose a task/run/candidate ID, copy a hash,
edit a decision JSON, select a runner, or manage a round number.  Those values
belong to the fixed host.

## Required flow

1. Run `harness.ps1 start <public-function>`.
2. Read the current `第N轮测试方案审查.zh-CN.md` report.
3. Submit feedback with `harness.ps1 comment "..."`.
4. When feedback requests any change, let the configured model produce a complete replacement
   candidate in a new immutable round and review it again.
5. Treat a model `approve` decision only as comment interpretation.  Fixed host
   code must also detect explicit execution consent and create a hash-bound
   execution approval.
6. Execute only the exact candidate, prompt, review packet, round, and runner
   bound by that approval.  Write `final_report.zh-CN.md` whether execution
   passes or fails.

## Fixed boundaries

- Generation and comment interpretation always use the configured Message API.
- Before approval, only candidate generation, pure materialization, and fixed
  validation gates may run.  The SGGK SDK runner must not run.
- A comment that combines revision with “execute” creates a new review round;
  it never modifies and executes in the same transition.
- Session, event, comment, round, candidate, provenance, and approval hashes are
  host-owned and append-only.
- Proprietary source excerpts may be sent only to the intranet profile.  An
  external protocol/model simulator uses the same candidate budgets and gates,
  but never receives proprietary source evidence.
- Unknown functions and unsupported adapter archetypes become explicit Harness
  extension backlog candidates.  Never invent runnable support.

## Internal IR

`test_harness/forms/api_test_form.schema.json` remains an internal host/model
contract for regression and interface distillation.  It is not a user form.
Low-level prompt-pack and pipeline CLIs remain diagnostic interfaces only.
