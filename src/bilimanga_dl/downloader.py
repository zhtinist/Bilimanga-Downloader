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

import math
import queue
import re
import threading
import time
from collections import deque
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

        # 建结果骨架 + 话任务清单（封面改为“每卷第一张图”，下载后再设置）
        results: dict = {}
        jobs = []  # (volume, chap_idx, chapter, vol_dir)
        for v in volumes:
            vol_dir = book_dir / safe_name(f"{v.index:02d}_{v.title}")
            vol_dir.mkdir(parents=True, exist_ok=True)
            results[v.index] = DownloadedVolume(
                volume=v, dir=vol_dir, cover=None,
                chapters=[DownloadedChapter(title=c.title) for c in v.chapters],
            )
            for ci, ch in enumerate(v.chapters):
                jobs.append((v, ci, ch, vol_dir))

        ceiling = max(2, self.config.parallel_chapters)
        scanned = [0]
        downloaded = [0]
        lock = threading.Lock()

        def report():
            if progress_cb:
                progress_cb("下载/扫描", downloaded[0], max(scanned[0], 1))

        report()

        # 阶段 1：并行扫描每话图片 URL（wait_for imagecontent，可靠）
        def scan_one(job):
            v, ci, ch, vd = job
            try:
                urls = self.scraper.fetch_chapter_images(ch, base)
            except Exception as exc:
                log.warning("解析话 %r 失败: %s", ch.title, exc)
                urls = []
            dchap = results[v.index].chapters[ci]
            with lock:
                for ii, u in enumerate(urls):
                    dest = vd / f"{ci:03d}_{ii:04d}.jpg"
                    dchap.images.append(dest)
                    img_tasks.append((u, dest, ch.url))
                scanned[0] += len(urls)
                report()

        img_tasks: list = []  # (url, dest, referer)
        with ThreadPoolExecutor(max_workers=min(ceiling, 3)) as pool:
            list(pool.map(scan_one, jobs))

        # 已存在的（断点续传）先计入
        pending = deque()
        for t in img_tasks:
            if self.config.resume_enabled and self._valid_jpeg(t[1]):
                with lock:
                    downloaded[0] += 1
            else:
                pending.append(t)
        report()

        def _do(task):
            u, dest, ref = task
            try:
                self._download_one(u, dest, ref)
                return task, self._valid_jpeg(dest)
            except Exception:
                return task, False

        # 阶段 2：自适应并发(AIMD) 逐张下载。起步≈缺口 10%，一波无错 +5%，出错 -3% + 退避
        n = len(pending)
        if n:
            limit = min(ceiling, max(2, math.ceil(n * 0.10)))
            step_up = max(1, round(n * 0.05))
            step_down = max(1, round(n * 0.03))
            MAX_ATTEMPTS = 4
            attempts: dict = {}
            while pending:
                wave = [pending.popleft() for _ in range(min(limit, len(pending)))]
                errors = 0
                with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                    for fut in as_completed([pool.submit(_do, t) for t in wave]):
                        task, ok = fut.result()
                        if ok:
                            with lock:
                                downloaded[0] += 1
                        else:
                            errors += 1
                            attempts[task[1]] = attempts.get(task[1], 0) + 1
                            if attempts[task[1]] < MAX_ATTEMPTS:
                                pending.append(task)
                        report()
                if errors == 0:
                    limit = min(ceiling, limit + step_up)
                else:
                    limit = max(2, limit - step_down)
                    time.sleep(min(5.0, 0.5 + errors * 0.3))

        # 最终补齐：仍缺失的**单线程、多次重试、渐进退避**稳妥补下，尽最大努力保证完整
        for _sweep in range(2):
            leftover = [t for t in img_tasks if not self._valid_jpeg(t[1])]
            if not leftover:
                break
            log.info("最终补齐第 %d 轮：%d 张（单线程稳妥）", _sweep + 1, len(leftover))
            for task in leftover:
                for k in range(5):
                    _, ok = _do(task)
                    if ok:
                        with lock:
                            downloaded[0] += 1
                        report()
                        break
                    time.sleep(1.5 + k)  # 渐进退避，等 429 冷却
        still = [t for t in img_tasks if not self._valid_jpeg(t[1])]
        if still:
            log.warning("仍有 %d 张无法下载(疑似源站失效)：%s",
                        len(still), [t[0] for t in still[:5]])

        # 过滤缺失/损坏并按页序排序；每卷封面 = 该卷第一张图
        for dv in results.values():
            for dchap in dv.chapters:
                imgs = [p for p in dchap.images if self._valid_jpeg(p)]
                imgs.sort(key=lambda p: p.name)
                dchap.images = imgs
            dv.cover = next((dc.images[0] for dc in dv.chapters if dc.images), None)
        return [results[v.index] for v in volumes]

    def download_volume(
        self,
        book: Book,
        volume: Volume,
        work_dir: Path,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> DownloadedVolume:
        return self.download_selected(book, [volume], work_dir, progress_cb)[0]
