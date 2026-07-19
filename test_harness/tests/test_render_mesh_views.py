from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from test_harness.tools import render_mesh_views


def cube_mesh(name: str, offset: float = 0.0, size: float = 10.0) -> dict[str, Any]:
    o = offset
    s = size
    verts = [
        o, o, o, o + s, o, o, o + s, o + s, o, o, o + s, o,
        o, o, o + s, o + s, o, o + s, o + s, o + s, o + s, o, o + s, o + s,
    ]
    tris = [
        0, 1, 2, 0, 2, 3,
        4, 6, 5, 4, 7, 6,
        0, 4, 5, 0, 5, 1,
        2, 6, 7, 2, 7, 3,
        0, 3, 7, 0, 7, 4,
        1, 5, 6, 1, 6, 2,
    ]
    return {"name": name, "faces": [{"verts": verts, "tris": tris}]}


def sample_mesh() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "sggk_mesh_dump",
        "bodies": [
            cube_mesh("target", 0.0),
            cube_mesh("tool", 20.0),
            cube_mesh("result_1", 5.0, 6.0),
        ],
    }


def test_render_mesh_views_non_blank(tmp_path: Path) -> None:
    out = tmp_path / "mesh.png"
    report = render_mesh_views.render_mesh_views(sample_mesh(), out)
    assert out.is_file()
    # 2 个输入 + 输入叠加 + 4 视角输出 = 7 个面板。
    assert report["panels"] == 7
    image = Image.open(out).convert("RGB")
    extrema = image.getextrema()
    # 非空白图：任一通道有对比度。
    assert any(high - low > 40 for low, high in extrema)
    # 输入冷色（蓝/绿）与输出暖色（橙）都应出现。
    pixels = image.getdata()
    assert any(b > r + 20 for r, g, b in pixels)
    assert any(r > b + 40 for r, g, b in pixels)


def test_render_suspect_views_red_tint(tmp_path: Path) -> None:
    out = tmp_path / "suspect.png"
    report = render_mesh_views.render_suspect_views(sample_mesh(), out)
    assert out.is_file()
    assert report["panels"] == 4
    image = Image.open(out).convert("RGB")
    pixels = image.getdata()
    # 红色调：存在明显偏红的像素。
    assert any(r > 120 and r > g + 40 and r > b + 40 for r, g, b in pixels)


def test_render_empty_mesh_still_writes(tmp_path: Path) -> None:
    out = tmp_path / "empty.png"
    render_mesh_views.render_mesh_views({"bodies": []}, out)
    assert out.is_file()


def test_role_mapping() -> None:
    assert render_mesh_views._role_of("target") == "target"
    assert render_mesh_views._role_of("tool") == "tool"
    assert render_mesh_views._role_of("result_1") == "result"
    assert render_mesh_views._role_of("suspect_0") == "suspect"
    assert render_mesh_views._role_of("misc") == "other"


def test_main_cli(tmp_path: Path, capsys: Any) -> None:
    import json

    mesh_path = tmp_path / "mesh.json"
    mesh_path.write_text(json.dumps(sample_mesh()), encoding="utf-8")
    out = tmp_path / "cli.png"
    rc = render_mesh_views.main(["--mesh", str(mesh_path), "--out", str(out)])
    assert rc == 0
    assert out.is_file()
    rc = render_mesh_views.main(["--mesh", str(tmp_path / "missing.json"), "--out", str(out)])
    assert rc == 2
