"""图片工具：保存并在需要时把 AVIF/WebP 转成 JPEG。

EPUB 与 img2pdf 都不接受 AVIF，因此统一转为 JPEG（保留原始像素尺寸，
不缩放、不留白，保证插图排版还原）。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Tuple

# 让 Pillow 支持 AVIF（若插件存在）
try:  # pragma: no cover - 取决于环境
    import pillow_avif  # noqa: F401
except Exception:
    pass

from PIL import Image


def save_as_jpeg(data: bytes, dest: Path, quality: int = 90) -> Tuple[int, int]:
    """把图片字节写为 JPEG，返回 (宽, 高)。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        size = img.size
        img.save(dest, format="JPEG", quality=quality)
    return size


def image_size(path: Path) -> Tuple[int, int]:
    with Image.open(path) as img:
        return img.size
