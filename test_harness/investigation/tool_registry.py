"""Strict read-only evidence tools available to Qwen investigation roles."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

from .contracts import validate_tool_args


SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl"}
QUERY_RE = re.compile(r"^[A-Za-z0-9_:~<>.,*&() +\-]{2,80}$")


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in value).strip("_")
    return safe[:64] or "item"


def _inside(root: Path, raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    candidates = [path.resolve()] if path.is_absolute() else [(Path.cwd() / path).resolve(), (root / path).resolve()]
    for resolved in candidates:
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _bbox_gap(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    a_min, a_max, b_min, b_max = a.get("min"), a.get("max"), b.get("min"), b.get("max")
    values = (a_min, a_max, b_min, b_max)
    if not all(
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
        for value in values
    ):
        return {"available": False, "reason": "input bounding boxes are unavailable"}
    gaps = [
        max(0.0, float(b_min[index]) - float(a_max[index]), float(a_min[index]) - float(b_max[index]))
        for index in range(3)
    ]
    overlaps = [
        max(0.0, min(float(a_max[index]), float(b_max[index])) - max(float(a_min[index]), float(b_min[index])))
        for index in range(3)
    ]
    return {
        "available": True,
        "axis_gaps": gaps,
        "axis_overlaps": overlaps,
        "bbox_distance": sum(value * value for value in gaps) ** 0.5,
        "bbox_overlap_all_axes": all(value == 0.0 for value in gaps),
        "evidence_quality": "deterministic_bbox_relation_not_exact_topology_contact",
    }


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    description: str
    args_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "description": self.description,
            "args_schema": self.args_schema,
        }


class InvestigationToolRegistry:
    def __init__(
        self,
        *,
        bundle_record: dict[str, Any],
        source_roots: list[Path],
        allow_source_content: bool,
    ) -> None:
        self.bundle_record = bundle_record
        self.bundle_dir = Path(str(bundle_record.get("bundle_dir") or "")).resolve()
        if not self.bundle_dir.is_dir():
            raise ValueError(f"bundle directory does not exist: {self.bundle_dir}")
        manifest_path = _inside(self.bundle_dir, bundle_record.get("bundle_manifest"))
        localization_path = _inside(self.bundle_dir, bundle_record.get("localization_summary"))
        if manifest_path is None or localization_path is None:
            raise ValueError("bundle manifest/localization path must stay inside bundle directory")
        self.manifest = _dict(_read(manifest_path))
        self.localization = _dict(_read(localization_path))
        self.failure_id = _safe_id(
            str(bundle_record.get("fingerprint") or self.manifest.get("fingerprint") or "failure")
        )
        self.source_roots = [path.resolve() for path in source_roots if path.is_dir()]
        self.allow_source_content = allow_source_content
        self.source_refs: dict[str, dict[str, Any]] = {}
        self.report_paths: dict[str, Path] = {}
        self.reproduction_ref_ids = {f"repro_{self.failure_id}"}
        signature = _dict(self.manifest.get("failure_signature"))
        signature_hash = hashlib.sha256(
            json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        self.signature_id = f"sig_{signature_hash}"
        copied = _dict(self.manifest.get("copied"))
        for name, raw in _dict(copied.get("reports")).items():
            path = _inside(self.bundle_dir, raw)
            if path is not None:
                report_id = _safe_id(str(name))
                self.report_paths[report_id] = path
        for name, raw in _dict(copied.get("topotrack_probe")).items():
            path = _inside(self.bundle_dir, raw)
            if path is not None:
                report_id = _safe_id(f"topotrack_probe_{name}")
                self.report_paths[report_id] = path
        replay = _dict(self.manifest.get("replay"))
        self.replay_status = str(replay.get("status") or bundle_record.get("replay_status") or "")
        stable_attempts = replay.get("stable_attempts", bundle_record.get("stable_attempts", 0))
        self.stable_attempts = (
            int(stable_attempts)
            if isinstance(stable_attempts, int) and not isinstance(stable_attempts, bool)
            else 0
        )
        attempt_count = replay.get("attempt_count")
        eligibility = _dict(self.manifest.get("investigation_eligibility"))
        signature = _dict(self.manifest.get("failure_signature"))
        if (
            self.replay_status != "stable_same_failure"
            or self.stable_attempts < 1
            or replay.get("signature_verified") is not True
            or attempt_count != self.stable_attempts
            or eligibility.get("root_cause") is not True
            or not signature.get("kind")
        ):
            raise ValueError(
                "root-cause investigation requires a verified stable_same_failure manifest, "
                "matching host-derived attempt count, immutable signature, and root-cause eligibility"
            )
        self.tools = self._build_tools()

    @property
    def tool_ids(self) -> set[str]:
        return set(self.tools)

    @property
    def source_ref_ids(self) -> set[str]:
        return set(self.source_refs)

    def catalog(self) -> list[dict[str, Any]]:
        return [self.tools[key].catalog_entry() for key in sorted(self.tools)]

    def _build_tools(self) -> dict[str, ToolSpec]:
        return {
            "failure.get_summary": ToolSpec(
                "failure.get_summary",
                "Return the bound failure signature, SDK status, oracle failures, and qualification context.",
                {"type": "object", "additionalProperties": False},
                self._failure_summary,
            ),
            "failure.get_reproduction": ToolSpec(
                "failure.get_reproduction",
                "Return immutable recipe/replay references and the stable expected signature id.",
                {"type": "object", "additionalProperties": False},
                self._reproduction,
            ),
            "failure.get_topotrack": ToolSpec(
                "failure.get_topotrack",
                "Return TopoTrack summary, ancestry evidence, and whether tracking was skipped or incomplete.",
                {"type": "object", "additionalProperties": False},
                self._topotrack,
            ),
            "geometry.get_bbox_relation": ToolSpec(
                "geometry.get_bbox_relation",
                "Compute deterministic target/tool bounding-box gaps; this is not exact contact proof.",
                {"type": "object", "additionalProperties": False},
                self._geometry_bbox,
            ),
            "artifact.list_reports": ToolSpec(
                "artifact.list_reports",
                "List report ids available inside the immutable failure bundle.",
                {"type": "object", "additionalProperties": False},
                self._list_reports,
            ),
            "artifact.read_report": ToolSpec(
                "artifact.read_report",
                "Read one allowlisted JSON report by report_id; filesystem paths are not accepted.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["report_id"],
                    "properties": {"report_id": {"type": "string", "maxLength": 96}},
                },
                self._read_report,
            ),
            "source.search_literal": ToolSpec(
                "source.search_literal",
                "Search trusted source snapshots for a bounded literal and return opaque source_ref_ids.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string", "minLength": 2, "maxLength": 80},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                },
                self._source_search,
            ),
            "source.read_excerpt": ToolSpec(
                "source.read_excerpt",
                "Read bounded context for a source_ref_id returned by source.search_literal.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_ref_id"],
                    "properties": {
                        "source_ref_id": {"type": "string", "maxLength": 96},
                        "before": {"type": "integer", "minimum": 0, "maximum": 80},
                        "after": {"type": "integer", "minimum": 0, "maximum": 80},
                    },
                },
                self._source_excerpt,
            ),
        }

    def execute(self, tool_id: str, args: Any) -> dict[str, Any]:
        spec = self.tools.get(tool_id)
        if spec is None:
            return {"ok": False, "error": f"unregistered tool {tool_id!r}"}
        if not isinstance(args, dict):
            return {"ok": False, "error": "tool args must be an object"}
        try:
            return {"ok": True, "tool_id": tool_id, "result": spec.handler(args)}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "tool_id": tool_id, "error": str(exc)}

    def _require_empty(self, args: dict[str, Any]) -> None:
        errors = validate_tool_args(args, required={})
        if errors:
            raise ValueError("; ".join(errors))

    def _failure_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_empty(args)
        return {
            "failure_id": self.failure_id,
            "fingerprint": self.manifest.get("fingerprint"),
            "case_id": self.manifest.get("representative_case_id"),
            "api": self.manifest.get("api"),
            "reasons": self.manifest.get("reasons", []),
            "status": self.manifest.get("status", {}),
            "failure_signature": self.manifest.get("failure_signature", {}),
            "signature_id": self.signature_id,
            "validation_failures": self.manifest.get("validation_failures", []),
            "validation_oracle_details": self.manifest.get("validation_oracle_details", []),
            "roundtrip_failures": self.manifest.get("roundtrip_failures", []),
            "replay": self.manifest.get("replay", {}),
            "stable_attempts": self.stable_attempts,
            "assessment_status": "candidate_only",
        }

    def _reproduction(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_empty(args)
        copied = _dict(self.manifest.get("copied"))
        recipes = {
            key: Path(str(value)).name
            for key, value in _dict(copied.get("recipes")).items()
            if isinstance(value, str) and _inside(self.bundle_dir, value) is not None
        }
        return {
            "reproduction_ref_id": next(iter(self.reproduction_ref_ids)),
            "expected_signature_id": self.signature_id,
            "failure_signature": self.manifest.get("failure_signature", {}),
            "replay": self.manifest.get("replay", {}),
            "stable_attempts": self.stable_attempts,
            "recipes": recipes,
            "fixed_reproduce_script_available": bool(
                _inside(self.bundle_dir, copied.get("reproduce_script"))
            ),
        }

    def _topotrack(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_empty(args)
        report = _dict(_read(self.report_paths.get("topo_track_summary_json", Path())))
        raw = _dict(_read(self.report_paths.get("topo_track_json", Path())))
        isolated_report = _dict(
            _read(self.report_paths.get("topotrack_probe_topo_track_summary_json", Path()))
        )
        isolated_raw = _dict(
            _read(self.report_paths.get("topotrack_probe_topo_track_json", Path()))
        )
        isolated_items = _list(isolated_raw.get("items"))[:32]
        return {
            "available": bool(report or raw or isolated_report or isolated_raw),
            "summary": report,
            "tracking_items": isolated_items or _list(raw.get("items"))[:32],
            "tracking_items_source": (
                "isolated_paired_capture" if isolated_items else "primary_safe_run"
            ),
            "isolated_capture": {
                "available": bool(isolated_report or isolated_raw),
                "summary": isolated_report,
                "tracking_items": isolated_items,
            },
            "localization_topotrack": self.localization.get("topo_track", {}),
            "isolated_probe": self.manifest.get("topotrack_probe", {}),
            "evidence_quality": "diagnostic_not_causal_proof",
        }

    def _geometry_bbox(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_empty(args)
        properties = _dict(_read(self.report_paths.get("input_properties_json", Path())))
        target = _list(properties.get("target"))
        tool = _list(properties.get("tool"))
        target_bbox = _dict(target[0].get("bbox")) if target and isinstance(target[0], dict) else {}
        tool_bbox = _dict(tool[0].get("bbox")) if tool and isinstance(tool[0], dict) else {}
        return _bbox_gap(target_bbox, tool_bbox)

    def _list_reports(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_empty(args)
        return {"report_ids": sorted(self.report_paths)}

    def _read_report(self, args: dict[str, Any]) -> dict[str, Any]:
        errors = validate_tool_args(args, required={"report_id": str})
        if errors:
            raise ValueError("; ".join(errors))
        report_id = args["report_id"]
        path = self.report_paths.get(report_id)
        if path is None:
            raise ValueError(f"unknown report_id {report_id!r}")
        if path.stat().st_size > 256_000:
            return {
                "report_id": report_id,
                "available": True,
                "truncated": True,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        return {"report_id": report_id, "available": True, "content": _read(path)}

    def _source_search(self, args: dict[str, Any]) -> dict[str, Any]:
        errors = validate_tool_args(args, required={"query": str}, optional={"max_results": int})
        if errors:
            raise ValueError("; ".join(errors))
        query = args["query"]
        if not QUERY_RE.fullmatch(query):
            raise ValueError("query must be a bounded identifier/literal without path or control characters")
        limit = max(1, min(int(args.get("max_results", 12)), 20))
        if not self.allow_source_content:
            return {
                "available": False,
                "reason": "source content tools are disabled for this profile",
                "results": [],
            }
        lowered = query.lower()
        results: list[dict[str, Any]] = []
        scanned = 0
        for root_index, root in enumerate(self.source_roots):
            for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
                if len(results) >= limit or scanned >= 5000:
                    break
                if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
                    continue
                scanned += 1
                if path.stat().st_size > 2_000_000:
                    continue
                try:
                    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                except OSError:
                    continue
                for line_number, line in enumerate(lines, 1):
                    if lowered not in line.lower():
                        continue
                    relative = path.resolve().relative_to(root).as_posix()
                    ref_hash = hashlib.sha256(
                        f"{root_index}:{relative}:{line_number}:{query}".encode()
                    ).hexdigest()[:16]
                    source_ref_id = f"src_{ref_hash}"
                    self.source_refs[source_ref_id] = {
                        "path": path.resolve(),
                        "root_id": f"source_root_{root_index}",
                        "relative_path": relative,
                        "line": line_number,
                    }
                    results.append(
                        {
                            "source_ref_id": source_ref_id,
                            "source_path": f"source_root_{root_index}:{relative}",
                            "line": line_number,
                            "line_text": line[:1000],
                        }
                    )
                    if len(results) >= limit:
                        break
            if len(results) >= limit or scanned >= 5000:
                break
        return {
            "available": bool(self.source_roots),
            "query": query,
            "scanned_files": scanned,
            "results": results,
        }

    def _source_excerpt(self, args: dict[str, Any]) -> dict[str, Any]:
        errors = validate_tool_args(
            args,
            required={"source_ref_id": str},
            optional={"before": int, "after": int},
        )
        if errors:
            raise ValueError("; ".join(errors))
        if not self.allow_source_content:
            raise ValueError("source content tools are disabled for this profile")
        source_ref_id = args["source_ref_id"]
        ref = self.source_refs.get(source_ref_id)
        if ref is None:
            raise ValueError(f"unknown source_ref_id {source_ref_id!r}")
        before = max(0, min(int(args.get("before", 20)), 80))
        after = max(0, min(int(args.get("after", 20)), 80))
        path = Path(ref["path"])
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        center = int(ref["line"])
        start = max(1, center - before)
        end = min(len(lines), center + after)
        return {
            "source_ref_id": source_ref_id,
            "source_path": f"{ref['root_id']}:{ref['relative_path']}",
            "line_start": start,
            "line_end": end,
            "lines": [
                {"line": index, "text": lines[index - 1][:2000]}
                for index in range(start, end + 1)
            ],
        }
