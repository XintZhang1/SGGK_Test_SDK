# Message API Harness Profiles

`run_message_harness_pipeline.py` is the only production authoring entrypoint.
Each independent response is exactly one JSON object from
`choices[0].message.content`. A task may generate several candidates in
parallel; fixed host code normalizes, gates, canonical-hash de-duplicates,
executes, scores, selects, and atomically promotes one candidate.

SiliconFlow is an explicit external simulator for the same manifest, candidate
contract, fixed gates, selection, and SDK execution path used by the intranet
Qwen endpoint. Equal protocol/model identity does not imply byte- or
token-identical sampling. It is never an implicit fallback for `intranet`.

## Boundary

- Profile, endpoint, model, credential, and optional CA bundle come only from
  environment variables.
- Only `message.content` is candidate data. Reasoning text and credentials are
  never persisted.
- Model JSON cannot provide commands, executable/runner paths, cwd,
  environment, shell, SDK/link flags, or dataset/output paths.
- Contract/fixed-gate repair is bounded. SDK/oracle failure enters
  qualification and replay; it never triggers code repair by the model.
- Only the integrated pipeline may set `authoring_accepted=true`.
- There is no human-authored or fixture-seeding acceptance path.

## Profiles

Intranet Qwen endpoint:

```powershell
$env:SGGK_QWEN_BASE_URL = "https://<intranet-host>/v1"
$env:SGGK_QWEN_MODEL = "<intranet-qwen-model-id>"
$env:SGGK_QWEN_API_KEY = "<optional-token>"
$env:SGGK_QWEN_CA_BUNDLE = "<optional-ca-pem>"
```

Explicit SiliconFlow simulator:

```powershell
$env:SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
$env:SILICONFLOW_MODEL = "<verified-qwen-model-id>"
$env:SILICONFLOW_API_KEY = "<token>"
```

Missing required variables fail closed. Setting only SiliconFlow variables
cannot make an `intranet` run use SiliconFlow.

## Candidate batch

```powershell
python .\test_harness\tools\build_model_prompt_pack.py `
  --out .\artifacts\model_prompt_pack

python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --run-id qwen36_batch `
  --candidate-count 3 `
  --candidate-parallelism 3 `
  --execute `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  .\artifacts\model_prompt_pack\model_task_manifest.json
```

The intranet default is three candidates and 32,768 output tokens per authoring
or investigation call; `siliconflow-test` defaults to one candidate and 8,192
tokens to bound external calls. Candidate count is limited to eight. Repeated
`--authoring-role` selects the role cycle. `--task-id` may be repeated for a
bounded subset. Existing accepted outputs require `--overwrite` for
replacement.

Selection goals are host-owned: `fixed_gate_only`, `must_pass_execution`,
`must_reproduce_target_signature`, `extension_backlog`, and
`adapter_build_pass`. `auto` requires execution success when `--execute` is
present. Completion order, latency, token usage, and model self-scores are
ignored.

Typed large campaigns use `campaign_request`; the model never emits a command:

```json
{
  "kind": "campaign_request",
  "profile_id": "abc_boolean_mass_recut",
  "args": {"target_cases": 1000, "preset": "smoke"},
  "notes": [],
  "expected_artifacts": []
}
```

The manifest registers bounded argument schemas. Fixed code later binds local
runner/data/output paths and executes an argv array with `shell=false`.

## New API adaptation

```powershell
python .\test_harness\tools\build_api_adaptation_task.py `
  .\test_harness\api_intakes\<api>.json `
  --out .\artifacts\api_adaptation\<api>

python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --candidate-count 3 `
  --selection-goal adapter_build_pass `
  --execute `
  .\artifacts\api_adaptation\<api>\model_task_manifest.json
```

Qwen emits only `api_plugin_candidate` spec/schema/examples/capability data.
Fixed code emits the registered C++ template and requires an isolated Release
build, positive/negative schema checks, runtime adapter registry presence, and
three equal semantic replay hashes. The gate binds the candidate to the trusted
API intake and records SDK-input, runner, runtime-registry, build-report, and
semantic hashes; a different valid API or weakened smoke oracle is rejected.

## Candidate bug investigation

```powershell
python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --execute `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --analyze-bugs `
  --bug-source-root $env:SGGK_SOURCE_ROOT `
  .\artifacts\model_prompt_pack\model_task_manifest.json
```

This enables deterministic qualification, three-attempt replay, bounded
signature-preserving reduction, paired isolated TopoTrack capture/control,
failure bundles, and parallel reproduction/topology/source/skeptical Qwen
investigators. Investigators request registered content tools only.
Only `stable_same_failure` groups whose every attempt matches the immutable
signature enter this root-cause lane. Other outcomes remain inconclusive and
cannot create a reproducer, draft, or Qwen hypothesis report. Replay counts are
host-derived rather than model-authored.

Source locations require opaque host-issued references. With
`siliconflow-test`, source excerpts are disabled unless
`--allow-external-source-evidence` is explicitly present. Final reports remain
`candidate_only` and require alternatives, counter-evidence, registered
falsification tools, and an immutable reproduction/signature reference.

## Evidence and exit status

Each task writes under
`artifacts/message_harness_pipeline/<run-id>/<task-id>/`. `task_summary.json`
records the complete candidate pool, roles, hashes, duplicate relations, fixed
gates, execution states, deterministic scores, selection, and artifacts. The
batch writes `pipeline_summary.json`.

Transport attempts retain redacted request/response, candidate, contract,
provenance, and hashes. Formal JSON/provenance appears only after selection.
Post-promotion consumers require hash-matching Message API provenance,
`authoring_accepted=true`, and a successful fixed gate.

- Exit `0`: every selected task reached its requested accepted/executed state.
- Exit `1`: a Message API, gate, selection, or requested execution failed.
- Exit `2`: profile, CLI, manifest, or trusted local input was invalid.

The internal `run_authoring_gateway.py` diagnostic cannot establish
`authoring_accepted` and is not a production entrypoint.

Local deployments may increase `--max-tokens`, candidate count, and bounded
investigation rounds. Response bytes, repairs, tool calls, SDK timeouts/jobs,
replay count, reducer trials, and all execution authority remain host bounded.
