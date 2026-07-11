from __future__ import annotations

from pathlib import Path

from reduce_failure_recipe import Observation, build_predicate, observation_preserves


def observation(returncode: int, stderr: str, *, phase: str = "") -> Observation:
    return Observation(
        label="test",
        recipe_path=Path("recipe.json"),
        returncode=returncode,
        elapsed_seconds=0.1,
        timed_out=False,
        stdout="",
        stderr=stderr,
        case_id="case",
        artifact_dir="",
        status={},
        validation={},
        topo_check={},
        run_state={"last_phase": phase} if phase else {},
    )


def test_reducer_does_not_accept_different_generic_nonzero_failure() -> None:
    baseline = observation(-1073741819, "access violation", phase="invoke_api")
    predicate = build_predicate(baseline)
    candidate = observation(1, "unknown recipe field", phase="parse")

    assert observation_preserves(candidate, predicate, match_error_code=True) is False


def test_reducer_accepts_same_crash_signature() -> None:
    baseline = observation(-1073741819, "access violation at address 123", phase="invoke_api")
    predicate = build_predicate(baseline)
    candidate = observation(-1073741819, "access violation at address 999", phase="invoke_api")

    assert observation_preserves(candidate, predicate, match_error_code=True) is True
