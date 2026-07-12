"""Install wheel payloads without pip, network access, or an existing Python."""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

repo_root = Path(__file__).resolve().parent.parent
wheelhouse = repo_root / "offline_bundle" / "wheelhouse"
runtime = Path(sys.executable).resolve().parent
target = runtime / "Lib" / "site-packages"
target.mkdir(parents=True, exist_ok=True)


def destination(member: str) -> Path | None:
    relative = PurePosixPath(member)
    parts = relative.parts
    if not parts or member.endswith("/"):
        return None
    root = target
    if ".data" in parts[0]:
        if len(parts) < 3 or parts[1] not in {"purelib", "platlib", "scripts"}:
            return None
        if parts[1] == "scripts":
            root = runtime / "Scripts"
        relative = PurePosixPath(*parts[2:])
    result = (root / Path(*relative.parts)).resolve()
    try:
        result.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"wheel member escapes target: {member}") from exc
    return result


wheel_files = sorted(wheelhouse.glob("*.whl"))
if not wheel_files:
    raise SystemExit("offline wheelhouse is empty")
for wheel in wheel_files:
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            output = destination(info.filename)
            if output is None:
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, output.open("wb") as sink:
                shutil.copyfileobj(source, sink)
    print(f"Installed {wheel.name}")
