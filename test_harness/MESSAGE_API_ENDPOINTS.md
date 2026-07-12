# Message API Endpoint Configuration

SGGK Harness has one model protocol and one user workflow.  The intranet Qwen
service and a SiliconFlow-hosted copy of the same model are replaceable endpoint
configurations for that protocol; they are not separate authoring modes.

Both endpoint profiles use exactly the same:

- `choices[0].message.content` exact-JSON contract;
- three independent candidate roles by default;
- 32,768-token candidate and investigation budget by default;
- fixed schema/normalization/materialization gates;
- immutable Chinese review rounds and natural-language comment schema;
- host approval attestation and real SDK execution path;
- provenance, qualification, replay, reduction, and final-report rules.

Sampling can still differ between two deployments even when model weights and
parameters match.  Fixed gates and semantic evidence, rather than byte-identical
model output, establish equivalence.

## Intranet endpoint

```powershell
$env:SGGK_HARNESS_PROFILE = "intranet"
$env:SGGK_QWEN_BASE_URL = "https://<intranet-host>/v1"
$env:SGGK_QWEN_MODEL = "<qwen-model-id>"
$env:SGGK_QWEN_API_KEY = "<optional-token>"
$env:SGGK_QWEN_CA_BUNDLE = "<optional-ca-pem>"
```

## Replaceable protocol/model simulator

```powershell
$env:SGGK_HARNESS_PROFILE = "siliconflow-test"
$env:SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
$env:SILICONFLOW_MODEL = "<same-qwen-model-id>"
$env:SILICONFLOW_API_KEY = "<token>"
$env:SILICONFLOW_CA_BUNDLE = "<optional-ca-pem>"
```

Missing variables fail closed.  One profile never falls back to the other.
Credentials, endpoint URLs, and reasoning text are not candidate data and are
not persisted in formal outputs.

## Identical ordinary workflow

After selecting either endpoint through environment configuration, the user
still runs only:

```powershell
.\harness.ps1 start api_boolean
.\harness.ps1 comment "增加退化输入和大坐标容差边界。"
.\harness.ps1 comment "这一版可以开始执行。"
```

The model cannot emit commands, runner/dataset/output paths, environment
variables, credentials, patches, or execution authority.  A Qwen `approve`
decision is only an interpretation of the comment; fixed host code separately
requires explicit consent and binds the latest candidate, prompt, review packet,
round, and runner hashes before execution.

## Proprietary source boundary

The external simulator never receives proprietary SDK source, source excerpts,
source-derived summaries, or source-search results.  Source-aware authoring and
root-cause investigation require the `intranet` profile.  There is no override
flag and no copy/paste fallback that weakens this boundary.

The external profile can still verify protocol compatibility and all generic
capability-based generation, comment, fixed-gate, and SDK execution behavior.

## Advanced diagnostics

Maintainers may inspect the low-level pipeline under
`artifacts/message_harness_pipeline/`, but ordinary API testing must use the
session orchestrator. Every Message-authored task is review-required; a task is rejected
by the lower-level pipeline unless its exact host approval attestation is
present; adding `--execute` cannot bypass review.
