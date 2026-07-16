from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from hashlib import sha256
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from test_harness.ui.application import HarnessUiApplication
from test_harness.ui.server import HarnessUiServer, JobManager


@pytest.fixture
def server(tmp_path):
    instance = HarnessUiServer(("127.0.0.1", 0), tmp_path)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance, f"http://127.0.0.1:{instance.server_address[1]}"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def get_json(url):
    with urlopen(url, timeout=2) as response:
        return response.status, json.load(response), response.headers


def post_json(url, payload, csrf_token):
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf_token},
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        return response.status, json.load(response)


def test_job_manager_uses_joinable_non_daemon_worker() -> None:
    release = threading.Event()
    manager = JobManager()

    manager.submit("nx_probe", lambda: release.wait(timeout=2))

    assert manager._thread is not None  # noqa: SLF001
    assert manager._thread.daemon is False  # noqa: SLF001
    running = manager.snapshot()
    assert running["status"] == "running"
    assert datetime.fromisoformat(running["started_at"].replace("Z", "+00:00")).tzinfo == UTC
    release.set()
    manager.wait(timeout=2)
    completed = manager.snapshot()
    assert completed["status"] == "completed"
    assert completed["started_at"] is None


def test_job_manager_clears_started_at_after_failure() -> None:
    release = threading.Event()
    manager = JobManager()

    def fail() -> None:
        release.wait(timeout=2)
        raise RuntimeError("expected failure")

    manager.submit("start", fail)
    assert manager.snapshot()["started_at"]
    release.set()
    manager.wait(timeout=2)

    failed = manager.snapshot()
    assert failed["status"] == "failed"
    assert failed["started_at"] is None


def test_ui_server_serves_state_and_security_headers(server) -> None:
    _instance, base = server
    status, value, headers = get_json(base + "/api/state")
    assert status == 200
    assert value["ok"]
    assert value["session"]["state"] == "idle"
    assert value["artifact_summary"]["title"] == "还没有测试产物"
    assert "二进制产物仍保留在 session 目录" in value["artifact_summary"]["detail"]
    assert value["csrf_token"]
    assert value["abc"]["status"] == "idle"
    assert "detection" in value["nx"]
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_ui_server_serves_visible_job_timer_assets(server) -> None:
    _instance, base = server
    with urlopen(base + "/", timeout=2) as response:
        index = response.read().decode("utf-8")
    with urlopen(base + "/job-status.js", timeout=2) as response:
        script = response.read().decode("utf-8")
    with urlopen(base + "/job-status.css", timeout=2) as response:
        stylesheet = response.read().decode("utf-8")

    assert 'id="jobProgress"' in index
    assert 'id="jobElapsed"' in index
    assert 'src="/job-status.js"' in index
    assert "模型处理中" in script
    assert "setInterval(paint, 1000)" in script
    assert all(operation in script for operation in ("start", "comment", "approve", "retry", "build", "nx_probe"))
    assert "ABC" not in script
    assert ".job-progress[hidden]" in stylesheet


def test_ui_server_serves_semantic_artifact_workspace(server) -> None:
    _instance, base = server
    with urlopen(base + "/", timeout=2) as response:
        index = response.read().decode("utf-8")
    with urlopen(base + "/app.js", timeout=2) as response:
        script = response.read().decode("utf-8")
    with urlopen(base + "/markdown-preview.js", timeout=2) as response:
        markdown_script = response.read().decode("utf-8")
        markdown_content_type = response.headers["Content-Type"]
    with urlopen(base + "/styles.css", timeout=2) as response:
        stylesheet = response.read().decode("utf-8")

    assert 'id="artifactSummary"' in index
    assert 'id="artifactActions"' in index
    assert 'id="previewTitle"' in index
    assert 'id="previewDescription"' in index
    assert 'id="repairButton"' in index
    assert 'src="/markdown-preview.js"' in index
    assert index.index('src="/markdown-preview.js"') < index.index('src="/app.js"')
    assert "javascript" in markdown_content_type
    assert "根据失败诊断修改测试方案" in script
    assert "renderArtifactSummary" in script
    assert "renderArtifactContent" in script
    assert "HarnessMarkdownPreview.renderInto" in script
    assert "个可查看文件" in script
    assert "二进制产物仍保留在 session 目录" in script
    assert 'group.id === "reports"' in script
    assert 'state?.session?.state === "awaiting_comment"' in script
    assert "执行失败后请先修订或按规则重试" in script
    assert "artifactRequestSerial" in script
    assert "ready_to_build" in script
    assert 'requestSession !== (state?.session?.session_id || "")' in script
    assert all(label in script for label in ("重点报告", "测试方案与代码", "SDK 运行结果", "技术细节"))
    assert "createTextNode" in markdown_script
    assert "textContent" in markdown_script
    assert "innerHTML" not in markdown_script
    assert "insertAdjacentHTML" not in markdown_script
    assert "DOMParser" not in markdown_script
    assert ".artifact-group" in stylesheet
    assert ".artifact-action" in stylesheet
    assert ".markdown-preview" in stylesheet
    assert ".markdown-code-block" in stylesheet


def test_ui_server_requires_csrf_for_post(server) -> None:
    _instance, base = server
    request = Request(base + "/api/retry", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(HTTPError) as raised:
        urlopen(request, timeout=2)
    assert raised.value.code == 403


def test_ui_server_rejects_untrusted_host_before_exposing_state(server) -> None:
    _instance, base = server
    request = Request(base + "/api/state", headers={"Host": "attacker.invalid"})
    with pytest.raises(HTTPError) as raised:
        urlopen(request, timeout=2)
    assert raised.value.code == 403


def test_ui_server_accepts_background_operation_with_csrf(server) -> None:
    instance, base = server
    instance.app.retry = lambda: {"ok": True}
    _status, state, _headers = get_json(base + "/api/state")
    request = Request(
        base + "/api/retry",
        data=b"{}",
        headers={"Content-Type": "application/json", "X-CSRF-Token": state["csrf_token"]},
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        assert response.status == 202


def test_ui_server_blocks_artifact_traversal(server) -> None:
    _instance, base = server
    with pytest.raises(HTTPError) as raised:
        urlopen(base + "/api/artifact?path=../outside.txt", timeout=2)
    assert raised.value.code == 404


def test_ui_server_validates_and_binds_existing_abc_index(server, tmp_path) -> None:
    _instance, base = server
    step = tmp_path / "abc" / "sample.step"
    step.parent.mkdir()
    step.write_text("ISO-10303-21;", encoding="utf-8")
    index = step.parent / "dataset_index.json"
    index.write_text(
        json.dumps({"files": [{"path": str(step), "sha256": sha256(step.read_bytes()).hexdigest()}]}),
        encoding="utf-8",
    )
    _status, state, _headers = get_json(base + "/api/state")

    status, checked = post_json(base + "/api/abc/validate", {"path": str(index)}, state["csrf_token"])
    assert status == 200
    assert checked["inspection"]["ready"] is True

    status, bound = post_json(base + "/api/abc/use-existing", {"path": str(index)}, state["csrf_token"])
    assert status == 200
    assert bound["settings"]["campaign_dataset"] == str(index)


def test_ui_server_exposes_abc_fetch_and_nx_probe_actions(server) -> None:
    instance, base = server
    instance.app.abc.start_fetch = lambda payload: {"status": "running", "request": payload}
    instance.app.probe_nx = lambda: {"status": "verified", "ok": True}
    _status, state, _headers = get_json(base + "/api/state")

    status, fetch = post_json(
        base + "/api/abc/fetch",
        {"mode": "plan", "out_root": "artifacts/abc"},
        state["csrf_token"],
    )
    assert status == 202
    assert fetch["abc"]["status"] == "running"

    status, probe = post_json(base + "/api/nx/probe", {}, state["csrf_token"])
    assert status == 202
    assert probe["job"]["operation"] == "nx_probe"


def test_nx_probe_cache_is_bound_to_selected_installation(tmp_path, monkeypatch) -> None:
    app = HarnessUiApplication(tmp_path)

    def detection(root: str) -> dict:
        return {
            "ok": True,
            "status": "ready_for_probe",
            "selected_root": root,
            "installations": [
                {
                    "root": root,
                    "paths": {"run_journal": root + "/NXBIN/run_journal.exe"},
                }
            ],
            "diagnostics": [],
        }

    first = detection("C:/Siemens/NX2506")
    second = detection("C:/Siemens/NX2512")
    monkeypatch.setattr("test_harness.ui.application.detect_nx_environment", lambda **_kwargs: first)
    monkeypatch.setattr(
        "test_harness.ui.application.probe_nx_python",
        lambda **_kwargs: {"ok": True, "status": "verified", "environment": first},
    )
    app.nx_state(refresh=True)
    assert app.probe_nx()["ok"] is True

    monkeypatch.setattr("test_harness.ui.application.detect_nx_environment", lambda **_kwargs: second)
    refreshed = app.nx_state(refresh=True)

    assert refreshed["detection"]["selected_root"] == "C:/Siemens/NX2512"
    assert refreshed["probe"]["status"] == "not_run"


def test_settings_reject_unvalidated_campaign_dataset(tmp_path) -> None:
    app = HarnessUiApplication(tmp_path)
    arbitrary = tmp_path / "not-an-index.json"
    arbitrary.write_text('{"unexpected": true}', encoding="utf-8")

    with pytest.raises(ValueError, match="dataset index"):
        app.save_settings({"settings": {"campaign_dataset": str(arbitrary)}})
