from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from test_harness.orchestration.workflow import _sha256_file, _sha256_json
from test_harness.tools.verify_external_chain import (
    RESULT_SCHEMA,
    SILICONFLOW_ENDPOINT_SHA256,
    render_markdown,
    verify_external_chain,
    write_outputs,
)

MODEL = "zai-org/GLM-5.2"
PROFILE = "siliconflow"
SOURCE_TYPE = "siliconflow_message_api"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, value: str = "artifact\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def repo_relative(repo: Path, path: Path) -> str:
    return path.relative_to(repo).as_posix()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def provider_metadata() -> dict[str, object]:
    return {
        "profile": PROFILE,
        "profile_category": "external",
        "endpoint_sha256": SILICONFLOW_ENDPOINT_SHA256,
        "base_url_env": "SILICONFLOW_BASE_URL",
        "api_key_env": "SILICONFLOW_API_KEY",
        "api_key_present": True,
        "model": MODEL,
        "model_env": "SILICONFLOW_MODEL",
        "base_url_locked": True,
        "model_locked": True,
        "default_thinking_mode": "disabled",
        "default_stream": True,
    }


def model_response(candidate: dict[str, object], response_id: str) -> dict[str, object]:
    content = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
    usage = {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}
    body_sha256 = sha256_text(f"fixture-sse-wire:{response_id}")
    body = {
        "id": response_id,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": usage,
    }
    response = {
        "mode": "json_schema",
        "stream": True,
        "transport_try": 1,
        "status": 200,
        "headers": {
            "content-type": "text/event-stream",
            "date": "Fri, 17 Jul 2026 00:00:00 GMT",
        },
        "body": body,
        "body_sha256": body_sha256,
        "body_bytes": 2048,
        "synthetic_body_sha256": sha256_text(json.dumps(body, sort_keys=True)),
        "stream_metadata": {
            "raw_stream_sha256": body_sha256,
            "raw_stream_bytes": 2048,
            "raw_stream_complete": True,
            "event_count": 3,
            "done": True,
            "finish_reason": "stop",
            "candidate_content_bytes": len(content.encode("utf-8")),
            "refusal_bytes": 0,
            "reasoning_content_sha256": "",
            "reasoning_content_chars": 0,
            "reasoning_content_bytes": 0,
            "error": "",
            "error_kind": "",
        },
    }
    return {
        "ok": True,
        "error": "",
        "error_kind": "",
        "candidate_source": "message.content",
        "message_content": content,
        "message_content_sha256": sha256_text(content),
        "reasoning_content_sha256": "",
        "reasoning_content_chars": 0,
        "finish_reason": "stop",
        "final_mode": "json_schema",
        "usage": usage,
        "events": [
            {
                "mode": "json_schema",
                "transport_try": 1,
                "request_sha256": sha256_text(f"fixture-request:{response_id}"),
                "status": 200,
                "retry": False,
                "retry_delay_seconds": 0.0,
            }
        ],
        "provider_responses": [response],
    }


def make_label_only_model_session(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    session_root = repo / "artifacts" / "harness_sessions" / "external_api_boolean_001"
    round_root = session_root / "rounds" / "0001"
    task_id = "api_boolean_external_chain"

    resolution = round_root / "resolution.json"
    prompt = round_root / "prompt" / "task.md"
    reviewed_manifest = round_root / "reviewed_manifest.json"
    candidate = round_root / "candidate.json"
    provenance = round_root / "provenance.json"
    review_packet = round_root / "review_packet.json"
    fixed_report = round_root / "fixed_review_report.zh-CN.md"
    subject_digest = round_root / "subject_digest.json"
    user_report = round_root / "user_review_report.zh-CN.md"
    write_json(resolution, {"resolved_api": "api_boolean"})
    write_text(prompt, "public interface prompt\n")
    write_json(reviewed_manifest, {"tasks": [{"task_id": task_id, "prompt_path": repo_relative(repo, prompt)}]})
    write_json(candidate, {"kind": "api_test", "task_id": task_id})
    write_json(provenance, {"provider": "siliconflow"})
    write_json(review_packet, {"accepted": True})
    write_text(fixed_report, "# 固定门禁报告\n")
    write_json(subject_digest, {"public_function": "api_boolean"})
    write_text(user_report, "# 用户审查报告\n")

    round_record = {
        "round_number": 1,
        "task_id": task_id,
        "resolution_path": repo_relative(repo, resolution),
        "resolution_sha256": _sha256_json(json.loads(resolution.read_text(encoding="utf-8"))),
        "manifest_path": repo_relative(repo, reviewed_manifest),
        "manifest_sha256": _sha256_file(reviewed_manifest),
        "candidate_path": repo_relative(repo, candidate),
        "candidate_sha256": _sha256_json(json.loads(candidate.read_text(encoding="utf-8"))),
        "provenance_path": repo_relative(repo, provenance),
        "provenance_sha256": _sha256_file(provenance),
        "review_packet_path": repo_relative(repo, review_packet),
        "review_packet_sha256": _sha256_file(review_packet),
        "fixed_review_report_path": repo_relative(repo, fixed_report),
        "fixed_review_report_sha256": _sha256_file(fixed_report),
        "subject_digest_path": repo_relative(repo, subject_digest),
        "subject_digest_sha256": _sha256_json(json.loads(subject_digest.read_text(encoding="utf-8"))),
        "user_review_report_path": repo_relative(repo, user_report),
        "user_review_report_sha256": _sha256_file(user_report),
    }
    round_record["round_sha256"] = _sha256_json(round_record)
    write_json(round_root / "round_manifest.json", round_record)

    approval_path = round_root / "comments" / "approved" / "approval" / "approval.json"
    execution_manifest_path = round_root / "comments" / "approved" / "approval" / "execution_manifest.json"
    approval_relative = repo_relative(repo, approval_path)
    execution_manifest_relative = repo_relative(repo, execution_manifest_path)
    write_json(
        execution_manifest_path,
        {
            "tasks": [
                {
                    "task_id": task_id,
                    "harness_session_id": "external_api_boolean_001",
                    "harness_round_number": 1,
                    "approved_round_sha256": round_record["round_sha256"],
                    "approved_candidate_sha256": round_record["candidate_sha256"],
                    "approval_attestation_path": approval_relative,
                }
            ]
        },
    )
    unsigned_approval = {
        "schema_version": 1,
        "record_type": "execution_approval",
        "decision": "approved_for_execution",
        "session_id": "external_api_boolean_001",
        "task_id": task_id,
        "round_number": 1,
        "round_sha256": round_record["round_sha256"],
        "candidate_sha256": round_record["candidate_sha256"],
        "reviewed_manifest_sha256": round_record["manifest_sha256"],
        "execution_manifest_path": execution_manifest_relative,
        "execution_manifest_sha256": _sha256_file(execution_manifest_path),
        "authority": "fixed_harness_host_after_model_comment_interpretation",
    }
    write_json(approval_path, {**unsigned_approval, "approval_sha256": _sha256_json(unsigned_approval)})

    execution_root = session_root / "execution" / "round_0001" / "attempt_0001"
    execution_result_path = execution_root / "execution_result.json"
    final_report_path = execution_root / "final_report.zh-CN.md"
    write_json(
        execution_result_path,
        {
            "ok": True,
            "results": [
                {
                    "authoring_accepted": True,
                    "execution": {"requested": True, "ok": True, "status": "passed"},
                }
            ],
        },
    )
    write_text(final_report_path, "# SGGK Harness 最终测试报告\n\n- 总体结果：`通过`\n")

    event_root = session_root / "events"
    previous = ""

    def add_event(sequence: int, event_type: str, payload: dict[str, object]) -> str:
        nonlocal previous
        unsigned = {
            "schema_version": 1,
            "sequence": sequence,
            "event_type": event_type,
            "previous_event_sha256": previous,
            "payload_sha256": _sha256_json(payload),
            "recorded_at": f"2026-07-17T00:00:0{sequence}Z",
            "payload": payload,
        }
        event_hash = _sha256_json(unsigned)
        write_json(event_root / f"{sequence:06d}.json", {**unsigned, "event_sha256": event_hash})
        previous = event_hash
        return event_hash

    add_event(1, "SESSION_CREATED", {"public_function": "api_boolean"})
    add_event(
        2,
        "EXECUTION_APPROVED",
        {
            "round_number": 1,
            "approval_sha256": _sha256_json(unsigned_approval),
            "execution_manifest_sha256": _sha256_file(execution_manifest_path),
        },
    )
    add_event(
        3,
        "EXECUTION_COMPLETED",
        {
            "round_number": 1,
            "attempt": 1,
            "execution_result_path": repo_relative(repo, execution_result_path),
            "execution_result_sha256": _sha256_file(execution_result_path),
            "final_report_sha256": _sha256_file(final_report_path),
        },
    )
    write_json(
        session_root / "session.json",
        {
            "schema_version": 1,
            "session_id": "external_api_boolean_001",
            "public_function": "api_boolean",
            "provider_profile": "siliconflow",
            "provider_profile_category": "external",
            "data_classification": "public_interface",
            "state": "completed",
            "current_round": 1,
            "current_round_sha256": round_record["round_sha256"],
            "approved_round": 1,
            "approval_path": approval_relative,
            "execution_manifest_path": execution_manifest_relative,
            "execution_manifest_sha256": _sha256_file(execution_manifest_path),
            "execution_attempt": 1,
            "final_report_path": repo_relative(repo, final_report_path),
            "event_sequence": 3,
            "event_head_sha256": previous,
        },
    )
    return session_root


def make_model_session(tmp_path: Path) -> Path:
    # Structural evidence fixture only. It exercises cross-binding rules and is
    # deliberately not claimed as cryptographic proof of a provider call.
    session_root = make_label_only_model_session(tmp_path)
    repo = session_root.parents[2]
    round_root = session_root / "rounds" / "0001"
    session_path = session_root / "session.json"
    round_path = round_root / "round_manifest.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    round_record = json.loads(round_path.read_text(encoding="utf-8"))
    task_id = str(round_record["task_id"])
    run_id = "external_api_boolean_001_review_r0001"
    candidate_id = "candidate_01_implementation"
    gateway_task_id = f"{task_id}__{candidate_id}__attempt_01"

    manifest_path = repo / round_record["manifest_path"]
    candidate_path = repo / round_record["candidate_path"]
    provenance_path = repo / round_record["provenance_path"]
    review_packet_path = repo / round_record["review_packet_path"]
    fixed_report_path = repo / round_record["fixed_review_report_path"]
    prompt_path = round_root / "prompt" / "task.md"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate_sha256 = _sha256_json(candidate)
    prompt_sha256 = _sha256_file(prompt_path)
    generation_response = model_response(candidate, "chatcmpl-siliconflow-generation-001")
    write_json(
        manifest_path,
        {
            "tasks": [
                {
                    "task_id": task_id,
                    "prompt_path": repo_relative(repo, prompt_path),
                    "expected_output_path": repo_relative(repo, candidate_path),
                    "provider_profile": PROFILE,
                    "provider_profile_category": "external",
                    "data_classification": "public_interface",
                    "allowed_profile_categories": ["external"],
                }
            ]
        },
    )
    write_json(
        provenance_path,
        {
            "schema_version": 3,
            "request_id": task_id,
            "source_type": SOURCE_TYPE,
            "source_label": PROFILE,
            "source_path": repo_relative(repo, prompt_path),
            "output_path": repo_relative(repo, candidate_path),
            "model": MODEL,
            "interface": "openai_compatible_chat_completions_message_content_json",
            "profile": PROFILE,
            "run_id": run_id,
            "attempt": 1,
            "prompt_sha256": prompt_sha256,
            "task_prompt_sha256": prompt_sha256,
            "candidate_sha256": candidate_sha256,
            "message_content_sha256": generation_response["message_content_sha256"],
            "usage": generation_response["usage"],
            "acceptance": {
                "authoring_accepted": True,
                "requires_fixed_gate": False,
                "accepted_by": "message_harness_pipeline",
            },
            "boundary": {
                "model_calls": True,
                "direct_api_calls": True,
                "runs_sdk": False,
                "executes_commands": False,
                "applies_patches": False,
                "commits_changes": False,
                "wired_into_harness": False,
            },
            "fixed_gate": {"ok": True, "gate_attempt": 1, "kind": "api_test"},
            "candidate_selection": {"candidate_id": candidate_id, "candidate_count": 1},
            "generated_artifact_review": {
                "review_packet_sha256": _sha256_file(review_packet_path),
                "review_report_sha256": _sha256_file(fixed_report_path),
            },
        },
    )

    gateway_root = round_root / "pipeline" / "gateway" / run_id / gateway_task_id
    attempt_root = gateway_root / "attempt_01"
    request_path = attempt_root / "request_manifest.json"
    raw_response_path = attempt_root / "raw_response.json"
    contract_path = attempt_root / "contract_report.json"
    attempt_candidate_path = attempt_root / "candidate.json"
    attempt_provenance_path = attempt_root / "provenance.json"
    write_json(
        request_path,
        {
            "schema_version": 1,
            "run_id": run_id,
            "task_id": gateway_task_id,
            "attempt": 1,
            "provider": provider_metadata(),
            "prompt": {
                "prompt_path": repo_relative(repo, prompt_path),
                "manifest_path": repo_relative(repo, manifest_path),
                "system_chars": 128,
                "system_sha256": sha256_text("fixture-system"),
                "user_chars": len(prompt_path.read_text(encoding="utf-8")),
                "user_sha256": prompt_sha256,
            },
            "response_options": {
                "response_mode": "auto",
                "schema_name": "sggk_authoring_candidate",
                "temperature": 0.2,
                "max_tokens": 32768,
                "thinking_mode": "disabled",
                "stream": True,
            },
            "boundary": {
                "runs_sdk": False,
                "executes_commands": False,
                "applies_patches": False,
                "commits_changes": False,
            },
        },
    )
    write_json(raw_response_path, generation_response)
    write_json(
        contract_path,
        {"ok": True, "kind": "api_test", "error_count": 0, "warning_count": 0, "diagnostics": []},
    )
    write_json(attempt_candidate_path, candidate)
    write_json(
        attempt_provenance_path,
        {
            "schema_version": 1,
            "run_id": run_id,
            "task_id": gateway_task_id,
            "attempt": 1,
            "profile": PROFILE,
            "source_type": SOURCE_TYPE,
            "model": MODEL,
            "prompt_sha256": prompt_sha256,
            "message_content_sha256": generation_response["message_content_sha256"],
            "candidate_sha256": candidate_sha256,
            "finish_reason": "stop",
            "response_mode": "json_schema",
            "usage": generation_response["usage"],
            "promotion": {"eligible": True, "completed": True},
            "boundary": {
                "model_calls": True,
                "direct_api_calls": True,
                "runs_sdk": False,
                "executes_commands": False,
                "applies_patches": False,
                "commits_changes": False,
                "wired_into_harness": False,
            },
        },
    )
    attempt_files = (request_path, raw_response_path, contract_path, attempt_candidate_path, attempt_provenance_path)
    write_json(
        attempt_root / "hashes.json",
        {"algorithm": "sha256", **{path.name: _sha256_file(path) for path in attempt_files}},
    )
    write_json(
        round_root / "pipeline" / "generation_result.json",
        {
            "schema_version": 1,
            "ok": True,
            "run_id": run_id,
            "message_calls": 1,
            "results": [
                {
                    "ok": True,
                    "task_id": task_id,
                    "run_id": run_id,
                    "authoring_accepted": True,
                    "accepted_path": repo_relative(repo, candidate_path),
                    "provenance_path": repo_relative(repo, provenance_path),
                    "selected_candidate_id": candidate_id,
                    "candidates": [
                        {
                            "candidate_id": candidate_id,
                            "candidate_sha256": candidate_sha256,
                            "attempts": [
                                {
                                    "candidate_id": candidate_id,
                                    "gate_attempt": 1,
                                    "gateway": {
                                        "ok": True,
                                        "task_id": gateway_task_id,
                                        "run_id": run_id,
                                        "attempts": 1,
                                        "staging_path": repo_relative(repo, gateway_root),
                                        "skipped": False,
                                        "error": "",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )

    round_record.update(
        {
            "schema_version": 1,
            "session_id": session["session_id"],
            "provider_profile": PROFILE,
            "provider_profile_category": "external",
            "data_classification": "public_interface",
            "allowed_profile_categories": ["external"],
            "run_id": run_id,
            "manifest_sha256": _sha256_file(manifest_path),
            "candidate_sha256": candidate_sha256,
            "provenance_sha256": _sha256_file(provenance_path),
        }
    )
    round_record["round_sha256"] = _sha256_json(
        {key: value for key, value in round_record.items() if key != "round_sha256"}
    )
    write_json(round_path, round_record)

    comment_root = round_root / "comments" / "provider_approved"
    comment_path = comment_root / "user_comment.txt"
    interpretation_path = comment_root / "interpretation.json"
    comment_text = "我批准当前测试方案，请执行真实 SDK 测试。"
    write_text(comment_path, comment_text)
    decision = {
        "decision": "approve",
        "summary_zh_cn": "当前候选已通过固定检查，可以执行。",
        "requested_changes": [],
        "constraints": ["保留现有语义断言。"],
    }
    context_sha256 = _sha256_json({"round": 1, "subject": round_record["subject_digest_sha256"]})
    contract_sha256 = _sha256_json({"contract": "review_comment_response"})
    write_json(
        comment_root / "message_task.json",
        {
            "schema_version": 1,
            "task_type": "review_comment",
            "comment_sha256": _sha256_file(comment_path),
            "context_sha256": context_sha256,
            "contract_sha256": contract_sha256,
            "comment": comment_text,
            "context": {
                "task_id": task_id,
                "run_id": run_id,
                "round_number": 1,
                "subject_sha256": round_record["subject_digest_sha256"],
            },
        },
    )
    comment_response = model_response(decision, "chatcmpl-siliconflow-comment-001")
    write_json(comment_root / "message_attempt_01.json", comment_response)
    interpretation = {
        "schema_version": 2,
        "record_type": "review_comment_decision",
        "status": "model_interpreted",
        "source": "model_message_api",
        "model_called": True,
        "review_id": "review_fixture_001",
        "round_id": "round_fixture_001",
        "comment_id": "comment_fixture_001",
        "task_id": task_id,
        "run_id": run_id,
        "round_number": 1,
        "subject_sha256": round_record["subject_digest_sha256"],
        "comment_sha256": _sha256_file(comment_path),
        "context_sha256": context_sha256,
        "contract_sha256": contract_sha256,
        "response_sha256": _sha256_json(decision),
        "decision": decision,
        "provider": provider_metadata(),
        "message_attempts": 1,
        "user_comment": comment_text,
    }
    write_json(interpretation_path, interpretation)

    approval_path = repo / session["approval_path"]
    execution_manifest_path = repo / session["execution_manifest_path"]
    execution_manifest = json.loads(execution_manifest_path.read_text(encoding="utf-8"))
    execution_task = execution_manifest["tasks"][0]
    execution_task["approved_round_sha256"] = round_record["round_sha256"]
    execution_task["approved_candidate_sha256"] = candidate_sha256
    write_json(execution_manifest_path, execution_manifest)
    unsigned_approval = {
        "schema_version": 1,
        "record_type": "execution_approval",
        "decision": "approved_for_execution",
        "session_id": session["session_id"],
        "task_id": task_id,
        "round_number": 1,
        "round_sha256": round_record["round_sha256"],
        "candidate_sha256": candidate_sha256,
        "reviewed_manifest_sha256": round_record["manifest_sha256"],
        "execution_manifest_path": session["execution_manifest_path"],
        "execution_manifest_sha256": _sha256_file(execution_manifest_path),
        "task_prompt_sha256": prompt_sha256,
        "review_packet_sha256": round_record["review_packet_sha256"],
        "comment_path": repo_relative(repo, comment_path),
        "comment_sha256": _sha256_file(comment_path),
        "interpretation_path": repo_relative(repo, interpretation_path),
        "interpretation_sha256": _sha256_json(interpretation),
        "authority": "fixed_harness_host_after_model_comment_interpretation",
    }
    approval = {**unsigned_approval, "approval_sha256": _sha256_json(unsigned_approval)}
    write_json(approval_path, approval)

    event_root = session_root / "events"
    previous = ""

    def add_event(sequence: int, event_type: str, payload: dict[str, object]) -> None:
        nonlocal previous
        unsigned = {
            "schema_version": 1,
            "sequence": sequence,
            "event_type": event_type,
            "previous_event_sha256": previous,
            "payload_sha256": _sha256_json(payload),
            "recorded_at": f"2026-07-17T00:00:{sequence:02d}Z",
            "payload": payload,
        }
        previous = _sha256_json(unsigned)
        write_json(event_root / f"{sequence:06d}.json", {**unsigned, "event_sha256": previous})

    add_event(1, "SESSION_CREATED", {"public_function": "api_boolean"})
    add_event(2, "ROUND_READY_FOR_REVIEW", {"round_number": 1, "round_sha256": round_record["round_sha256"]})
    add_event(3, "COMMENT_RECEIVED", {"round_number": 1, "comment_sha256": _sha256_file(comment_path)})
    add_event(
        4,
        "COMMENT_INTERPRETED",
        {"round_number": 1, "decision": "approve", "interpretation_sha256": _sha256_json(interpretation)},
    )
    add_event(
        5,
        "EXECUTION_APPROVED",
        {
            "round_number": 1,
            "approval_sha256": approval["approval_sha256"],
            "execution_manifest_sha256": _sha256_file(execution_manifest_path),
        },
    )
    final_report_path = repo / session["final_report_path"]
    execution_result_path = final_report_path.parent / "execution_result.json"
    add_event(
        6,
        "EXECUTION_COMPLETED",
        {
            "round_number": 1,
            "attempt": 1,
            "execution_result_path": repo_relative(repo, execution_result_path),
            "execution_result_sha256": _sha256_file(execution_result_path),
            "final_report_sha256": _sha256_file(final_report_path),
        },
    )
    session.update(
        {
            "profile": PROFILE,
            "provider_profile": PROFILE,
            "provider_profile_category": "external",
            "current_round_sha256": round_record["round_sha256"],
            "execution_manifest_sha256": _sha256_file(execution_manifest_path),
            "event_sequence": 6,
            "event_head_sha256": previous,
        }
    )
    write_json(session_path, session)
    return session_root


def make_abc_corpus(tmp_path: Path) -> Path:
    root = tmp_path / "abc_corpus"
    source = root / "dataset" / "sample.step"
    source.parent.mkdir(parents=True)
    source_bytes = b"ISO-10303-21;\n"
    source.write_bytes(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    archive = root / "downloads" / "abc_0000_step_v00.7z"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"fixture archive bytes")
    archive_md5 = hashlib.md5(archive.read_bytes()).hexdigest()
    include_list = root / "extracted" / "abc_0000_step_v00_include.txt"
    write_text(include_list, "dataset/sample.step\n")
    fetch_plan = root / "abc_full_fetch_plan.json"
    write_json(
        fetch_plan,
        {
            "archives": [
                {
                    "chunk": "0000",
                    "format": "step",
                    "archive": archive.name,
                    "size_bytes": archive.stat().st_size,
                    "md5": archive_md5,
                }
            ]
        },
    )
    dataset_index = root / "dataset_index.json"
    write_json(
        dataset_index,
        {
            "hash_inputs": True,
            "total_files": 1,
            "files": [
                {
                    "index": 0,
                    "path": str(source),
                    "api": "step_import",
                    "size_bytes": len(source_bytes),
                    "sha256": source_sha256,
                }
            ],
        },
    )
    write_json(
        root / "source_archive_provenance.json",
        {
            "schema_version": 1,
            "kind": "abc_download_extract_binding",
            "ok": True,
            "plan": {
                "path": str(fetch_plan),
                "sha256": _sha256_file(fetch_plan),
                "chunk": "0000",
                "format": "step",
            },
            "archive": {
                "path": str(archive),
                "expected_size_bytes": archive.stat().st_size,
                "actual_size_bytes": archive.stat().st_size,
                "expected_md5": archive_md5,
                "actual_md5": archive_md5,
                "size_verified": True,
                "md5_verified": True,
            },
            "extraction": {
                "include_list": str(include_list),
                "include_list_sha256": _sha256_file(include_list),
                "archive_member": "dataset/sample.step",
                "path": str(source),
                "size_bytes": len(source_bytes),
                "sha256": source_sha256,
            },
            "dataset_index": {
                "path": str(dataset_index),
                "file_sha256": _sha256_file(dataset_index),
                "selected_index": 0,
                "recorded_source_sha256": source_sha256,
                "source_sha256_verified": True,
            },
        },
    )
    recipe = root / "_recipes" / "case_001.json"
    write_json(recipe, {"case_id": "case_001", "api": "step_import", "source_file": str(source)})
    case_root = root / "case_001"
    write_json(case_root / "manifest.json", {"case_id": "case_001", "api": "step_import"})
    write_json(case_root / "report" / "status.json", {"succeeded": True, "result_body_count": 1})
    triage_root = root / "triage"
    write_json(
        triage_root / "triage_summary.json",
        {
            "roots": [str(root)],
            "total_cases": 1,
            "artifact_cases": 1,
            "pre_artifact_failure_cases": 0,
            "passed_cases": 1,
            "failed_cases": 0,
            "failure_group_count": 0,
            "warning_cases": 0,
            "command_failures": 0,
            "failures": [],
        },
    )
    write_json(
        root / "corpus_summary.json",
        {
            "runner": "sggk_case_runner.exe",
            "out_root": str(root),
            "total": 1,
            "executed": 1,
            "skipped": 0,
            "passed": 1,
            "failed": 0,
            "timed_out": 0,
            "results": [
                {
                    "recipe": str(recipe),
                    "returncode": 0,
                    "timed_out": False,
                    "skipped": False,
                    "source_file": str(source),
                    "api": "step_import",
                }
            ],
            "triage": {"returncode": 0, "out": str(triage_root)},
        },
    )
    write_json(
        root / "corpus_manifest.json",
        {
            "hash_inputs": True,
            "dataset_lists": [str(dataset_index)],
            "inputs": [
                {
                    "index": 0,
                    "source_file": str(source),
                    "case_id": "case_001",
                    "api": "step_import",
                    "size_bytes": len(source_bytes),
                    "sha256": source_sha256,
                }
            ],
        },
    )
    return root


def make_nx_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "nx_bundle"
    source_bytes = b"ISO-10303-21;\n"
    source = root / "dataset" / "sample.step"
    source.parent.mkdir(parents=True)
    source.write_bytes(source_bytes)
    digest = hashlib.sha256(source_bytes).hexdigest()
    dataset_index = root / "dataset_index.json"
    write_json(
        dataset_index,
        {
            "files": [
                {
                    "index": 0,
                    "path": str(source),
                    "api": "step_import",
                    "size_bytes": len(source_bytes),
                    "sha256": digest,
                }
            ]
        },
    )
    write_json(
        root / "nx" / "measurement.json",
        {
            "schema_version": 1,
            "kind": "sggk_nx_step_measurement",
            "ok": True,
            "status": "completed",
            "input": {"name": "sample.step", "sha256": digest, "size_bytes": len(source_bytes)},
            "nx": {"version": "2512", "full_version": "2512.0", "session_type": "Session"},
            "units": {
                "length": "millimeter",
                "area": "square_millimeter",
                "volume": "cubic_millimeter",
            },
            "import": {
                "ok": True,
                "protocol": "STEP AP214",
                "flatten_assembly": True,
                "body_count": 2,
                "solid_body_count": 2,
                "sheet_body_count": 0,
                "unknown_body_count": 0,
            },
            "measurement": {
                "ok": True,
                "accuracy": 0.999,
                "body_count": 2,
                "measured_body_count": 2,
                "total_area": 30.0,
                "total_abs_volume": 12.0,
                "bodies": [
                    {
                        "index": 0,
                        "tag": 100,
                        "body_type": "solid",
                        "measurement_ok": True,
                        "area": 10.0,
                        "abs_volume": 5.0,
                        "error": "",
                    },
                    {
                        "index": 1,
                        "tag": 101,
                        "body_type": "solid",
                        "measurement_ok": True,
                        "area": 20.0,
                        "abs_volume": 7.0,
                        "error": "",
                    },
                ],
            },
            "diagnostics": [{"code": "NX_STEP_MEASUREMENT_COMPLETED", "severity": "info", "message": "ok"}],
        },
    )
    write_json(
        root / "comparison" / "comparison.json",
        {
            "schema_version": 1,
            "kind": "nx_sggk_step_comparison",
            "ok": False,
            "input": {
                "same_input": True,
                "sha256": digest,
                "nx_sha256": digest,
                "sggk_sha256": digest,
            },
            "tolerances": {
                "abs_tol": 0.01,
                "rel_tol": 0.00001,
                "formula": "abs(nx-sggk) <= abs_tol + rel_tol * max(abs(nx), abs(sggk))",
            },
            "nx": {
                "version": "2512.0",
                "import_ok": True,
                "measurement_ok": True,
                "body_count": 2,
                "total_area": 30.0,
                "total_abs_volume": 12.0,
            },
            "sggk": {
                "version": "1.4.10",
                "import_ok": True,
                "measurement_ok": True,
                "body_count": 1,
                "shell_count": 2,
                "total_area": 30.0,
                "total_abs_volume": 12.0,
            },
            "checks": {
                "input_sha256": {"kind": "sha256_identity", "ok": True, "nx": digest, "sggk": digest},
                "import": {
                    "kind": "boolean_required_true",
                    "ok": True,
                    "expected": True,
                    "nx": True,
                    "sggk": True,
                },
                "body_count": {"kind": "exact_integer", "ok": False, "nx": 2, "sggk": 1, "delta": 1},
                "total_area": {
                    "kind": "abs_rel_numeric",
                    "ok": True,
                    "nx": 30.0,
                    "sggk": 30.0,
                    "delta": 0.0,
                    "abs_delta": 0.0,
                    "tolerance": 0.0103,
                    "abs_tol": 0.01,
                    "rel_tol": 0.00001,
                },
                "total_abs_volume": {
                    "kind": "abs_rel_numeric",
                    "ok": True,
                    "nx": 12.0,
                    "sggk": 12.0,
                    "delta": 0.0,
                    "abs_delta": 0.0,
                    "tolerance": 0.01012,
                    "abs_tol": 0.01,
                    "rel_tol": 0.00001,
                },
            },
            "failures": ["body_count_failed"],
            "diagnostics": [
                {
                    "code": "NX_SGGK_COMPOUND_BODY_SHELL_AGGREGATION",
                    "severity": "info",
                    "classification": "cross_kernel_representation_difference",
                    "nx_body_count": 2,
                    "sggk_body_count": 1,
                    "sggk_shell_count": 2,
                    "geometry_bug_confirmed": False,
                    "message": "NX body 数与 SGGK compound shell 数一致，属于表示差异。",
                }
            ],
        },
    )
    measurement_path = root / "nx" / "measurement.json"
    comparison_path = root / "comparison" / "comparison.json"
    write_json(
        root / "run_summary.json",
        {
            "schema_version": 1,
            "kind": "nx_sggk_step_compare_run",
            "ok": True,
            "outcome": "comparison_mismatch",
            "comparison_ok": False,
            "selection": {
                "dataset_index": str(dataset_index),
                "index": 0,
                "source": str(source),
                "sha256": digest,
                "size_bytes": len(source_bytes),
                "verified": True,
            },
            "paths": {
                "nx_measurement": str(measurement_path),
                "comparison_json": str(comparison_path),
            },
            "steps": {
                "sggk": {"status": "completed", "returncode": 0},
                "nx": {"status": "completed", "returncode": 0},
                "comparison": {"status": "completed_with_mismatch", "returncode": 2},
            },
        },
    )
    return root


def make_known_bug_campaign(tmp_path: Path) -> Path:
    root = tmp_path / "known_bug_campaign"
    registry = root / "known_bug_records" / "bug_registry.json"
    registry_report = root / "known_bug_records" / "bug_registry.md"
    replay_recipes = root / "known_bug_records" / "registry_replay_recipes.txt"
    replay = root / "known_bug_replay" / "recipe_summary.json"
    regression = root / "known_bug_regression" / "registry_regression.json"
    regression_report = root / "known_bug_regression" / "registry_regression.md"
    write_json(registry, {"total": 1, "bugs": [{"bug_id": "bug-001", "fingerprint": "abc123"}]})
    write_text(registry_report, "# Registry\n")
    write_text(replay_recipes, "recipe.json\n")
    write_json(replay, {"total": 1, "executed": 1, "results": [{"case_id": "bug-001", "returncode": 2}]})
    write_json(
        regression,
        {
            "total": 1,
            "status_counts": {"still_failing": 1},
            "results": [{"bug_id": "bug-001", "status": "still_failing", "reason": "expected failure reproduced"}],
        },
    )
    write_text(regression_report, "# Regression\n")
    write_text(root / "campaign_report.md", "# Campaign\n")
    write_text(root / "campaign_report.zh-CN.md", "# 已知 bug campaign\n")
    summary = root / "campaign_summary.json"
    write_json(
        summary,
        {
            "args": {"no_preview": True, "no_geometry_audit": True},
            "commands": [
                {"name": "known_bug_record_materialize", "ok": True, "returncode": 0},
                {"name": "known_bug_replay", "ok": True, "returncode": 2},
                {"name": "known_bug_regression", "ok": True, "returncode": 0},
            ],
            "lanes": [],
            "known_bug_regression": {
                "materialize_ok": True,
                "replay_ok": True,
                "regression_ok": True,
                "registry_path": str(registry),
                "registry_report": str(registry_report),
                "replay_recipes": str(replay_recipes),
                "replay_summary": str(replay),
                "regression_summary": str(regression),
                "regression_report": str(regression_report),
                "status_counts": {"still_failing": 1},
            },
        },
    )
    write_json(
        root / "campaign_verification" / "campaign_verification.json",
        {
            "summary_kind": "campaign",
            "campaign_root": str(root),
            "summary_path": str(summary),
            "ok": True,
            "error_count": 0,
            "warning_count": 0,
            "check_count": 1,
            "checks": [{"severity": "ok", "kind": "summary", "message": "campaign verified", "path": str(summary)}],
        },
    )
    return root


def make_inputs(tmp_path: Path) -> dict[str, Path]:
    return {
        "model_session": make_model_session(tmp_path),
        "abc_corpus": make_abc_corpus(tmp_path),
        "nx_bundle": make_nx_bundle(tmp_path),
        "known_bug_campaign": make_known_bug_campaign(tmp_path),
    }


def test_external_chain_accepts_complete_hash_bound_fixture_and_classified_nx_difference(
    tmp_path: Path,
) -> None:
    result = verify_external_chain(**make_inputs(tmp_path))

    schema = json.loads(RESULT_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(result)
    assert result["ok"] is True
    assert all(lane["ok"] for lane in result["lanes"].values())
    assert result["lanes"]["nx_comparison"]["warning_count"] == 1
    markdown = render_markdown(result)
    assert "外网 Harness 全链路验收报告" in markdown
    assert "总体结论：**通过**" in markdown
    assert "明确失败数" in markdown

    json_path, markdown_path = write_outputs(tmp_path / "verification", result)
    assert json.loads(json_path.read_text(encoding="utf-8"))["ok"] is True
    assert "已知 bug 资料链" in markdown_path.read_text(encoding="utf-8")


def test_external_chain_missing_inputs_returns_all_four_lane_errors(tmp_path: Path) -> None:
    result = verify_external_chain(
        model_session=tmp_path / "missing" / "session.json",
        abc_corpus=tmp_path / "missing" / "corpus_summary.json",
        nx_bundle=tmp_path / "missing_nx",
        known_bug_campaign=tmp_path / "missing" / "campaign_verification.json",
    )

    assert result["ok"] is False
    assert result["error_count"] >= 4
    assert all(lane["status"] == "failed" for lane in result["lanes"].values())


def test_external_chain_detects_final_report_nx_diagnostic_and_regression_classification_tampering(
    tmp_path: Path,
) -> None:
    inputs = make_inputs(tmp_path)
    session = json.loads((inputs["model_session"] / "session.json").read_text(encoding="utf-8"))
    repo = inputs["model_session"].parents[2]
    final_report = repo / session["final_report_path"]
    final_report.write_text(final_report.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    comparison_path = inputs["nx_bundle"] / "comparison" / "comparison.json"
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    comparison["diagnostics"] = []
    write_json(comparison_path, comparison)

    regression_path = inputs["known_bug_campaign"] / "known_bug_regression" / "registry_regression.json"
    regression = json.loads(regression_path.read_text(encoding="utf-8"))
    regression["results"][0]["reason"] = ""
    write_json(regression_path, regression)

    result = verify_external_chain(**inputs)

    assert result["ok"] is False
    codes = {
        item["code"]
        for lane in result["lanes"].values()
        for item in lane["errors"]
    }
    assert "MODEL_FINAL_REPORT_HASH_INVALID" in codes
    assert "NX_DIAGNOSTIC_MISSING" in codes
    assert "KNOWN_BUG_REGRESSION_UNCLASSIFIED" in codes


def test_external_chain_rejects_label_only_external_model_session(tmp_path: Path) -> None:
    inputs = make_inputs(tmp_path / "valid_lanes")
    inputs["model_session"] = make_label_only_model_session(tmp_path / "label_only")

    result = verify_external_chain(**inputs)

    assert result["ok"] is False
    codes = {item["code"] for item in result["lanes"]["model_harness"]["errors"]}
    assert "MODEL_PROFILE_NOT_SILICONFLOW" in codes
    assert "MODEL_ROUND_PROFILE_INVALID" in codes
    assert "MODEL_PROVIDER_PROVENANCE_INVALID" in codes


def test_external_chain_rejects_hash_consistent_forged_generation_response(tmp_path: Path) -> None:
    inputs = make_inputs(tmp_path)
    raw_response_path = next(inputs["model_session"].rglob("raw_response.json"))
    raw_response = json.loads(raw_response_path.read_text(encoding="utf-8"))
    raw_response["provider_responses"][-1]["body"]["model"] = "deterministic-local-fake"
    write_json(raw_response_path, raw_response)
    hashes_path = raw_response_path.parent / "hashes.json"
    hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    hashes["raw_response.json"] = _sha256_file(raw_response_path)
    write_json(hashes_path, hashes)

    result = verify_external_chain(**inputs)

    assert result["ok"] is False
    codes = {item["code"] for item in result["lanes"]["model_harness"]["errors"]}
    assert "MODEL_PROVIDER_GENERATION_RESPONSE_INVALID" in codes


def test_external_chain_rejects_forged_siliconflow_endpoint(tmp_path: Path) -> None:
    inputs = make_inputs(tmp_path)
    request_path = next(inputs["model_session"].rglob("request_manifest.json"))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["provider"]["endpoint_sha256"] = sha256_text("https://fake.invalid/chat/completions")
    write_json(request_path, request)
    hashes_path = request_path.parent / "hashes.json"
    hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    hashes["request_manifest.json"] = _sha256_file(request_path)
    write_json(hashes_path, hashes)

    result = verify_external_chain(**inputs)

    assert result["ok"] is False
    codes = {item["code"] for item in result["lanes"]["model_harness"]["errors"]}
    assert "MODEL_PROVIDER_REQUEST_PROFILE_INVALID" in codes


def test_external_chain_rejects_forged_comment_provider_response(tmp_path: Path) -> None:
    inputs = make_inputs(tmp_path)
    comment_response_path = next(
        path
        for path in inputs["model_session"].rglob("message_attempt_01.json")
        if "provider_approved" in path.parts
    )
    comment_response = json.loads(comment_response_path.read_text(encoding="utf-8"))
    comment_response["provider_responses"][-1]["body"]["id"] = ""
    write_json(comment_response_path, comment_response)

    result = verify_external_chain(**inputs)

    assert result["ok"] is False
    codes = {item["code"] for item in result["lanes"]["model_harness"]["errors"]}
    assert "MODEL_COMMENT_PROVIDER_RESPONSE_INVALID" in codes


def test_external_chain_rejects_tampered_abc_download_archive_and_nx_source(
    tmp_path: Path,
) -> None:
    inputs = make_inputs(tmp_path)
    archive = inputs["abc_corpus"] / "downloads" / "abc_0000_step_v00.7z"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    nx_source = inputs["nx_bundle"] / "dataset" / "sample.step"
    nx_source.write_bytes(nx_source.read_bytes() + b"tampered")

    result = verify_external_chain(**inputs)

    assert result["ok"] is False
    abc_codes = {item["code"] for item in result["lanes"]["abc_sggk"]["errors"]}
    nx_codes = {item["code"] for item in result["lanes"]["nx_comparison"]["errors"]}
    assert "ABC_DOWNLOAD_ARCHIVE_HASH_MISMATCH" in abc_codes
    assert "NX_SOURCE_SHA_MISMATCH" in nx_codes
