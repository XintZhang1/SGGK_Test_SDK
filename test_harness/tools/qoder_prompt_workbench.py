#!/usr/bin/env python3
"""Local web UI for the Qoder prompt-pack workflow.

The server is intentionally dependency-free so it can run on the intranet
Windows machine with the same Python used by the harness. It wraps the fixed
prompt-pack builder, reads generated prompts, and saves Qoder JSON outputs
under artifacts.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from subprocess import run
import sys
import traceback
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = REPO_ROOT / "test_harness" / "ui" / "qoder_prompt_workbench"
BUILDER = REPO_ROOT / "test_harness" / "tools" / "build_qoder_prompt_pack.py"

DEFAULTS = {
    "forms_dir": "test_harness/forms/interface_distillation",
    "out": "artifacts/qoder_prompt_pack",
    "model_output_root": "artifacts/model_outputs",
    "source_output_root": "artifacts/source_model_outputs",
    "source_task_dir": "artifacts/interface_distillation_windows_full_40chunk_v2/source_attack_tasks",
    "source_task_jsonl": "",
    "source_task_limit": 80,
    "max_prompt_chars": 60000,
    "qoder_hard_token_limit": 200000,
    "run_tag": "qoder_ui",
}


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def repo_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def safe_repo_file(value: str, *, must_exist: bool = True) -> Path:
    path = repo_path(value)
    if not is_relative_to(path, REPO_ROOT):
        raise ValueError(f"path must stay inside repo: {value}")
    if must_exist and not path.is_file():
        raise FileNotFoundError(value)
    return path


def safe_artifact_output(value: str) -> Path:
    path = repo_path(value)
    if not is_relative_to(path, REPO_ROOT):
        raise ValueError(f"output path must stay inside repo: {value}")
    rel = path.resolve().relative_to(REPO_ROOT.resolve())
    if not rel.parts or rel.parts[0] != "artifacts":
        raise ValueError("Qoder outputs must be saved under artifacts/")
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def rel_display(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def load_checkpoint(out_value: str) -> dict[str, Any]:
    out_root = repo_path(out_value)
    checkpoint_path = out_root / "qoder_session_checkpoint.json"
    index_path = out_root / "qoder_session_index.md"
    resume_path = out_root / "qoder_resume_prompt.md"
    if not checkpoint_path.is_file():
        return {
            "exists": False,
            "out": out_value,
            "checkpoint_path": rel_display(checkpoint_path),
            "index_path": rel_display(index_path),
            "resume_path": rel_display(resume_path),
            "tasks": [],
        }

    checkpoint = read_json(checkpoint_path)
    raw_tasks = checkpoint.get("tasks") if isinstance(checkpoint.get("tasks"), list) else []
    tasks = []
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict):
            continue
        task = dict(raw_task)
        expected_output_path = task.get("expected_output_path")
        if isinstance(expected_output_path, str) and expected_output_path:
            task["output_exists"] = repo_path(expected_output_path).is_file()
        tasks.append(task)
    return {
        "exists": True,
        "out": out_value,
        "checkpoint_path": rel_display(checkpoint_path),
        "index_path": rel_display(index_path),
        "resume_path": rel_display(resume_path),
        "generated_at": checkpoint.get("generated_at"),
        "run_tag": checkpoint.get("run_tag"),
        "safe_prompt_char_budget": checkpoint.get("safe_prompt_char_budget"),
        "qoder_hard_token_limit": checkpoint.get("qoder_hard_token_limit"),
        "tasks": tasks,
        "summary": {
            "total": len(tasks),
            "done": sum(1 for task in tasks if task.get("output_exists")),
            "interface": sum(1 for task in tasks if task.get("task_type") == "interface_form"),
            "source": sum(1 for task in tasks if task.get("task_type") == "source_attack"),
            "over_budget": sum(1 for task in tasks if task.get("over_safe_budget")),
        },
    }


def find_task(out_value: str, task_id: str) -> dict[str, Any]:
    state = load_checkpoint(out_value)
    for task in state.get("tasks", []):
        if str(task.get("task_id")) == task_id:
            return task
    raise KeyError(task_id)


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "QoderPromptWorkbench/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400, detail: str = "") -> None:
        payload = {"ok": False, "error": message}
        if detail:
            payload["detail"] = detail
        self.send_json(payload, status)

    def read_body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("request body must be a JSON object")
        return loaded

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.handle_api_get(parsed.path, parse_qs(parsed.query))
                return
            self.handle_static(parsed.path)
        except Exception as exc:  # pragma: no cover - keeps UI debuggable.
            self.send_error_json(str(exc), 500, traceback.format_exc())

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_error_json("unknown endpoint", 404)
                return
            self.handle_api_post(parsed.path)
        except json.JSONDecodeError as exc:
            self.send_error_json(f"invalid JSON request: {exc}", 400)
        except Exception as exc:  # pragma: no cover - keeps UI debuggable.
            self.send_error_json(str(exc), 500, traceback.format_exc())

    def handle_static(self, path_value: str) -> None:
        if path_value in ("", "/"):
            path_value = "/index.html"
        rel = unquote(path_value.lstrip("/"))
        static_path = (UI_ROOT / rel).resolve()
        if not is_relative_to(static_path, UI_ROOT) or not static_path.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(static_path.name)[0] or "application/octet-stream"
        data = static_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def handle_api_get(self, path_value: str, query: dict[str, list[str]]) -> None:
        if path_value == "/api/defaults":
            self.send_json({"ok": True, "repo_root": str(REPO_ROOT), "defaults": DEFAULTS})
            return
        if path_value == "/api/state":
            out_value = query.get("out", [DEFAULTS["out"]])[0]
            self.send_json({"ok": True, "state": load_checkpoint(out_value)})
            return
        if path_value == "/api/task":
            out_value = query.get("out", [DEFAULTS["out"]])[0]
            task_id = query.get("task_id", [""])[0]
            task = find_task(out_value, task_id)
            out_root = repo_path(out_value)
            resume_path = out_root / "qoder_resume_prompt.md"
            prompt_path = safe_repo_file(str(task.get("prompt_path") or ""))
            output_path = repo_path(str(task.get("expected_output_path") or ""))
            output_text = read_text(output_path) if output_path.is_file() else ""
            self.send_json(
                {
                    "ok": True,
                    "task": task,
                    "resume_prompt": read_text(resume_path) if resume_path.is_file() else "",
                    "task_prompt": read_text(prompt_path),
                    "output_text": output_text,
                }
            )
            return
        self.send_error_json("unknown endpoint", 404)

    def handle_api_post(self, path_value: str) -> None:
        if path_value == "/api/build-pack":
            body = self.read_body_json()
            config = dict(DEFAULTS)
            config.update({key: value for key, value in body.items() if value is not None})
            command = [
                sys.executable,
                str(BUILDER),
                "--forms-dir",
                str(config["forms_dir"]),
                "--out",
                str(config["out"]),
                "--model-output-root",
                str(config["model_output_root"]),
                "--source-output-root",
                str(config["source_output_root"]),
                "--max-prompt-chars",
                str(int(config["max_prompt_chars"])),
                "--qoder-hard-token-limit",
                str(int(config["qoder_hard_token_limit"])),
                "--run-tag",
                str(config["run_tag"]),
            ]
            source_jsonl = str(config.get("source_task_jsonl") or "").strip()
            source_dir = str(config.get("source_task_dir") or "").strip()
            if source_jsonl:
                command.extend(["--source-task-jsonl", source_jsonl])
            elif source_dir:
                command.extend(["--source-task-dir", source_dir])
            source_limit = int(config.get("source_task_limit") or 0)
            if source_limit:
                command.extend(["--source-task-limit", str(source_limit)])
            completed = run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
            self.send_json(
                {
                    "ok": completed.returncode == 0,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "command": command,
                    "state": load_checkpoint(str(config["out"])) if completed.returncode == 0 else None,
                },
                200 if completed.returncode == 0 else 500,
            )
            return
        if path_value == "/api/save-output":
            body = self.read_body_json()
            expected_path = str(body.get("expected_output_path") or "")
            content = str(body.get("content") or "").strip()
            if not expected_path:
                raise ValueError("expected_output_path is required")
            if not content:
                raise ValueError("Qoder JSON content is empty")
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("Qoder output must be one JSON object")
            path = safe_artifact_output(expected_path)
            write_json(path, parsed)
            self.send_json({"ok": True, "saved_path": rel_display(path), "bytes": path.stat().st_size})
            return
        self.send_error_json("unknown endpoint", 404)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), WorkbenchHandler)
    print(f"Qoder prompt workbench: http://{args.host}:{args.port}/", flush=True)
    print(f"Repo root: {REPO_ROOT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Qoder prompt workbench.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
