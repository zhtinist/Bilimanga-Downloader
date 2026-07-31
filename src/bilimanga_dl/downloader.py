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


def cleanup_book_temp(temp_dir: Path, title: str) -> None:
    """删除某本书的临时图片目录（异常/取消中断后清理，避免占盘）。"""
    import shutil
    try:
        shutil.rmtree(Path(temp_dir) / safe_name(title), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


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
    """自适应并发限流器：每 1 秒按这一秒的错误率决定加/减并发。

    - 加线程随时生效（放开信号量，新任务立即可进）。
    - 减线程是**优雅缩减**：只降上限 ``limit``，正在下载的线程不打断，
      等它们自然结束后不再派新活即可（不掐断、不重下、不浪费）。
    - 决策（每秒采样一次这一秒内的成功/失败）：
        错误率 == 0        → +1（顺利就加）
        错误率 >= 40%      → -2（猛减，明显被限速）
        0 < 错误率 < 40%   → -1（温和减）
    """

    def __init__(self, start: int, lo: int, hi: int,
                 on_change: Optional[Callable[[int], None]] = None):
        self.limit = max(lo, min(hi, start))
        self.lo, self.hi = lo, hi
        self._active = 0
        self._ok = 0
        self._fail = 0
        self._stop = False
        self._cv = threading.Condition()
        self._mon: Optional[threading.Thread] = None
        self._on_change = on_change

    def start(self):
        if self._on_change:
            self._on_change(self.limit)  # 先播报初始线程数
        self._mon = threading.Thread(target=self._loop, daemon=True)
        self._mon.start()

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def acquire(self):
        with self._cv:
            while self._active >= self.limit and not self._stop:
                self._cv.wait(timeout=0.5)
            self._active += 1

    def release(self, ok: bool):
        with self._cv:
            self._active -= 1
            if ok:
                self._ok += 1
            else:
                self._fail += 1
            self._cv.notify_all()

    def _loop(self):
        while True:
            time.sleep(1.0)
            changed = False
            with self._cv:
                if self._stop:
                    return
                total = self._ok + self._fail
                if total > 0:
                    old = self.limit
                    rate = self._fail / total
                    if rate == 0:
                        self.limit = min(self.hi, self.limit + 1)
                    elif rate >= 0.40:
                        self.limit = max(self.lo, self.limit - 2)
                    else:
                        self.limit = max(self.lo, self.limit - 1)
                    self._ok = self._fail = 0
                    self._cv.notify_all()
                    changed = self.limit != old
                    log.debug("并发调节: 上限=%d (本秒成功后清零)", self.limit)
            if changed and self._on_change:
                self._on_change(self.limit)


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
        on_start: Optional[Callable] = None,
        on_total: Optional[Callable] = None,
        on_image: Optional[Callable] = None,
        on_phase: Optional[Callable] = None,
        on_done: Optional[Callable] = None,
        on_concurrency: Optional[Callable] = None,
    ) -> List[Path]:
        """逐卷串行下载 + 校对/打包后台重叠。每卷一条进度：下载→校对→打包→完成。

        - 下载：一次只下一卷（其余等待），卷内多图并发，AIMD 从 4 线程起步按网络自适应。
        - 下完立即交给后台线程做本地校对(查缺失/空文件，不联网) + 打包，
          与下一卷的下载重叠进行，逐卷产出 EPUB/PDF。
        - 无论正常结束还是中途异常，都在 finally 里停调节器、关线程池（不泄漏线程）。

        回调（均可选，参数首位为卷号 vidx）：
          on_start(vidx)         该卷开始下载
          on_total(vidx, n)      该卷图片总数已知
          on_image(vidx)         该卷下载完成一张
          on_phase(vidx, phase)  阶段变化：'validate' / 'package'
          on_done(vidx, path)    该卷完成，path 为成品路径（失败为 None）
          on_concurrency(n)      当前并发线程数变化（初始 4，自适应升降）
        """
        base = book.base_url
        book_dir = temp_dir / safe_name(book.title)
        book_dir.mkdir(parents=True, exist_ok=True)

        # 线程数：固定从 4 起步，自适应在 [2, 8] 间升降（不再受用户配置的手填值影响）。
        limiter = _Adaptive(start=4, lo=2, hi=8, on_change=on_concurrency)
        limiter.start()  # 启动每秒错误率采样的并发调节器
        img_pool = ThreadPoolExecutor(max_workers=8)
        pkg_pool = ThreadPoolExecutor(max_workers=2)  # 后台校对+打包
        outputs: list = []
        out_lock = threading.Lock()

        def _fetch(u, dest, ref, tries=3):
            # 就地补图（边下边检测）：某张失败即在本任务内退避重试，
            # 与其它并发下载的图重叠进行，不必等整波结束再回头补。
            for attempt in range(tries):
                limiter.acquire()
                ok = False
                try:
                    if self.config.resume_enabled and self._valid_jpeg(dest):
                        ok = True
                    else:
                        try:
                            self._download_one(u, dest, ref)
                        except Exception:
                            pass
                        ok = self._valid_jpeg(dest)
                finally:
                    limiter.release(ok)
                if ok:
                    return True
                if attempt < tries - 1:
                    time.sleep(0.4 * (attempt + 1))  # 退避后就地再试
            return False

        def _finalize(vidx, dv):
            # Agent2：本地校对（不联网，查缺失/空文件）
            if on_phase:
                on_phase(vidx, "validate")
            for dc in dv.chapters:
                dc.images = [p for p in dc.images if self._valid_file(p)]
                dc.images.sort(key=lambda p: p.name)
            dv.cover = next((dc.images[0] for dc in dv.chapters if dc.images), None)
            # Agent3：打包
            path = None
            if any(dc.images for dc in dv.chapters):
                if on_phase:
                    on_phase(vidx, "package")
                try:
                    path = build_fn(book, dv, out_dir)
                    with out_lock:
                        outputs.append(path)
                    import shutil
                    shutil.rmtree(dv.dir, ignore_errors=True)
                except Exception as exc:
                    log.warning("打包卷 %d 失败: %s", vidx, exc)
            else:
                log.warning("卷 %d 无图片，跳过打包", vidx)
            if on_done:
                on_done(vidx, path)

        pkg_futures = []
        try:
            # Agent1：逐卷串行下载
            for v in volumes:
                vd = book_dir / safe_name(f"{v.index:02d}_{v.title}")
                vd.mkdir(parents=True, exist_ok=True)
                dv = DownloadedVolume(volume=v, dir=vd, cover=None,
                                      chapters=[DownloadedChapter(title=c.title) for c in v.chapters])
                if on_start:
                    on_start(v.index)
                # 扫描该卷所有话的图片 URL
                tasks = []  # (url, dest, ref, ci, ii)
                for ci, ch in enumerate(v.chapters):
                    try:
                        urls = self.scraper.fetch_chapter_images(ch, base)
                    except Exception as exc:
                        log.warning("解析话 %r 失败: %s", ch.title, exc)
                        urls = []
                    dchap = dv.chapters[ci]
                    dchap.images = [vd / f"{ci:03d}_{ii:04d}.jpg" for ii in range(len(urls))]
                    for ii, u in enumerate(urls):
                        tasks.append((u, dchap.images[ii], ch.url))
                if on_total:
                    on_total(v.index, len(tasks))

                def _one(t, vidx=v.index):
                    ok = _fetch(t[0], t[1], t[2])
                    if ok and on_image:
                        on_image(vidx)
                    return t, ok

                # 每张图已在 _fetch 里就地重试；这里的外层轮次只作兜底安全网
                # （应对限流冷却时间超过单张重试预算的极端情况）。
                pending = list(tasks)
                for _round in range(2):
                    if not pending:
                        break
                    futs = [img_pool.submit(_one, t) for t in pending]
                    for f in futs:
                        try:
                            f.result()
                        except Exception:
                            pass
                    pending = [t for t in tasks if not self._valid_file(t[1])]
                    if pending:
                        time.sleep(1.0)  # 兜底轮次间退避

                # 下完 → 交后台校对+打包（与下一卷下载重叠）
                pkg_futures.append(pkg_pool.submit(_finalize, v.index, dv))

            for f in pkg_futures:
                try:
                    f.result()
                except Exception:
                    pass
            return outputs
        finally:
            # 无论正常/异常/中断，都收好线程池与调节器，避免线程与内存泄漏。
            limiter.stop()
            img_pool.shutdown(wait=False)
            pkg_pool.shutdown(wait=True)


    @staticmethod
    def _valid_file(path: Path) -> bool:
        try:
            return path.exists() and path.stat().st_size > 1000
        except OSError:
            return False
