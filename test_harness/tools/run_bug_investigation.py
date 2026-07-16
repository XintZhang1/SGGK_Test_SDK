#!/usr/bin/env python3
"""Run parallel, tool-bounded model investigation over qualified failure bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_harness.authoring_gateway.client import CompletionOptions  # noqa: E402
from test_harness.authoring_gateway.config import (  # noqa: E402
    PROFILE_SPECS,
    ConfigError,
    GatewayConfig,
    load_gateway_config,
)
from test_harness.investigation.orchestrator import run_bundle_investigation  # noqa: E402
from test_harness.investigation.session import INVESTIGATOR_ROLES  # noqa: E402


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=sorted(PROFILE_SPECS))
    parser.add_argument("--bundle-index", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-root", action="append", default=[])
    parser.add_argument("--role", action="append", choices=sorted(INVESTIGATOR_ROLES), default=[])
    parser.add_argument("--parallelism", type=int, default=4)
    parser.add_argument("--max-rounds", type=int, default=16)
    parser.add_argument("--max-tool-calls", type=int, default=32)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument(
        "--thinking-mode",
        choices=("omit", "enabled", "disabled"),
        default=None,
        help="Override the provider profile's default thinking mode",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="Default 32768 for every Message API endpoint profile",
    )
    parser.add_argument("--seed", type=int)
    return parser


def build_completion_options(
    config: GatewayConfig,
    args: argparse.Namespace,
) -> CompletionOptions:
    max_tokens = args.max_tokens or 32_768
    if max_tokens <= 0:
        raise ValueError("--max-tokens must be positive")
    return CompletionOptions(
        response_mode="auto",
        temperature=args.temperature,
        max_tokens=max_tokens,
        thinking_mode=args.thinking_mode or config.profile.default_thinking_mode,
        stream=config.profile.default_stream,
        seed=args.seed,
    )


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_gateway_config(args.profile)
        index_path = Path(args.bundle_index).resolve()
        index = _read(index_path)
        if not isinstance(index, dict) or not isinstance(index.get("bundles"), list):
            raise ValueError("bundle index must contain a bundles array")
        output_root = Path(args.out).resolve()
        source_roots = [Path(raw).resolve() for raw in args.source_root]
        missing_roots = [str(path) for path in source_roots if not path.is_dir()]
        if missing_roots:
            raise ValueError(f"source roots do not exist: {missing_roots}")
        allow_source = args.profile == "intranet"
        options = build_completion_options(config, args)
        records: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for bundle in index["bundles"]:
            if not isinstance(bundle, dict):
                continue
            failure_id = str(bundle.get("fingerprint") or "failure")
            if (
                bundle.get("investigation_eligible") is not True
                or bundle.get("investigation_lane") != "stable_root_cause"
                or bundle.get("replay_status") != "stable_same_failure"
                or not isinstance(bundle.get("stable_attempts"), int)
                or bundle.get("stable_attempts", 0) < 1
            ):
                skipped.append(
                    {
                        "failure_id": failure_id,
                        "reason": "not eligible for stable root-cause investigation",
                        "replay_status": bundle.get("replay_status"),
                    }
                )
                continue
            records.append(
                run_bundle_investigation(
                    config=config,
                    bundle=bundle,
                    output_root=output_root / failure_id,
                    source_roots=source_roots,
                    allow_source_content=allow_source,
                    role_ids=args.role or tuple(INVESTIGATOR_ROLES),
                    parallelism=args.parallelism,
                    max_rounds=args.max_rounds,
                    max_tool_calls=args.max_tool_calls,
                    completion_options=options,
                )
            )
        summary = {
            "schema_version": 1,
            "assessment_status": "candidate_only",
            "profile": args.profile,
            "source_content_enabled": allow_source,
            "bundle_count": len(records),
            "skipped_count": len(skipped),
            "skipped": skipped,
            "passed": sum(item.get("ok") is True for item in records),
            "failed": sum(item.get("ok") is not True for item in records),
            "records": records,
        }
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "investigation_index.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if all(item.get("ok") is True for item in records) else 1
    except (ConfigError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
