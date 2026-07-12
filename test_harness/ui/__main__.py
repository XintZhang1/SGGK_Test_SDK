from __future__ import annotations

import argparse
from pathlib import Path

from .server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="SGGK Harness local UI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    run_server(repo_root=Path(__file__).resolve().parents[2], port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
