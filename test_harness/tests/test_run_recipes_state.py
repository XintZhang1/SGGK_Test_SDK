from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import run_recipes


def test_windows_extended_path_supports_drive_unc_and_existing_namespaces() -> None:
    drive = r"C:\deep\recipe.json"
    unc = r"\\server\share\deep\recipe.json"
    extended = r"\\?\C:\deep\recipe.json"
    device = r"\\.\pipe\sggk"

    assert run_recipes._windows_extended_path(drive) == r"\\?\C:\deep\recipe.json"
    assert run_recipes._windows_extended_path(unc) == r"\\?\UNC\server\share\deep\recipe.json"
    assert run_recipes._windows_extended_path(extended) == extended
    assert run_recipes._windows_extended_path(device) == device
    assert run_recipes._windows_extended_path("relative/recipe.json") == "relative/recipe.json"
    assert run_recipes.display_path(r"\\?\C:\deep\recipe.json") == drive
    assert run_recipes.display_path(r"\\?\UNC\server\share\deep\recipe.json") == unc


def write_recipe(path: Path, case_id: str) -> None:
    path.write_text(json.dumps({"case_id": case_id, "api": "check_sgt"}), encoding="utf-8")


def test_recipe_list_order_is_preserved(tmp_path: Path) -> None:
    first = tmp_path / "z.json"
    second = tmp_path / "a.json"
    write_recipe(first, "first")
    write_recipe(second, "second")
    manifest = tmp_path / "suite.txt"
    manifest.write_text("z.json\na.json\n", encoding="utf-8")

    assert run_recipes.iter_recipe_files([], [str(manifest)]) == [first.resolve(), second.resolve()]


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    write_recipe(first, "duplicate")
    write_recipe(second, "duplicate")

    assert run_recipes.validate_unique_case_ids([first, second]) == 1


def test_launcher_freezes_recipe_and_writes_run_state_before_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"runner")
    recipe = tmp_path / "recipe.json"
    write_recipe(recipe, "state_case")
    out = tmp_path / "out"

    def fake_run(command, **kwargs):
        state_path = out / "state_case" / "run_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["phase"] == "launching"
        assert (out / "state_case" / "input" / "recipe.json").is_file()
        expected_recipe = run_recipes.native_path_argument(recipe)
        expected_out = run_recipes.native_path_argument(out)
        assert command[command.index("--recipe") + 1] == expected_recipe
        assert command[command.index("--out") + 1] == expected_out
        if os.name == "nt":
            assert expected_recipe.startswith("\\\\?\\")
            assert expected_out.startswith("\\\\?\\")
        return SimpleNamespace(returncode=0, stdout="case_id=state_case\n", stderr="")

    monkeypatch.setattr(run_recipes.subprocess, "run", fake_run)

    result = run_recipes.run_one(
        runner,
        recipe,
        out,
        timeout=1.0,
        sdk_threads=2,
        capture_flat_topotrack=True,
    )
    state = json.loads((out / "state_case" / "run_state.json").read_text(encoding="utf-8"))

    assert result["returncode"] == 0
    assert result["command"][-1] == "--capture-flat-topotrack"
    assert state["phase"] == "completed"
    assert state["recipe_sha1"] == run_recipes.file_sha1(recipe)
    assert state["runner_sha1"] == run_recipes.file_sha1(runner)


def test_execution_phase_is_inferred_from_monotonic_artifacts(tmp_path: Path) -> None:
    case_dir = tmp_path / "phase_case"
    (case_dir / "input").mkdir(parents=True)
    (case_dir / "input" / "recipe.json").write_text("{}", encoding="utf-8")
    assert run_recipes.infer_execution_phase(case_dir)[0] == "parse"

    (case_dir / "manifest.json").write_text("{}", encoding="utf-8")
    assert run_recipes.infer_execution_phase(case_dir)[0] == "build_inputs"

    (case_dir / "input" / "target.sgt").write_bytes(b"sgt")
    assert run_recipes.infer_execution_phase(case_dir)[0] == "invoke_api"

    (case_dir / "report").mkdir()
    (case_dir / "report" / "status.json").write_text("{}", encoding="utf-8")
    assert run_recipes.infer_execution_phase(case_dir)[0] == "serialize_result"

    (case_dir / "report" / "topo_check.json").write_text("{}", encoding="utf-8")
    assert run_recipes.infer_execution_phase(case_dir)[0] == "topocheck"

    (case_dir / "report" / "validation.json").write_text("{}", encoding="utf-8")
    assert run_recipes.infer_execution_phase(case_dir)[0] == "oracle"
