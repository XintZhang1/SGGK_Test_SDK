#!/usr/bin/env python3
"""Build small-model source-attack tasks from scan_source_risks.py output."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_harness.authoring_gateway.source_evidence import build_source_contract  # noqa: E402


SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_risk_report", help="source_risk_report.json or scan output directory")
    parser.add_argument("--out", required=True, help="Output directory for source attack tasks")
    parser.add_argument("--max-tasks", type=int, default=120, help="Maximum tasks to emit; 0 means all")
    parser.add_argument("--context-lines", type=int, default=12, help="Source lines before/after each finding")
    parser.add_argument("--min-severity", choices=["critical", "high", "medium", "low"], default="medium")
    parser.add_argument("--family", action="append", default=[], help="Only include suggested_attack_family value; can repeat")
    parser.add_argument("--include-category", action="append", default=[], help="Only include findings containing this category; can repeat")
    parser.add_argument("--task-prefix", default="sggk_src_task", help="Prefix for generated task_id values")
    parser.add_argument("--write-dsl-seeds", action="store_true", help="Write each task's dsl_seed to seed_dsl/<task_id>.json when present")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", text.strip()).strip("_").lower()
    return value or "task"


def report_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_dir():
        return path / "source_risk_report.json"
    return path


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def source_key(item: dict[str, Any]) -> tuple[str, int, str]:
    source = as_dict(item.get("source"))
    return (
        str(source.get("file") or "").replace("/", "\\").lower(),
        int(source.get("line") or 0),
        str(item.get("suggested_attack_family") or ""),
    )


def seed_index(seeds: list[Any]) -> dict[tuple[str, int, str], dict[str, Any]]:
    index: dict[tuple[str, int, str], dict[str, Any]] = {}
    fallback: dict[tuple[str, int, str], dict[str, Any]] = {}
    for seed in seeds:
        if not isinstance(seed, dict):
            continue
        key = source_key(seed)
        index.setdefault(key, seed)
        fallback.setdefault((key[0], key[1], ""), seed)
    index.update({key: value for key, value in fallback.items() if key not in index})
    return index


def resolve_source_path(source_file: str, scan_cwd: str, report_dir: Path) -> Path:
    raw = Path(source_file)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        if scan_cwd:
            candidates.append(Path(scan_cwd) / raw)
        candidates.append(Path.cwd() / raw)
        candidates.append(report_dir / raw)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve() if candidates else raw


def read_source_lines(path: Path) -> tuple[list[str], str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [], f"read_error:{exc.__class__.__name__}"
    try:
        text = data.decode("utf-8-sig")
        return text.splitlines(), ""
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace").splitlines(), "decode_replace"


def excerpt_for(path: Path, line_number: int, radius: int) -> dict[str, Any]:
    lines, warning = read_source_lines(path)
    if not lines or line_number <= 0:
        return {
            "path": str(path),
            "available": False,
            "warning": warning or "source_unavailable",
            "start_line": 0,
            "end_line": 0,
            "lines": [],
            "text": "",
        }
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    excerpt_lines = [
        {"line": number, "text": lines[number - 1]}
        for number in range(start, end + 1)
    ]
    text = "\n".join(f"{item['line']:5d}: {item['text']}" for item in excerpt_lines)
    return {
        "path": str(path),
        "available": True,
        "warning": warning,
        "start_line": start,
        "end_line": end,
        "lines": excerpt_lines,
        "text": text,
    }


def approved_source_root(report: dict[str, Any], source_path: Path) -> Path:
    scan = as_dict(report.get("scan"))
    scan_cwd = Path(str(scan.get("cwd") or Path.cwd())).resolve()
    matches: list[Path] = []
    for raw in as_list(scan.get("paths")):
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = scan_cwd / candidate
        try:
            resolved = candidate.resolve(strict=True)
            root = resolved if resolved.is_dir() else resolved.parent
            source_path.resolve(strict=True).relative_to(root)
            matches.append(root)
        except (OSError, ValueError):
            continue
    if not matches:
        raise ValueError(f"source file is outside the roots recorded by the scan: {source_path}")
    return max(matches, key=lambda item: len(item.parts))


def passes_filters(finding: dict[str, Any], args: argparse.Namespace) -> bool:
    severity = str(finding.get("severity") or "low")
    if SEVERITY_RANK.get(severity, 99) > SEVERITY_RANK[args.min_severity]:
        return False
    if args.family and str(finding.get("suggested_attack_family") or "") not in set(args.family):
        return False
    categories = set(str(item) for item in as_list(finding.get("categories")))
    if args.include_category and not categories.intersection(set(args.include_category)):
        return False
    return True


def task_id(prefix: str, index: int, finding: dict[str, Any]) -> str:
    source = as_dict(finding.get("source"))
    raw = f"{source.get('file')}:{source.get('line')}:{finding.get('id')}:{finding.get('suggested_attack_family')}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{slug(prefix)}_{index:04d}_{slug(str(finding.get('suggested_attack_family') or 'risk'))}_{digest}"


def model_prompt(finding: dict[str, Any]) -> str:
    source = as_dict(finding.get("source"))
    values = ", ".join(str(item.get("raw")) for item in as_list(finding.get("numeric_literals"))[:6]) or "none"
    categories = ", ".join(str(item) for item in as_list(finding.get("categories")))
    return (
        "Read the bound source excerpt, summarize the risky control flow, and produce SGGK attack DSL "
        "for the cited risk. Include source_review bound to the host-issued task, finding, source "
        "contract, opaque source references, and generated case IDs. Connect source references to "
        "risky branches, branches to at least two failure hypotheses, and hypotheses to test "
        "enhancements and cases. "
        "Use the exact source values where relevant, add nearby variants around geom_tol=1e-5 "
        "and topo_tol=1e-2, prefer legal adversarial geometry, and include hard validation "
        "oracles. The host owns every command and post-processing action. "
        f"Source: {source.get('file')}:{source.get('line')}. "
        f"Risk categories: {categories}. Numeric literals: {values}."
    )


def required_output_contract() -> dict[str, Any]:
    return {
        "format": "json_object",
        "allowed_kinds": ["attack_dsl", "cluster_seed", "needs_harness_extension"],
        "rules": [
            "Emit exactly one JSON object with no Markdown wrapper.",
            "Use needs_harness_extension when the harness does not support the API directly.",
            "Include stable chain operation id fields for generated topology provenance.",
            "Add measurable expectations for result bodies, properties, point/face/body relation, clash, distance, or exact plane-extreme checks.",
            "Do not emit commands, runners, paths, environment variables, or source patches.",
            "Bind source_review task/finding/contract hashes and opaque source references to the exact task values.",
            "Connect every generated case to a test enhancement through resolvable IDs.",
        ],
    }


def harness_context(scan: dict[str, Any]) -> dict[str, Any]:
    constants = as_dict(as_dict(scan.get("scan")).get("constants"))
    return {
        "topology_modeling_tolerance": constants.get("topology_modeling_tolerance", 1e-2),
        "geometry_tolerance": constants.get("geometry_tolerance", 1e-5),
        "max_model_size": constants.get("max_model_size", 5e5),
        "preferred_schema": "SGGK attack DSL v1",
        "preferred_runner": "sggk_case_runner",
        "verification": [
            "compile_attack_dsl.py --check --report for generated DSL structure validation",
            "compile_attack_dsl.py --out for reviewed DSL recipe emission",
            "run_recipes.py for process-isolated execution",
            "render_case_preview.py contact sheet for visual confirmation",
            "audit_case_geometry.py for duplicate and tolerance-band checks",
        ],
    }


def build_tasks(report: dict[str, Any], report_dir: Path, args: argparse.Namespace, out_dir: Path) -> list[dict[str, Any]]:
    scan_cwd = str(as_dict(report.get("scan")).get("cwd") or "")
    seed_by_source = seed_index(as_list(report.get("attack_seed_drafts")))
    findings = [
        finding for finding in as_list(report.get("findings"))
        if isinstance(finding, dict) and passes_filters(finding, args)
    ]
    if args.max_tasks:
        findings = findings[: args.max_tasks]
    tasks: list[dict[str, Any]] = []
    for index, finding in enumerate(findings, start=1):
        source = as_dict(finding.get("source"))
        source_file = str(source.get("file") or "")
        line_number = int(source.get("line") or 0)
        source_path = resolve_source_path(source_file, scan_cwd, report_dir)
        seed = seed_by_source.get(source_key(finding)) or seed_by_source.get((source_file.replace("/", "\\").lower(), line_number, ""))
        tid = task_id(args.task_prefix, index, finding)
        excerpt = excerpt_for(source_path, line_number, args.context_lines)
        if not excerpt.get("available"):
            raise ValueError(f"cannot build a source task without a readable excerpt: {source_path}")
        source_root = approved_source_root(report, source_path)
        contract, host_bindings = build_source_contract(
            task_id=tid,
            finding=finding,
            source_path=source_path,
            source_root=source_root,
            line_start=int(excerpt["start_line"]),
            line_end=int(excerpt["end_line"]),
        )
        source_ref = contract["source_refs"][0]
        excerpt["path"] = source_ref["relative_path"]
        excerpt_sha256 = str(source_ref["content_sha256"])
        task: dict[str, Any] = {
            "schema_version": 1,
            "task_id": tid,
            "task_type": "sggk_source_attack",
            "data_classification": "proprietary_source",
            "allowed_profile_categories": ["intranet"],
            "review_required": True,
            "source_ref": source_ref["source_ref_id"],
            "source_contract": contract,
            "host_source_bindings": host_bindings,
            "source_excerpt": excerpt,
            "source_excerpt_sha256": excerpt_sha256,
            "finding": finding,
            "seed_draft": seed,
            "model_prompt": model_prompt(finding),
            "required_output": required_output_contract(),
            "harness_context": harness_context(report),
            "post_generation_checks": [
                "Read the cited source before trusting the generated attack.",
                "Run compile_attack_dsl.py --check --report before compiling or running generated DSL.",
                "Render previews and inspect contact sheets before filing bugs.",
                "Run geometry audit for tolerance families.",
                "When a modeling oracle fails and topo tracking is missing, record missing topo tracking as diagnostic context.",
            ],
        }
        if args.write_dsl_seeds and isinstance(seed, dict) and isinstance(seed.get("dsl_seed"), dict):
            dsl_seed = seed_dsl_with_task_metadata(seed["dsl_seed"], tid, task, out_dir)
            dsl_path = out_dir / "seed_dsl" / f"{tid}.json"
            write_json(dsl_path, dsl_seed)
            task["seed_dsl_path"] = str(dsl_path)
        tasks.append(task)
    return tasks


def seed_dsl_with_task_metadata(dsl_seed: dict[str, Any], task_id: str, task: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    result = deepcopy(dsl_seed)
    context = as_dict(task.get("harness_context"))
    constants = result.get("constants")
    if not isinstance(constants, dict):
        constants = {}
    constants.setdefault("topo_tol", context.get("topology_modeling_tolerance", 1e-2))
    constants.setdefault("geom_tol", context.get("geometry_tolerance", 1e-5))
    constants.setdefault("max_model_size", context.get("max_model_size", 5e5))
    result["constants"] = constants
    finding = as_dict(task.get("finding"))
    source = as_dict(finding.get("source"))
    metadata = {
        "source_task_id": task_id,
        "source_task_path": str(out_dir / "source_attack_tasks.jsonl"),
        "source_risk_id": as_str(finding.get("id")),
        "source_risk_family": as_str(finding.get("suggested_attack_family")),
        "source_risk_categories": ",".join(str(item) for item in as_list(finding.get("categories"))),
        "source_ref": f"{source.get('file')}:{source.get('line')}",
    }
    cases = result.get("cases")
    if not isinstance(cases, list):
        return result
    for case in cases:
        if not isinstance(case, dict):
            continue
        case_metadata = case.get("metadata")
        if not isinstance(case_metadata, dict):
            case_metadata = {}
        case_metadata = {**metadata, **case_metadata}
        case["metadata"] = case_metadata
        case.setdefault("source_ref", metadata["source_ref"])
    return result


def markdown_manifest(tasks: list[dict[str, Any]], report_path_value: Path) -> str:
    severity_counts = Counter(str(as_dict(task.get("finding")).get("severity") or "") for task in tasks)
    family_counts = Counter(str(as_dict(task.get("finding")).get("suggested_attack_family") or "") for task in tasks)
    lines = [
        "# Source Attack Tasks",
        "",
        f"- Source risk report: `{report_path_value}`",
        f"- Tasks: `{len(tasks)}`",
        "",
        "## Severity Counts",
        "",
    ]
    for key, value in sorted(severity_counts.items()):
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Family Counts", ""])
    for key, value in sorted(family_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Tasks", ""])
    for task in tasks[:60]:
        finding = as_dict(task.get("finding"))
        source = as_dict(finding.get("source"))
        lines.append(
            f"- `{task.get('task_id')}` `{finding.get('severity')}` `{finding.get('suggested_attack_family')}` "
            f"`{source.get('file')}:{source.get('line')}`"
        )
        if task.get("seed_dsl_path"):
            lines.append(f"  - seed DSL: `{task['seed_dsl_path']}`")
    lines.extend(
        [
            "",
            "## Suggested Loop",
            "",
            "```powershell",
            "python .\\test_harness\\tools\\compile_attack_dsl.py <reviewed_task_dsl.json> --check --report .\\artifacts\\source_task_dsl_checks\\reviewed_task_check.json",
            "python .\\test_harness\\tools\\compile_attack_dsl.py <reviewed_task_dsl.json> --out .\\artifacts\\compiled_source_task",
            "python .\\test_harness\\tools\\run_recipes.py --runner .\\build\\test_harness\\Release\\sggk_case_runner.exe --recipe .\\artifacts\\compiled_source_task --out .\\artifacts\\source_task_run --triage-out .\\artifacts\\source_task_triage --preview-out .\\artifacts\\source_task_preview --geometry-audit-out .\\artifacts\\source_task_audit",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.max_tasks < 0:
        print("--max-tasks must be >= 0")
        return 1
    if args.context_lines < 0:
        print("--context-lines must be >= 0")
        return 1
    path = report_path(args.source_risk_report)
    report = load_json(path)
    if not isinstance(report, dict):
        print(f"invalid report: {path}")
        return 1
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = build_tasks(report, path.parent, args, out_dir)
    payload = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "source_risk_report": str(path),
        "task_count": len(tasks),
        "tasks": tasks,
    }
    write_json(out_dir / "source_attack_tasks.json", payload)
    with (out_dir / "source_attack_tasks.jsonl").open("w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False) + "\n")
    (out_dir / "source_attack_task_manifest.md").write_text(markdown_manifest(tasks, path), encoding="utf-8")
    (out_dir / "source_attack_task_ids.txt").write_text(
        "\n".join(str(task["task_id"]) for task in tasks) + ("\n" if tasks else ""),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "tasks": len(tasks),
                "out": str(out_dir),
                "jsonl": str(out_dir / "source_attack_tasks.jsonl"),
                "manifest": str(out_dir / "source_attack_task_manifest.md"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
