"""Host-bound evidence contracts for intranet source-guided authoring.

The model may explain source behavior, but it may not invent the source identity
or the relationship between a branch, a failure hypothesis, an enhancement,
and a generated case.  This module keeps those identities deterministic and
revalidates the current source bytes before an output can be accepted.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_source(path: Path) -> tuple[bytes, list[str]]:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    return data, text.splitlines()


def line_content_sha256(lines: Sequence[str], line_start: int, line_end: int) -> str:
    payload = [
        {"line": line_number, "text": lines[line_number - 1]}
        for line_number in range(line_start, line_end + 1)
    ]
    return sha256_json(payload)


def build_source_contract(
    *,
    task_id: str,
    finding: Mapping[str, Any],
    source_path: Path,
    source_root: Path,
    line_start: int,
    line_end: int,
    root_id: str = "source_root_0",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Create the model-visible contract plus host-only local bindings."""

    return build_source_contract_from_ranges(
        task_id=task_id,
        finding=finding,
        source_root=source_root,
        source_ranges=[
            {
                "source_path": source_path,
                "line_start": line_start,
                "line_end": line_end,
            }
        ],
        root_id=root_id,
    )


def build_source_contract_from_ranges(
    *,
    task_id: str,
    finding: Mapping[str, Any],
    source_root: Path,
    source_ranges: Sequence[Mapping[str, Any]],
    root_id: str = "source_root_0",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bind one or more implementation definitions into a single contract."""

    if not source_ranges:
        raise ValueError("source_ranges must contain at least one definition")

    resolved_root = source_root.resolve(strict=True)
    finding_id = str(finding.get("id") or "")
    refs: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    seen_ranges: set[tuple[str, int, int]] = set()
    for source_range in source_ranges:
        resolved_path = Path(str(source_range.get("source_path") or "")).resolve(strict=True)
        relative_path = resolved_path.relative_to(resolved_root).as_posix()
        data, lines = read_source(resolved_path)
        line_start = int(source_range.get("line_start") or 0)
        line_end = int(source_range.get("line_end") or 0)
        if not (1 <= line_start <= line_end <= len(lines)):
            raise ValueError("source excerpt line range is outside the current source file")
        range_key = (relative_path, line_start, line_end)
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)
        ref_seed = {
            "root_id": root_id,
            "relative_path": relative_path,
            "line_start": line_start,
            "line_end": line_end,
        }
        source_ref_id = f"src_{sha256_json(ref_seed)[:16]}"
        refs.append(
            {
                "source_ref_id": source_ref_id,
                **ref_seed,
                "content_sha256": line_content_sha256(lines, line_start, line_end),
                "file_sha256": sha256_bytes(data),
            }
        )
        bindings.append(
            {
                "source_ref_id": source_ref_id,
                "root_path": str(resolved_root),
                "source_path": str(resolved_path),
            }
        )
    if not refs:
        raise ValueError("source_ranges did not contain a unique definition")
    contract: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "finding_id": finding_id,
        "finding_sha256": sha256_json(dict(finding)),
        "source_refs": refs,
    }
    contract["source_contract_sha256"] = sha256_json(contract)
    return contract, bindings


def validate_contract_hash(contract: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    supplied = contract.get("source_contract_sha256")
    unsigned = {key: value for key, value in contract.items() if key != "source_contract_sha256"}
    if not isinstance(supplied, str) or len(supplied) != 64:
        errors.append("source_contract_sha256 must be a 64-character SHA-256")
    elif supplied != sha256_json(unsigned):
        errors.append("source_contract_sha256 does not match the canonical contract")
    refs = contract.get("source_refs")
    if not isinstance(refs, list) or not refs:
        errors.append("source contract must contain at least one source reference")
    return errors


def verify_current_source(
    contract: Mapping[str, Any],
    bindings: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Re-read current bytes and reject moved, escaped, or stale evidence."""

    errors = validate_contract_hash(contract)
    refs = contract.get("source_refs") if isinstance(contract.get("source_refs"), list) else []
    refs_by_id = {
        str(item.get("source_ref_id")): item
        for item in refs
        if isinstance(item, dict) and item.get("source_ref_id")
    }
    bindings_by_id = {
        str(item.get("source_ref_id")): item
        for item in bindings
        if isinstance(item, Mapping) and item.get("source_ref_id")
    }
    if set(refs_by_id) != set(bindings_by_id):
        errors.append("host source bindings do not exactly match contract source_ref_ids")
    current: list[dict[str, Any]] = []
    for ref_id, ref in refs_by_id.items():
        binding = bindings_by_id.get(ref_id)
        if binding is None:
            continue
        try:
            root = Path(str(binding.get("root_path") or "")).resolve(strict=True)
            path = Path(str(binding.get("source_path") or "")).resolve(strict=True)
            relative = path.relative_to(root).as_posix()
            if relative != ref.get("relative_path"):
                raise ValueError("relative source path changed")
            data, lines = read_source(path)
            line_start = int(ref.get("line_start") or 0)
            line_end = int(ref.get("line_end") or 0)
            if not (1 <= line_start <= line_end <= len(lines)):
                raise ValueError("bound line range is outside current source")
            content_hash = line_content_sha256(lines, line_start, line_end)
            file_hash = sha256_bytes(data)
            if content_hash != ref.get("content_sha256"):
                raise ValueError("bound excerpt content changed")
            if file_hash != ref.get("file_sha256"):
                raise ValueError("bound source file changed")
            current.append(
                {
                    "source_ref_id": ref_id,
                    "relative_path": relative,
                    "line_start": line_start,
                    "line_end": line_end,
                    "content_sha256": content_hash,
                    "file_sha256": file_hash,
                }
            )
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"source reference {ref_id} is stale or unsafe: {exc}")
    return errors, current


def _object_list(review: Mapping[str, Any], key: str, errors: list[str]) -> list[dict[str, Any]]:
    value = review.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
        errors.append(f"source_review.{key} must be a non-empty array of objects")
        return []
    return list(value)


def _unique_ids(items: Sequence[Mapping[str, Any]], key: str, label: str, errors: list[str]) -> set[str]:
    result: set[str] = set()
    for item in items:
        value = item.get(key)
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            errors.append(f"{label}.{key} must be a stable identifier")
            continue
        if value in result:
            errors.append(f"duplicate {label}.{key}: {value}")
        result.add(value)
    return result


def _refs(value: Any, allowed: set[str], label: str, errors: list[str]) -> set[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        errors.append(f"{label} must be a non-empty string array")
        return set()
    result = set(value)
    unknown = result - allowed
    if unknown:
        errors.append(f"{label} contains unknown ids: {sorted(unknown)}")
    return result


def validate_source_review(
    review: Mapping[str, Any],
    contract: Mapping[str, Any],
    generated_case_ids: Sequence[str],
) -> list[str]:
    """Validate the complete source-ref -> branch -> hypothesis -> case graph."""

    errors = validate_contract_hash(contract)
    if review.get("schema_version") != 1:
        errors.append("source_review.schema_version must equal 1")
    if review.get("task_id") != contract.get("task_id"):
        errors.append("source_review.task_id does not match the source contract")
    if review.get("finding_id") != contract.get("finding_id"):
        errors.append("source_review.finding_id does not match the source contract")
    if review.get("source_contract_sha256") != contract.get("source_contract_sha256"):
        errors.append("source_review.source_contract_sha256 does not match the source contract")
    summary = review.get("summary")
    if not isinstance(summary, str) or len(summary.strip()) < 20:
        errors.append("source_review.summary must contain at least 20 characters")

    contract_refs = {
        str(item.get("source_ref_id")): item
        for item in contract.get("source_refs", [])
        if isinstance(item, dict)
    }
    review_refs = _object_list(review, "source_refs", errors)
    review_ref_ids = _unique_ids(review_refs, "source_ref_id", "source_ref", errors)
    if review_ref_ids != set(contract_refs):
        errors.append("source_review.source_refs must exactly match the host-issued source_ref_ids")
    for item in review_refs:
        expected = contract_refs.get(str(item.get("source_ref_id")))
        if expected is None:
            continue
        for key in ("line_start", "line_end", "content_sha256"):
            if item.get(key) != expected.get(key):
                errors.append(f"source_review source reference {item.get('source_ref_id')} changed {key}")

    branches = _object_list(review, "risky_branches", errors)
    branch_ids = _unique_ids(branches, "branch_id", "risky_branch", errors)
    branch_ref_usage: set[str] = set()
    for branch in branches:
        branch_ref_usage |= _refs(
            branch.get("source_ref_ids"),
            set(contract_refs),
            f"risky_branch[{branch.get('branch_id')}].source_ref_ids",
            errors,
        )
        for field in ("condition", "risk"):
            if not isinstance(branch.get(field), str) or len(branch[field].strip()) < 8:
                errors.append(f"risky_branch[{branch.get('branch_id')}].{field} is too short")
    if branch_ref_usage != set(contract_refs):
        errors.append("every host-issued source reference must be used by a risky branch")

    hypotheses = _object_list(review, "failure_hypotheses", errors)
    hypothesis_ids = _unique_ids(hypotheses, "hypothesis_id", "failure_hypothesis", errors)
    if len(hypotheses) < 2:
        errors.append("source_review must contain at least two failure hypotheses")
    branch_usage: set[str] = set()
    for hypothesis in hypotheses:
        branch_usage |= _refs(
            hypothesis.get("branch_ids"),
            branch_ids,
            f"failure_hypothesis[{hypothesis.get('hypothesis_id')}].branch_ids",
            errors,
        )
        for field in ("trigger", "observable_failure"):
            if not isinstance(hypothesis.get(field), str) or len(hypothesis[field].strip()) < 8:
                errors.append(f"failure_hypothesis[{hypothesis.get('hypothesis_id')}].{field} is too short")
    if branch_ids - branch_usage:
        errors.append("every risky branch must be referenced by a failure hypothesis")

    enhancements = _object_list(review, "test_enhancements", errors)
    _unique_ids(enhancements, "enhancement_id", "test_enhancement", errors)
    case_ids = set(generated_case_ids)
    hypothesis_usage: set[str] = set()
    case_usage: set[str] = set()
    for enhancement in enhancements:
        hypothesis_usage |= _refs(
            enhancement.get("hypothesis_ids"),
            hypothesis_ids,
            f"test_enhancement[{enhancement.get('enhancement_id')}].hypothesis_ids",
            errors,
        )
        case_usage |= _refs(
            enhancement.get("case_ids"),
            case_ids,
            f"test_enhancement[{enhancement.get('enhancement_id')}].case_ids",
            errors,
        )
        if not isinstance(enhancement.get("strategy"), str) or len(enhancement["strategy"].strip()) < 8:
            errors.append(f"test_enhancement[{enhancement.get('enhancement_id')}].strategy is too short")
        for field in ("perturbations", "oracles"):
            values = enhancement.get(field)
            if not isinstance(values, list) or not values or not all(isinstance(item, str) and item.strip() for item in values):
                errors.append(f"test_enhancement[{enhancement.get('enhancement_id')}].{field} must be non-empty strings")
    if hypothesis_ids - hypothesis_usage:
        errors.append("every failure hypothesis must be referenced by a test enhancement")
    if case_ids - case_usage:
        errors.append("every generated case must be referenced by a test enhancement")
    return errors


def generated_case_ids(kind: str, normalized: Mapping[str, Any]) -> list[str]:
    if kind == "needs_harness_extension":
        smoke = normalized.get("minimum_smoke_case")
        value = smoke.get("case_id") if isinstance(smoke, dict) else None
        return [value] if isinstance(value, str) and value else []
    cases = normalized.get("cases")
    if isinstance(cases, list):
        return [
            str(item.get("case_id"))
            for item in cases
            if isinstance(item, dict) and isinstance(item.get("case_id"), str) and item.get("case_id")
        ]
    value = normalized.get("case_id")
    return [value] if isinstance(value, str) and value else []
