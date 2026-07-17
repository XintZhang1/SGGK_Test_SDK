"""Friendly CLI: public function in, natural-language review comments out."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Sequence

from test_harness.authoring_gateway.config import DEFAULT_PROFILE, PROFILE_SPECS, ConfigError

from .runtime import MessageApiRuntime
from .workflow import HarnessWorkflow, WorkflowError


REPO_ROOT = Path(__file__).resolve().parents[2]


def _runner_from_environment() -> Path | None:
    configured = os.environ.get("SGGK_CASE_RUNNER", "").strip()
    if configured:
        return Path(configured).resolve()
    default = REPO_ROOT / "build" / "test_harness" / "Release" / "sggk_case_runner.exe"
    return default.resolve() if default.is_file() else None


def _sdk_dir_from_environment() -> Path | None:
    value = os.environ.get("SGGK_SDK_DIR", "").strip()
    return Path(value).resolve() if value else None


def _source_root_from_environment() -> Path | None:
    value = os.environ.get("SGGK_SOURCE_ROOT", "").strip()
    return Path(value).resolve() if value else None


class _OfflineRuntime:
    """Placeholder used by read-only status/show commands without API config."""

    def __init__(self, profile: str) -> None:
        self.provider_profile = profile
        profile_spec = PROFILE_SPECS.get(profile)
        self.provider_profile_category = profile_spec.category if profile_spec is not None else ""
        # Plain-data attribute mirrored from MessageApiRuntime so read-only
        # status/show commands can construct the workflow without API config.
        self.campaign_dataset = ""

    def __getattr__(self, name: str) -> Any:
        raise WorkflowError(f"runtime operation {name} is unavailable in read-only mode")


def _workflow(*, require_runtime: bool = True) -> HarnessWorkflow:
    profile = os.environ.get("SGGK_HARNESS_PROFILE", DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
    profile_spec = PROFILE_SPECS.get(profile)
    default_thinking_mode = profile_spec.default_thinking_mode if profile_spec else "omit"
    runtime: Any
    if require_runtime:
        runtime = MessageApiRuntime(
            repo_root=REPO_ROOT,
            profile=profile,
            candidate_count=int(os.environ.get("SGGK_HARNESS_CANDIDATES", "3")),
            candidate_parallelism=int(os.environ.get("SGGK_HARNESS_PARALLELISM", "3")),
            max_tokens=int(os.environ.get("SGGK_HARNESS_MAX_TOKENS", "32768")),
            thinking_mode=os.environ.get("SGGK_HARNESS_THINKING", default_thinking_mode),
            jobs=int(os.environ.get("SGGK_HARNESS_JOBS", "1")),
            execution_timeout_seconds=float(os.environ.get("SGGK_HARNESS_TIMEOUT", "180")),
            campaign_dataset=os.environ.get("SGGK_CAMPAIGN_DATASET", ""),
        )
    else:
        runtime = _OfflineRuntime(profile)
    return HarnessWorkflow(
        runtime,
        repo_root=REPO_ROOT,
        profile=profile,
        sdk_dir=_sdk_dir_from_environment(),
        source_root=_source_root_from_environment(),
        runner_path=_runner_from_environment(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SGGK Message API Harness：用户只输入接口名和自然语言审查意见。"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="输入一个 public function 并生成第 1 轮审查")
    start.add_argument("public_function")
    comment = commands.add_parser("comment", help="提交一条自然语言审查意见")
    comment.add_argument("text")
    commands.add_parser("status", help="查看当前会话状态")
    commands.add_parser("show", help="显示当前审查报告或最终报告")
    commands.add_parser("retry", help="重试未完成的已批准执行，不改变候选")
    return parser


def _print_payload(payload: dict[str, Any]) -> None:
    state_names = {
        "awaiting_comment": "等待自然语言审查意见",
        "completed": "测试完成",
        "execution_failed": "执行未完成/失败",
        "rejected": "任务已拒绝",
    }
    print(f"接口：{payload.get('public_function', '')}")
    print(f"状态：{state_names.get(str(payload.get('state')), payload.get('state', ''))}")
    if payload.get("current_round"):
        print(f"当前轮次：第 {payload['current_round']} 轮（由 Harness 管理）")
    if payload.get("review_report_path"):
        print(f"审查报告：{payload['review_report_path']}")
    if payload.get("final_report_path"):
        print(f"最终报告：{payload['final_report_path']}")
    if payload.get("answer_path"):
        print(f"模型回答：{payload['answer_path']}")
    if payload.get("notice_path"):
        print(f"提示：{payload['notice_path']}")
    if payload.get("last_error"):
        print(f"错误摘要：{payload['last_error']}")
    if payload.get("state") == "awaiting_comment":
        print('下一步：.\\harness.ps1 comment "你的自然语言意见"')


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workflow = _workflow(require_runtime=args.command not in {"status", "show"})
        if args.command == "start":
            payload = workflow.start(args.public_function)
        elif args.command == "comment":
            payload = workflow.comment(args.text)
        elif args.command == "status":
            payload = workflow.status()
        elif args.command == "show":
            path = workflow.show()
            print(f"报告：{path}")
            print(path.read_text(encoding="utf-8-sig"))
            return 0
        elif args.command == "retry":
            payload = workflow.retry()
        else:  # pragma: no cover - argparse owns the command set
            raise WorkflowError(f"unsupported command: {args.command}")
        _print_payload(dict(payload))
        return 0 if payload.get("state") != "execution_failed" else 1
    except (ConfigError, WorkflowError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Harness 无法继续：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
