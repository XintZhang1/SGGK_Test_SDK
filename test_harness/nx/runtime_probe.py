"""Minimal script executed by NX's run_journal.exe, never by the UI process."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PROBE_REVISION = 2
PROBE_KIND = "sggk_nx_python_probe"
PROBE_PREFIX = "SGGK_NX_PROBE_JSON="


def collect_probe(nonce: str) -> dict[str, Any]:
    """Import NXOpen only inside the isolated journal process."""

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "probe_revision": PROBE_REVISION,
        "kind": PROBE_KIND,
        "nonce": nonce,
        "ok": False,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable_name": Path(sys.executable).name,
        },
        "nxopen": {},
        "error_type": "",
        "error": "",
    }
    try:
        import NXOpen  # type: ignore[import-not-found]

        session = NXOpen.Session.GetSession()
        if session is None:
            raise RuntimeError("NXOpen.Session.GetSession() returned no session")
        nxopen: dict[str, Any] = {
            "module_file": str(getattr(NXOpen, "__file__", "") or ""),
            "session_type": type(session).__name__,
            "has_uf_session": False,
        }
        try:
            import NXOpen.UF  # type: ignore[import-not-found]

            nxopen["has_uf_session"] = NXOpen.UF.UFSession.GetUFSession() is not None
        except Exception as exc:
            # Core NXOpen availability remains useful even if the optional UF
            # layer is damaged; preserve the reason as diagnostic metadata.
            nxopen["uf_error_type"] = type(exc).__name__
            nxopen["uf_error"] = str(exc)[:1000]
        get_environment = getattr(session, "GetEnvironmentVariableValue", None)
        if callable(get_environment):
            versions: dict[str, str] = {}
            for key in ("UGII_VERSION", "UGII_FULL_VERSION"):
                try:
                    value = get_environment(key)
                except Exception:
                    continue
                if value:
                    versions[key] = str(value)
            nxopen["versions"] = versions
        payload["nxopen"] = nxopen
        payload["ok"] = True
    except Exception as exc:
        payload["error_type"] = type(exc).__name__
        payload["error"] = str(exc)[:4000]
    return payload


def write_result(payload: dict[str, Any], output_path: str) -> None:
    if not output_path:
        return
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(destination)


def _probe_inputs(arguments: list[str] | None = None) -> tuple[str, str]:
    """Prefer journal arguments because NX launchers may not preserve env vars.

    ``run_journal.exe`` can hand a journal to an already-running NX process or
    start NX through another launcher.  In either case, transient variables
    added by the Harness parent process are less reliable than ``-args``.
    Environment variables remain as a compatibility fallback for direct or
    older invocations of this bundled script.
    """

    values = list(sys.argv[1:] if arguments is None else arguments)
    nonce = values[0] if values else os.environ.get("SGGK_NX_PROBE_NONCE", "")
    output_path = values[1] if len(values) > 1 else os.environ.get("SGGK_NX_PROBE_OUTPUT", "")
    return nonce, output_path


def main() -> int:
    nonce, output_path = _probe_inputs()
    payload = collect_probe(nonce)
    payload["transport"] = {
        "nonce_source": "argument" if len(sys.argv) > 1 else "environment",
        "output_source": "argument" if len(sys.argv) > 2 else "environment",
    }
    try:
        write_result(payload, output_path)
    except OSError as exc:
        # stdout is an independent authenticated result channel.  A temp-path
        # encoding, antivirus, or permission problem must not turn a working
        # NXOpen session into an opaque "invalid_result" report.
        payload["result_write_error_type"] = type(exc).__name__
        payload["result_write_error"] = str(exc)[:1000]
    print(PROBE_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0 if payload["ok"] else 3


if __name__ == "__main__":
    # NX owns the journal process lifecycle.  Raising SystemExit can be reported
    # as a journal failure by some NX releases even when the probe succeeded.
    main()
