from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from typing import Any

from test_harness.authoring_gateway.client import (
    HttpResponse,
    OpenAICompatibleMessageClient,
    canonical_json_bytes,
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
        model="Qwen3.6-35B-A3B",
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


def test_later_execution_pass_is_selected_and_only_then_promoted(tmp_path: Path) -> None:
    values = [candidate("candidate_fails"), candidate("candidate_passes"), candidate("candidate_also_fails")]
    harness = pipeline(tmp_path, values, {"candidate_passes"})

    result = harness.run_task(
        task(tmp_path),
        run_id="later_pass",
        candidate_count=3,
        candidate_parallelism=1,
        max_contract_repairs=0,
        max_gate_repairs=0,
        execute=True,
        runner="unused-by-mock",
    )

    assert result.ok
    assert result.selected_candidate_id == "candidate_02_test_design"
    assert set(harness.executed) == {"candidate_fails", "candidate_passes", "candidate_also_fails"}
    assert json.loads((tmp_path / result.accepted_path).read_text()) == values[1]
    provenance = json.loads((tmp_path / result.provenance_path).read_text())
    assert provenance["candidate_selection"]["policy"] == "must_pass_execution"
    assert len(provenance["candidate_selection"]["pool"]) == 3


def test_no_execution_pass_never_creates_formal_output(tmp_path: Path) -> None:
    values = [candidate("fail_one"), candidate("fail_two")]
    harness = pipeline(tmp_path, values, set())
    spec = task(tmp_path)

    result = harness.run_task(
        spec,
        run_id="all_fail",
        candidate_count=2,
        candidate_parallelism=1,
        max_contract_repairs=0,
        max_gate_repairs=0,
        execute=True,
        runner="unused-by-mock",
    )

    assert not result.ok
    assert not result.authoring_accepted
    assert "no independent candidate satisfied" in result.error
    assert not Path(spec.expected_output_path).exists()


def test_canonical_duplicate_is_gated_but_executed_once(tmp_path: Path) -> None:
    duplicate = candidate("same_candidate")
    unique = candidate("unique_candidate")
    harness = pipeline(tmp_path, [duplicate, duplicate, unique], {"same_candidate", "unique_candidate"})

    result = harness.run_task(
        task(tmp_path),
        run_id="deduplicate",
        candidate_count=3,
        candidate_parallelism=1,
        max_contract_repairs=0,
        max_gate_repairs=0,
        execute=True,
        runner="unused-by-mock",
    )

    assert result.ok
    assert harness.executed.count("same_candidate") == 1
    assert harness.executed.count("unique_candidate") == 1
    assert result.candidates[1]["duplicate_of"] == "candidate_01_implementation"
    expected_hash = hashlib.sha256(canonical_json_bytes(duplicate)).hexdigest()
    assert result.candidates[0]["candidate_sha256"] == expected_hash


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


def test_stable_target_reproducer_is_successful_even_though_execution_fails(tmp_path: Path) -> None:
    value = candidate("stable_reproducer")
    harness = pipeline(tmp_path, [value], set())
    harness._matches_target_signature = lambda *_args, **_kwargs: (True, 3)  # type: ignore[method-assign]

    result = harness.run_task(
        task(tmp_path),
        run_id="stable_reproducer",
        candidate_count=1,
        candidate_parallelism=1,
        max_contract_repairs=0,
        max_gate_repairs=0,
        execute=True,
        runner="unused-by-mock",
        selection_goal="must_reproduce_target_signature",
        target_failure_signature={"kind": "oracle_failure"},
    )

    assert result.ok
    assert result.authoring_accepted
    assert not result.execution.ok
    assert result.candidates[0]["score"]["target_signature_match"] is True
    assert result.candidates[0]["score"]["stable_replay_count"] == 3


def test_adapter_build_goal_rejects_an_executable_non_plugin_candidate(tmp_path: Path) -> None:
    value = candidate("ordinary_recipe")
    harness = pipeline(tmp_path, [value], {"ordinary_recipe"})

    result = harness.run_task(
        task(tmp_path),
        run_id="adapter_kind_binding",
        candidate_count=1,
        candidate_parallelism=1,
        max_contract_repairs=0,
        max_gate_repairs=0,
        execute=True,
        runner="unused-by-mock",
        selection_goal="adapter_build_pass",
    )

    assert not result.ok
    assert not result.authoring_accepted
    assert result.candidates[0]["fixed_gate"]["kind"] == "flat_recipe"
    assert result.candidates[0]["score"]["eligible"] is False
