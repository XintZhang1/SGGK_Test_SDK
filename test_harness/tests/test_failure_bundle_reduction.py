from __future__ import annotations

import json
import hashlib
from pathlib import Path

from export_failure_bundles import (
    build_manifest,
    copy_bundle_files,
    select_topotrack_probe,
    topotrack_probe_by_case,
    verified_reproduction,
)


SIGNATURE = {
    "schema_version": 1,
    "kind": "oracle_failure",
    "returncode": 2,
    "phase": "oracle",
    "exception_code": "",
    "sdk_error_code": None,
    "validation_failures": ["failure"],
    "topology_failures": [],
    "message_signature": "",
}


def stable_replay(recipe: Path | None = None) -> dict[str, object]:
    attempts = [
        {
            "matches_expected": True,
            "failure_signature": SIGNATURE,
            "recipe": str(recipe) if recipe is not None else "",
            "returncode": 2,
        }
        for _ in range(3)
    ]
    return {
        "status": "stable_same_failure",
        "expected_failure_signature": SIGNATURE,
        "attempt_count": 3,
        "attempts": attempts,
    }


def preserved_reduction(tmp_path: Path, reduced: Path) -> dict[str, object]:
    observation = {
        "returncode": 2,
        "timed_out": False,
        "stderr": "",
        "status": {"succeeded": True},
        "validation": {"ok": False, "failures": ["failure"]},
        "topo_check": {},
        "run_state": {"phase": "oracle"},
    }
    summary = {
        "predicate": {"failure_signature": SIGNATURE},
        "baseline_observation": observation,
        "final_observation": observation,
    }
    summary_path = tmp_path / "reduction_summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    return {
        "status": "preserved",
        "ok": True,
        "signature_verified": True,
        "replay_status": "stable_same_failure",
        "stable_attempts": 3,
        "trusted_replay_signature": SIGNATURE,
        "reduced_recipe": str(reduced),
        "reduced_recipe_sha256": hashlib.sha256(reduced.read_bytes()).hexdigest(),
        "summary_path": str(summary_path),
        "reduction_summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
    }


def test_reduced_recipe_becomes_primary_bundle_reproducer(tmp_path: Path) -> None:
    reduced = tmp_path / "source_reduced.json"
    reduced.write_text(json.dumps({"case_id": "reduced", "api": "check_sgt"}), encoding="utf-8")
    bundle = tmp_path / "bundle"

    copied = copy_bundle_files(
        bundle,
        group={"representative_case_id": "case", "representative_case_dir": ""},
        failure={},
        seed={"failure_signature": SIGNATURE},
        replay=stable_replay(reduced),
        reduction=preserved_reduction(tmp_path, reduced),
        topotrack_probe={},
        preview_dirs=[],
        include_full_artifact=False,
    )

    reduced_copy = Path(copied["recipes"]["reduced"])
    reproduce = Path(copied["reproduce_script"])
    assert reduced_copy.is_file()
    assert "reduced_recipe.json" in reproduce.read_text(encoding="utf-8")


def test_unverified_reduction_cannot_replace_the_stable_replay_recipe(tmp_path: Path) -> None:
    replay_recipe = tmp_path / "replay.json"
    replay_recipe.write_text(json.dumps({"case_id": "replay"}), encoding="utf-8")
    wrong_reduced = tmp_path / "wrong_reduced.json"
    wrong_reduced.write_text(json.dumps({"case_id": "wrong"}), encoding="utf-8")
    reduction = preserved_reduction(tmp_path, wrong_reduced)
    reduction["status"] = "not_preserved"
    reduction["ok"] = False

    copied = copy_bundle_files(
        tmp_path / "bundle",
        group={"representative_case_id": "case", "representative_case_dir": ""},
        failure={},
        seed={"failure_signature": SIGNATURE},
        replay=stable_replay(replay_recipe),
        reduction=reduction,
        topotrack_probe={},
        preview_dirs=[],
        include_full_artifact=False,
    )

    assert "reduced" not in copied["recipes"]
    assert Path(copied["recipes"]["replay"]).is_file()
    assert "replay_recipe.json" in Path(copied["reproduce_script"]).read_text(encoding="utf-8")


def test_changed_failure_cannot_create_a_formal_reproducer(tmp_path: Path) -> None:
    recipe = tmp_path / "changed.json"
    recipe.write_text(json.dumps({"case_id": "changed", "api": "check_sgt"}), encoding="utf-8")
    replay = {
        "status": "changed_failure",
        "expected_failure_signature": SIGNATURE,
        "attempts": [
            {
                "matches_expected": False,
                "failure_signature": {**SIGNATURE, "returncode": 1},
                "recipe": str(recipe),
                "returncode": 2,
            }
        ],
    }

    copied = copy_bundle_files(
        tmp_path / "bundle",
        group={"representative_case_id": "case", "representative_case_dir": "", "recipe_paths": [str(recipe)]},
        failure={},
        seed={"failure_signature": SIGNATURE},
        replay=replay,
        reduction={"reduced_recipe": str(recipe)},
        topotrack_probe={},
        preview_dirs=[],
        include_full_artifact=False,
    )

    assert verified_reproduction(replay, SIGNATURE)["eligible"] is False
    assert copied["recipes"] == {}
    assert "reproduce_script" not in copied


def test_replay_match_flag_cannot_override_a_different_signature() -> None:
    replay = {
        "status": "stable_same_failure",
        "expected_failure_signature": SIGNATURE,
        "attempts": [
            {
                "matches_expected": True,
                "failure_signature": {
                    **SIGNATURE,
                    "validation_failures": ["different failure"],
                },
            }
        ],
    }

    facts = verified_reproduction(replay, SIGNATURE)

    assert facts["eligible"] is False
    assert facts["stable_attempts"] == 0


def test_replay_cannot_rebind_the_original_seed_signature() -> None:
    rebound = {
        **SIGNATURE,
        "validation_failures": ["rebound failure"],
    }
    replay = {
        "status": "stable_same_failure",
        "expected_failure_signature": rebound,
        "attempt_count": 3,
        "attempts": [
            {"matches_expected": True, "failure_signature": rebound}
            for _ in range(3)
        ],
    }

    facts = verified_reproduction(replay, SIGNATURE)

    assert facts["eligible"] is False
    assert facts["signature_bound_to_seed"] is False


def test_topotrack_only_success_is_not_root_cause_eligible() -> None:
    replay = stable_replay()

    manifest = build_manifest(
        {"failure_signature": SIGNATURE},
        {},
        {},
        replay,
        {},
        {"classification": "topotrack_only_modeling_ok"},
        {},
    )

    assert manifest["investigation_eligibility"]["root_cause"] is False
    assert manifest["replay"]["stable_attempts"] == 0


def test_isolated_topotrack_probe_is_copied_and_compacted_into_manifest(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    summary = capture / "report/topo_track_summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(json.dumps({"item_count": 4, "ancestor_count": 7}), encoding="utf-8")
    probe = {
        "classification": "topotrack_capture_available_with_failure",
        "evidence_quality": "diagnostic_not_causal_proof",
        "source_returncode": 2,
        "capture_returncode": 2,
        "probe_returncode": 2,
        "capture_artifact_dir": str(capture),
        "capture_topotrack": {
            "available": True,
            "item_count": 4,
            "ancestor_count": 7,
            "resolved_ancestor_count": 6,
        },
    }
    bundle = tmp_path / "bundle"
    copied = copy_bundle_files(
        bundle,
        group={"representative_case_id": "case", "representative_case_dir": ""},
        failure={},
        seed={},
        replay={},
        reduction={},
        topotrack_probe=probe,
        preview_dirs=[],
        include_full_artifact=False,
    )
    manifest = build_manifest({}, {}, {}, {}, {}, probe, copied)

    assert Path(copied["topotrack_probe"]["topo_track_summary.json"]).is_file()
    assert manifest["topotrack_probe"]["classification"] == probe["classification"]
    assert manifest["topotrack_probe"]["capture_topotrack"]["ancestor_count"] == 7


def test_duplicate_case_id_probe_is_bound_to_representative_source(tmp_path: Path) -> None:
    lane_a = tmp_path / "lane_a" / "shared_case"
    lane_b = tmp_path / "lane_b" / "shared_case"
    recipe_a = tmp_path / "lane_a" / "recipe.json"
    recipe_b = tmp_path / "lane_b" / "recipe.json"
    lookup = topotrack_probe_by_case(
        {
            "results": [
                {
                    "case_id": "shared_case",
                    "campaign_lane": "lane_a",
                    "source_artifact_dir": str(lane_a),
                    "source_recipe": str(recipe_a),
                    "classification": "topotrack_only_modeling_ok",
                },
                {
                    "case_id": "shared_case",
                    "campaign_lane": "lane_b",
                    "source_artifact_dir": str(lane_b),
                    "source_recipe": str(recipe_b),
                    "classification": "topotrack_capture_available_with_failure",
                },
            ]
        }
    )

    selected = select_topotrack_probe(
        lookup,
        "shared_case",
        {"case_dir": str(lane_a), "recipe_path": str(recipe_a)},
        {"representative_case_dir": str(lane_a), "recipe_paths": [str(recipe_a)]},
    )

    assert selected["campaign_lane"] == "lane_a"
    assert selected["classification"] == "topotrack_only_modeling_ok"
    assert selected["identity_status"] == "source_bound"


def test_ambiguous_duplicate_case_id_probe_fails_closed() -> None:
    lookup = topotrack_probe_by_case(
        {
            "results": [
                {
                    "case_id": "shared_case",
                    "campaign_lane": "lane_a",
                    "classification": "topotrack_only_modeling_ok",
                },
                {
                    "case_id": "shared_case",
                    "campaign_lane": "lane_b",
                    "classification": "topotrack_capture_available_with_failure",
                },
            ]
        }
    )
    selected = select_topotrack_probe(lookup, "shared_case", {}, {})
    manifest = build_manifest(
        {"representative_case_id": "shared_case"},
        {},
        {"failure_signature": SIGNATURE},
        stable_replay(),
        {},
        selected,
        {},
    )

    assert selected["classification"] == "ambiguous_probe_identity"
    assert selected["identity_candidate_count"] == 2
    assert manifest["investigation_eligibility"]["root_cause"] is False
    assert manifest["replay"]["stable_attempts"] == 0
