# -*- coding: utf-8 -*-

from pathlib import Path
from unittest.mock import patch

from src.md2img import _image_html_document, _markdown_to_image_wkhtml, _shrink_large_png


def test_image_html_uses_poster_sized_typography() -> None:
    html = _image_html_document("# 日报\n\n正文")
    assert "font-size: 20px !important" in html
    assert "font-size: 32px !important" in html
    assert "max-width: 1120px !important" in html


def test_wkhtml_render_uses_full_resolution_options() -> None:
    captured = {}

    def fake_render(html, output, options):
        captured["html"] = html
        captured["options"] = options
        return b"small-png"

    with patch("imgkit.from_string", side_effect=fake_render):
        result = _markdown_to_image_wkhtml("# 清晰日报")

    assert result == b"small-png"
    assert captured["options"]["width"] == "1280"
    assert captured["options"]["zoom"] == "1.0"
    assert captured["options"]["disable-smart-width"] == ""


def test_png_compression_quantizes_before_resizing(tmp_path) -> None:
    commands = []

    class SimpleResult:
        returncode = 0

    def fake_run(command, **_kwargs):
        commands.append(command)
        output_path = Path(command[-1].removeprefix("PNG8:"))
        output_path.write_bytes(b"x" * 100)
        return SimpleResult()

    with patch("src.md2img.shutil.which", return_value="/usr/bin/convert"), patch(
        "src.md2img.tempfile.mkdtemp", return_value=str(tmp_path)
    ), patch("src.md2img.subprocess.run", side_effect=fake_run):
        result = _shrink_large_png(b"y" * 1000, target_bytes=500)

    assert result == b"x" * 100
    assert "-resize" not in commands[0]
    assert commands[0][commands[0].index("-colors") + 1] == "256"

