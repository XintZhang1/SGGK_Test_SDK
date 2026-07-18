#!/usr/bin/env python3
"""Advisory vision-model review of executed-case geometry previews.

Sends bounded host-rendered geometry previews (``report/preview.png``) to the
configured vision profile (SiliconFlow Qwen2.5-VL) and stages a
``visual_review_report`` JSON plus a Chinese markdown summary. The output is
advisory evidence only: it never gates, approves, executes, or alters any
candidate, verdict, or state machine. Only the fixed prompt and the geometry
renders leave the host — never recipe JSON, paths, or credentials.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from test_harness.authoring_gateway.client import ClientError
from test_harness.authoring_gateway.config import ConfigError, load_gateway_config
from test_harness.authoring_gateway.gateway import AuthoringGateway, GatewayError, TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "test_harness" / "schemas" / "visual_review_report.schema.json"
RENDER_TOOL = Path(__file__).resolve().with_name("render_case_preview.py")
DEFAULT_PROFILE = "siliconflow_vision"
DEFAULT_MAX_CASES = 4
HARD_MAX_CASES = 8
RENDER_TIMEOUT_SECONDS = 300
ADVISORY_NOTE = "咨询性意见，仅供参考；不参与门禁、批准、执行或失败归因"

PLAUSIBILITY_ZH = {"plausible": "合理", "suspect": "存疑", "implausible": "不合理"}
CONSISTENCY_ZH = {"consistent": "一致", "inconsistent": "不一致", "unclear": "不明确"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", help="Case capsule root; uses each case's report/preview.png")
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Standalone image path (repeatable; case_id is the file stem). Overrides --cases-root.",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="Vision provider profile name")
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory for visual_review_report.json/.zh-CN.md (must stay under artifacts/)",
    )
    parser.add_argument(
        "--max-cases",
        type=int,
        default=DEFAULT_MAX_CASES,
        help=f"Maximum cases/images per review (default {DEFAULT_MAX_CASES}, hard cap {HARD_MAX_CASES})",
    )
    parser.add_argument(
        "--render-missing",
        action="store_true",
        help="Render report/preview.png via render_case_preview.py for cases missing it",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.max_cases <= HARD_MAX_CASES:
        parser.error(f"--max-cases must be between 1 and {HARD_MAX_CASES}")
    if not args.image and not args.cases_root:
        parser.error("either --cases-root or at least one --image is required")
    return args


def _is_case_dir(path: Path) -> bool:
    return (path / "manifest.json").is_file() and (path / "report").is_dir()


def _case_id(case_dir: Path) -> str:
    try:
        manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return case_dir.name
    value = manifest.get("case_id")
    return value.strip() if isinstance(value, str) and value.strip() else case_dir.name


def scan_case_dirs(cases_root: Path, limit: int) -> list[Path]:
    """Find case capsule dirs (mirrors render_case_preview.iter_case_dirs)."""

    cases: set[Path] = set()
    if _is_case_dir(cases_root):
        cases.add(cases_root.resolve())
    for manifest in cases_root.rglob("manifest.json"):
        case_dir = manifest.parent
        if "_recipes" not in case_dir.parts and _is_case_dir(case_dir):
            cases.add(case_dir.resolve())
    return sorted(cases, key=lambda item: str(item).lower())[:limit]


def render_missing_previews(case_dirs: list[Path]) -> list[str]:
    """Render previews for cases missing them via one fixed subprocess call."""

    missing = [case_dir for case_dir in case_dirs if not (case_dir / "report" / "preview.png").is_file()]
    if not missing:
        return []
    command = [sys.executable, str(RENDER_TOOL), *(str(case_dir) for case_dir in missing)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"预览渲染执行失败：{exc}"]
    notes: list[str] = []
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-200:]
        notes.append(f"预览渲染返回 rc={completed.returncode}：{tail}")
    still_missing = [
        case_dir.name for case_dir in missing if not (case_dir / "report" / "preview.png").is_file()
    ]
    notes.extend(f"用例 {name} 仍无预览图，已跳过" for name in still_missing)
    return notes


def build_prompt(images: list[tuple[str, Path]]) -> str:
    """The fixed bounded Chinese instruction; case order matches image order."""

    listing = "\n".join(f"{index}. case_id = \"{case_id}\"" for index, (case_id, _) in enumerate(images, 1))
    return f"""你是 SGGK 几何内核测试 Harness 的视觉复核助手。随后是 {len(images)} 张测试用例几何预览图，
顺序与下面用例清单一一对应。每张图包含 ISO 与 XY/XZ/YZ 三个正交视图：
蓝色=target，橙色=tool，绿色=result，并打印了 bbox 数值与校验状态。

用例顺序（第 N 张图对应第 N 行）：
{listing}

请逐例判断：
- geometry_plausibility：target/tool/result 的位置、尺寸与数量关系在几何上是否符合建模/布尔操作直觉；
- view_consistency：ISO 与三个正交视图是否描绘同一几何，并与打印的 bbox 数值相符；
- misuse_flags：明显误用，只能取自 tool_misplaced / scale_suspect / empty_result / view_mismatch / other，
  无则空数组；数组元素只能是字符串，禁止 null。

只返回一个 JSON 对象（不要 markdown 代码块或多余文字），结构严格为：
{{"kind":"visual_review_report","schema_version":1,
"case_reviews":[{{"case_id":"…","geometry_plausibility":"plausible|suspect|implausible",
"view_consistency":"consistent|inconsistent|unclear","misuse_flags":["…"],"confidence":0.0,
"notes_zh_cn":"≤500字中文说明"}}],"overall_notes_zh_cn":"≤1000字中文总体说明"}}

case_reviews 必须覆盖清单中的每一个 case_id，不多不少，且不得包含其他字段。
你的判断仅为咨询性参考，不能改变任何测试结论；不要返回命令、路径、批准或执行指令。"""


def summarize(report: dict[str, Any]) -> dict[str, int]:
    reviews = [item for item in report.get("case_reviews", []) if isinstance(item, dict)]
    return {
        "reviewed": len(reviews),
        "plausible": sum(1 for item in reviews if item.get("geometry_plausibility") == "plausible"),
        "suspect": sum(1 for item in reviews if item.get("geometry_plausibility") == "suspect"),
        "implausible": sum(1 for item in reviews if item.get("geometry_plausibility") == "implausible"),
        "flags": sum(
            len([flag for flag in item.get("misuse_flags", []) if isinstance(flag, str)])
            for item in reviews
            if isinstance(item.get("misuse_flags"), list)
        ),
    }


def render_markdown(report: dict[str, Any], profile: str) -> str:
    summary = summarize(report)
    lines = [
        "# 视觉模型复核报告（咨询性意见，仅供参考）",
        "",
        f"> 本报告由视觉模型（profile `{profile}`）对宿主渲染的几何预览图作出的判断生成，"
        "不参与任何门禁、批准、执行或失败归因结论。",
        "",
        f"- 复核用例：`{summary['reviewed']}` 例",
        f"- 合理 `{summary['plausible']}` / 存疑 `{summary['suspect']}` / 不合理 `{summary['implausible']}`",
        f"- 误用标记合计：`{summary['flags']}` 项",
        "",
        "## 用例明细",
        "",
    ]
    for review in report.get("case_reviews", []):
        if not isinstance(review, dict):
            continue
        plausibility_raw = str(review.get("geometry_plausibility") or "")
        consistency_raw = str(review.get("view_consistency") or "")
        plausibility = PLAUSIBILITY_ZH.get(plausibility_raw, plausibility_raw or "未知")
        consistency = CONSISTENCY_ZH.get(consistency_raw, consistency_raw or "未知")
        flags = review.get("misuse_flags") if isinstance(review.get("misuse_flags"), list) else []
        flag_text = "、".join(f"`{flag}`" for flag in flags) if flags else "无"
        confidence = review.get("confidence")
        confidence_text = f"{float(confidence):.2f}" if isinstance(confidence, int | float) else "—"
        lines.extend(
            [
                f"### `{review.get('case_id', '')}`",
                "",
                f"- 合理性：**{plausibility}**",
                f"- 视图一致性：**{consistency}**",
                f"- 误用标记：{flag_text}",
                f"- 置信度：`{confidence_text}`",
                f"- 说明：{review.get('notes_zh_cn', '')}",
                "",
            ]
        )
    lines.extend(["## 总体说明", "", str(report.get("overall_notes_zh_cn") or "（无）"), ""])
    return "\n".join(lines)


def collect_images(args: argparse.Namespace) -> tuple[list[tuple[str, Path]], list[str]]:
    """Resolve (case_id, image_path) pairs plus human-facing skip notes."""

    notes: list[str] = []
    if args.image:
        images: list[tuple[str, Path]] = []
        for raw in args.image[: args.max_cases]:
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                notes.append(f"图片不存在，已跳过：{raw}")
                continue
            images.append((path.stem, path))
        return images, notes
    cases_root = Path(args.cases_root).expanduser()
    if not cases_root.is_absolute():
        cases_root = (REPO_ROOT / cases_root).resolve()
    if not cases_root.is_dir():
        raise GatewayError(f"cases root 不存在：{cases_root}")
    case_dirs = scan_case_dirs(cases_root, args.max_cases)
    if args.render_missing:
        notes.extend(render_missing_previews(case_dirs))
    images = []
    for case_dir in case_dirs:
        preview = case_dir / "report" / "preview.png"
        if preview.is_file():
            images.append((_case_id(case_dir), preview))
        else:
            notes.append(f"用例 {case_dir.name} 无 report/preview.png，已跳过")
    return images, notes


def run(args: argparse.Namespace, *, gateway: AuthoringGateway) -> int:
    out_dir = Path(args.out).expanduser()
    if not out_dir.is_absolute():
        out_dir = (REPO_ROOT / out_dir).resolve()
    images, notes = collect_images(args)
    for note in notes:
        print(f"提示：{note}")
    if not images:
        print("没有可复核的几何预览图；未发起视觉模型调用。")
        return 2
    task = TaskSpec(
        task_id="visual_review",
        task_type="visual_review",
        prompt=build_prompt(images),
        expected_output_path=out_dir / "visual_review_report.json",
        output_contract={
            "type": "json_object",
            "kind_field": "kind",
            "allowed_kinds": ["visual_review_report"],
        },
        image_paths=tuple(str(path) for _, path in images),
        metadata={
            "provider_profile": args.profile,
            "provider_profile_category": "external",
            "data_classification": "public_interface",
            "allowed_profile_categories": ["external"],
        },
    )
    try:
        result = gateway.run_task(task, max_repairs=1, overwrite=True)
    except (GatewayError, ClientError, OSError) as exc:
        print(f"视觉复核任务无法执行：{exc}")
        return 2
    if not result.ok:
        detail = "；".join(str(item.get("message", "")) for item in result.diagnostics[:3] if isinstance(item, dict))
        print(f"视觉复核未通过固定契约：{result.error or detail}")
        print(f"原始证据（受限保存，供诊断）：{result.staging_path}")
        return 1
    report_path = gateway.repo_root / result.promoted_path
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"视觉复核报告不可解析：{exc}")
        return 1
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(report),
        key=lambda error: list(error.path),
    )
    if schema_errors:
        first = schema_errors[0]
        print(f"视觉复核报告未通过固定 schema：{list(first.path)}: {first.message[:200]}")
        print(f"原始证据（受限保存，供诊断）：{result.staging_path}")
        return 1
    markdown_path = out_dir / "visual_review_report.zh-CN.md"
    markdown_path.write_text(render_markdown(report, args.profile), encoding="utf-8")
    summary = summarize(report)
    print(
        f"视觉复核完成：复核 {summary['reviewed']} 例；合理 {summary['plausible']} / "
        f"存疑 {summary['suspect']} / 不合理 {summary['implausible']}；误用标记 {summary['flags']} 项"
        f"（{ADVISORY_NOTE}）→ {result.promoted_path}"
    )
    return 0


def main(argv: list[str] | None = None, *, gateway: AuthoringGateway | None = None) -> int:
    args = parse_args(argv)
    if gateway is None:
        try:
            config = load_gateway_config(args.profile)
        except ConfigError as exc:
            print(f"视觉模型 profile 未配置：{exc}")
            return 2
        gateway = AuthoringGateway(config, repo_root=REPO_ROOT)
    return run(args, gateway=gateway)


if __name__ == "__main__":
    sys.exit(main())
