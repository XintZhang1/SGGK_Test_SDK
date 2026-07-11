from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_offset_body_smoke_proves_requested_offset_geometry() -> None:
    recipe = json.loads(
        (REPO_ROOT / "test_harness/interface_example_packs/api_offset_body.example_recipe.json").read_text(
            encoding="utf-8"
        )
    )
    expectations = recipe["expectations"]

    assert recipe["offset_distance"] == 0.05
    assert "total_abs_volume" in expectations
    checks = expectations.get("plane_extreme_checks", [])
    expected_extremes = {item["id"]: item["expected"] for item in checks}
    assert expected_extremes == {
        "offset_x_min": -10.05,
        "offset_x_max": 10.05,
        "offset_z_min": -0.05,
        "offset_z_max": 25.05,
    }
