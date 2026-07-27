"""下载选定卷（章）的插图。

输出到工作目录，结构：
    <work>/<安全书名>/<章号_章标题>/cover.jpg
    <work>/<安全书名>/<章号_章标题>/<话号>_<图号>.jpg

支持：
- 并发下载（线程池）
- 断点续传：已存在且可正常打开的 JPEG 直接跳过
- 每话图片数量校验（下载数与页面解析数一致）
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from PIL import Image

from .config import Config
from .imageutil import save_as_jpeg
from .logutil import get_logger
from .models import Book, Volume
from .net import Net
from .scraper import Scraper

log = get_logger("downloader")


_ILLEGAL = re.compile(r'[?*"<>|:/\\\x00-\x1f]')


def safe_name(name: str) -> str:
    """跨平台安全文件名。"""
    cleaned = _ILLEGAL.sub("_", name).strip().strip(".")
    return cleaned or "untitled"


@dataclass
class DownloadedChapter:
    title: str
    images: List[Path] = field(default_factory=list)


@dataclass
class DownloadedVolume:
    volume: Volume
    dir: Path
    cover: Optional[Path]
    chapters: List[DownloadedChapter] = field(default_factory=list)


class Downloader:
    def __init__(self, net: Net, scraper: Scraper, config: Config):
        self.net = net
        self.scraper = scraper
        self.config = config

    def _valid_jpeg(self, path: Path) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return False
        try:
            with Image.open(path) as img:
                img.verify()
            return True
        except Exception:
            return False

    def _download_one(self, url: str, dest: Path, referer: str) -> Path:
        # 断点续传：已完整存在则跳过
        if self.config.resume_enabled and self._valid_jpeg(dest):
            log.debug("跳过已存在: %s", dest.name)
            return dest
        data = self.net.get_bytes(url, referer=referer)
        save_as_jpeg(data, dest)
        log.debug("已保存: %s (%d bytes)", dest.name, len(data))
        return dest

    def _download_cover(self, book: Book, book_dir: Path) -> Optional[Path]:
        if not book.cover_url:
            return None
        cover_path = book_dir / "cover.jpg"
        try:
            if not (self.config.resume_enabled and self._valid_jpeg(cover_path)):
                data = self.net.get_bytes(book.cover_url, referer=book.base_url)
                save_as_jpeg(data, cover_path)
            return cover_path
        except Exception as exc:
            log.warning("封面下载失败: %s", exc)
            return None

    def download_selected(
        self,
        book: Book,
        volumes: List[Volume],
        work_dir: Path,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> List[DownloadedVolume]:
        """下载多个卷（章）——跨所有选中话并行取 URL + 扁平并行下图。

        临时图片落盘到 ``work_dir/<书名>/<章号_章标题>/``，边下边写，避免囤内存。
        """
        base = book.base_url
        book_dir = work_dir / safe_name(book.title)
        book_dir.mkdir(parents=True, exist_ok=True)
        cover_path = self._download_cover(book, book_dir)

        # 建结果骨架 + 话任务清单
        results: dict = {}
        jobs = []  # (volume, chap_idx, chapter, vol_dir)
        for v in volumes:
            vol_dir = book_dir / safe_name(f"{v.index:02d}_{v.title}")
            vol_dir.mkdir(parents=True, exist_ok=True)
            results[v.index] = DownloadedVolume(
                volume=v, dir=vol_dir, cover=cover_path,
                chapters=[DownloadedChapter(title=c.title) for c in v.chapters],
            )
            for ci, ch in enumerate(v.chapters):
                jobs.append((v, ci, ch, vol_dir))

        par = max(1, self.config.parallel_chapters)

        # 阶段 1：并行解析每话的图片 URL
        def fetch_urls(job):
            v, ci, ch, _vd = job
            try:
                return (v.index, ci), self.scraper.fetch_chapter_images(ch, base)
            except Exception as exc:
                log.warning("解析话 %r 图片失败: %s", ch.title, exc)
                return (v.index, ci), []

        url_map: dict = {}
        with ThreadPoolExecutor(max_workers=par) as pool:
            for key, urls in pool.map(fetch_urls, jobs):
                url_map[key] = urls

        # 组织图片下载任务（扁平）
        img_tasks = []  # (url, dest, referer)
        for (v, ci, ch, vd) in jobs:
            dchap = results[v.index].chapters[ci]
            for ii, u in enumerate(url_map[(v.index, ci)]):
                dest = vd / f"{ci:03d}_{ii:04d}.jpg"
                dchap.images.append(dest)
                img_tasks.append((u, dest, ch.url))
        total = len(img_tasks)
        log.info("待下载 %d 话 / %d 张图片，并发 %d", len(jobs), total, par)

        # 阶段 2：多标签并行、逐张下载（稳定的同步 XHR，跨 DrissionPage 版本都可靠）
        done = 0
        lock = threading.Lock()

        def dl(task):
            u, dest, ref = task
            self._download_one(u, dest, ref)  # 内含断点续传

        with ThreadPoolExecutor(max_workers=par) as pool:
            futs = {pool.submit(dl, t): t for t in img_tasks}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:
                    log.warning("图片下载失败 %s: %s", futs[fut][1].name, exc)
                with lock:
                    done += 1
                    cur = done
                if progress_cb:
                    progress_cb("下载图片", cur, total)

        # 补漏：对首轮未下到的图重试，【单独显示进度条】，避免看起来卡住。轮数由配置控制。
        def _missing():
            return [t for t in img_tasks if not self._valid_jpeg(t[1])]

        def _redl(task):
            try:
                self._download_one(*task)
            except Exception:
                pass

        for _round in range(max(0, self.config.retry_missing_rounds)):
            left = _missing()
            if not left:
                break
            log.info("补漏第 %d 轮：%d 张", _round + 1, len(left))
            rtotal = len(left)
            rdone = 0
            desc = f"补齐缺失(第{_round + 1}轮)"
            if progress_cb:
                progress_cb(desc, 0, rtotal)
            with ThreadPoolExecutor(max_workers=par) as pool:
                for _f in as_completed([pool.submit(_redl, t) for t in left]):
                    rdone += 1
                    if progress_cb:
                        progress_cb(desc, rdone, rtotal)

        still = _missing()
        if still:
            log.warning("仍有 %d 张图片未下到", len(still))

        # 过滤缺失/损坏，保持顺序
        for dv in results.values():
            for dchap in dv.chapters:
                dchap.images = [p for p in dchap.images if self._valid_jpeg(p)]
        return [results[v.index] for v in volumes]

    def download_volume(
        self,
        book: Book,
        volume: Volume,
        work_dir: Path,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> DownloadedVolume:
        return self.download_selected(book, [volume], work_dir, progress_cb)[0]
