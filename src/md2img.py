# -*- coding: utf-8 -*-
"""Markdown 转 PNG 图片工具，用于图片通知渠道。"""

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from src.formatters import markdown_to_html_document

logger = logging.getLogger(__name__)


def _markdown_to_image_m2f(markdown_text: str) -> Optional[bytes]:
    """Use markdown-to-file when it is available."""
    if shutil.which("m2f") is None:
        logger.warning("m2f (markdown-to-file) not found in PATH. Fallback to text.")
        return None

    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        md_path = os.path.join(temp_dir, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_text)
        result = subprocess.run(
            ["m2f", md_path, "png", f"outputDirectory={temp_dir}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        png_path = os.path.join(temp_dir, "report.png")
        if result.returncode != 0 or not os.path.isfile(png_path):
            logger.warning("m2f conversion failed: returncode=%s", result.returncode)
            return None
        with open(png_path, "rb") as f:
            return f.read()
    except subprocess.TimeoutExpired:
        logger.warning("m2f conversion timed out (60s)")
        return None
    except Exception as exc:
        logger.warning("markdown_to_image (m2f) failed: %s", exc)
        return None
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError as exc:
                logger.debug("Failed to remove temp dir %s: %s", temp_dir, exc)


def _markdown_to_image_wkhtml(markdown_text: str) -> Optional[bytes]:
    """Render Markdown via imgkit/wkhtmltoimage at a mobile-friendly resolution."""
    try:
        import imgkit
    except ImportError:
        logger.debug("imgkit not installed, markdown_to_image unavailable")
        return None

    html = markdown_to_html_document(markdown_text)
    try:
        options = {
            "format": "png",
            "encoding": "UTF-8",
            "quiet": "",
            # 企业微信图片上限约 2MB。缩放后仍适合手机阅读，且避免大图回退为文本。
            "zoom": "0.65",
        }
        out = imgkit.from_string(html, False, options=options)
        if out and isinstance(out, bytes) and len(out) > 0:
            logger.info("Markdown rendered as PNG: %d bytes", len(out))
            return out
        logger.warning("imgkit.from_string returned empty or invalid result")
        return None
    except OSError as exc:
        if "wkhtmltoimage" in str(exc).lower() or "wkhtmltopdf" in str(exc).lower():
            logger.debug("wkhtmltopdf/wkhtmltoimage not found: %s", exc)
        else:
            logger.warning("imgkit/wkhtmltoimage error: %s", exc)
        return None
    except Exception as exc:
        logger.warning("markdown_to_image conversion failed: %s", exc)
        return None


def markdown_to_image(markdown_text: str, max_chars: int = 15000) -> Optional[bytes]:
    """Convert Markdown to a PNG image, or return ``None`` for text fallback."""
    if len(markdown_text) > max_chars:
        logger.warning(
            "Markdown content (%d chars) exceeds max_chars (%d), skipping image conversion",
            len(markdown_text),
            max_chars,
        )
        return None
    try:
        from src.config import get_config

        engine = getattr(get_config(), "md2img_engine", "wkhtmltoimage")
    except Exception:
        engine = "wkhtmltoimage"

    if engine == "markdown-to-file":
        return _markdown_to_image_m2f(markdown_text)
    return _markdown_to_image_wkhtml(markdown_text)
