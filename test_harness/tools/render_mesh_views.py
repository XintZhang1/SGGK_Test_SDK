"""Render real shaded mesh views of failure-case geometry with Pillow only.

Reads the bounded JSON produced by ``sggk_mesh_dump`` and draws painter's-
algorithm shaded views (ISO + XY/XZ/YZ panels) with flat normal-based
shading and thin edge outlines.  Panels carry Chinese role labels only — no
numbers anywhere.  A second entry point renders suspect-topology bodies alone
with a red tint when debug-geometry evidence exists.

Everything is deterministic: fixed light, fixed palette, fixed layout.  This
is diagnostic evidence rendering; it never feeds any verdict.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[2]

PANEL_PX = 360
TITLE_PX = 30
MAX_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
)

# 角色 → 基色（输入冷色，输出暖色，疑似拓扑红色由 _tint 单独处理）。
ROLE_COLORS = {
    "target": (91, 141, 191),
    "tool": (111, 168, 116),
    "result": (222, 158, 74),
    "suspect": (210, 84, 80),
    "other": (150, 150, 158),
}
BACKGROUND = (24, 22, 26)
PANEL_BG = (31, 29, 34)
PANEL_LINE = (58, 54, 62)
TEXT = (226, 222, 230)
LIGHT_DIR = (0.35, 0.55, 0.76)

FONT_CANDIDATES = MAX_FONTS


def _load_font(size: int) -> Any:
    for candidate in FONT_CANDIDATES:
        try:
            if Path(candidate).is_file():
                return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _role_of(name: str) -> str:
    lowered = name.casefold()
    if lowered.startswith("target") or "target" in lowered:
        return "target"
    if lowered.startswith("tool") or "tool" in lowered:
        return "tool"
    if lowered.startswith("result") or lowered.startswith("output"):
        return "result"
    if "suspect" in lowered or "debug" in lowered:
        return "suspect"
    return "other"


def _body_color(name: str) -> tuple[int, int, int]:
    return ROLE_COLORS[_role_of(name)]


def _view_transform(
    view: str,
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """Return (right, up, depth) orthonormal axes for the view."""

    if view == "xy":
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    if view == "xz":
        return (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)
    if view == "yz":
        return (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)
    # iso：绕 Z 轴 -45°，再绕 X 轴约 -54.7° 的标准轴测。
    inv = 1.0 / math.sqrt(3.0)
    right = (math.sqrt(3.0) / 2.0 * 0.7071, -math.sqrt(3.0) / 2.0 * 0.7071 * -1.0, 0.0)
    right = (0.70710678, 0.70710678, 0.0)
    depth = (inv, -inv, inv)
    # up = depth × right
    up = (
        depth[1] * right[2] - depth[2] * right[1],
        depth[2] * right[0] - depth[0] * right[2],
        depth[0] * right[1] - depth[1] * right[0],
    )
    up_len = math.sqrt(sum(c * c for c in up))
    up = tuple(c / up_len for c in up)
    return right, up, depth


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(c * c for c in vector))
    if length <= 0:
        return (0.0, 0.0, 1.0)
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _collect_triangles(bodies: list[dict[str, Any]], names: set[str] | None) -> list[dict[str, Any]]:
    triangles: list[dict[str, Any]] = []
    for body in bodies:
        if not isinstance(body, dict):
            continue
        name = str(body.get("name") or "")
        if names is not None and name not in names:
            continue
        color = _body_color(name)
        for face in body.get("faces") or []:
            if not isinstance(face, dict):
                continue
            verts = face.get("verts")
            tris = face.get("tris")
            if not isinstance(verts, list) or not isinstance(tris, list):
                continue
            for index in range(0, len(tris) - 2, 3):
                try:
                    i0, i1, i2 = (3 * int(tris[index]), 3 * int(tris[index + 1]), 3 * int(tris[index + 2]))
                    a = (float(verts[i0]), float(verts[i0 + 1]), float(verts[i0 + 2]))
                    b = (float(verts[i1]), float(verts[i1 + 1]), float(verts[i1 + 2]))
                    c = (float(verts[i2]), float(verts[i2 + 1]), float(verts[i2 + 2]))
                except (IndexError, TypeError, ValueError):
                    continue
                triangles.append({"pts": (a, b, c), "color": color})
    return triangles


def _shade(color: tuple[int, int, int], normal: tuple[float, float, float]) -> tuple[int, int, int]:
    dot = abs(sum(n * light for n, light in zip(normal, LIGHT_DIR, strict=True)))
    factor = 0.38 + 0.62 * min(1.0, dot)
    return (
        min(255, int(color[0] * factor)),
        min(255, int(color[1] * factor)),
        min(255, int(color[2] * factor)),
    )


def _outline(color: tuple[int, int, int]) -> tuple[int, int, int]:
    return (int(color[0] * 0.45), int(color[1] * 0.45), int(color[2] * 0.45))


def _render_panel(
    triangles: list[dict[str, Any]],
    view: str,
    size: int,
    tint_red: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (size, size), PANEL_BG)
    if not triangles:
        return image
    right, up, depth = _view_transform(view)

    projected: list[tuple[float, list[tuple[float, float]], tuple[int, int, int], tuple[int, int, int]]] = []
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for tri in triangles:
        a, b, c = tri["pts"]
        normal = _normalize(
            (
                (b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1]),
                (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2]),
                (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]),
            )
        )
        screen: list[tuple[float, float]] = []
        depth_sum = 0.0
        for point in (a, b, c):
            x = sum(p * r for p, r in zip(point, right, strict=True))
            y = sum(p * u for p, u in zip(point, up, strict=True))
            z = sum(p * d for p, d in zip(point, depth, strict=True))
            screen.append((x, y))
            depth_sum += z
        for x, y in screen:
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
        base = tri["color"]
        if tint_red:
            base = ROLE_COLORS["suspect"]
        projected.append((depth_sum / 3.0, screen, _shade(base, normal), _outline(base)))

    span = max(max_x - min_x, max_y - min_y)
    if span <= 0:
        return image
    margin = size * 0.08
    scale = (size - 2 * margin) / span
    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0

    draw = ImageDraw.Draw(image)
    projected.sort(key=lambda item: item[0])
    for _, screen, fill, outline in projected:
        polygon = [((x - cx) * scale + size / 2.0, size / 2.0 - (y - cy) * scale) for x, y in screen]
        draw.polygon(polygon, fill=fill)
        draw.line(polygon + [polygon[0]], fill=outline, width=1)
    return image


def _titled_panel(image: Image.Image, title: str, font: Any) -> Image.Image:
    panel = Image.new("RGB", (image.width, image.height + TITLE_PX), PANEL_BG)
    draw = ImageDraw.Draw(panel)
    draw.rectangle((0, 0, panel.width, TITLE_PX - 2), fill=(40, 37, 44))
    draw.text((10, 5), title, font=font, fill=TEXT)
    panel.paste(image, (0, TITLE_PX))
    draw.rectangle((0, 0, panel.width - 1, panel.height - 1), outline=PANEL_LINE)
    return panel


def _layout(panels: list[Image.Image]) -> Image.Image:
    if not panels:
        return Image.new("RGB", (PANEL_PX, PANEL_PX), BACKGROUND)
    columns = 4 if len(panels) > 2 else len(panels)
    rows = math.ceil(len(panels) / columns)
    width = columns * panels[0].width
    height = rows * panels[0].height
    grid = Image.new("RGB", (width, height), BACKGROUND)
    for index, panel in enumerate(panels):
        x = (index % columns) * panel.width
        y = (index // columns) * panel.height
        grid.paste(panel, (x, y))
    return grid


def render_mesh_views(
    mesh: dict[str, Any],
    out_path: Path,
    *,
    panel_px: int = PANEL_PX,
) -> dict[str, Any]:
    """Render the case grid PNG; returns a small render report."""

    bodies = [body for body in (mesh.get("bodies") or []) if isinstance(body, dict)]
    names = {str(body.get("name") or "") for body in bodies}
    inputs = sorted(name for name in names if _role_of(name) in {"target", "tool"})
    results = sorted(name for name in names if _role_of(name) == "result")
    others = sorted(name for name in names if name not in set(inputs) and name not in set(results))

    font = _load_font(16)
    panels: list[Image.Image] = []
    for index, name in enumerate(inputs):
        triangles = _collect_triangles(bodies, {name})
        view = _render_panel(triangles, "iso", panel_px)
        panels.append(_titled_panel(view, f"输入几何{index + 1}", font))
    if len(inputs) >= 2:
        triangles = _collect_triangles(bodies, set(inputs))
        panels.append(_titled_panel(_render_panel(triangles, "iso", panel_px), "输入叠加", font))
    output_names = set(results) | set(others)
    if output_names:
        triangles = _collect_triangles(bodies, output_names)
        for view in ("iso", "xy", "xz", "yz"):
            panels.append(_titled_panel(_render_panel(triangles, view, panel_px), "输出结果", font))
    if not panels:
        triangles = _collect_triangles(bodies, None)
        panels.append(_titled_panel(_render_panel(triangles, "iso", panel_px), "几何网格", font))

    grid = _layout(panels)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    grid.save(out_path)
    return {"out": str(out_path), "panels": len(panels), "size": [grid.width, grid.height]}


def render_suspect_views(
    mesh: dict[str, Any],
    out_path: Path,
    *,
    panel_px: int = PANEL_PX,
) -> dict[str, Any]:
    """Render suspect-topology bodies alone with a red tint (ISO + XY/XZ/YZ)."""

    bodies = [body for body in (mesh.get("bodies") or []) if isinstance(body, dict)]
    triangles = _collect_triangles(bodies, None)
    font = _load_font(16)
    panels = [
        _titled_panel(_render_panel(triangles, view, panel_px, tint_red=True), "疑似拓扑", font)
        for view in ("iso", "xy", "xz", "yz")
    ]
    grid = _layout(panels)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    grid.save(out_path)
    return {"out": str(out_path), "panels": len(panels), "size": [grid.width, grid.height]}


def load_mesh(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mesh", required=True, help="sggk_mesh_dump mesh.json path")
    parser.add_argument("--out", required=True, help="Output PNG path")
    parser.add_argument("--suspect", action="store_true", help="Render the red-tinted suspect-topology grid")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mesh_path = Path(args.mesh).expanduser()
    if not mesh_path.is_file():
        print(f"网格文件不存在：{mesh_path}")
        return 2
    mesh = load_mesh(mesh_path)
    out_path = Path(args.out).expanduser()
    renderer = render_suspect_views if args.suspect else render_mesh_views
    report = renderer(mesh, out_path)
    print(f"已渲染网格视图：{report['out']}（{report['panels']} 个面板）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
