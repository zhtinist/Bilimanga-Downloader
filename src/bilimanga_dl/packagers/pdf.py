"""用 img2pdf 把一个已下载的卷打包成 PDF。

- 每张插图独占一页，页面尺寸即图片尺寸（按 96 DPI 换算），不裁切、不留白。
- 封面作为第一页。
- img2pdf 无损嵌入 JPEG，不会二次压缩。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import img2pdf

from ..downloader import DownloadedVolume, safe_name
from ..core.logutil import get_logger
from ..models import Book

log = get_logger("pdf")


def build_pdf(book: Book, dv: DownloadedVolume, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    title = f"{book.title} - {dv.volume.title}"

    image_paths: List[str] = []
    if dv.cover and dv.cover.exists():
        image_paths.append(str(dv.cover))
    for dchap in dv.chapters:
        for img_path in dchap.images:
            if img_path.exists():
                image_paths.append(str(img_path))

    if not image_paths:
        raise RuntimeError(f"没有可用于生成 PDF 的图片：{title}")

    out_path = out_dir / (safe_name(title) + ".pdf")
    log.info("生成 PDF: %s (%d 页)", out_path, len(image_paths))
    # 每页尺寸贴合图片像素，避免留白/拉伸
    layout = img2pdf.get_layout_fun(None)
    with open(out_path, "wb") as f:
        f.write(img2pdf.convert(image_paths, layout_fun=layout))
    return out_path


# ---- 插件封装 ----
from .base import Packager  # noqa: E402
from ..core.registry import packagers  # noqa: E402


@packagers.register
class PdfPackager(Packager):
    """PDF 打包器插件（漫画；每图整页）。"""
    fmt = "pdf"
    ext = ".pdf"

    def build(self, book, dv, out_dir):
        return build_pdf(book, dv, out_dir)
