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


class _Adaptive:
    """自适应并发限流器（AIMD）：按网络反馈实时调整同时下载的图片数。

    连续成功若干次则 +1（加性增），遇失败/429 则乘性减，介于 [lo, hi]。
    """

    def __init__(self, start: int, lo: int, hi: int):
        self.limit = max(lo, min(hi, start))
        self.lo, self.hi = lo, hi
        self._active = 0
        self._ok = 0
        self._cv = threading.Condition()

    def acquire(self):
        with self._cv:
            while self._active >= self.limit:
                self._cv.wait()
            self._active += 1

    def release(self, ok: bool):
        with self._cv:
            self._active -= 1
            if ok:
                self._ok += 1
                if self._ok >= 6 and self.limit < self.hi:
                    self.limit += 1
                    self._ok = 0
            else:
                self._ok = 0
                self.limit = max(self.lo, self.limit - 2)
            self._cv.notify_all()


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
        counted: set = set()
        scanned_by_job: dict = {}
        lock = threading.Lock()

        def report():
            if progress_cb:
                progress_cb("下载/扫描", len(counted), max(scanned[0], 1))

        def _mark_saved(dest):
            with lock:
                if dest not in counted and self._valid_jpeg(dest):
                    counted.add(dest)
                    report()

        def _set_scanned(jobkey, k):
            with lock:
                scanned_by_job[jobkey] = k
                scanned[0] = sum(scanned_by_job.values())
                report()

        report()

        # 阶段 1：并行扫描每话 URL（wait_for imagecontent，可靠）
        img_tasks: list = []
        def scan_one(job):
            v, ci, ch, vd = job
            try:
                urls = self.scraper.fetch_chapter_images(ch, base)
            except Exception as exc:
                log.warning("解析话 %r 失败: %s", ch.title, exc)
                urls = []
            dchap = results[v.index].chapters[ci]
            local = []
            for ii, u in enumerate(urls):
                dest = vd / f"{ci:03d}_{ii:04d}.jpg"
                dchap.images.append(dest)
                local.append((u, dest, ch.url))
            with lock:
                img_tasks.extend(local)
            _set_scanned((v.index, ci), len(urls))

        with ThreadPoolExecutor(max_workers=min(ceiling, 3)) as pool:
            list(pool.map(scan_one, jobs))

        # 断点续传：已存在的先计入
        pending = deque()
        for t in img_tasks:
            if self.config.resume_enabled and self._valid_jpeg(t[1]):
                _mark_saved(t[1])
            else:
                pending.append(t)

        def _do(task):
            u, dest, ref = task
            try:
                self._download_one(u, dest, ref)
                return task, self._valid_jpeg(dest)
            except Exception:
                return task, False

        # 阶段 2：AIMD 自适应并发逐张下载（同步 XHR，可靠）。
        # 起步≈缺口 10%，一波无错 +5%，出错(429/失败) -3% 并退避。
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
                            _mark_saved(task[1])
                        else:
                            errors += 1
                            attempts[task[1]] = attempts.get(task[1], 0) + 1
                            if attempts[task[1]] < MAX_ATTEMPTS:
                                pending.append(task)
                if errors == 0:
                    limit = min(ceiling, limit + step_up)
                else:
                    limit = max(2, limit - step_down)
                    time.sleep(min(5.0, 0.5 + errors * 0.3))

        # 最终补齐：仍缺失的单线程多次重试，尽力保证完整
        for _sweep in range(2):
            leftover = [t for t in img_tasks if not self._valid_jpeg(t[1])]
            if not leftover:
                break
            log.info("最终补齐第 %d 轮：%d 张", _sweep + 1, len(leftover))
            for task in leftover:
                for k in range(5):
                    _, ok = _do(task)
                    if ok:
                        _mark_saved(task[1])
                        break
                    time.sleep(1.0 + k)

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

    # ---------------- 三阶段流水线 ----------------
    def run_pipeline(
        self,
        book: Book,
        volumes: List[Volume],
        temp_dir: Path,
        out_dir: Path,
        build_fn: Callable,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
        on_packaged: Optional[Callable] = None,
    ) -> List[Path]:
        """下载(网络)→校对(本地)→打包(本地) 三阶段流水线，边下边校边打包。

        - Agent1(下载)：逐话处理，话内多张图并发；用自适应限流器按网络实时调节全局并发。
        - Agent2(校对)：本地检查缺失/空文件，不联网；未过则回退给 Agent1 重下(最多 3 次)。
        - Agent3(打包)：某卷全部话校对完成即打包成 EPUB/PDF 到 out_dir，逐卷产出。

        build_fn(book, DownloadedVolume, out_dir) -> Path。返回已产出成品路径列表。
        """
        base = book.base_url
        book_dir = temp_dir / safe_name(book.title)
        book_dir.mkdir(parents=True, exist_ok=True)

        ceiling = max(2, self.config.parallel_chapters)
        limiter = _Adaptive(start=min(ceiling, 4), lo=2, hi=min(ceiling, 8))

        results: dict = {}
        vol_dirs: dict = {}
        chapters_left: dict = {}   # v.index -> 未最终确定的话数
        for v in volumes:
            vd = book_dir / safe_name(f"{v.index:02d}_{v.title}")
            vd.mkdir(parents=True, exist_ok=True)
            vol_dirs[v.index] = vd
            results[v.index] = DownloadedVolume(
                volume=v, dir=vd, cover=None,
                chapters=[DownloadedChapter(title=c.title) for c in v.chapters])
            chapters_left[v.index] = len(v.chapters)

        dl_q: "queue.Queue" = queue.Queue()
        val_q: "queue.Queue" = queue.Queue()
        pkg_q: "queue.Queue" = queue.Queue()
        for v in volumes:
            for ci, ch in enumerate(v.chapters):
                dl_q.put((v, ci, ch, vol_dirs[v.index], 0))

        scanned = [0]
        counted: set = set()
        lock = threading.Lock()
        outputs: list = []
        vols_left = [len(volumes)]
        done_evt = threading.Event()
        img_pool = ThreadPoolExecutor(max_workers=max(2, min(ceiling, 8)))

        def report():
            if progress_cb:
                progress_cb("下载/扫描", len(counted), max(scanned[0], 1))

        report()

        def _dl_image(u, dest, ref):
            limiter.acquire()
            ok = False
            try:
                if not (self.config.resume_enabled and self._valid_jpeg(dest)):
                    try:
                        self._download_one(u, dest, ref)
                    except Exception:
                        pass
                ok = self._valid_jpeg(dest)
            finally:
                limiter.release(ok)
            if ok:
                with lock:
                    if dest not in counted:
                        counted.add(dest)
                        report()
            return ok

        # Agent1：下载
        def agent1():
            while True:
                item = dl_q.get()
                if item is None:
                    dl_q.task_done()
                    return
                v, ci, ch, vd, attempt = item
                try:
                    urls = self.scraper.fetch_chapter_images(ch, base)
                except Exception as exc:
                    log.warning("解析话 %r 失败: %s", ch.title, exc)
                    urls = []
                dchap = results[v.index].chapters[ci]
                tasks = []
                with lock:
                    first_scan = not dchap.images
                    dchap.images = [vd / f"{ci:03d}_{ii:04d}.jpg" for ii in range(len(urls))]
                    if first_scan:
                        scanned[0] += len(urls)
                    for ii, u in enumerate(urls):
                        tasks.append((u, dchap.images[ii], ch.url))
                # 话内多图并发（受全局限流器节流）
                futs = [img_pool.submit(_dl_image, u, dd, rf) for (u, dd, rf) in tasks]
                for f in futs:
                    try:
                        f.result()
                    except Exception:
                        pass
                report()
                val_q.put((v, ci, tasks, attempt))
                dl_q.task_done()

        # Agent2：本地校对（查漏/空文件，不联网）
        def agent2():
            while True:
                item = val_q.get()
                if item is None:
                    val_q.task_done()
                    return
                v, ci, tasks, attempt = item
                missing = [t for t in tasks if not self._valid_file(t[1])]
                if missing and attempt < 3:
                    log.info("校对：话 %d-%d 缺 %d 张，回退重下(第%d次)",
                             v.index, ci, len(missing), attempt + 1)
                    dl_q.put((v, ci, v.chapters[ci], vol_dirs[v.index], attempt + 1))
                    val_q.task_done()
                    continue
                if missing:
                    log.warning("话 %d-%d 仍缺 %d 张，尽力打包", v.index, ci, len(missing))
                # 该话最终确定
                fire_pkg = None
                with lock:
                    chapters_left[v.index] -= 1
                    if chapters_left[v.index] == 0:
                        fire_pkg = v.index
                if fire_pkg is not None:
                    pkg_q.put(fire_pkg)
                val_q.task_done()

        # Agent3：打包（本地 CPU）
        def agent3():
            while True:
                vidx = pkg_q.get()
                if vidx is None:
                    pkg_q.task_done()
                    return
                dv = results[vidx]
                # 组装：过滤有效 + 按页序 + 每卷封面=第一张图
                for dc in dv.chapters:
                    imgs = [p for p in dc.images if self._valid_file(p)]
                    imgs.sort(key=lambda p: p.name)
                    dc.images = imgs
                dv.cover = next((dc.images[0] for dc in dv.chapters if dc.images), None)
                if any(dc.images for dc in dv.chapters):
                    try:
                        out = build_fn(book, dv, out_dir)
                        with lock:
                            outputs.append(out)
                        if on_packaged:
                            on_packaged(out, dv.volume)
                        # 打包成功后清理该卷临时图
                        import shutil
                        shutil.rmtree(dv.dir, ignore_errors=True)
                    except Exception as exc:
                        log.warning("打包卷 %d 失败: %s", vidx, exc)
                else:
                    log.warning("卷 %d 无图片，跳过打包", vidx)
                with lock:
                    vols_left[0] -= 1
                    if vols_left[0] <= 0:
                        done_evt.set()
                pkg_q.task_done()

        n1 = max(1, min(ceiling, 3))
        n2, n3 = 2, 2
        threads = []
        for fn, cnt in ((agent1, n1), (agent2, n2), (agent3, n3)):
            for _ in range(cnt):
                th = threading.Thread(target=fn, daemon=True)
                th.start()
                threads.append(th)

        done_evt.wait()
        # 收尾：给各 agent 发停止信号
        for _ in range(n1):
            dl_q.put(None)
        for _ in range(n2):
            val_q.put(None)
        for _ in range(n3):
            pkg_q.put(None)
        for th in threads:
            th.join(timeout=10)
        img_pool.shutdown(wait=False)
        return outputs

    @staticmethod
    def _valid_file(path: Path) -> bool:
        try:
            return path.exists() and path.stat().st_size > 1000
        except OSError:
            return False
