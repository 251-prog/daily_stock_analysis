# -*- coding: utf-8 -*-

from pathlib import Path
from unittest.mock import patch

from src.md2img import (
    _image_html_document,
    _markdown_to_image_wkhtml,
    _shrink_large_png,
    markdown_to_image_cards,
    split_markdown_stock_cards,
)


def test_image_html_uses_poster_sized_typography() -> None:
    html = _image_html_document("# 日报\n\n正文")
    assert "font-size: 24px !important" in html
    assert "font-size: 40px !important" in html
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
    assert captured["options"]["width"] == "1920"
    assert captured["options"]["zoom"] == "1.5"
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


def test_split_markdown_stock_cards_repeats_context_and_footer() -> None:
    report = """## 收盘研究简报

覆盖 2 只股票

### 股票甲 · 000001
结论甲
---

### 股票乙 · 600000
结论乙
---

*生成于 18:00｜仅供个人研究参考*"""

    cards = split_markdown_stock_cards(report)

    assert len(cards) == 2
    assert all("## 收盘研究简报" in card for card in cards)
    assert all("覆盖 2 只股票" in card for card in cards)
    assert all("*生成于 18:00" in card for card in cards)
    assert "股票甲" in cards[0] and "股票乙" not in cards[0]
    assert "股票乙" in cards[1] and "股票甲" not in cards[1]


def test_markdown_to_image_cards_renders_each_stock_card() -> None:
    report = "## 日报\n\n### 股票甲 · 000001\n甲\n\n### 股票乙 · 600000\n乙"

    with patch("src.md2img.markdown_to_image", side_effect=[b"one", b"two"]) as render:
        images = markdown_to_image_cards(report)

    assert images == [b"one", b"two"]
    assert render.call_count == 2
