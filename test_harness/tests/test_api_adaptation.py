from __future__ import annotations

import json
import shutil
from pathlib import Path

from api_archetype_mapping import build_intake, map_signature

from test_harness.authoring_gateway.gateway import load_manifest_tasks
from test_harness.orchestration.public_doc_discovery import discover_public_doc_evidence
from test_harness.tests.test_harness_orchestration import make_workflow
from test_harness.tools.api_adaptation_contract import (
    build_adaptation_contract,
    sha256_json,
    validate_adaptation_contract,
)
from test_harness.tools.build_api_test_task import render_api_adaptation_prompt
from test_harness.tools.materialize_api_plugin_candidate import (
    materialize,
)
from test_harness.tools.materialize_api_plugin_candidate import (
    validate_candidate as validate_plugin_candidate,
)
from test_harness.tools.promote_api_plugin import promote


def test_build_task_refreshes_stale_module_capabilities() -> None:
    import build_api_test_task as task_module

    task_module.CAPABILITIES = {"apis": {}}
    task_module.API_GUIDANCE = {}
    try:
        task_module.refresh_capabilities()
        assert "api_combine_bodies" in task_module.SUPPORTED_APIS
        assert (
            task_module.API_GUIDANCE["api_combine_bodies"]["preferred_format"] == "flat_recipe"
        )
    finally:
        task_module.refresh_capabilities()

REPO_ROOT = Path(__file__).resolve().parents[2]

COMBINE_DECLARATION = "SG_DECLSPEC BodyPtr api_combine_bodies(const BodyList& bodies, bool clone = true);"
HOLLOW_DECLARATION = (
    "OFFSET_DECLSPEC OffsetRetPtr api_hollow_body(const BodyPtr& body, double offset, "
    "const OffsetOpts& opts = OffsetOpts());"
)
HOLLOW_SIGNATURE = (
    "OffsetRetPtr api_hollow_body(const BodyPtr& body, double offset, "
    "const OffsetOpts& opts = OffsetOpts())"
)


# --- archetype mapping -----------------------------------------------------


def test_map_signature_body_list_to_body() -> None:
    mapped = map_signature("api_combine_bodies", COMBINE_DECLARATION)

    assert mapped is not None
    assert mapped["adapter_archetype"] == "body_list_to_body"
    assert mapped["function_signature"] == (
        "BodyPtr api_combine_bodies(const BodyList& bodies, bool clone = true)"
    )
    assert mapped["scalar_params"] == [{"name": "clone", "cpp_type": "bool", "has_default": True}]

    intake = build_intake("api_combine_bodies", COMBINE_DECLARATION, "ModelingBase/API.h", "req_1")
    assert intake is not None
    assert intake["sdk_modules"] == ["ModelingBase", "Topology"]
    assert intake["input_roles"] == ["target", "tool"]
    assert intake["required_oracles"] == ["result_bodies", "properties", "topocheck"]
    assert intake["topotrack"]["mode"] == "unavailable"
    contract = build_adaptation_contract(intake)
    assert validate_adaptation_contract(contract, sha256_json(contract)) == []


def test_map_signature_unary_body_to_bodies() -> None:
    mapped = map_signature("api_hollow_body", HOLLOW_DECLARATION)

    assert mapped is not None
    assert mapped["adapter_archetype"] == "unary_body_to_bodies"
    assert mapped["return_type"] == "OffsetRetPtr"
    assert mapped["function_signature"] == HOLLOW_SIGNATURE
    assert mapped["scalar_params"] == [
        {"name": "offset", "cpp_type": "double", "has_default": False}
    ]
    assert mapped["opts_param"] == {"name": "opts", "type": "OffsetOpts"}

    intake = build_intake("api_hollow_body", HOLLOW_DECLARATION, "Offset/API.h", "req_2")
    assert intake is not None
    assert intake["adapter_archetype"] == "unary_body_to_bodies"
    assert intake["sdk_modules"] == ["Offset", "Topology"]
    assert intake["input_roles"] == ["target"]
    assert intake["topotrack"]["mode"] == "status_only"
    contract = build_adaptation_contract(intake)
    assert validate_adaptation_contract(contract, sha256_json(contract)) == []


def test_map_signature_unary_allows_defaulted_scalars_and_strings() -> None:
    declaration = (
        "ChamferRetPtr api_chamfer_body(const BodyPtr& body, double dist = 1.0, "
        "std::string label, const ChamferOpts& opts = ChamferOpts())"
    )
    mapped = map_signature("api_chamfer_body", declaration)

    assert mapped is not None
    assert mapped["scalar_params"] == [
        {"name": "dist", "cpp_type": "double", "has_default": True},
        {"name": "label", "cpp_type": "std::string", "has_default": False},
    ]


def test_map_signature_rejects_non_archetype_shapes() -> None:
    negatives = [
        # Non-body return.
        "int api_count_faces(const BodyPtr& body)",
        # Topology/geometry parameter instead of a body.
        "SectionRetPtr api_section(const FacePtr& face, double z)",
        # Body-list archetype requires defaulted scalars after the list.
        "BodyPtr api_merge(const BodyList& bodies, double gap)",
        # Unary archetype requires a BodyPtr first parameter.
        "OffsetRetPtr api_offset_list(const BodyList& bodies, double offset)",
        # Two Opts parameters cannot be driven by the fixed template.
        "BlendRetPtr api_blend(const BodyPtr& body, const BlendOpts& a = BlendOpts(), "
        "const BlendOpts& b = BlendOpts())",
        # An Opts parameter without a default cannot be omitted safely.
        "BlendRetPtr api_blend2(const BodyPtr& body, const BlendOpts& opts)",
        # Non-scalar parameter type.
        "RetPtr api_sweep(const BodyPtr& body, const CurvePtr& path)",
        # Trailing qualifiers are not a plain free-function declaration.
        "OffsetRetPtr api_hollow_body(const BodyPtr& body) const",
        # Not a function declaration at all.
        "typedef OffsetRetPtr api_hollow_body",
    ]
    for declaration in negatives:
        name = declaration.split("(", 1)[0].split()[-1]
        assert map_signature(name, declaration) is None, declaration

    # Unknown SDK module in the header path rejects the intake.
    assert build_intake("api_hollow_body", HOLLOW_DECLARATION, "Mystery/API.h", "req") is None
    assert build_intake("api_hollow_body", HOLLOW_DECLARATION, "API.h", "req") is None


# --- public doc discovery --------------------------------------------------

DOCS_PAGE = """<html><body><table>
<tr class="memitem:abc123"><td class="memItemLeft"><a class="el" href="x.html">OffsetRetPtr</a></td>
<td class="memItemRight"><a class="el" href="ns.html#abc123">sggk::api_hollow_body</a>
(const BodyPtr &amp;body, double offset)</td></tr>
<tr class="memdesc:abc123"><td class="mdescLeft">&#160;</td>
<td class="mdescRight">Hollow out one solid body with the given offset distance.
<a href="ns.html#abc123">More...</a><br /></td></tr>
</table></body></html>"""

SOURCE_PAGE = """<html><body><div class="fragment">OFFSET_DECLSPEC OffsetRetPtr
api_hollow_body(const BodyPtr&amp; body, double offset, const OffsetOpts&amp; opts);</div></body></html>"""


def test_public_doc_discovery_extracts_bounded_briefs(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "html"
    docs.mkdir(parents=True)
    (docs / "_offset_2_a_p_i_8h.html").write_text(DOCS_PAGE, encoding="utf-8")
    (docs / "_offset_2_a_p_i_8h_source.html").write_text(SOURCE_PAGE, encoding="utf-8")

    evidence = discover_public_doc_evidence("api_hollow_body", docs)

    assert len(evidence) == 1
    record = evidence[0]
    assert record["doc_ref_id"] == "doc_001"
    assert record["page"] == "_offset_2_a_p_i_8h.html"
    assert "Hollow out one solid body" in record["brief"]
    assert len(record["brief"]) <= 2000
    assert len(record["brief_sha256"]) == 64
    # The *_source.html page must never contribute raw header text.
    assert "OFFSET_DECLSPEC" not in json.dumps(evidence)
    assert str(tmp_path) not in json.dumps(evidence)


def test_public_doc_discovery_tolerates_absent_docs(tmp_path: Path) -> None:
    assert discover_public_doc_evidence("api_hollow_body", tmp_path / "missing") == []
    assert discover_public_doc_evidence("api_hollow_body", None) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert discover_public_doc_evidence("api_missing", empty) == []


# --- adaptation prompt -----------------------------------------------------


def _hollow_contract() -> dict:
    intake = build_intake("api_hollow_body", HOLLOW_DECLARATION, "Offset/API.h", "req_prompt")
    assert intake is not None
    return build_adaptation_contract(intake)


def test_api_adaptation_prompt_carries_contract_rules_and_no_paths(tmp_path: Path) -> None:
    contract = _hollow_contract()
    prompt = render_api_adaptation_prompt(contract, [], {"target_api": "api_hollow_body"})

    assert '"api_plugin_candidate"' in prompt
    assert "unary_body_to_bodies" in prompt
    assert "scalar_params" in prompt
    assert "additionalProperties" in prompt
    assert contract["function_signature"] in prompt
    assert contract["function_signature_sha256"] in prompt
    assert contract["intake_sha256"] in prompt
    assert str(REPO_ROOT) not in prompt
    assert str(tmp_path) not in prompt
    # Only the module-relative header may appear.
    assert "Offset/API.h" in prompt


# --- unary materialization -------------------------------------------------


def _unary_candidate() -> dict:
    smoke = {
        "api": "api_hollow_body",
        "case_id": "generated_hollow_body_smoke",
        "hollow_offset": 5.0,
        "modeling_tol": 0.01,
        "check_valid": True,
        "topo_track": False,
        "target_kind": "solid_sphere",
        "target_radius": 10.0,
        "expectations": {
            "result_bodies": {"min": 1},
            "require_property_calculations": True,
            "require_finite_properties": True,
            "require_nonnegative_volume": True,
        },
    }
    negative = json.loads(json.dumps(smoke))
    negative["hollow_offste"] = 5.0
    return {
        "kind": "api_plugin_candidate",
        "api": "api_hollow_body",
        "description": "Adapt Offset api_hollow_body through the fixed unary_body_to_bodies archetype.",
        "adapter_spec": {
            "archetype": "unary_body_to_bodies",
            "function_name": "api_hollow_body",
            "sdk_header": "Offset/API.h",
            "sdk_modules": ["Offset", "Topology"],
            "scalar_params": [
                {
                    "name": "offset",
                    "cpp_type": "double",
                    "recipe_field": "hollow_offset",
                    "default": 5.0,
                }
            ],
        },
        "recipe_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": [
                "api",
                "case_id",
                "hollow_offset",
                "target_kind",
                "target_radius",
                "expectations",
            ],
            "properties": {
                "api": {"const": "api_hollow_body"},
                "case_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"},
                "hollow_offset": {"type": "number", "exclusiveMinimum": 0},
                "modeling_tol": {"type": "number", "exclusiveMinimum": 0},
                "check_valid": {"type": "boolean"},
                "topo_track": {"type": "boolean"},
                "target_kind": {"const": "solid_sphere"},
                "target_radius": {"type": "number", "exclusiveMinimum": 0},
                "target_translate_x": {"type": "number"},
                "target_translate_y": {"type": "number"},
                "target_translate_z": {"type": "number"},
                "expectations": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "result_bodies",
                        "require_property_calculations",
                        "require_finite_properties",
                        "require_nonnegative_volume",
                    ],
                    "properties": {
                        "result_bodies": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["min"],
                            "properties": {
                                "min": {"type": "integer", "minimum": 1},
                            },
                        },
                        "require_property_calculations": {"const": True},
                        "require_finite_properties": {"const": True},
                        "require_nonnegative_volume": {"const": True},
                    },
                },
            },
        },
        "smoke_recipe": smoke,
        "negative_recipe": negative,
        "capability": {
            "preferred_format": "flat_recipe",
            "runner_recipe_api": True,
            "body_required": ["target"],
            "required_fields": [
                "api",
                "case_id",
                "hollow_offset",
                "target_kind",
                "target_radius",
            ],
            "supported_body_builders": ["solid_sphere"],
            "supported_oracles": ["result_bodies", "properties", "topocheck"],
            "notes": ["Generated from a fixed unary_body_to_bodies adapter archetype."],
        },
        "topotrack": {
            "mode": "status_only",
            "reason": "api_hollow_body returns OffsetRetPtr; the fixed adapter records ModelingRet status only",
        },
    }


def test_unary_candidate_materializes_fixed_ret_adapter(tmp_path: Path) -> None:
    value = _unary_candidate()
    contract = _hollow_contract()

    report = materialize(
        value,
        tmp_path,
        expected_contract=contract,
        expected_contract_sha256=sha256_json(contract),
    )

    assert report["ok"], report["errors"]
    plugin = Path(report["materialized_plugin"])
    adapter = (plugin / "adapter.inc").read_text(encoding="utf-8")
    assert "sggk::api_hollow_body(target, offset)" in adapter
    assert "ToBodyVector(ret->ResultBodies())" in adapter
    assert 'FindDouble(json, "hollow_offset", offset);' in adapter
    assert "system(" not in adapter
    assert "#include" not in adapter
    manifest = json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["archetype"] == "unary_body_to_bodies"
    assert manifest["input_roles"] == ["target"]
    assert manifest["topotrack"]["mode"] == "status_only"


def test_unary_scalar_params_are_bound_to_the_trusted_signature() -> None:
    value = _unary_candidate()
    value["adapter_spec"]["scalar_params"][0]["name"] = "gap"
    contract = _hollow_contract()

    errors = validate_plugin_candidate(
        value,
        expected_contract=contract,
        expected_contract_sha256=sha256_json(contract),
    )

    assert any("must match the trusted signature parameters" in error for error in errors)


def test_unary_smoke_oracles_cannot_be_weakened() -> None:
    value = _unary_candidate()
    value["smoke_recipe"]["expectations"]["result_bodies"]["min"] = 0
    value["capability"]["supported_oracles"] = ["result_bodies"]
    value["adapter_spec"]["scalar_params"][0]["recipe_field"] = "target_radius"

    errors = validate_plugin_candidate(value)

    assert any("result_bodies.min must be an integer >= 1" in error for error in errors)
    assert any("missing fixed unary_body_to_bodies host oracles" in error for error in errors)
    assert any("collides with runner fields" in error for error in errors)


def test_unary_recipe_schema_must_lock_oracles() -> None:
    value = _unary_candidate()
    result_schema = value["recipe_schema"]["properties"]["expectations"]["properties"]["result_bodies"]
    result_schema["properties"]["min"] = {"type": "number"}

    errors = validate_plugin_candidate(value)

    assert any("strictly require an integer min with minimum=1" in error for error in errors)


def test_unary_smoke_must_use_flat_runner_body_builder_convention() -> None:
    value = _unary_candidate()
    value["smoke_recipe"].pop("target_kind")
    value["smoke_recipe"]["body"] = {"type": "planar_sheet", "params": {"length": 10.0}}
    value["recipe_schema"]["required"] = [
        item for item in value["recipe_schema"]["required"] if item != "target_kind"
    ]
    del value["recipe_schema"]["properties"]["target_translate_z"]

    errors = validate_plugin_candidate(value)

    assert any("smoke_recipe.target_kind must be one of the fixed runner body builders" in error for error in errors)
    assert any("recipe_schema.required must contain target_kind" in error for error in errors)
    assert any("target_translate_z" in error for error in errors)


def test_body_list_archetype_still_forbids_scalar_params() -> None:
    value = json.loads(
        (REPO_ROOT / "test_harness/api_plugin_candidates/api_combine_bodies.example.json").read_text(
            encoding="utf-8"
        )
    )
    value["adapter_spec"]["scalar_params"] = []

    errors = validate_plugin_candidate(value)

    assert errors


# --- workflow wiring -------------------------------------------------------


def _fake_offset_sdk(root: Path, declarations: str) -> Path:
    sdk = root / "sdk"
    header = sdk / "include" / "Offset" / "API.h"
    header.parent.mkdir(parents=True)
    header.write_text(f"namespace sggk {{\n{declarations}\n}}\n", encoding="utf-8")
    return sdk


def _round_task(tmp_path: Path, pattern: str) -> tuple[dict, Path]:
    session_root = next((tmp_path / "artifacts/harness_sessions").glob(pattern))
    manifest_path = session_root / "rounds/0001/prompt/model_task_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest["tasks"][0], manifest_path


def test_extension_backlog_mappable_signature_emits_api_adaptation(tmp_path: Path) -> None:
    sdk = _fake_offset_sdk(tmp_path, f"    {HOLLOW_DECLARATION}")
    workflow, _runtime = make_workflow(tmp_path)
    workflow.sdk_dir = sdk.resolve()

    started = workflow.start("api_hollow_body")

    assert started["state"] == "awaiting_comment"
    task, manifest_path = _round_task(tmp_path, "*_api_hollow_body_*")
    assert task["task_type"] == "api_adaptation"
    assert task["interface_family"] == "api_adaptation"
    assert task["output_contract"]["allowed_kinds"] == ["api_plugin_candidate"]
    contract = task["adaptation_contract"]
    assert task["target_api"] == "api_hollow_body"
    assert task["adapter_archetype"] == "unary_body_to_bodies"
    assert task["intake_sha256"] == contract["intake_sha256"]
    assert len(task["adaptation_contract_sha256"]) == 64
    # The manifest metadata is exactly what the fixed plugin gate requires.
    assert validate_adaptation_contract(contract, task["adaptation_contract_sha256"]) == []
    specs = load_manifest_tasks(manifest_path, tmp_path)
    assert specs[0].task_type == "api_adaptation"
    assert specs[0].metadata["adaptation_contract"] == contract
    assert specs[0].metadata["adaptation_contract_sha256"] == task["adaptation_contract_sha256"]
    prompt = (tmp_path / task["prompt_path"]).read_text(encoding="utf-8")
    assert "api_plugin_candidate" in prompt
    assert contract["function_signature"] in prompt
    assert str(sdk) not in prompt
    assert "OFFSET_DECLSPEC" not in prompt


def test_extension_backlog_same_archetype_overloads_pick_first_declaration(tmp_path: Path) -> None:
    declarations = (
        "    OFFSET_DECLSPEC ThickenRetPtr api_bulk_thicken(const BodyPtr& body, double minDist, "
        "double maxDist, const ThickenOpts& opts = ThickenOpts());\n"
        "    OFFSET_DECLSPEC ThickenRetPtr api_bulk_thicken(const BodyPtr& body, double thickness, "
        "bool both, const ThickenOpts& opts = ThickenOpts());"
    )
    sdk = _fake_offset_sdk(tmp_path, declarations)
    workflow, _runtime = make_workflow(tmp_path)
    workflow.sdk_dir = sdk.resolve()

    workflow.start("api_bulk_thicken")

    task, _manifest_path = _round_task(tmp_path, "*_api_bulk_thicken_*")
    assert task["task_type"] == "api_adaptation"
    contract = task["adaptation_contract"]
    assert task["adapter_archetype"] == "unary_body_to_bodies"
    assert "minDist" in contract["function_signature"]
    assert "thickness, " not in contract["function_signature"]


def test_extension_backlog_cross_archetype_overloads_keep_design_route(tmp_path: Path) -> None:
    declarations = (
        "    OFFSET_DECLSPEC ThickenRetPtr api_mixed_body(const BodyPtr& body, "
        "const ThickenOpts& opts = ThickenOpts());\n"
        "    SG_DECLSPEC BodyPtr api_mixed_body(const BodyList& bodies, bool clone = true);"
    )
    sdk = _fake_offset_sdk(tmp_path, declarations)
    workflow, _runtime = make_workflow(tmp_path)
    workflow.sdk_dir = sdk.resolve()

    workflow.start("api_mixed_body")

    task, _manifest_path = _round_task(tmp_path, "*_api_mixed_body_*")
    assert task["task_type"] == "interface_dsl_design"
    assert task["output_contract"]["allowed_kinds"] == ["needs_harness_extension"]
    assert "adaptation_contract" not in task


def test_external_profile_maps_archetype_from_host_local_header_read(tmp_path: Path) -> None:
    sdk = _fake_offset_sdk(tmp_path, f"    {HOLLOW_DECLARATION}")
    docs = tmp_path / "docs" / "html"
    docs.mkdir(parents=True)
    (docs / "_offset_2_a_p_i_8h.html").write_text(DOCS_PAGE, encoding="utf-8")
    (docs / "_offset_2_a_p_i_8h_source.html").write_text(SOURCE_PAGE, encoding="utf-8")
    workflow, _runtime = make_workflow(tmp_path, profile="siliconflow")
    workflow.sdk_dir = sdk.resolve()

    workflow.start("api_hollow_body")

    task, _manifest_path = _round_task(tmp_path, "*_api_hollow_body_*")
    assert task["task_type"] == "api_adaptation"
    assert task["data_classification"] == "public_interface"
    assert task["allowed_profile_categories"] == ["external"]
    prompt = (tmp_path / task["prompt_path"]).read_text(encoding="utf-8")
    assert "doc_001" in prompt
    assert "Hollow out one solid body" in prompt
    assert "OFFSET_DECLSPEC" not in prompt
    assert str(sdk) not in prompt


def test_api_adaptation_runtime_options_share_long_generation_lane(tmp_path: Path) -> None:
    from test_harness.authoring_gateway.config import PROFILE_SPECS, GatewayConfig
    from test_harness.orchestration.runtime import (
        INTERFACE_DESIGN_MAX_TOKENS,
        INTERFACE_DESIGN_TIMEOUT_SECONDS,
        LONG_GENERATION_TASK_TYPES,
        MessageApiRuntime,
    )

    assert {"interface_dsl_design", "api_adaptation"} <= set(LONG_GENERATION_TASK_TYPES)
    config = GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="zai-org/GLM-5.2",
        api_key="test-key",
        max_retries=0,
    )
    runtime = MessageApiRuntime(
        repo_root=tmp_path,
        profile="intranet",
        config=config,
        candidate_count=3,
        candidate_parallelism=3,
    )

    options = runtime._authoring_options("api_adaptation")
    assert options.thinking_mode == "enabled"
    assert options.max_tokens == INTERFACE_DESIGN_MAX_TOKENS
    assert options.request_timeout_seconds == INTERFACE_DESIGN_TIMEOUT_SECONDS

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": [{"task_type": "api_adaptation"}]}), encoding="utf-8")
    assert runtime._manifest_task_type(manifest) == "api_adaptation"


def test_dsl_heavy_interface_form_joins_long_generation_lane(tmp_path: Path) -> None:
    from test_harness.authoring_gateway.config import PROFILE_SPECS, GatewayConfig
    from test_harness.orchestration.runtime import (
        INTERFACE_DESIGN_MAX_TOKENS,
        INTERFACE_DESIGN_TIMEOUT_SECONDS,
        MessageApiRuntime,
    )

    config = GatewayConfig(
        profile=PROFILE_SPECS["intranet"],
        base_url="https://message-api.invalid/v1",
        model="zai-org/GLM-5.2",
        api_key="test-key",
        max_retries=0,
    )
    runtime = MessageApiRuntime(
        repo_root=tmp_path,
        profile="intranet",
        config=config,
        candidate_count=3,
        candidate_parallelism=3,
    )

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_type": "interface_form",
                        "output_contract": {"allowed_kinds": ["attack_dsl", "needs_harness_extension"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert runtime._manifest_allowed_kinds(manifest) == ["attack_dsl", "needs_harness_extension"]
    options = runtime._authoring_options("interface_form", dsl_heavy=True)
    assert options.thinking_mode == "enabled"
    assert options.max_tokens == INTERFACE_DESIGN_MAX_TOKENS
    assert options.request_timeout_seconds == INTERFACE_DESIGN_TIMEOUT_SECONDS

    flat_manifest = tmp_path / "flat.json"
    flat_manifest.write_text(
        json.dumps({"tasks": [{"task_type": "interface_form", "output_contract": {"allowed_kinds": ["flat_recipe"]}}]}),
        encoding="utf-8",
    )
    assert "attack_dsl" not in runtime._manifest_allowed_kinds(flat_manifest)
    short_options = runtime._authoring_options("interface_form")
    assert short_options.thinking_mode != "enabled"


# --- promotion -------------------------------------------------------------

BUILTIN_RUNNER_APIS = (
    "api_boolean",
    "api_boolean_slice",
    "api_boolean_split",
    "api_offset2d",
    "api_offset_body",
    "api_topology_section",
    "check_sgt",
    "step_import",
    "iges_import",
    "step_roundtrip",
    "iges_roundtrip",
)


def _stage_promotion_repo(repo: Path) -> None:
    harness = repo / "test_harness"
    (harness / "api_plugins").mkdir(parents=True)
    (harness / "src").mkdir(parents=True)
    (harness / "recipes").mkdir(parents=True)
    (harness / "dsl").mkdir(parents=True)
    # The staged world intentionally implements only the built-ins; drop any
    # plugin APIs the real registry gained after this fixture was written.
    staged_capabilities = json.loads(
        (REPO_ROOT / "test_harness/interface_capabilities.json").read_text(encoding="utf-8")
    )
    staged_apis = staged_capabilities.get("apis")
    if isinstance(staged_apis, dict):
        keep = set(BUILTIN_RUNNER_APIS) | {"needs_harness_extension"}
        for api_name in [name for name in staged_apis if name not in keep]:
            staged_apis.pop(api_name, None)
    (harness / "interface_capabilities.json").write_text(
        json.dumps(staged_capabilities, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    shutil.copytree(
        REPO_ROOT / "test_harness/interface_example_packs",
        harness / "interface_example_packs",
    )
    for name in (
        "boolean_split_plane_smoke.json",
        "boolean_slice_smoke.json",
        "offset2d_line_smoke.json",
        "offset2d_cannot_connect_smoke.json",
        "offset2d_crv_degenerate_smoke.json",
        "topology_section_spheres_smoke.json",
    ):
        shutil.copy(REPO_ROOT / "test_harness/recipes" / name, harness / "recipes" / name)
    shutil.copy(
        REPO_ROOT / "test_harness/dsl/oracle_checks_smoke.json",
        harness / "dsl/oracle_checks_smoke.json",
    )
    (harness / "src/sggk_case_runner.cpp").write_text(
        "\n".join(f'recipe.api == "{api}"' for api in BUILTIN_RUNNER_APIS) + "\n",
        encoding="utf-8",
    )


def _write_attested_build(repo: Path, *, ok: bool = True) -> Path:
    materialized_root = repo / "artifacts/gate/materialized"
    report = materialize(_unary_candidate(), materialized_root)
    assert report["ok"]
    plugin_source = Path(report["materialized_plugin"])
    build_root = repo / "artifacts/execution/plugin_build"
    build_root.mkdir(parents=True, exist_ok=True)
    commands = [
        {"name": name, "argv": [], "returncode": 0, "ok": True}
        for name in (
            "cmake_configure",
            "cmake_build",
            "validate_positive_recipe",
            "validate_negative_recipe",
            "smoke_replay_01",
            "smoke_replay_02",
            "smoke_replay_03",
        )
    ]
    commands.append(
        {
            "name": "list_adapters",
            "argv": [],
            "returncode": 0,
            "ok": True,
            "adapter": {"api": "api_hollow_body", "source": "plugin"},
        }
    )
    payload = {
        "schema_version": 1,
        "ok": ok,
        "api": "api_hollow_body",
        "candidate_plugin": str(plugin_source),
        "sdk_identity": {"algorithm": "test", "files": [], "sha256": "a" * 64},
        "runner_sha256": "b" * 64,
        "runtime_registry_sha256": "c" * 64,
        "smoke_replays": 3,
        "stable_semantic_evidence": ok,
        "semantic_hashes": ["d" * 64, "d" * 64, "d" * 64],
        "commands": commands,
    }
    report_path = build_root / "plugin_build_report.json"
    report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return report_path


def _capabilities(repo: Path) -> dict:
    return json.loads((repo / "test_harness/interface_capabilities.json").read_text(encoding="utf-8"))


def test_promotion_registers_attested_plugin(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _stage_promotion_repo(repo)
    report_path = _write_attested_build(repo)

    outcome = promote(report_path, repo)

    assert outcome["ok"], outcome["errors"]
    assert outcome["api"] == "api_hollow_body"
    plugin_dir = repo / "test_harness/api_plugins/api_hollow_body"
    assert (plugin_dir / "plugin.json").is_file()
    assert (plugin_dir / "adapter.inc").is_file()
    apis = _capabilities(repo)["apis"]
    assert apis["api_hollow_body"]["runner_recipe_api"] is True
    assert apis["api_hollow_body"]["supported_oracles"] == [
        "result_bodies",
        "properties",
        "topocheck",
    ]

    # Re-promotion without --replace fails closed and leaves the tree untouched.
    before = (repo / "test_harness/interface_capabilities.json").read_bytes()
    again = promote(report_path, repo)
    assert not again["ok"]
    assert any("--replace" in error for error in again["errors"])
    assert (repo / "test_harness/interface_capabilities.json").read_bytes() == before

    replaced = promote(report_path, repo, replace=True)
    assert replaced["ok"], replaced["errors"]
    assert replaced["replaced_existing"] is True


def test_promotion_rejects_unattested_or_conflicting_builds(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _stage_promotion_repo(repo)
    report_path = _write_attested_build(repo, ok=False)

    outcome = promote(report_path, repo)

    assert not outcome["ok"]
    assert not (repo / "test_harness/api_plugins/api_hollow_body").exists()

    report_path = _write_attested_build(repo)
    capabilities = _capabilities(repo)
    capabilities["apis"]["api_hollow_body"] = {
        "preferred_format": "flat_recipe",
        "runner_recipe_api": True,
        "required_fields": ["api", "case_id"],
        "supported_oracles": ["topocheck"],
    }
    (repo / "test_harness/interface_capabilities.json").write_text(
        json.dumps(capabilities, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    before = (repo / "test_harness/interface_capabilities.json").read_bytes()

    conflicted = promote(report_path, repo)

    assert not conflicted["ok"]
    assert any("conflicting registration" in error for error in conflicted["errors"])
    assert not (repo / "test_harness/api_plugins/api_hollow_body").exists()
    assert (repo / "test_harness/interface_capabilities.json").read_bytes() == before
