from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any

from test_harness.authoring_gateway.client import (
    HttpResponse,
    OpenAICompatibleMessageClient,
)
from test_harness.authoring_gateway.config import PROFILE_SPECS, GatewayConfig
from test_harness.authoring_gateway.gateway import TaskSpec
from test_harness.tools.run_message_harness_pipeline import (
    ExecutionResult,
    MessageHarnessPipeline,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def response(candidate: dict[str, Any]) -> HttpResponse:
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(candidate)},
            }
        ]
    }
    return HttpResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode())


def config() -> GatewayConfig:
    return GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="zai-org/GLM-5.2",
        api_key="parallel-test-key",
        request_timeout_seconds=1.0,
        max_retries=0,
    )


def candidate(case_id: str) -> dict[str, Any]:
    recipe = json.loads(
        (REPO_ROOT / "test_harness/recipes/topology_section_spheres_smoke.json").read_text(
            encoding="utf-8"
        )
    )
    recipe["case_id"] = case_id
    # The fixed complexity gate rejects default-placed primitive-only recipes;
    # give the mock candidate a non-default tolerance focus so it passes.
    recipe["modeling_tol"] = 0.005
    return {"kind": "flat_recipe", "recipe": recipe, "notes": []}


class QueueTransport:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self.items = [response(item) for item in candidates]
        self.lock = threading.Lock()

    def post(self, **_kwargs: Any) -> HttpResponse:
        with self.lock:
            if not self.items:
                raise AssertionError("response queue exhausted")
            return self.items.pop(0)


class EvaluationPipeline(MessageHarnessPipeline):
    def __init__(self, *args: Any, passing: set[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.passing = passing
        self.executed: list[str] = []

    def _execute(self, task: TaskSpec, gate: Any, task_root: Path, **_kwargs: Any) -> ExecutionResult:
        del task, task_root
        normalized = json.loads((self.repo_root / gate.normalized_path).read_text(encoding="utf-8"))
        case_id = normalized["case_id"]
        self.executed.append(case_id)
        ok = case_id in self.passing
        return ExecutionResult(
            True,
            ok,
            "passed" if ok else "sdk_or_oracle_failures_triaged",
            error="" if ok else "mock execution failure",
            candidate_cause="" if ok else "oracle_or_sdk_requires_classification",
        )


def pipeline(tmp_path: Path, candidates: list[dict[str, Any]], passing: set[str]) -> EvaluationPipeline:
    gateway_config = config()
    client = OpenAICompatibleMessageClient(
        gateway_config,
        transport=QueueTransport(candidates),
    )
    return EvaluationPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=REPO_ROOT,
        client=client,
        gate_timeout_seconds=30.0,
        passing=passing,
    )


def task(tmp_path: Path) -> TaskSpec:
    return TaskSpec(
        task_id="parallel_selection",
        prompt="Generate a topology-section flat recipe.",
        expected_output_path=tmp_path / "artifacts/accepted/parallel.json",
        output_contract={"type": "json_object", "allowed_kinds": ["flat_recipe"]},
    )


def test_message_candidates_cannot_generate_and_execute_in_one_step(tmp_path: Path) -> None:
    spec = task(tmp_path)
    harness = pipeline(tmp_path, [candidate("candidate_one")], {"candidate_one"})

    result = harness.run_task(
        spec,
        run_id="approval_required",
        candidate_count=1,
        candidate_parallelism=1,
        max_contract_repairs=0,
        max_gate_repairs=0,
        execute=True,
        runner="unused-by-mock",
    )

    assert not result.ok
    assert not result.authoring_accepted
    assert "cannot generate and execute in one step" in result.error
    assert harness.executed == []
    assert not Path(spec.expected_output_path).exists()


class BarrierTransport:
    def __init__(self) -> None:
        self.barrier = threading.Barrier(2)
        self.counter = 0
        self.lock = threading.Lock()
        self.overlapped = False

    def post(self, **_kwargs: Any) -> HttpResponse:
        with self.lock:
            index = self.counter
            self.counter += 1
        try:
            self.barrier.wait(timeout=5.0)
            self.overlapped = True
        except threading.BrokenBarrierError as exc:  # pragma: no cover - diagnostic assertion below
            raise AssertionError("Message API branches did not overlap") from exc
        return response(candidate(f"parallel_{index}"))


def test_message_candidate_branches_overlap_when_parallelism_allows(tmp_path: Path) -> None:
    gateway_config = config()
    transport = BarrierTransport()
    harness = MessageHarnessPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=REPO_ROOT,
        client=OpenAICompatibleMessageClient(gateway_config, transport=transport),
        gate_timeout_seconds=30.0,
    )

    result = harness.run_task(
        task(tmp_path),
        run_id="parallel_overlap",
        candidate_count=2,
        candidate_parallelism=2,
        max_contract_repairs=0,
        max_gate_repairs=0,
    )

    assert result.ok
    assert transport.overlapped
    assert result.message_calls == 2


def test_target_signature_accepts_stable_same_failure_replay(tmp_path: Path) -> None:
    gateway_config = config()
    harness = MessageHarnessPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=REPO_ROOT,
        client=OpenAICompatibleMessageClient(
            gateway_config,
            transport=QueueTransport([]),
        ),
    )
    signature = {
        "schema_version": 1,
        "kind": "sdk_status",
        "returncode": 2,
        "sdk_error_code": 17,
    }
    triage = tmp_path / "artifacts/triage"
    replay = tmp_path / "artifacts/replay"
    triage.mkdir(parents=True)
    replay.mkdir(parents=True)
    (triage / "regression_seeds.json").write_text(
        json.dumps([{"fingerprint": "fp_same", "failure_signature": signature}]),
        encoding="utf-8",
    )
    (replay / "replay_summary.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "fingerprint": "fp_same",
                        "status": "stable_same_failure",
                        "expected_failure_signature": signature,
                        "attempt_count": 3,
                        "attempts": [
                            {
                                "matches_expected": True,
                                "failure_signature": signature,
                                "returncode": 2,
                            }
                            for _ in range(3)
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    execution = ExecutionResult(
        True,
        False,
        "sdk_or_oracle_failures_triaged",
        artifacts={"triage": "artifacts/triage", "replay": "artifacts/replay"},
    )

    matched, stable_count = harness._matches_target_signature(execution, signature)

    assert matched
    assert stable_count == 3


def test_target_signature_rejects_legacy_status_and_declared_only_attempts(tmp_path: Path) -> None:
    gateway_config = config()
    harness = MessageHarnessPipeline(
        gateway_config,
        repo_root=tmp_path,
        tool_repo_root=REPO_ROOT,
        client=OpenAICompatibleMessageClient(gateway_config, transport=QueueTransport([])),
    )
    signature = {"schema_version": 1, "kind": "crash", "exception_code": "0xC0000005"}
    triage = tmp_path / "artifacts/triage"
    replay = tmp_path / "artifacts/replay"
    triage.mkdir(parents=True)
    replay.mkdir(parents=True)
    (triage / "regression_seeds.json").write_text(
        json.dumps([{"fingerprint": "fp_legacy", "failure_signature": signature}]),
        encoding="utf-8",
    )
    (replay / "replay_summary.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "fingerprint": "fp_legacy",
                        "status": "stable_failure",
                        "expected_failure_signature": signature,
                        "attempt_count": 3,
                        "attempts": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    execution = ExecutionResult(
        True,
        False,
        "sdk_or_oracle_failures_triaged",
        artifacts={"triage": "artifacts/triage", "replay": "artifacts/replay"},
    )

    matched, stable_count = harness._matches_target_signature(execution, signature)

    assert not matched
    assert stable_count == 0
