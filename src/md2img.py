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


def _shrink_large_png(image_bytes: bytes, target_bytes: int = 1_800_000) -> bytes:
    """Shrink a PNG while preserving readable text and pixel dimensions first."""
    if len(image_bytes) <= target_bytes or shutil.which("convert") is None:
        return image_bytes

    temp_dir = tempfile.mkdtemp()
    source_path = os.path.join(temp_dir, "source.png")
    best = image_bytes
    try:
        with open(source_path, "wb") as f:
            f.write(image_bytes)
        # Text posters compress well with indexed colour. Try full-size
        # quantization before any resize; only resize gently as a last resort.
        attempts = (
            (None, "256"),
            (None, "128"),
            ("90%", "128"),
            ("80%", "96"),
        )
        for index, (resize, colors) in enumerate(attempts):
            output_path = os.path.join(temp_dir, f"compressed-{index}.png")
            command = ["convert", source_path]
            if resize:
                command.extend(["-resize", resize])
            command.extend(["-colors", colors, "-strip", f"PNG8:{output_path}"])
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0 or not os.path.isfile(output_path):
                continue
            with open(output_path, "rb") as f:
                candidate = f.read()
            if len(candidate) < len(best):
                best = candidate
            if len(best) <= target_bytes:
                break
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("PNG compression failed: %s", exc)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if len(best) < len(image_bytes):
        logger.info("PNG compressed: %d -> %d bytes", len(image_bytes), len(best))
    return best


def _image_html_document(markdown_text: str) -> str:
    """Return report HTML with high-resolution poster-specific typography."""
    html = markdown_to_html_document(markdown_text)
    image_css = """
        body {
            font-size: 20px !important;
            line-height: 1.62 !important;
            max-width: 1120px !important;
            padding: 34px 40px !important;
            background: #ffffff !important;
        }
        h1 { font-size: 32px !important; }
        h2 { font-size: 28px !important; }
        h3 { font-size: 24px !important; }
        table { font-size: 18px !important; }
        th, td { padding: 10px 14px !important; }
        p, li { letter-spacing: 0.01em; }
    """
    return html.replace("</style>", f"{image_css}</style>", 1)


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

    html = _image_html_document(markdown_text)
    try:
        options = {
            "format": "png",
            "encoding": "UTF-8",
            "quiet": "",
            # Render at poster resolution. Compression below preserves these
            # pixels before considering a small resize for WeCom's size limit.
            "width": "1280",
            "zoom": "1.0",
            "disable-smart-width": "",
        }
        out = imgkit.from_string(html, False, options=options)
        if out and isinstance(out, bytes) and len(out) > 0:
            out = _shrink_large_png(out)
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
