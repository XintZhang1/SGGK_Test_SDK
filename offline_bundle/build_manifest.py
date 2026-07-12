"""Regenerate the integrity manifest for every binary offline payload."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
urls = {
    "archives/python-3.11.9-embeddable-amd64.zip": "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embeddable-amd64.zip",
    "archives/cmake-4.3.3-windows-x86_64.zip": "https://github.com/Kitware/CMake/releases/download/v4.3.3/cmake-4.3.3-windows-x86_64.zip",
    "archives/vc_redist.x64.exe": "https://aka.ms/vc14/vc_redist.x64.exe",
}
files = []
for path in sorted([*root.glob("archives/*"), *root.glob("wheelhouse/*.whl")]):
    relative = path.relative_to(root).as_posix()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    files.append(
        {"path": relative, "bytes": path.stat().st_size, "sha256": digest, "source": urls.get(relative, "PyPI wheel")}
    )
manifest = {
    "schema_version": 1,
    "target": "Windows x86-64, CPython 3.11, Visual Studio 2022",
    "python": "3.11.9 embeddable amd64",
    "cmake": "4.3.3 windows x86_64",
    "files": files,
}
(root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Wrote {len(files)} records")
