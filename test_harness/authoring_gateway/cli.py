"""Command-line entry point for the provider-neutral Message API gateway."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .client import CompletionOptions
from .config import PROFILE_SPECS, ConfigError, load_gateway_config
from .gateway import AuthoringGateway, GatewayError, TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[2]


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", required=True, choices=sorted(PROFILE_SPECS))
    parser.add_argument("--staging-root", default="artifacts/authoring_gateway")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--response-mode", choices=("auto", "json_schema", "json_object", "none"), default="auto")
    parser.add_argument("--thinking-mode", choices=("omit", "enabled", "disabled"), default="omit")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-repairs", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--request-timeout", type=float)
    parser.add_argument("--max-retries", type=int)
    parser.add_argument("--backoff-base", type=float)
    parser.add_argument("--max-retry-delay", type=float)
    parser.add_argument("--response-bytes-limit", type=int)
    parser.add_argument("--max-prompt-chars", type=int, default=250000)
    parser.add_argument("--max-repair-context-chars", type=int, default=120000)
    parser.add_argument("--system-prompt-file", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    task = subparsers.add_parser("task", help="Run one prompt/output-contract task")
    _add_common(task)
    task.add_argument("--task-id", required=True)
    task.add_argument("--prompt", required=True, help="UTF-8 prompt file inside the repository")
    task.add_argument("--output", required=True, help="Formal JSON path inside the repository")
    task.add_argument("--contract", default="", help="Output-contract JSON file inside the repository")
    task.add_argument(
        "--campaign-profiles",
        default="",
        help="JSON object mapping allowed campaign profile ids to args schemas",
    )
    task.add_argument("--allowed-kind", action="append", default=[])

    manifest = subparsers.add_parser("manifest", help="Run model_prompt_pack/model_task_manifest.json")
    _add_common(manifest)
    manifest.add_argument("manifest")
    manifest.add_argument("--task-id", action="append", default=[])
    manifest.add_argument("--stop-on-error", action="store_true")
    return parser


def _repo_path(value: str, label: str) -> Path:
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise GatewayError(f"{label} must stay inside repository root: {value}") from exc
    return resolved


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise GatewayError(f"{label} must contain one JSON object: {path}")
    return loaded


def _options(args: argparse.Namespace) -> CompletionOptions:
    return CompletionOptions(
        response_mode=args.response_mode,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        thinking_mode=args.thinking_mode,
        seed=args.seed,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_gateway_config(
            args.profile,
            request_timeout_seconds=args.request_timeout,
            max_retries=args.max_retries,
            backoff_base_seconds=args.backoff_base,
            max_retry_delay_seconds=args.max_retry_delay,
            response_bytes_limit=args.response_bytes_limit,
        )
        system_prompt = ""
        if args.system_prompt_file:
            system_prompt = _repo_path(args.system_prompt_file, "system_prompt_file").read_text(
                encoding="utf-8-sig"
            )
        gateway_kwargs: dict[str, Any] = {
            "repo_root": REPO_ROOT,
            "staging_root": args.staging_root,
            "max_prompt_chars": args.max_prompt_chars,
            "max_repair_context_chars": args.max_repair_context_chars,
        }
        if system_prompt:
            gateway_kwargs["system_prompt"] = system_prompt
        gateway = AuthoringGateway(config, **gateway_kwargs)
        options = _options(args)
        if args.command == "task":
            prompt_path = _repo_path(args.prompt, "prompt")
            output_path = _repo_path(args.output, "output")
            if args.contract:
                contract = _read_json_object(_repo_path(args.contract, "contract"), "contract")
            else:
                allowed = list(dict.fromkeys(args.allowed_kind))
                if not allowed:
                    raise GatewayError("task requires --contract or at least one --allowed-kind")
                contract = {"type": "json_object", "kind_field": "kind", "allowed_kinds": allowed}
            campaign_profiles = (
                _read_json_object(
                    _repo_path(args.campaign_profiles, "campaign_profiles"),
                    "campaign_profiles",
                )
                if args.campaign_profiles
                else {}
            )
            task = TaskSpec(
                task_id=args.task_id,
                prompt=prompt_path.read_text(encoding="utf-8-sig"),
                prompt_path=prompt_path.relative_to(REPO_ROOT).as_posix(),
                expected_output_path=output_path,
                output_contract=contract,
                allowed_campaign_profiles=campaign_profiles,
            )
            result = gateway.run_task(
                task,
                run_id=args.run_id or None,
                completion_options=options,
                max_repairs=args.max_repairs,
                overwrite=args.overwrite,
            )
            payload = result.as_dict()
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0 if result.ok else 1

        result = gateway.run_manifest(
            _repo_path(args.manifest, "manifest"),
            run_id=args.run_id or None,
            task_ids=args.task_id,
            completion_options=options,
            max_repairs=args.max_repairs,
            overwrite=args.overwrite,
            continue_on_error=not args.stop_on_error,
        )
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return 0 if result.ok else 1
    except (ConfigError, GatewayError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
