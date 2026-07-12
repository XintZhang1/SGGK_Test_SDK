from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from test_harness.ui.server import HarnessUiServer


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


def test_ui_server_serves_state_and_security_headers(server) -> None:
    _instance, base = server
    status, value, headers = get_json(base + "/api/state")
    assert status == 200
    assert value["ok"]
    assert value["session"]["state"] == "idle"
    assert value["csrf_token"]
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_ui_server_requires_csrf_for_post(server) -> None:
    _instance, base = server
    request = Request(base + "/api/retry", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
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
