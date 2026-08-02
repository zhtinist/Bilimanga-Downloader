"""轻小说内容源插件（哔哩轻小说 bilinovel）。

默认走无浏览器手机站引擎（:class:`novel_mobile.MobileNovelDownloader`，三段流水线）；
解析失败自动回退浏览器引擎（:class:`novel.NovelDownloader`）。引擎跨本复用（会话/限流
桶沿用）。固定输出 EPUB。
"""

from __future__ import annotations

from typing import List, Optional

from ..config import Config
from ..core.logutil import get_logger
from ..core.net import Net
from ..core.registry import sources
from ..models import Book, Volume
from ..novel import parse_novel_no
from .base import Callbacks, Source

log = get_logger("source.novel")


@sources.register
class NovelSource(Source):
    name = "轻小说 (bilinovel)"
    kind = "novel"

    def __init__(self, net: Net, config: Config):
        self.net = net
        self.config = config
        self._mobile = None      # MobileNovelDownloader（复用）
        self.engine = None       # 当前书使用的引擎（mobile 或浏览器回退）

    @classmethod
    def matches(cls, raw: str) -> bool:
        low = (raw or "").lower()
        if "bilinovel.com" in low or "linovelib" in low:
            return True
        has_manga = "bilimanga.net" in low or "bilicomic" in low
        return ("/novel/" in low) and not has_manga

    @classmethod
    def parse_book_no(cls, raw: str) -> str:
        return parse_novel_no(raw)

    def _ensure_mobile(self):
        if self._mobile is None:
            from ..novel_mobile import MobileNovelDownloader
            # 正文抓取仍串行（限流安全）；num_thread 只用于插图并发下载。
            nt = max(1, int(getattr(self.config, "concurrency_max", 4)))
            self._mobile = MobileNovelDownloader(num_thread=nt,
                                                 proxy=self.config.proxy or "")
        return self._mobile

    def fetch_book(self, book_no: str) -> Book:
        # 优先手机站引擎
        try:
            eng = self._ensure_mobile()
            book = eng.fetch_book(book_no)
            if book.volumes:
                self.engine = eng
                return book
        except Exception as exc:  # noqa: BLE001
            log.warning("手机站引擎解析失败，回退浏览器：%s", exc)
        # 回退浏览器引擎
        from ..novel import NovelDownloader
        eng = NovelDownloader(self.net)
        book = eng.fetch_book(book_no)
        self.engine = eng
        return book

    def download(self, book: Book, volumes: List[Volume], fmt: str,
                 storage, callbacks: Optional[Callbacks] = None) -> List[str]:
        from .base import split_existing
        cb = callbacks or Callbacks()
        out_dir = storage.stage_dir("小说", book.title)
        engine = self.engine or self._ensure_mobile()
        locations: List[str] = []

        # 冲突处理：成品已存在于该存储的卷直接跳过（不重下、不覆盖）。
        todo, skipped = split_existing(
            book, volumes, fmt, "小说", storage,
            enabled=getattr(self.config, "resume_enabled", True))
        for v, fname in skipped:
            if cb.on_skip:
                cb.on_skip(v.index, fname)
        if not todo:
            return locations
        volumes = todo

        def on_done(vidx, path):
            if path:
                locations.append(storage.commit(path, "小说", book.title))
            if cb.on_done:
                cb.on_done(vidx, path)

        if hasattr(engine, "run_pipeline"):
            engine.run_pipeline(book, volumes, out_dir,
                                on_start=cb.on_start, on_total=cb.on_total,
                                on_image=cb.on_image, on_phase=cb.on_phase,
                                on_done=on_done)
        else:  # 浏览器回退引擎：逐卷串行
            for v in volumes:
                if cb.on_start:
                    cb.on_start(v.index)
                try:
                    path = engine.download_volume(
                        book, v, out_dir, on_phase=cb.on_phase,
                        on_total=cb.on_total, on_image=cb.on_image)
                except Exception as exc:  # noqa: BLE001
                    log.warning("卷 %d 下载失败：%s", v.index, exc)
                    path = None
                on_done(v.index, path)
        return locations

    def close(self) -> None:
        if self._mobile is not None and hasattr(self._mobile, "close"):
            try:
                self._mobile.close()
            except Exception:  # noqa: BLE001
                pass
            self._mobile = None
