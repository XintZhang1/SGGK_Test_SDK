from __future__ import annotations

from typing import Any

import pytest

from audit_case_geometry import apply_relative_tolerance_checks, tolerance_family_key


def report_bbox_case(case_id: str, token: str, clearance: float, axis: int = 0) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "api": "api_boolean",
        "boolean_type": "INTERSECTION",
        "dsl_case": "",
        "variant_name": case_id,
        "input_contact": {"signed_clearance": clearance, "signed_axis": axis},
        "tolerance_check": {
            "token": token,
            "mode": "relative_family_pending",
            "expected_signed_clearance": {
                "exact": 0.0,
                "gap_geom": 1e-5,
                "overlap_geom": -1e-5,
                "gap_topo": 1e-2,
                "overlap_topo": -1e-2,
            }[token],
            "actual_signed_clearance": clearance,
            "ok": None,
        },
    }


def test_relative_family_check_cancels_report_bbox_inflation() -> None:
    prefix = "campaign_shape_cylinder_intersection"
    inflated_exact = -1.5e-4
    cases = [
        report_bbox_case(f"{prefix}_exact_a1b2c3d4", "exact", inflated_exact),
        report_bbox_case(f"{prefix}_gap_geom_tol_b1c2d3e4", "gap_geom", inflated_exact + 1e-5),
        report_bbox_case(f"{prefix}_overlap_topo_tol_c1d2e3f4", "overlap_topo", inflated_exact - 1e-2),
    ]

    apply_relative_tolerance_checks(cases, topo_tol=1e-2, geom_tol=1e-5, slack=0.25)

    assert all(case["tolerance_check"]["ok"] is True for case in cases)
    assert cases[1]["tolerance_check"]["actual_offset_from_baseline"] == pytest.approx(1e-5)
    assert cases[2]["tolerance_check"]["actual_offset_from_baseline"] == pytest.approx(-1e-2)


def test_relative_family_check_requires_one_exact_baseline() -> None:
    case = report_bbox_case("campaign_shape_gap_topo_tol_a1b2c3d4", "gap_topo", 1e-2)

    apply_relative_tolerance_checks([case], topo_tol=1e-2, geom_tol=1e-5, slack=0.25)

    assert case["tolerance_check"]["ok"] is None
    assert case["tolerance_check"]["verification_status"] == "unverified"
    assert case["tolerance_check"]["reason"] == "missing_exact_baseline"


def test_tolerance_family_key_only_accepts_terminal_variant() -> None:
    assert tolerance_family_key("shape_gap_geom_tol_1234abcd", "gap_geom") == "shape"
    assert tolerance_family_key("exact_measurement_shape_1234abcd", "exact") is None
