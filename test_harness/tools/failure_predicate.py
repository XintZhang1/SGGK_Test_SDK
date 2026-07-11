"""Stable failure signatures shared by triage, replay, and reduction.

The return code alone is never a sufficient reproduction predicate.  This
module classifies observable runner evidence without importing the SDK and
produces a JSON-safe signature that can travel with a regression seed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s`\"']+")
POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\s`\"']+/)*[^\s`\"']+")
HEX_EXCEPTION_RE = re.compile(r"\b0x[cC][0-9A-Fa-f]{7}\b")
PHASES = (
    "parse",
    "build_inputs",
    "invoke_api",
    "serialize_result",
    "topocheck",
    "oracle",
    "finalize",
)


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_failure_text(value: Any, *, limit: int = 320) -> str:
    text = str(value or "").lower()
    text = WINDOWS_PATH_RE.sub("<path>", text)
    text = POSIX_PATH_RE.sub("<path>", text)
    text = NUMBER_RE.sub("<num>", text)
    return " ".join(text.split())[:limit]


def validation_failure_keys(validation: Mapping[str, Any]) -> tuple[str, ...]:
    failures = validation.get("failures")
    if not isinstance(failures, list):
        return ()
    keys = {normalize_failure_text(item) for item in failures}
    return tuple(sorted(key for key in keys if key))


def topology_failure_keys(topo_check: Mapping[str, Any]) -> tuple[str, ...]:
    bodies = topo_check.get("bodies")
    if not isinstance(bodies, list):
        return ()
    keys: set[str] = set()
    for body in bodies:
        if not isinstance(body, dict) or body.get("ok") is not False:
            continue
        keys.add(
            json.dumps(
                {
                    "error_code": body.get("error_code"),
                    "error_string": normalize_failure_text(body.get("error_string")),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return tuple(sorted(keys))


def _exception_code(returncode: int, stderr: str) -> str:
    match = HEX_EXCEPTION_RE.search(stderr or "")
    if match:
        return match.group(0).upper()
    unsigned = returncode & 0xFFFFFFFF
    if unsigned >= 0xC0000000:
        return f"0x{unsigned:08X}"
    return ""


def _phase(run_state: Mapping[str, Any], stderr: str) -> str:
    for key in ("last_phase", "phase"):
        value = run_state.get(key)
        if isinstance(value, str) and value:
            return value
    lowered = (stderr or "").lower()
    for phase in PHASES:
        if phase in lowered:
            return phase
    return ""


def _typed_status_succeeded(status: Mapping[str, Any], validation: Mapping[str, Any]) -> bool:
    for payload in (status, validation):
        if (
            isinstance(payload.get("status_semantics"), str)
            and payload.get("expected_status_matched") is True
            and payload.get("test_outcome_succeeded") is True
            and isinstance(payload.get("expected_status"), str)
            and isinstance(payload.get("actual_status"), str)
        ):
            return True
    return False


def _phase_from_evidence(kind: str, recorded_phase: str) -> str:
    """Prefer completed artifact evidence over the launcher's coarse marker.

    ``run_recipes.py`` writes ``launching`` before the child process starts so
    pre-artifact crashes remain observable. Once a status, TopoCheck, or
    validation report exists, that marker is necessarily stale and must not
    become the long-lived failure location.
    """

    definitive = {
        "sdk_api_error": "invoke_api",
        "topology_failure": "topocheck",
        "oracle_failure": "oracle",
        "pass": "finalize",
    }
    return definitive.get(kind, recorded_phase)


def build_failure_signature(
    *,
    returncode: int,
    timed_out: bool = False,
    stderr: str = "",
    status: Mapping[str, Any] | None = None,
    validation: Mapping[str, Any] | None = None,
    topo_check: Mapping[str, Any] | None = None,
    run_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    status = status or {}
    validation = validation or {}
    topo_check = topo_check or {}
    run_state = run_state or {}
    validation_keys = validation_failure_keys(validation)
    topology_keys = topology_failure_keys(topo_check)
    exception_code = _exception_code(returncode, stderr)
    phase = _phase(run_state, stderr)
    typed_status_succeeded = _typed_status_succeeded(status, validation)

    if timed_out:
        kind = "timeout"
    elif status.get("succeeded") is False and not typed_status_succeeded:
        kind = "sdk_api_error"
    elif validation.get("ok") is False:
        kind = "oracle_failure"
    elif topology_keys:
        kind = "topology_failure"
    elif returncode == 0:
        kind = "pass"
    elif exception_code:
        kind = "crash"
    else:
        kind = "runner_error"

    phase = _phase_from_evidence(kind, phase)
    raw_error_code = None if typed_status_succeeded else status.get("error_code")
    error_code = raw_error_code if isinstance(raw_error_code, int) and not isinstance(raw_error_code, bool) else None
    signature: dict[str, Any] = {
        "schema_version": 1,
        "kind": kind,
        "returncode": int(returncode),
        "phase": phase,
        "exception_code": exception_code,
        "sdk_error_code": error_code,
        "validation_failures": list(validation_keys),
        "topology_failures": list(topology_keys),
        "message_signature": normalize_failure_text(
            status.get("error_message") or status.get("message") or stderr
        ),
    }
    return signature


def signature_from_artifact(
    artifact_dir: str | Path,
    *,
    returncode: int,
    timed_out: bool = False,
    stderr: str = "",
) -> dict[str, Any]:
    root = Path(artifact_dir) if artifact_dir else Path("__missing_artifact__")
    report = root / "report"
    return build_failure_signature(
        returncode=returncode,
        timed_out=timed_out,
        stderr=stderr,
        status=read_object(report / "status.json"),
        validation=read_object(report / "validation.json"),
        topo_check=read_object(report / "topo_check.json"),
        run_state=read_object(root / "run_state.json"),
    )


def signatures_match(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> tuple[bool, str]:
    expected_kind = str(expected.get("kind") or "")
    observed_kind = str(observed.get("kind") or "")
    if not expected_kind or expected_kind == "pass":
        return False, "missing_expected_failure_kind"
    if expected_kind != observed_kind:
        return False, f"kind_changed:{expected_kind}->{observed_kind}"

    if expected_kind == "timeout":
        return True, "same_timeout"
    if expected_kind == "sdk_api_error":
        expected_code = expected.get("sdk_error_code")
        if expected_code is not None and observed.get("sdk_error_code") != expected_code:
            return False, "sdk_error_code_changed"
    elif expected_kind == "oracle_failure":
        expected_keys = set(expected.get("validation_failures") or [])
        observed_keys = set(observed.get("validation_failures") or [])
        if expected_keys and not expected_keys.issubset(observed_keys):
            return False, "oracle_failure_changed"
    elif expected_kind == "topology_failure":
        expected_keys = set(expected.get("topology_failures") or [])
        observed_keys = set(observed.get("topology_failures") or [])
        if expected_keys and not expected_keys.intersection(observed_keys):
            return False, "topology_failure_changed"
    elif expected_kind == "crash":
        expected_code = str(expected.get("exception_code") or "")
        if expected_code and str(observed.get("exception_code") or "") != expected_code:
            return False, "exception_code_changed"
        expected_phase = str(expected.get("phase") or "")
        if expected_phase and str(observed.get("phase") or "") != expected_phase:
            return False, "crash_phase_changed"
    elif expected_kind == "runner_error":
        if int(observed.get("returncode", 0)) != int(expected.get("returncode", 0)):
            return False, "runner_returncode_changed"

    expected_message = str(expected.get("message_signature") or "")
    observed_message = str(observed.get("message_signature") or "")
    if expected_message and observed_message and expected_message != observed_message:
        return False, "message_signature_changed"
    return True, f"same_{expected_kind}"
