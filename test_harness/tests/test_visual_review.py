from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from test_harness.authoring_gateway.client import (
    ClientError,
    CompletionOptions,
    HttpResponse,
    OpenAICompatibleMessageClient,
    prepare_images,
)
from test_harness.authoring_gateway.config import (
    PROFILE_SPECS,
    SILICONFLOW_DEFAULT_BASE_URL,
    SILICONFLOW_DEFAULT_MODEL,
    SILICONFLOW_VISION_DEFAULT_MODEL,
    ConfigError,
    load_gateway_config,
)
from test_harness.authoring_gateway.contracts import (
    normalize_visual_review_candidate,
    validate_candidate,
)
from test_harness.authoring_gateway.gateway import AuthoringGateway, GatewayError, TaskSpec
from test_harness.orchestration import workflow as workflow_module
from test_harness.orchestration.workflow import HarnessWorkflow
from test_harness.tools import run_visual_review
from test_harness.ui.state import execution_overview

API_KEY = "test-api-key-never-persist"
VISION_PROFILE = "siliconflow_vision"


class QueueTransport:
    def __init__(self, *items: HttpResponse | Exception) -> None:
        self.items = list(items)
        self.requests: list[dict[str, Any]] = []

    def post(self, **kwargs: Any) -> HttpResponse:
        self.requests.append(dict(kwargs))
        if not self.items:
            raise AssertionError("mock transport queue is empty")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def provider_response(content: str, *, status: int = 200) -> HttpResponse:
    payload = {
        "id": "mock-completion",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    return HttpResponse(status, {"content-type": "application/json"}, json.dumps(payload).encode())


def make_png(path: Path, *, size: tuple[int, int] = (64, 48), color: tuple[int, int, int] = (200, 30, 30)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def visual_candidate(case_id: str = "case_a") -> dict[str, Any]:
    return {
        "kind": "visual_review_report",
        "schema_version": 1,
        "case_reviews": [
            {
                "case_id": case_id,
                "geometry_plausibility": "plausible",
                "view_consistency": "consistent",
                "misuse_flags": [],
                "confidence": 0.9,
                "notes_zh_cn": "几何关系正常。",
            }
        ],
        "overall_notes_zh_cn": "总体未见明显异常。",
    }


def visual_contract() -> dict[str, Any]:
    return {"type": "json_object", "kind_field": "kind", "allowed_kinds": ["visual_review_report"]}


def vision_config() -> Any:
    return load_gateway_config(VISION_PROFILE, environ={"SILICONFLOW_API_KEY": API_KEY})


def make_vision_gateway(repo: Path, transport: QueueTransport) -> AuthoringGateway:
    config = vision_config()
    client = OpenAICompatibleMessageClient(
        config,
        transport=transport,
        sleeper=lambda _delay: None,
        random_source=lambda: 0.0,
    )
    return AuthoringGateway(config, repo_root=repo, client=client)


def visual_task(
    repo: Path,
    image_paths: tuple[str, ...],
    *,
    output: str = "artifacts/visual_review/out.json",
) -> TaskSpec:
    return TaskSpec(
        task_id="visual_review",
        task_type="visual_review",
        prompt="Judge the attached geometry previews.",
        expected_output_path=repo / output,
        output_contract=visual_contract(),
        image_paths=image_paths,
        metadata={
            "provider_profile": VISION_PROFILE,
            "provider_profile_category": "external",
            "data_classification": "public_interface",
            "allowed_profile_categories": ["external"],
        },
    )


def make_case_dir(cases_root: Path, name: str, *, with_preview: bool = True) -> Path:
    case_dir = cases_root / name
    (case_dir / "report").mkdir(parents=True, exist_ok=True)
    (case_dir / "manifest.json").write_text(json.dumps({"case_id": name}), encoding="utf-8")
    if with_preview:
        make_png(case_dir / "report" / "preview.png")
    return case_dir


# ---------------------------------------------------------------------------
# Vision profile lock
# ---------------------------------------------------------------------------


def test_vision_profile_loads_locked_and_shares_siliconflow_key() -> None:
    config = load_gateway_config(VISION_PROFILE, environ={"SILICONFLOW_API_KEY": API_KEY})

    assert config.profile is PROFILE_SPECS[VISION_PROFILE]
    assert config.profile.category == "external"
    assert config.base_url == SILICONFLOW_DEFAULT_BASE_URL
    assert config.endpoint_url == f"{SILICONFLOW_DEFAULT_BASE_URL}/chat/completions"
    assert config.model == SILICONFLOW_VISION_DEFAULT_MODEL
    assert config.profile.default_thinking_mode == "omit"
    assert config.profile.default_stream is True
    assert config.profile.provenance_source_type == "siliconflow_vision_message_api"
    assert config.api_key == API_KEY
    assert API_KEY not in repr(config)
    assert API_KEY not in json.dumps(config.public_metadata())


def test_vision_profile_fails_closed_and_rejects_unlocked_values() -> None:
    with pytest.raises(ConfigError, match="SILICONFLOW_API_KEY"):
        load_gateway_config(VISION_PROFILE, environ={})

    with pytest.raises(ConfigError, match="must use https"):
        load_gateway_config(
            VISION_PROFILE,
            environ={
                "SILICONFLOW_API_KEY": API_KEY,
                "SILICONFLOW_VISION_BASE_URL": "http://api.siliconflow.cn/v1",
            },
        )

    with pytest.raises(ConfigError, match="api.siliconflow.cn"):
        load_gateway_config(
            VISION_PROFILE,
            environ={
                "SILICONFLOW_API_KEY": API_KEY,
                "SILICONFLOW_VISION_BASE_URL": "https://attacker.invalid/v1",
            },
        )

    with pytest.raises(ConfigError, match="Qwen/Qwen3-VL-32B-Instruct"):
        load_gateway_config(
            VISION_PROFILE,
            environ={
                "SILICONFLOW_API_KEY": API_KEY,
                "SILICONFLOW_VISION_MODEL": "another/model",
            },
        )


def test_authoring_profile_lock_is_unchanged_by_vision_profile() -> None:
    assert PROFILE_SPECS["siliconflow"].default_model == SILICONFLOW_DEFAULT_MODEL
    assert PROFILE_SPECS["siliconflow"].model_locked is True
    assert PROFILE_SPECS["siliconflow"].base_url_locked is True
    config = load_gateway_config("siliconflow", environ={"SILICONFLOW_API_KEY": API_KEY})
    assert config.model == "zai-org/GLM-5.2"
    assert config.profile.default_thinking_mode == "disabled"
    with pytest.raises(ConfigError, match="zai-org/GLM-5.2"):
        load_gateway_config(
            "siliconflow",
            environ={"SILICONFLOW_API_KEY": API_KEY, "SILICONFLOW_MODEL": "Qwen/Qwen3-VL-32B-Instruct"},
        )


# ---------------------------------------------------------------------------
# prepare_images budgets
# ---------------------------------------------------------------------------


def test_prepare_images_downscales_and_binds_source_hash(tmp_path: Path) -> None:
    source = make_png(tmp_path / "big.png", size=(3000, 2000))
    source_bytes = source.read_bytes()

    prepared = prepare_images([source])

    assert len(prepared) == 1
    image = prepared[0]
    assert image.sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert image.mime == "image/png"
    assert image.source_bytes == len(source_bytes)
    assert image.prepared_bytes == len(image.data)
    assert image.prepared_bytes <= 2 * 1024 * 1024
    from PIL import Image

    with Image.open(io.BytesIO(image.data)) as decoded:
        assert max(decoded.size) <= 1600
        assert decoded.format == "PNG"


def test_prepare_images_enforces_count_cap(tmp_path: Path) -> None:
    one = make_png(tmp_path / "a.png")
    two = make_png(tmp_path / "b.png")

    with pytest.raises(ClientError, match="too many images"):
        prepare_images([one, two], max_images=1)


def test_prepare_images_enforces_per_image_budget(tmp_path: Path) -> None:
    source = make_png(tmp_path / "a.png")

    with pytest.raises(ClientError, match="exceeds 10 bytes"):
        prepare_images([source], max_image_bytes=10)


def test_prepare_images_enforces_total_budget(tmp_path: Path) -> None:
    one = make_png(tmp_path / "a.png", size=(400, 300))
    two = make_png(tmp_path / "b.png", size=(400, 300), color=(30, 30, 200))

    with pytest.raises(ClientError, match="total budget"):
        prepare_images([one, two], max_total_bytes=100)


def test_prepare_images_rejects_non_image_suffix(tmp_path: Path) -> None:
    text = tmp_path / "notes.txt"
    text.write_text("not an image", encoding="utf-8")

    with pytest.raises(ClientError, match=".png/.jpg"):
        prepare_images([text])


def test_prepare_images_rejects_relative_paths(tmp_path: Path) -> None:
    make_png(tmp_path / "a.png")

    with pytest.raises(ClientError, match="absolute"):
        prepare_images([Path("a.png")])


def test_prepare_images_rejects_undecodable_bytes(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.png"
    bogus.write_bytes(b"this is not a png")

    with pytest.raises(ClientError, match="cannot be decoded"):
        prepare_images([bogus])


def test_prepare_images_empty_input() -> None:
    assert prepare_images([]) == []


# ---------------------------------------------------------------------------
# Multimodal payload
# ---------------------------------------------------------------------------


def test_payload_with_images_uses_ordered_content_parts(tmp_path: Path) -> None:
    source = make_png(tmp_path / "preview.png")
    images = tuple(prepare_images([source]))
    transport = QueueTransport(provider_response('{"ok":true}'))
    client = OpenAICompatibleMessageClient(vision_config(), transport=transport)

    result = client.create_completion(
        system_prompt="Return JSON only.",
        user_prompt="Judge this preview.",
        options=CompletionOptions(response_mode="none", max_tokens=64, images=images),
    )

    assert result.ok
    assert result.image_count == 1
    assert result.image_sha256 == [images[0].sha256]
    payload = json.loads(transport.requests[0]["body"])
    assert payload["model"] == SILICONFLOW_VISION_DEFAULT_MODEL
    messages = payload["messages"]
    assert messages[0] == {"role": "system", "content": "Return JSON only."}
    content = messages[1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Judge this preview."}
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    import base64

    assert base64.b64decode(url.split(",", 1)[1]) == images[0].data


def test_text_only_payload_is_byte_identical_to_before() -> None:
    transport = QueueTransport(provider_response('{"ok":true}'))
    client = OpenAICompatibleMessageClient(vision_config(), transport=transport)

    result = client.create_completion(
        system_prompt="Return JSON only.",
        user_prompt="Return one small JSON object.",
        options=CompletionOptions(response_mode="none", temperature=0.0, max_tokens=64),
    )

    assert result.ok
    assert result.image_count == 0
    assert result.image_sha256 == []
    payload = json.loads(transport.requests[0]["body"])
    assert payload == {
        "model": SILICONFLOW_VISION_DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": "Return one small JSON object."},
        ],
        "temperature": 0.0,
        "max_tokens": 64,
    }


# ---------------------------------------------------------------------------
# Gateway image plumbing and provenance
# ---------------------------------------------------------------------------


def test_gateway_binds_image_hashes_into_manifest_and_provenance(tmp_path: Path) -> None:
    source = make_png(tmp_path / "artifacts" / "cases" / "case_a" / "report" / "preview.png")
    expected_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    transport = QueueTransport(provider_response(json.dumps(visual_candidate("case_a"))))
    gateway = make_vision_gateway(tmp_path, transport)

    result = gateway.run_task(
        visual_task(tmp_path, (str(source),)),
        run_id="vision_run",
        max_repairs=0,
    )

    assert result.ok, result.error
    attempt = tmp_path / "artifacts/authoring_gateway/vision_run/visual_review/attempt_01"
    manifest = json.loads((attempt / "request_manifest.json").read_text(encoding="utf-8"))
    assert manifest["images"]["count"] == 1
    item = manifest["images"]["items"][0]
    assert item["sha256"] == expected_sha
    assert item["mime"] == "image/png"
    assert item["source_bytes"] == source.stat().st_size
    assert item["prepared_bytes"] > 0
    assert item["path"].endswith("preview.png")
    assert manifest["provider"]["model"] == SILICONFLOW_VISION_DEFAULT_MODEL
    provenance = json.loads((attempt / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["image_count"] == 1
    assert provenance["image_sha256"] == [expected_sha]
    raw = json.loads((attempt / "raw_response.json").read_text(encoding="utf-8"))
    assert raw["image_count"] == 1
    # Pixel data never persists: no base64 payload anywhere in the attempt dir.
    assert "base64" not in (attempt / "request_manifest.json").read_text(encoding="utf-8")
    payload = json.loads(transport.requests[0]["body"])
    assert isinstance(payload["messages"][1]["content"], list)


def test_gateway_rejects_images_outside_repo(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_preview.png"
    make_png(outside)
    transport = QueueTransport(provider_response(json.dumps(visual_candidate())))
    gateway = make_vision_gateway(tmp_path, transport)

    with pytest.raises(GatewayError, match="inside repository root"):
        gateway.run_task(visual_task(tmp_path, (str(outside),)), run_id="escape", max_repairs=0)
    assert transport.requests == []


def test_gateway_rejects_missing_image(tmp_path: Path) -> None:
    transport = QueueTransport(provider_response(json.dumps(visual_candidate())))
    gateway = make_vision_gateway(tmp_path, transport)

    with pytest.raises(GatewayError, match="does not exist"):
        gateway.run_task(
            visual_task(tmp_path, (str(tmp_path / "artifacts" / "none.png"),)),
            run_id="missing",
            max_repairs=0,
        )
    assert transport.requests == []


def test_gateway_rejects_too_many_images(tmp_path: Path) -> None:
    paths = tuple(str(make_png(tmp_path / "artifacts" / f"p{index}.png")) for index in range(9))
    transport = QueueTransport(provider_response(json.dumps(visual_candidate())))
    gateway = make_vision_gateway(tmp_path, transport)

    with pytest.raises(GatewayError, match="too many images"):
        gateway.run_task(visual_task(tmp_path, paths), run_id="toomany", max_repairs=0)
    assert transport.requests == []


# ---------------------------------------------------------------------------
# visual_review_report contract and JSON schema
# ---------------------------------------------------------------------------


def test_visual_review_contract_accepts_valid_candidate() -> None:
    report = validate_candidate(visual_candidate(), visual_contract())

    assert report.ok, report.as_dict()
    assert report.kind == "visual_review_report"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda c: c.update(schema_version=2), "VISUAL_REVIEW_SCHEMA_VERSION_INVALID"),
        (lambda c: c.update(case_reviews=[]), "VISUAL_REVIEW_CASES_MISSING"),
        (lambda c: c["case_reviews"][0].update(geometry_plausibility="maybe"), "VISUAL_REVIEW_PLAUSIBILITY_INVALID"),
        (lambda c: c["case_reviews"][0].update(view_consistency="fuzzy"), "VISUAL_REVIEW_CONSISTENCY_INVALID"),
        (lambda c: c["case_reviews"][0].update(misuse_flags=["bad_flag"]), "VISUAL_REVIEW_FLAGS_UNKNOWN"),
        (lambda c: c["case_reviews"][0].update(confidence=1.5), "VISUAL_REVIEW_CONFIDENCE_INVALID"),
        (lambda c: c["case_reviews"][0].update(confidence=True), "VISUAL_REVIEW_CONFIDENCE_INVALID"),
        (lambda c: c["case_reviews"][0].update(notes_zh_cn="长" * 501), "VISUAL_REVIEW_CASE_NOTES_INVALID"),
        (lambda c: c.update(overall_notes_zh_cn="长" * 1001), "VISUAL_REVIEW_OVERALL_NOTES_INVALID"),
        (lambda c: c.update(extra_field=True), "VISUAL_REVIEW_FIELDS_UNKNOWN"),
        (lambda c: c.update(execute=True), "VISUAL_REVIEW_AUTHORITY_FIELD_FORBIDDEN"),
        (lambda c: c["case_reviews"][0].update(approve=True), "VISUAL_REVIEW_AUTHORITY_FIELD_FORBIDDEN"),
        (lambda c: c["case_reviews"][0].update(commands=["run"]), "VISUAL_REVIEW_AUTHORITY_FIELD_FORBIDDEN"),
        (lambda c: c.update(commands=["run"]), "FREEFORM_COMMAND_FIELD_FORBIDDEN"),
    ],
)
def test_visual_review_contract_rejects_invalid_candidates(mutate: Any, code: str) -> None:
    candidate = visual_candidate()
    mutate(candidate)

    report = validate_candidate(candidate, visual_contract())

    assert not report.ok
    assert code in {item.error_code for item in report.diagnostics}


def test_visual_review_kind_is_locked_by_allowed_kinds() -> None:
    contract = {"type": "json_object", "kind_field": "kind", "allowed_kinds": ["flat_recipe"]}

    report = validate_candidate(visual_candidate(), contract)

    assert not report.ok
    assert "MODEL_OUTPUT_KIND_NOT_ALLOWED" in {item.error_code for item in report.diagnostics}


def test_null_misuse_flags_are_normalized_before_validation() -> None:
    candidate = visual_candidate()
    candidate["case_reviews"][0]["misuse_flags"] = [None]

    raw_report = validate_candidate(candidate, visual_contract())
    assert not raw_report.ok

    normalized = normalize_visual_review_candidate(candidate)
    assert normalized["case_reviews"][0]["misuse_flags"] == []
    assert candidate["case_reviews"][0]["misuse_flags"] == [None]
    normalized_report = validate_candidate(normalized, visual_contract())
    assert normalized_report.ok, normalized_report.as_dict()


def test_visual_review_json_schema_file_roundtrip() -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "test_harness/schemas/visual_review_report.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)

    assert list(validator.iter_errors(visual_candidate())) == []
    extra = visual_candidate()
    extra["approve"] = True
    assert list(validator.iter_errors(extra))
    bad_enum = visual_candidate()
    bad_enum["case_reviews"][0]["geometry_plausibility"] = "maybe"
    assert list(validator.iter_errors(bad_enum))


# ---------------------------------------------------------------------------
# CLI end-to-end with a fake client
# ---------------------------------------------------------------------------


def run_cli(
    tmp_path: Path,
    argv: list[str],
    transport: QueueTransport,
    capsys: Any,
) -> tuple[int, str]:
    gateway = make_vision_gateway(tmp_path, transport)
    rc = run_visual_review.main(argv, gateway=gateway)
    return rc, capsys.readouterr().out


def test_cli_reviews_case_previews_and_writes_advisory_reports(tmp_path: Path, capsys: Any) -> None:
    cases_root = tmp_path / "artifacts" / "cases"
    make_case_dir(cases_root, "case_b")
    make_case_dir(cases_root, "case_a")
    candidate = visual_candidate("case_a")
    candidate["case_reviews"].append(
        {
            "case_id": "case_b",
            "geometry_plausibility": "suspect",
            "view_consistency": "unclear",
            "misuse_flags": ["tool_misplaced"],
            "confidence": 0.4,
            "notes_zh_cn": "tool 与 target 似乎分离。",
        }
    )
    transport = QueueTransport(provider_response(json.dumps(candidate)))

    rc, out = run_cli(
        tmp_path,
        [
            "--cases-root",
            str(cases_root),
            "--out",
            str(tmp_path / "artifacts" / "visual_review"),
            "--max-cases",
            "4",
        ],
        transport,
        capsys,
    )

    assert rc == 0, out
    report_path = tmp_path / "artifacts" / "visual_review" / "visual_review_report.json"
    markdown_path = tmp_path / "artifacts" / "visual_review" / "visual_review_report.zh-CN.md"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["kind"] == "visual_review_report"
    assert len(report["case_reviews"]) == 2
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "咨询性意见，仅供参考" in markdown
    assert "合理性：**存疑**" in markdown
    assert "`tool_misplaced`" in markdown
    assert "视觉复核完成" in out
    assert "存疑 1" in out
    assert "仅供参考" in out
    # Only the fixed prompt plus images left the host: no paths, no recipes.
    payload = json.loads(transport.requests[0]["body"])
    text_part = payload["messages"][1]["content"][0]["text"]
    assert "case_a" in text_part and "case_b" in text_part
    assert "preview.png" not in text_part
    assert str(cases_root) not in text_part
    # Staged attempt evidence carries image hashes for both cases.
    staging = list((tmp_path / "artifacts" / "authoring_gateway").rglob("request_manifest.json"))
    assert staging
    manifest = json.loads(staging[-1].read_text(encoding="utf-8"))
    assert manifest["images"]["count"] == 2


def test_cli_standalone_image_mode_uses_file_stem(tmp_path: Path, capsys: Any) -> None:
    image = make_png(tmp_path / "artifacts" / "shots" / "standalone_case.png")
    transport = QueueTransport(provider_response(json.dumps(visual_candidate("standalone_case"))))

    rc, out = run_cli(
        tmp_path,
        [
            "--image",
            str(image),
            "--out",
            str(tmp_path / "artifacts" / "visual_review"),
        ],
        transport,
        capsys,
    )

    assert rc == 0, out
    payload = json.loads(transport.requests[0]["body"])
    assert "standalone_case" in payload["messages"][1]["content"][0]["text"]


def test_cli_returns_two_when_no_images(tmp_path: Path, capsys: Any) -> None:
    cases_root = tmp_path / "artifacts" / "cases"
    cases_root.mkdir(parents=True)
    transport = QueueTransport(provider_response(json.dumps(visual_candidate())))

    rc, out = run_cli(
        tmp_path,
        ["--cases-root", str(cases_root), "--out", str(tmp_path / "artifacts" / "visual_review")],
        transport,
        capsys,
    )

    assert rc == 2
    assert "没有可复核的几何预览图" in out
    assert transport.requests == []


def test_cli_model_failure_preserves_bounded_evidence(tmp_path: Path, capsys: Any) -> None:
    cases_root = tmp_path / "artifacts" / "cases"
    make_case_dir(cases_root, "case_a")
    transport = QueueTransport(
        provider_response("not-json-at-all"),
        provider_response("still-not-json"),
    )

    rc, out = run_cli(
        tmp_path,
        ["--cases-root", str(cases_root), "--out", str(tmp_path / "artifacts" / "visual_review")],
        transport,
        capsys,
    )

    assert rc == 1
    assert "未通过固定契约" in out
    assert "原始证据" in out
    staging = list((tmp_path / "artifacts" / "authoring_gateway").rglob("raw_response.json"))
    assert staging  # bounded raw evidence preserved for diagnosis
    assert not (tmp_path / "artifacts" / "visual_review" / "visual_review_report.json").exists()


def test_cli_render_missing_invokes_preview_renderer(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases_root = tmp_path / "artifacts" / "cases"
    case_dir = make_case_dir(cases_root, "case_a", with_preview=False)
    calls: list[list[str]] = []

    def fake_render(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append(list(args[0]))
        make_png(case_dir / "report" / "preview.png")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run_visual_review.subprocess, "run", fake_render)
    transport = QueueTransport(provider_response(json.dumps(visual_candidate("case_a"))))

    rc, _out = run_cli(
        tmp_path,
        [
            "--cases-root",
            str(cases_root),
            "--out",
            str(tmp_path / "artifacts" / "visual_review"),
            "--render-missing",
        ],
        transport,
        capsys,
    )

    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == run_visual_review.sys.executable
    assert calls[0][1].endswith("render_case_preview.py")


# ---------------------------------------------------------------------------
# Workflow advisory hook
# ---------------------------------------------------------------------------


def fake_workflow_self(tmp_path: Path, *, api_key: str = API_KEY, category: str = "external") -> SimpleNamespace:
    return SimpleNamespace(
        repo_root=tmp_path,
        profile_category=category,
        runtime=SimpleNamespace(config=SimpleNamespace(api_key=api_key)),
    )


def make_execution_layout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    execution_root = tmp_path / "artifacts" / "session" / "execution" / "round_0001" / "attempt_0001"
    cases_root = execution_root / "cases"
    make_case_dir(cases_root, "case_a")
    execution_root.mkdir(parents=True, exist_ok=True)
    rel = "artifacts/session/execution/round_0001/attempt_0001/cases"
    return execution_root, {"cases": rel}


def public_session() -> dict[str, Any]:
    return {"data_classification": "public_interface"}


def test_visual_hook_skips_when_execution_failed(tmp_path: Path) -> None:
    execution_root, artifacts = make_execution_layout(tmp_path)

    result = HarnessWorkflow._run_visual_review(
        fake_workflow_self(tmp_path), public_session(), execution_root, artifacts, False
    )

    assert result == {"ran": False, "note": "执行未通过，跳过视觉模型复核"}


def test_visual_hook_never_sends_proprietary_sessions(tmp_path: Path) -> None:
    execution_root, artifacts = make_execution_layout(tmp_path)
    session = {"data_classification": "proprietary_source"}

    result = HarnessWorkflow._run_visual_review(
        fake_workflow_self(tmp_path), session, execution_root, artifacts, True
    )

    assert result["ran"] is False
    assert "不发送外网" in result["note"]


def test_visual_hook_skips_intranet_profile(tmp_path: Path) -> None:
    execution_root, artifacts = make_execution_layout(tmp_path)

    result = HarnessWorkflow._run_visual_review(
        fake_workflow_self(tmp_path, category="intranet"), public_session(), execution_root, artifacts, True
    )

    assert result["ran"] is False
    assert "不配置外网视觉模型" in result["note"]


def test_visual_hook_skips_with_note_when_api_key_missing(tmp_path: Path) -> None:
    execution_root, artifacts = make_execution_layout(tmp_path)

    result = HarnessWorkflow._run_visual_review(
        fake_workflow_self(tmp_path, api_key=""), public_session(), execution_root, artifacts, True
    )

    assert result["ran"] is False
    assert "视觉模型未配置" in result["note"]


def test_visual_hook_failure_is_note_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_root, artifacts = make_execution_layout(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("cannot start process")

    monkeypatch.setattr(workflow_module.subprocess, "run", boom)

    result = HarnessWorkflow._run_visual_review(
        fake_workflow_self(tmp_path), public_session(), execution_root, artifacts, True
    )

    assert result["ran"] is True
    assert result["ok"] is False
    assert "视觉复核执行失败" in result["note"]


def test_visual_hook_nonzero_exit_is_note_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_root, artifacts = make_execution_layout(tmp_path)
    monkeypatch.setattr(
        workflow_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="模型服务不可用"),
    )

    result = HarnessWorkflow._run_visual_review(
        fake_workflow_self(tmp_path), public_session(), execution_root, artifacts, True
    )

    assert result["ran"] is True
    assert result["ok"] is False
    assert "rc=1" in result["note"]


def test_visual_hook_records_bounded_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_root, artifacts = make_execution_layout(tmp_path)
    out_root = execution_root / "visual_review"
    out_root.mkdir(parents=True)
    reviews = [
        {
            "case_id": f"case_{index:02d}",
            "geometry_plausibility": "suspect" if index % 2 else "plausible",
            "view_consistency": "consistent",
            "misuse_flags": ["other"] * 12,
            "confidence": 0.5,
            "notes_zh_cn": "…",
        }
        for index in range(30)
    ]
    (out_root / "visual_review_report.json").write_text(
        json.dumps({"kind": "visual_review_report", "schema_version": 1, "case_reviews": reviews,
                    "overall_notes_zh_cn": "…"}),
        encoding="utf-8",
    )
    (out_root / "visual_review_report.zh-CN.md").write_text("# 复核", encoding="utf-8")
    monkeypatch.setattr(
        workflow_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = HarnessWorkflow._run_visual_review(
        fake_workflow_self(tmp_path), public_session(), execution_root, artifacts, True
    )

    assert result["ran"] is True and result["ok"] is True
    assert result["summary"] == {"reviewed": 30, "plausible": 15, "suspect": 15, "implausible": 0, "flags": 240}
    assert len(result["cases"]) == 24
    assert all(len(row["flags"]) <= 8 for row in result["cases"])
    assert result["report_path"].endswith("visual_review_report.json")
    assert result["markdown_path"].endswith("visual_review_report.zh-CN.md")


def test_final_report_marks_visual_review_advisory(tmp_path: Path) -> None:
    session = {"public_function": "api_boolean"}
    round_record = {"round_number": 1, "user_review_report_path": "", "review_packet_path": "", "candidate_path": ""}
    visual_review = {
        "ran": True,
        "ok": True,
        "summary": {"reviewed": 2, "plausible": 1, "suspect": 1, "implausible": 0, "flags": 1},
        "cases": [{"case_id": "case_b", "plausibility": "suspect", "flags": ["tool_misplaced"]}],
        "markdown_path": "artifacts/x/visual_review/visual_review_report.zh-CN.md",
    }

    HarnessWorkflow._write_final_report(
        tmp_path / "final_report.zh-CN.md",
        session=session,
        round_record=round_record,
        result={},
        task_result={},
        passed=True,
        visual_review=visual_review,
    )

    text = (tmp_path / "final_report.zh-CN.md").read_text(encoding="utf-8")
    assert "视觉模型复核（咨询性意见，仅供参考）" in text
    assert "不参与门禁、批准、执行或失败归因" in text
    assert "`case_b`：suspect" in text
    assert "visual_review_report.zh-CN.md" in text


# ---------------------------------------------------------------------------
# UI state projection
# ---------------------------------------------------------------------------


def make_overview_session(tmp_path: Path, visual_review: Any) -> tuple[Path, dict[str, Any]]:
    session_root = tmp_path / "artifacts" / "harness_sessions" / "s1"
    attempt_rel = "artifacts/harness_sessions/s1/execution/round_0001/attempt_0001"
    attempt_root = session_root / "execution" / "round_0001" / "attempt_0001"
    attempt_root.mkdir(parents=True, exist_ok=True)
    (attempt_root / "execution_result.json").write_text(json.dumps({"ok": True, "results": []}), encoding="utf-8")
    session: dict[str, Any] = {
        "session_id": "s1",
        "state": "completed",
        "current_execution_attempt_path": attempt_rel,
    }
    if visual_review is not None:
        session["visual_review"] = visual_review
    return session_root, session


def test_execution_overview_defaults_visual_review_absent(tmp_path: Path) -> None:
    session_root, session = make_overview_session(tmp_path, None)

    overview = execution_overview(tmp_path, session_root, session)

    assert overview["available"] is True
    assert overview["visual_review"] == {"ran": False}


def test_execution_overview_passes_through_bounded_visual_review(tmp_path: Path) -> None:
    record = {
        "ran": True,
        "ok": True,
        "report_path": (
            "artifacts/harness_sessions/s1/execution/round_0001/attempt_0001"
            "/visual_review/visual_review_report.json"
        ),
        "summary": {"reviewed": 30, "plausible": 28, "suspect": 2, "implausible": 0, "flags": 3},
        "cases": [
            {"case_id": f"case_{index:02d}", "plausibility": "suspect", "flags": ["other"] * 12}
            for index in range(30)
        ],
    }
    session_root, session = make_overview_session(tmp_path, record)

    overview = execution_overview(tmp_path, session_root, session)

    visual = overview["visual_review"]
    assert visual["ran"] is True and visual["ok"] is True
    assert visual["summary"] == {"reviewed": 30, "plausible": 28, "suspect": 2, "implausible": 0, "flags": 3}
    assert len(visual["cases"]) == 24
    assert all(len(row["flags"]) <= 8 for row in visual["cases"])
    assert visual["report_path"] == "execution/round_0001/attempt_0001/visual_review/visual_review_report.json"
