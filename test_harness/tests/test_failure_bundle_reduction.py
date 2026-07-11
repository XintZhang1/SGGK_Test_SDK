from __future__ import annotations

import json
from pathlib import Path

from export_failure_bundles import copy_bundle_files


def test_reduced_recipe_becomes_primary_bundle_reproducer(tmp_path: Path) -> None:
    reduced = tmp_path / "source_reduced.json"
    reduced.write_text(json.dumps({"case_id": "reduced", "api": "check_sgt"}), encoding="utf-8")
    bundle = tmp_path / "bundle"

    copied = copy_bundle_files(
        bundle,
        group={"representative_case_id": "case", "representative_case_dir": ""},
        failure={},
        seed={},
        replay={},
        reduction={"reduced_recipe": str(reduced)},
        preview_dirs=[],
        include_full_artifact=False,
    )

    reduced_copy = Path(copied["recipes"]["reduced"])
    reproduce = Path(copied["reproduce_script"])
    assert reduced_copy.is_file()
    assert "reduced_recipe.json" in reproduce.read_text(encoding="utf-8")
