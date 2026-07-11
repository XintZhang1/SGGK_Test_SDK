from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from validate_recipe import validate_file


REPO_ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = REPO_ROOT / "test_harness" / "suites" / "api_smoke_suite.txt"


def suite_recipe_paths() -> list[Path]:
    paths: list[Path] = []
    for raw in SUITE_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        paths.append((SUITE_PATH.parent / line).resolve())
    return paths


def test_api_smoke_suite_has_no_ignored_artifact_input_dependency() -> None:
    source_recipes = 0
    for recipe_path in suite_recipe_paths():
        recipe = json.loads(recipe_path.read_text(encoding="utf-8-sig"))
        source_file = recipe.get("source_file")
        if not isinstance(source_file, str) or not source_file:
            continue
        source_recipes += 1
        portable = PurePosixPath(source_file.replace("\\", "/"))
        source_path = (REPO_ROOT / portable).resolve()

        assert not portable.parts or portable.parts[0] != "artifacts"
        assert source_path.is_relative_to(REPO_ROOT.resolve())
        assert source_path.is_file(), f"missing durable smoke fixture: {source_file}"

    assert source_recipes == 3


def test_api_smoke_suite_recipes_pass_strict_asset_validation() -> None:
    failures = {
        str(path): validate_file(path, check_assets=True)
        for path in suite_recipe_paths()
        if validate_file(path, check_assets=True)
    }

    assert failures == {}
