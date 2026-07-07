#!/usr/bin/env python3
"""Render quick PNG previews for SGGK case artifacts.

The preview is intentionally lightweight: it uses artifact JSON reports instead
of launching the SDK GUI, so it is suitable for batch sanity checks and contact
sheets. It draws input/result bounding boxes and selected input edge locators,
then prints exact bbox and validation numbers beside the sketch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from PIL import Image, ImageDraw, ImageFont


CANVAS_W = 1600
CANVAS_H = 1000
BG = (248, 249, 251)
INK = (26, 32, 44)
MUTED = (98, 108, 124)
GRID = (222, 226, 232)
TARGET = (37, 99, 235)
TOOL = (217, 119, 6)
RESULT = (22, 163, 74)
FAIL = (220, 38, 38)
PASS = (5, 150, 105)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Case artifact dirs or artifact roots")
    parser.add_argument("--out-dir", help="Output directory; defaults to each case report directory")
    parser.add_argument("--contact-sheet", help="Optional contact-sheet PNG path")
    parser.add_argument("--limit", type=int, default=0, help="Maximum case previews to render; 0 means all")
    parser.add_argument("--max-edges", type=int, default=80, help="Maximum input edge locators to draw per role")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"_json_error": f"{exc.msg} at line {exc.lineno}, column {exc.colno}"}


def is_case_dir(path: Path) -> bool:
    return (path / "manifest.json").is_file() and (path / "report").is_dir()


def iter_case_dirs(paths: list[str]) -> list[Path]:
    cases: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if is_case_dir(path):
            cases.add(path.resolve())
            continue
        if not path.is_dir():
            continue
        for manifest in path.rglob("manifest.json"):
            case_dir = manifest.parent
            if "_recipes" not in case_dir.parts and is_case_dir(case_dir):
                cases.add(case_dir.resolve())
    return sorted(cases, key=lambda item: str(item).lower())


def sanitize_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return text or "case"


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def fmt_num(value: Any) -> str:
    num = as_num(value)
    if num is None:
        return str(value)
    if abs(num) >= 10000 or (0 < abs(num) < 0.001):
        return f"{num:.8e}"
    return f"{num:.12g}"


def bbox_from_locator(locator: Any) -> dict[str, Any] | None:
    if not isinstance(locator, dict):
        return None
    bbox = locator.get("bbox")
    if isinstance(bbox, dict) and not bbox.get("empty") and bbox_extents(bbox) is not None:
        return bbox
    return None


def bbox_extents(bbox: dict[str, Any]) -> tuple[list[float], list[float]] | None:
    mins = bbox.get("min")
    maxs = bbox.get("max")
    if not (isinstance(mins, list) and isinstance(maxs, list) and len(mins) == 3 and len(maxs) == 3):
        return None
    try:
        mn = [float(v) for v in mins]
        mx = [float(v) for v in maxs]
    except (TypeError, ValueError):
        return None
    return mn, mx


def bbox_points(bbox: dict[str, Any]) -> list[tuple[float, float, float]]:
    extents = bbox_extents(bbox)
    if extents is None:
        return []
    mn, mx = extents
    return [
        (x, y, z)
        for x in (mn[0], mx[0])
        for y in (mn[1], mx[1])
        for z in (mn[2], mx[2])
    ]


def bbox_label(bbox: dict[str, Any]) -> str:
    extents = bbox_extents(bbox)
    if extents is None:
        return "bbox unavailable"
    mn, mx = extents
    return f"min=[{', '.join(fmt_num(v) for v in mn)}] max=[{', '.join(fmt_num(v) for v in mx)}]"


def bbox_signature_values(bbox: dict[str, Any]) -> list[float]:
    extents = bbox_extents(bbox)
    if extents is None:
        return []
    mn, mx = extents
    return mn + mx


def input_boxes(input_index: Any) -> dict[str, list[dict[str, Any]]]:
    boxes: dict[str, list[dict[str, Any]]] = {"target": [], "tool": []}
    if not isinstance(input_index, dict):
        return boxes
    for item in input_index.get("inputs", []):
        if not isinstance(item, dict):
            continue
        role = as_str(item.get("role")) or "input"
        for topo in item.get("topologies", []):
            if not isinstance(topo, dict):
                continue
            if topo.get("type") == "Body":
                bbox = bbox_from_locator(topo.get("locator"))
                if bbox:
                    boxes.setdefault(role, []).append(bbox)
    return boxes


def input_edges(input_index: Any, max_edges: int) -> dict[str, list[tuple[tuple[float, float, float], tuple[float, float, float]]]]:
    edges: dict[str, list[tuple[tuple[float, float, float], tuple[float, float, float]]]] = {"target": [], "tool": []}
    if not isinstance(input_index, dict):
        return edges
    for item in input_index.get("inputs", []):
        if not isinstance(item, dict):
            continue
        role = as_str(item.get("role")) or "input"
        bucket = edges.setdefault(role, [])
        for topo in item.get("topologies", []):
            if len(bucket) >= max_edges:
                break
            if not isinstance(topo, dict) or topo.get("type") != "Edge":
                continue
            locator = topo.get("locator")
            if not isinstance(locator, dict):
                continue
            start = locator.get("start_point")
            end = locator.get("end_point")
            if not (isinstance(start, list) and isinstance(end, list) and len(start) == 3 and len(end) == 3):
                continue
            try:
                bucket.append((tuple(float(v) for v in start), tuple(float(v) for v in end)))
            except (TypeError, ValueError):
                continue
    return edges


def result_boxes(properties: Any) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    if not isinstance(properties, dict):
        return boxes
    for body in properties.get("bodies", []):
        if isinstance(body, dict):
            bbox = bbox_from_locator({"bbox": body.get("bbox")})
            if bbox:
                boxes.append(bbox)
    return boxes


def iso_project(point: tuple[float, float, float]) -> tuple[float, float]:
    x, y, z = point
    return ((x - y) * 0.8660254, z - (x + y) * 0.32)


def ortho_project(point: tuple[float, float, float], axes: tuple[int, int]) -> tuple[float, float]:
    return (point[axes[0]], point[axes[1]])


def make_transform(points: list[tuple[float, float]], rect: tuple[int, int, int, int]):
    x0, y0, x1, y1 = rect
    if not points:
        return lambda p: ((x0 + x1) // 2, (y0 + y1) // 2)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max(max_x - min_x, 1e-9)
    height = max(max_y - min_y, 1e-9)
    scale = min((x1 - x0 - 40) / width, (y1 - y0 - 40) / height)
    cx = (min_x + max_x) * 0.5
    cy = (min_y + max_y) * 0.5
    rcx = (x0 + x1) * 0.5
    rcy = (y0 + y1) * 0.5

    def transform(p: tuple[float, float]) -> tuple[int, int]:
        return (int(rcx + (p[0] - cx) * scale), int(rcy - (p[1] - cy) * scale))

    return transform


BOX_EDGES = [
    (0, 1), (0, 2), (0, 4), (3, 1), (3, 2), (3, 7),
    (5, 1), (5, 4), (5, 7), (6, 2), (6, 4), (6, 7),
]


def draw_bbox(draw: ImageDraw.ImageDraw, bbox: dict[str, Any], project, transform, color: tuple[int, int, int], width: int = 3) -> None:
    corners = bbox_points(bbox)
    if len(corners) != 8:
        return
    pts = [transform(project(p)) for p in corners]
    for a, b in BOX_EDGES:
        draw.line([pts[a], pts[b]], fill=color, width=width)


def draw_edges(draw: ImageDraw.ImageDraw, edges: list[tuple[tuple[float, float, float], tuple[float, float, float]]], project, transform, color: tuple[int, int, int]) -> None:
    for start, end in edges:
        draw.line([transform(project(start)), transform(project(end))], fill=color, width=1)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrapped_lines(text: str, max_chars: int) -> list[str]:
    result: list[str] = []
    for raw in str(text).splitlines() or [""]:
        line = raw
        while len(line) > max_chars:
            result.append(line[:max_chars])
            line = line[max_chars:]
        result.append(line)
    return result


def draw_text_block(draw: ImageDraw.ImageDraw, xy: tuple[int, int], lines: list[str], font, fill=INK, line_h: int = 22, max_lines: int = 20) -> int:
    x, y = xy
    count = 0
    for line in lines:
        if count >= max_lines:
            draw.text((x, y), "...", font=font, fill=fill)
            y += line_h
            break
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h
        count += 1
    return y


def collect_project_points(boxes_by_role: dict[str, list[dict[str, Any]]], result: list[dict[str, Any]], edges_by_role: dict[str, list[Any]], project) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for boxes in list(boxes_by_role.values()) + [result]:
        for bbox in boxes:
            points.extend(project(p) for p in bbox_points(bbox))
    for edges in edges_by_role.values():
        for start, end in edges:
            points.append(project(start))
            points.append(project(end))
    return points


def bbox_signature(case_data: dict[str, Any]) -> str:
    payload = json.dumps(case_data.get("signature", {}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def case_preview(case_dir: Path, out_path: Path, max_edges: int) -> dict[str, Any]:
    manifest = load_json(case_dir / "manifest.json") or {}
    status = load_json(case_dir / "report" / "status.json") or {}
    input_index = load_json(case_dir / "report" / "input_topology_index.json") or {}
    properties = load_json(case_dir / "report" / "properties.json") or {}
    validation = load_json(case_dir / "report" / "validation.json") or {}
    topo_check = load_json(case_dir / "report" / "topo_check.json") or {}

    case_id = as_str(manifest.get("case_id")) or case_dir.name
    api = as_str(manifest.get("api"))
    dsl = manifest.get("dsl") if isinstance(manifest.get("dsl"), dict) else {}
    options = manifest.get("options") if isinstance(manifest.get("options"), dict) else {}

    boxes_by_role = input_boxes(input_index)
    edges_by_role = input_edges(input_index, max_edges)
    result = result_boxes(properties)

    validation_ok = validation.get("ok") if isinstance(validation, dict) else None
    succeeded = status.get("succeeded") if isinstance(status, dict) else None
    topo_failures = []
    if isinstance(topo_check, dict):
        topo_failures = [
            item for item in topo_check.get("bodies", [])
            if isinstance(item, dict) and item.get("ok") is False
        ]

    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(img)
    font = load_font(21)
    small = load_font(17)
    tiny = load_font(14)
    title_font = load_font(30, bold=True)
    mono = load_font(16)

    draw.rectangle([0, 0, CANVAS_W, 78], fill=(239, 242, 247))
    status_color = PASS if validation_ok is True and succeeded is not False and not topo_failures else FAIL
    draw.text((28, 20), case_id, font=title_font, fill=INK)
    draw.text((28, 54), f"api={api}  succeeded={succeeded}  validation_ok={validation_ok}", font=small, fill=status_color)
    if dsl:
        draw.text((640, 20), f"dsl={as_str(dsl.get('case_id'))} variant={as_str(dsl.get('variant'))}", font=small, fill=MUTED)
        draw.text((640, 45), as_str(dsl.get("hypothesis"))[:95], font=tiny, fill=MUTED)

    panels = {
        "ISO": (25, 105, 910, 650, iso_project),
        "XY": (940, 105, 1238, 360, lambda p: ortho_project(p, (0, 1))),
        "XZ": (1268, 105, 1566, 360, lambda p: ortho_project(p, (0, 2))),
        "YZ": (940, 395, 1238, 650, lambda p: ortho_project(p, (1, 2))),
    }
    for name, (x0, y0, x1, y1, project) in panels.items():
        draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255), outline=GRID, width=1)
        draw.text((x0 + 10, y0 + 8), name, font=small, fill=MUTED)
        transform = make_transform(collect_project_points(boxes_by_role, result, edges_by_role, project), (x0, y0 + 28, x1, y1))
        for role, color in [("target", TARGET), ("tool", TOOL)]:
            for bbox in boxes_by_role.get(role, []):
                draw_bbox(draw, bbox, project, transform, color, width=3 if name == "ISO" else 2)
            draw_edges(draw, edges_by_role.get(role, []), project, transform, color)
        for bbox in result:
            draw_bbox(draw, bbox, project, transform, RESULT, width=4 if name == "ISO" else 2)

    legend_y = 665
    for label, color in [("target", TARGET), ("tool", TOOL), ("result", RESULT)]:
        draw.line([(35, legend_y), (75, legend_y)], fill=color, width=5)
        draw.text((85, legend_y - 11), label, font=small, fill=INK)
        legend_y += 28

    left_lines: list[str] = []
    left_lines.append("Input / result boxes")
    for role in ("target", "tool"):
        for i, bbox in enumerate(boxes_by_role.get(role, [])):
            left_lines.append(f"{role}[{i}] {bbox_label(bbox)}")
    for i, bbox in enumerate(result):
        left_lines.append(f"result[{i}] {bbox_label(bbox)}")
    if not result:
        left_lines.append("result bbox unavailable; rerun with current runner to populate properties.bbox")
    draw_text_block(draw, (25, 730), left_lines, mono, fill=INK, line_h=22, max_lines=10)

    validation_lines: list[str] = ["Validation"]
    if isinstance(validation, dict):
        totals = validation.get("totals") if isinstance(validation.get("totals"), dict) else {}
        if totals:
            validation_lines.append(
                "totals len={length} area={area} vol={volume} absvol={abs_volume}".format(
                    **{k: fmt_num(totals.get(k)) for k in ("length", "area", "volume", "abs_volume")}
                )
            )
        failures = validation.get("failures")
        if isinstance(failures, list) and failures:
            validation_lines.append("failures:")
            for failure in failures[:5]:
                validation_lines.extend("  " + line for line in wrapped_lines(as_str(failure), 74))
        else:
            validation_lines.append("failures: none")
    if isinstance(options, dict):
        validation_lines.append(
            "options tol={tol} boolean={bt} topo_track={tt}".format(
                tol=fmt_num(options.get("modeling_tol")),
                bt=as_str(options.get("boolean_type")),
                tt=options.get("topo_track"),
            )
        )
    draw_text_block(draw, (940, 675), validation_lines, mono, fill=INK, line_h=22, max_lines=12)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)

    signature_payload = {
        "case_id": case_id,
        "api": api,
        "dsl_case": as_str(dsl.get("case_id")) if isinstance(dsl, dict) else "",
        "dsl_variant": as_str(dsl.get("variant")) if isinstance(dsl, dict) else "",
        "target": [bbox_signature_values(bbox) for bbox in boxes_by_role.get("target", [])],
        "tool": [bbox_signature_values(bbox) for bbox in boxes_by_role.get("tool", [])],
        "result": [bbox_signature_values(bbox) for bbox in result],
    }
    return {
        "case_id": case_id,
        "case_dir": str(case_dir),
        "preview": str(out_path),
        "validation_ok": validation_ok,
        "succeeded": succeeded,
        "signature": signature_payload,
        "signature_hash": hashlib.sha1(json.dumps(signature_payload, sort_keys=True).encode("utf-8")).hexdigest()[:12],
    }


def make_contact_sheet(previews: list[dict[str, Any]], out_path: Path) -> None:
    if not previews:
        return
    thumb_w, thumb_h = 520, 325
    cols = min(3, max(1, len(previews)))
    rows = (len(previews) + cols - 1) // cols
    title_h = 58
    sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + title_h)), BG)
    draw = ImageDraw.Draw(sheet)
    small = load_font(16)
    title = load_font(18, bold=True)
    for index, item in enumerate(previews):
        col = index % cols
        row = index // cols
        x = col * thumb_w
        y = row * (thumb_h + title_h)
        img = Image.open(item["preview"]).convert("RGB")
        img.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        sheet.paste(img, (x, y + title_h))
        status_color = PASS if item.get("validation_ok") is True else FAIL
        draw.text((x + 10, y + 8), item["case_id"][:42], font=title, fill=INK)
        draw.text((x + 10, y + 34), f"validation={item.get('validation_ok')} sig={item.get('signature_hash')}", font=small, fill=status_color)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)


def main() -> int:
    args = parse_args()
    cases = iter_case_dirs(args.paths)
    if args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        print("no case artifacts found")
        return 1

    out_dir = Path(args.out_dir).resolve() if args.out_dir else None
    previews: list[dict[str, Any]] = []
    for case_dir in cases:
        manifest = load_json(case_dir / "manifest.json") or {}
        case_id = as_str(manifest.get("case_id")) or case_dir.name
        out_path = (
            out_dir / f"{sanitize_name(case_id)}.png"
            if out_dir
            else case_dir / "report" / "preview.png"
        )
        item = case_preview(case_dir, out_path, args.max_edges)
        previews.append(item)
        print(f"preview={out_path}")

    if args.contact_sheet:
        make_contact_sheet(previews, Path(args.contact_sheet).resolve())
        print(f"contact_sheet={Path(args.contact_sheet).resolve()}")

    if out_dir:
        index_root = out_dir
    elif args.contact_sheet:
        index_root = Path(args.contact_sheet).resolve().parent
    else:
        index_root = cases[0] / "report"
    index_path = index_root / "preview_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(previews, indent=2), encoding="utf-8")
    print(f"index={index_path}")
    print(f"rendered={len(previews)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
