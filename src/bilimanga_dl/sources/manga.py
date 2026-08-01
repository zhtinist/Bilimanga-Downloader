"""漫画内容源插件（bilimanga.net）。

封装：网址/书号匹配、详情+目录解析（:class:`scraper.Scraper`）、逐卷下载打包
（:class:`downloader.Downloader` 的三段流水线）→ 交存储保存。浏览器由共享的
:class:`core.net.Net` 惰性启动并跨本复用。
"""

from __future__ import annotations

from typing import List, Optional

from ..config import Config, TEMP_DOWNLOAD_DIR
from ..core.net import Net
from ..core.registry import packagers, sources
from ..models import Book, Volume
from ..scraper import Scraper, parse_book_no
from .base import Callbacks, Source


@sources.register
class MangaSource(Source):
    name = "漫画 (bilimanga)"
    kind = "manga"

    def __init__(self, net: Net, config: Config):
        self.net = net
        self.config = config
        self._scraper = Scraper(net)

    # ---- 匹配 ----
    @classmethod
    def matches(cls, raw: str) -> bool:
        low = (raw or "").lower()
        if "bilimanga.net" in low or "bilicomic.net" in low or "bilicomic" in low:
            return True
        has_novel = "bilinovel.com" in low or "linovelib" in low or "/novel/" in low
        return (("/detail/" in low) or ("/read/" in low)) and not has_novel

    @classmethod
    def parse_book_no(cls, raw: str) -> str:
        return parse_book_no(raw)

    # ---- 解析 ----
    def fetch_book(self, book_no: str) -> Book:
        detail = f"{self.config.site}/detail/{book_no}.html"
        self.net.warm_up(detail)
        return self._scraper.fetch_book(book_no)

    # ---- 下载 → 打包 → 存储 ----
    def download(self, book: Book, volumes: List[Volume], fmt: str,
                 storage, callbacks: Optional[Callbacks] = None) -> List[str]:
        from ..downloader import Downloader
        cb = callbacks or Callbacks()
        pkg_cls = packagers.find(lambda c: getattr(c, "fmt", None) == fmt) \
            or packagers.find(lambda c: getattr(c, "fmt", None) == "epub")
        packager = pkg_cls()
        out_dir = storage.stage_dir("漫画", book.title)
        locations: List[str] = []

        def on_done(vidx, path):
            if path:
                locations.append(storage.commit(path, "漫画", book.title))
            if cb.on_done:
                cb.on_done(vidx, path)

        downloader = Downloader(self.net, self._scraper, self.config)
        downloader.run_pipeline(
            book, volumes, TEMP_DOWNLOAD_DIR, out_dir, packager.build,
            on_start=cb.on_start, on_total=cb.on_total, on_image=cb.on_image,
            on_phase=cb.on_phase, on_done=on_done, on_concurrency=cb.on_concurrency)
        return locations

    def close(self) -> None:
        pass  # net 由外层统一关闭
