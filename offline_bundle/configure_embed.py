"""Enable repo imports and site-packages in the CPython embeddable runtime."""

from __future__ import annotations

import sys
from pathlib import Path

runtime = Path(sys.executable).resolve().parent
pth_files = list(runtime.glob("python*._pth"))
if len(pth_files) != 1:
    raise SystemExit("expected exactly one python*._pth file")
pth_files[0].write_text(
    "python311.zip\n.\nLib\\site-packages\n..\\..\n..\\..\\test_harness\\tools\nimport site\n",
    encoding="utf-8",
    newline="\n",
)
(runtime / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
print(f"Configured {pth_files[0]}")
