"""Loopback-only HTTP server for the SGGK Harness UI."""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .application import HarnessUiApplication

MAX_REQUEST_BYTES = 64 * 1024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "status": "idle",
            "operation": "",
            "error": "",
            "started_at": None,
        }
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def submit(self, operation: str, callback: Callable[[], Any]) -> None:
        with self._lock:
            if self._state["status"] == "running":
                raise RuntimeError("another Harness operation is already running")
            self._state = {
                "status": "running",
                "operation": operation,
                "error": "",
                "started_at": _utc_now(),
            }

        def run() -> None:
            try:
                callback()
            except Exception as exc:  # surfaced to the local UI
                result = {
                    "status": "failed",
                    "operation": operation,
                    "error": str(exc),
                    "started_at": None,
                }
            else:
                result = {
                    "status": "completed",
                    "operation": operation,
                    "error": "",
                    "started_at": None,
                }
            with self._lock:
                self._state = result

        thread = threading.Thread(target=run, name=f"harness-ui-{operation}", daemon=False)
        with self._lock:
            self._thread = thread
        thread.start()

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)


class HarnessUiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], repo_root: Path) -> None:
        self.app = HarnessUiApplication(repo_root)
        self.jobs = JobManager()
        self.csrf_token = secrets.token_urlsafe(32)
        self.static_root = Path(__file__).with_name("static")
        super().__init__(address, HarnessUiHandler)


class HarnessUiHandler(BaseHTTPRequestHandler):
    server: HarnessUiServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        payload = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body size is invalid")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("request must use application/json")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request root must be an object")
        return value

    def _trusted_host(self) -> bool:
        port = int(self.server.server_address[1])
        host = self.headers.get("Host", "").strip().casefold()
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def do_GET(self) -> None:
        if not self._trusted_host():
            self._error(HTTPStatus.FORBIDDEN, "untrusted Host header")
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/state":
            try:
                state = self.server.app.public_state()
                state["job"] = self.server.jobs.snapshot()
                state["csrf_token"] = self.server.csrf_token
                self._json({"ok": True, **state})
            except Exception as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if parsed.path == "/api/health":
            self._json({"ok": True})
            return
        if parsed.path == "/api/abc/status":
            self._json({"ok": True, "abc": self.server.app.abc.snapshot()})
            return
        if parsed.path == "/api/nx/environment":
            self._json({"ok": True, "nx": self.server.app.nx_state(refresh=True)})
            return
        if parsed.path == "/api/artifact":
            relative = parse_qs(parsed.query).get("path", [""])[0]
            try:
                self._json({"ok": True, **self.server.app.artifact(relative)})
            except (OSError, ValueError) as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc))
            return
        names = {
            "/": "index.html",
            "/app.js": "app.js",
            "/job-status.js": "job-status.js",
            "/markdown-preview.js": "markdown-preview.js",
            "/styles.css": "styles.css",
            "/job-status.css": "job-status.css",
        }
        name = names.get(parsed.path)
        if name is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        path = self.server.static_root / name
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "UI asset is missing")
            return
        payload = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._headers(HTTPStatus.OK, f"{content_type}; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if not self._trusted_host():
            self._error(HTTPStatus.FORBIDDEN, "untrusted Host header")
            return
        if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf_token):
            self._error(HTTPStatus.FORBIDDEN, "invalid CSRF token")
            return
        try:
            payload = self._body()
            path = urlsplit(self.path).path
            if path == "/api/settings":
                self._json({"ok": True, "settings": self.server.app.save_settings(payload)})
                return
            if path == "/api/abc/fetch":
                result = self.server.app.abc.start_fetch(payload)
                self._json({"ok": True, "abc": result}, HTTPStatus.ACCEPTED)
                return
            if path == "/api/abc/cancel":
                self._json({"ok": True, "abc": self.server.app.abc.cancel()}, HTTPStatus.ACCEPTED)
                return
            if path == "/api/abc/validate":
                self._json({"ok": True, "inspection": self.server.app.inspect_abc(str(payload.get("path") or ""))})
                return
            if path == "/api/abc/use-existing":
                self._json({"ok": True, **self.server.app.use_existing_abc(str(payload.get("path") or ""))})
                return
            if path == "/api/nx/probe":
                self.server.jobs.submit("nx_probe", self.server.app.probe_nx)
                self._json({"ok": True, "job": self.server.jobs.snapshot()}, HTTPStatus.ACCEPTED)
                return
            operations: dict[str, tuple[str, Callable[[], Any]]] = {
                "/api/start": ("start", lambda: self.server.app.start(str(payload.get("public_function") or ""))),
                "/api/comment": ("comment", lambda: self.server.app.comment(str(payload.get("comment") or ""))),
                "/api/approve": ("approve", self.server.app.approve),
                "/api/retry": ("retry", self.server.app.retry),
                "/api/build": ("build", self.server.app.build_runner),
            }
            selected = operations.get(path)
            if selected is None:
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            self.server.jobs.submit(*selected)
            self._json({"ok": True, "job": self.server.jobs.snapshot()}, HTTPStatus.ACCEPTED)
        except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))


def run_server(*, repo_root: str | Path, port: int = 8765, open_browser: bool = True) -> None:
    server = HarnessUiServer(("127.0.0.1", port), Path(repo_root).resolve())
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.jobs.wait()


__all__ = ["HarnessUiApplication", "HarnessUiServer", "run_server"]
