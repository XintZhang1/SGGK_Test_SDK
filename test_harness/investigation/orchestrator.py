"""Parallel investigator orchestration and deterministic report aggregation."""

from __future__ import annotations

import concurrent.futures
import copy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Sequence

from test_harness.authoring_gateway.client import CompletionOptions, OpenAICompatibleMessageClient
from test_harness.authoring_gateway.config import GatewayConfig

from .session import INVESTIGATOR_ROLES, InvestigationOutcome, InvestigationSession
from .tool_registry import InvestigationToolRegistry


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _confidence(hypothesis: dict[str, Any]) -> float:
    confidence = _dict(hypothesis.get("confidence"))
    score = confidence.get("score")
    return float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0


def aggregate_outcomes(
    *,
    bundle: dict[str, Any],
    outcomes: Sequence[InvestigationOutcome],
) -> dict[str, Any]:
    successful = [outcome for outcome in outcomes if outcome.ok]
    stable_attempts = int(bundle.get("stable_attempts") or 0)
    ranked: list[dict[str, Any]] = []
    for outcome in successful:
        for hypothesis in outcome.report.get("hypotheses", []):
            if not isinstance(hypothesis, dict):
                continue
            bound_hypothesis = copy.deepcopy(hypothesis)
            reproduction = _dict(bound_hypothesis.get("reproduction_path"))
            if reproduction:
                reproduction["stable_attempts"] = stable_attempts
                bound_hypothesis["reproduction_path"] = reproduction
            ranked.append(
                {
                    "investigator_role": outcome.role_id,
                    "session_id": outcome.session_id,
                    "reported_confidence": _confidence(hypothesis),
                    "hypothesis": bound_hypothesis,
                }
            )
    ranked.sort(
        key=lambda item: (
            -item["reported_confidence"],
            item["investigator_role"],
            str(item["hypothesis"].get("hypothesis_id") or ""),
        )
    )
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank
    classification_counts: dict[str, int] = {}
    for outcome in successful:
        kind = str(_dict(outcome.report.get("issue_classification")).get("kind") or "inconclusive")
        classification_counts[kind] = classification_counts.get(kind, 0) + 1
    return {
        "schema_version": 1,
        "kind": "multi_agent_bug_hypothesis_report",
        "assessment_status": "candidate_only",
        "confirmed_bug": False,
        "confirmed_root_cause": False,
        "failure_id": str(bundle.get("fingerprint") or ""),
        "representative_case_id": str(bundle.get("representative_case_id") or ""),
        "investigator_count": len(outcomes),
        "successful_investigators": len(successful),
        "failed_investigators": len(outcomes) - len(successful),
        "classification_counts": classification_counts,
        "ranked_hypotheses": ranked,
        "investigators": [outcome.as_dict() for outcome in outcomes],
        "reproduction": {
            "bundle_manifest": str(bundle.get("bundle_manifest") or ""),
            "fixed_bug_report": str(bundle.get("bug_report") or ""),
            "replay_status": str(bundle.get("replay_status") or ""),
            "stable_attempts": stable_attempts,
        },
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Candidate Bug Investigation: {report.get('representative_case_id', '')}",
        "",
        "> This is a Qwen-assisted candidate analysis, not a confirmed SDK bug or confirmed root cause.",
        "",
        f"- Failure ID: `{report.get('failure_id', '')}`",
        f"- Investigators: `{report.get('successful_investigators', 0)}/{report.get('investigator_count', 0)}` successful",
        f"- Classifications: `{report.get('classification_counts', {})}`",
        f"- Replay: `{_dict(report.get('reproduction')).get('replay_status', '')}`",
        "",
        "## Possible Root Causes and Locations",
        "",
    ]
    for item in report.get("ranked_hypotheses", []):
        if not isinstance(item, dict):
            continue
        hypothesis = _dict(item.get("hypothesis"))
        confidence = _dict(hypothesis.get("confidence"))
        lines.extend(
            [
                f"### {item.get('rank')}. {hypothesis.get('hypothesis_id', '')} — {hypothesis.get('category', '')}",
                "",
                f"- Investigator: `{item.get('investigator_role', '')}`",
                f"- Confidence: `{confidence.get('score', 0)}` (`{confidence.get('band', '')}`)",
                f"- Candidate claim: {hypothesis.get('statement', '')}",
                f"- Basis: {confidence.get('basis', '')}",
            ]
        )
        locations = hypothesis.get("suspect_locations")
        if isinstance(locations, list) and locations:
            lines.append("- Suspect locations:")
            for location in locations:
                if not isinstance(location, dict):
                    continue
                lines.append(
                    "  - `{ref}` `{symbol}` lines `{start}-{end}` ({role}): {why}".format(
                        ref=location.get("source_ref_id", ""),
                        symbol=location.get("symbol", ""),
                        start=location.get("line_start", 0),
                        end=location.get("line_end", 0),
                        role=location.get("role", ""),
                        why=location.get("rationale", ""),
                    )
                )
        lines.append("- Supporting evidence:")
        for evidence in hypothesis.get("supporting_evidence", []):
            if isinstance(evidence, dict):
                lines.append(
                    f"  - `{evidence.get('evidence_id', '')}`: {evidence.get('assertion', '')}"
                )
        contradicting = hypothesis.get("contradicting_evidence")
        if isinstance(contradicting, list) and contradicting:
            lines.append("- Counter-evidence:")
            for evidence in contradicting:
                if isinstance(evidence, dict):
                    lines.append(
                        f"  - `{evidence.get('evidence_id', '')}`: {evidence.get('assertion', '')}"
                    )
        reproduction = _dict(hypothesis.get("reproduction_path"))
        lines.append(
            "- Reproduction: `{ref}` expected signature `{sig}`, stable attempts `{attempts}`".format(
                ref=reproduction.get("reproduction_ref_id", ""),
                sig=reproduction.get("expected_signature_id", ""),
                attempts=reproduction.get("stable_attempts", 0),
            )
        )
        for probe in hypothesis.get("falsification_tests", []):
            if isinstance(probe, dict):
                lines.append(
                    f"- Falsification probe `{probe.get('tool_id', '')}`: {probe.get('experiment', '')} → {probe.get('expected_discriminator', '')}"
                )
        lines.append("")
    lines.extend(
        [
            "## Reproduction Bundle",
            "",
            f"- Bundle manifest: `{_dict(report.get('reproduction')).get('bundle_manifest', '')}`",
            f"- Deterministic report: `{_dict(report.get('reproduction')).get('fixed_bug_report', '')}`",
            "",
        ]
    )
    return "\n".join(lines)


def run_bundle_investigation(
    *,
    config: GatewayConfig,
    bundle: dict[str, Any],
    output_root: Path,
    source_roots: list[Path],
    allow_source_content: bool,
    role_ids: Sequence[str] = tuple(INVESTIGATOR_ROLES),
    parallelism: int = 4,
    max_rounds: int = 16,
    max_tool_calls: int = 32,
    completion_options: CompletionOptions | None = None,
) -> dict[str, Any]:
    unknown = sorted(set(role_ids) - set(INVESTIGATOR_ROLES))
    if unknown:
        raise ValueError(f"unknown investigator roles: {unknown}")
    if not role_ids:
        raise ValueError("at least one investigator role is required")
    if not 1 <= parallelism <= 8:
        raise ValueError("parallelism must be between 1 and 8")

    def run_role(index: int, role_id: str) -> InvestigationOutcome:
        registry = InvestigationToolRegistry(
            bundle_record=bundle,
            source_roots=source_roots,
            allow_source_content=allow_source_content,
        )
        options = completion_options or CompletionOptions(
            response_mode="auto",
            temperature=0.2,
            max_tokens=16_384,
            thinking_mode="enabled",
        )
        if options.seed is not None:
            options = replace(options, seed=options.seed + index)
        session = InvestigationSession(
            client=OpenAICompatibleMessageClient(config),
            registry=registry,
            role_id=role_id,
            output_root=output_root / "investigators" / role_id,
            completion_options=options,
            max_rounds=max_rounds,
            max_tool_calls=max_tool_calls,
            secret_values=(config.api_key,),
        )
        return session.run()

    outcomes: list[InvestigationOutcome] = []
    if len(role_ids) == 1 or parallelism == 1:
        outcomes = [run_role(index, role_id) for index, role_id in enumerate(role_ids)]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(parallelism, len(role_ids)),
            thread_name_prefix="sggk-bug-investigator",
        ) as executor:
            futures = {
                executor.submit(run_role, index, role_id): (index, role_id)
                for index, role_id in enumerate(role_ids)
            }
            indexed: list[tuple[int, InvestigationOutcome]] = []
            for future in concurrent.futures.as_completed(futures):
                index, role_id = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # pragma: no cover - defensive thread boundary
                    outcome = InvestigationOutcome(
                        False,
                        f"inv_{bundle.get('fingerprint', 'failure')}_{role_id}"[:96],
                        role_id,
                        0,
                        0,
                        error=str(exc),
                    )
                indexed.append((index, outcome))
            outcomes = [outcome for _, outcome in sorted(indexed, key=lambda item: item[0])]
    report = aggregate_outcomes(bundle=bundle, outcomes=outcomes)
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "bug_hypothesis_report.json"
    markdown_path = output_root / "bug_hypothesis_report.md"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    return {
        "ok": report["successful_investigators"] > 0,
        "failure_id": report["failure_id"],
        "report": str(report_path),
        "markdown": str(markdown_path),
        "successful_investigators": report["successful_investigators"],
        "failed_investigators": report["failed_investigators"],
    }
