from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator


TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from run_recipes import write_recipe_review_index  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_recipe(path: Path, case_id: str, api: str) -> None:
    path.write_text(
        json.dumps(
            {
                "case_id": case_id,
                "api": api,
                "source_file": "test_harness/fixtures/bug_records/freecad_e67361f3777637c2/target.sgt",
                "expectations": {
                    "result_bodies": {"min": 1},
                    "require_finite_properties": True,
                },
                "hypothesis": "复杂导入拓扑在容差边界附近仍应产生有限且可验证的结果。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_recipe_review_index_is_hash_bound_and_comment_managed(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_recipe(first, "complex_case_001", "check_sgt")
    _write_recipe(second, "complex_case_002", "step_roundtrip")

    review = write_recipe_review_index(tmp_path / "run", [first, second])
    index_path = Path(review["index_path"])
    rows = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()]

    assert review["record_count"] == 2
    assert [row["case_id"] for row in rows] == ["complex_case_001", "complex_case_002"]
    assert all(
        row["review_workflow"]["status"] == "awaiting_natural_language_comment"
        for row in rows
    )
    assert rows[0]["recipe_sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert rows[1]["recipe_sha256"] == hashlib.sha256(second.read_bytes()).hexdigest()
    assert review["index_sha256"] == hashlib.sha256(index_path.read_bytes()).hexdigest()
    report = Path(review["report_path"]).read_text(encoding="utf-8")
    assert "机器校验通过不会自动触发 SDK 执行" in report
    assert "用户不需要编辑任何审批 JSON" in report
    assert Path(review["review_state_path"]).name == "recipe_review_state.internal.json"
    assert "decision_template_path" not in review
    assert "复杂_case" not in report
    assert "complex_case_001" in report


def test_all_checked_in_interface_forms_match_the_authoritative_schema() -> None:
    forms_root = REPO_ROOT / "test_harness/forms/interface_distillation"
    schema = json.loads(
        (REPO_ROOT / "test_harness/forms/api_test_form.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for path in sorted(forms_root.glob("*.json")):
        if path.name == "00_manifest.json":
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        for error in validator.iter_errors(value):
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            errors.append(f"{path.name}:{location}: {error.message}")
    assert not errors
