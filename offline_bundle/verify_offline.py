"""Verify payload hashes; with the embedded runtime, also verify imports."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
for record in manifest["files"]:
    path = root / record["path"]
    if not path.is_file():
        raise SystemExit(f"MISSING: {record['path']}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != record["sha256"] or path.stat().st_size != record["bytes"]:
        raise SystemExit(f"HASH MISMATCH: {record['path']}")
print(f"Verified {len(manifest['files'])} offline payloads")
for module in ("jsonschema", "PIL", "pytest"):
    imported = importlib.import_module(module)
    print(f"Imported {module}: {getattr(imported, '__version__', 'ok')}")
importlib.import_module("test_harness.ui")
print("Imported test_harness.ui: ok")
ruff = Path(__file__).resolve().parent.parent / ".offline_runtime" / "python" / "Scripts" / "ruff.exe"
if not ruff.is_file():
    raise SystemExit("MISSING: embedded ruff executable")
print("Found embedded ruff executable: ok")
