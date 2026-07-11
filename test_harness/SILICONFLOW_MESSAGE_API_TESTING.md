# Message API Harness Pipeline

The production authoring path is `run_message_harness_pipeline.py`. It accepts
an explicit provider profile, reads exactly one JSON object from
`choices[0].message.content`, stages it as a candidate, runs the output contract
and kind-specific fixed harness gate, performs bounded diagnostic repair, and
only then atomically promotes a formal output plus provenance.

SiliconFlow is an explicit external test profile for exercising the same
Message API path before an intranet deployment. It is never an implicit
fallback for the `intranet` profile.

## Boundary

- The profile, endpoint, model, credential, and optional CA bundle come only
  from environment variables.
- `reasoning_content` is never a candidate; only its character count and
  SHA-256 may be recorded.
- Credentials, authorization headers, and reasoning text are never persisted.
- The transport gateway does not run the SDK, execute model-authored commands,
  apply a patch, or invoke Git.
- Contract errors and fixed-gate errors have separate bounded repair budgets.
  SDK/oracle failures never trigger model repair; they enter triage/replay.
- Only pipeline provenance may set `authoring_accepted=true`.

## Profiles

Intranet Qwen endpoint:

```powershell
$env:SGGK_QWEN_BASE_URL = "https://<intranet-host>/v1"
$env:SGGK_QWEN_MODEL = "<intranet-qwen-model-id>"
$env:SGGK_QWEN_API_KEY = "<optional-token>"
$env:SGGK_QWEN_CA_BUNDLE = "<optional-ca-pem>"
```

Explicit SiliconFlow test endpoint:

```powershell
$env:SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
$env:SILICONFLOW_MODEL = "<verified-qwen-model-id>"
$env:SILICONFLOW_API_KEY = "<token>"
```

Missing variables fail closed. Setting only the SiliconFlow variables cannot
make an `intranet` run use SiliconFlow.

## Production batch

Build the deterministic prompt pack, then run its manifest:

```powershell
python .\test_harness\tools\build_model_prompt_pack.py `
  --out .\artifacts\model_prompt_pack

python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile siliconflow-test `
  --run-id siliconflow_qwen_smoke `
  --max-contract-repairs 1 `
  --max-gate-repairs 1 `
  .\artifacts\model_prompt_pack\model_task_manifest.json
```

Use `--profile intranet` for the internal endpoint. `--task-id <id>` may be
repeated to select a bounded subset. Add `--execute --runner <runner>` for the
test/triage/report stage, and `--campaign-dataset <root>` when a selected task
may return `campaign_request`. Existing formal outputs must be a verified
fixed-gate accepted pair; replacement requires explicit `--overwrite`.

## Bounded single task

```powershell
python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --task-id api_boolean_smoke `
  --run-id api_boolean_smoke `
  .\artifacts\model_prompt_pack\model_task_manifest.json
```

The manifest owns prompt, expected formal output, allowed kinds, campaign
profiles, and task identity. CLI arguments cannot replace that contract.

Large corpus work uses a typed `campaign_request`, never a command string:

```json
{
  "kind": "campaign_request",
  "profile_id": "abc_boolean_mass_recut",
  "args": {"target_cases": 1000, "preset": "smoke"},
  "notes": [],
  "expected_artifacts": []
}
```

The prompt-pack manifest carries `allowed_campaign_profiles` with bounded JSON
schemas and defaults, but no executable, runner, dataset, or output binding.
Fixed local code resolves an accepted request to an argv array later with
`shell=False`; model output cannot provide executable or path bindings.

## Evidence, acceptance, and exit status

Each pipeline task writes under
`artifacts/message_harness_pipeline/<run-id>/<task-id>/` and nests gateway
attempts, fixed-gate reports, repair evidence, optional execution evidence, and
`task_summary.json`. The batch writes `pipeline_summary.json`.

Low-level transport attempts include:

- `request_manifest.json`
- `raw_response.json` (reasoning removed)
- `candidate.json` when an object was parsed and it contains no configured secret
- `contract_report.json`
- `provenance.json`
- `hashes.json`

A transport/contract-successful attempt is still only a candidate. Formal
`<output>.json` and `<output>.provenance.json` are published only after the
pipeline's fixed gate passes. Formal provenance records the candidate hash,
fixed-gate evidence, and `authoring_accepted=true`.

- Exit `0`: every selected task reached its requested accepted/executed state.
- Exit `1`: at least one Message API, fixed-gate, or requested execution task failed.
- Exit `2`: profile, CLI, manifest, or local input configuration was invalid.

`run_authoring_gateway.py` remains a low-level transport/contract diagnostic
tool. Its contract-level promotion must target disposable debug/staging paths;
it does not establish `authoring_accepted` and is not a production entrypoint.
