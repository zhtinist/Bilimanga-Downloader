"""下载任务队列：后台串行下载，边下边可继续加任务。

主线程只负责交互（解析 / 选卷 / 选项），选好即把任务加入队列并立刻回主界面；一个后台
worker 线程按加入顺序（FIFO）逐本下载，进度以简洁行打印。worker 用**独立的资源上下文**
（自己的 :class:`~bilimanga_dl.ui.cli._Shared`：独立 Net / 内容源实例），与主线程的解析
互不干扰、无共享可变状态。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

from ..config import Config

# 后台 worker 与主线程共用一把打印锁，避免进度行和菜单输出交错撕裂。
print_lock = threading.Lock()


def qprint(msg: str) -> None:
    with print_lock:
        print(msg, flush=True)


_TARGET_LABEL = {"local": "本地", "baidu": "百度网盘", "onedrive": "OneDrive"}


@dataclass
class Task:
    seq: int
    is_novel: bool
    book_no: str
    title: str
    selected: List[int]
    fmt: str
    target: str
    status: str = "排队"          # 排队 / 下载中 / 完成 / 失败
    total_vols: int = 0
    done_vols: int = 0
    error: str = ""
    locations: List[str] = field(default_factory=list)


class DownloadQueue:
    """FIFO 下载队列 + 单个后台 worker。"""

    def __init__(self, config: Config):
        self.config = config
        self._q: deque = deque()
        self._cv = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._seq = 0
        self._current: Optional[Task] = None
        self.tasks: List[Task] = []
        self._shared = None            # worker 专用 _Shared（惰性建）

    # ---- 入队 ----
    def add(self, is_novel: bool, book_no: str, title: str,
            selected: List[int], fmt: str, target: str) -> Task:
        with self._cv:
            self._seq += 1
            t = Task(self._seq, is_novel, book_no, title, list(selected),
                     fmt, target, total_vols=len(selected))
            self._q.append(t)
            self.tasks.append(t)
            self._cv.notify()
        self._ensure_worker()
        return t

    def _ensure_worker(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    # ---- 状态查询 ----
    def pending_count(self) -> int:
        with self._cv:
            return len(self._q)

    def busy(self) -> bool:
        with self._cv:
            return self._current is not None or len(self._q) > 0

    def status_summary(self) -> str:
        """一行队列概览，供主界面顶部显示。无任何任务时返回空串。"""
        parts = []
        cur = self._current
        if cur:
            parts.append(f"▶ 下载中：{cur.title}（卷 {cur.done_vols}/{cur.total_vols}）")
        pend = self.pending_count()
        if pend:
            parts.append(f"⏳ 排队 {pend} 本")
        done = sum(1 for t in self.tasks if t.status == "完成")
        fail = sum(1 for t in self.tasks if t.status == "失败")
        if done:
            parts.append(f"✔ 完成 {done}")
        if fail:
            parts.append(f"✖ 失败 {fail}")
        return "   ".join(parts)

    # ---- worker ----
    def _run(self) -> None:
        from .cli import _Shared            # 局部导入避免循环依赖
        self._shared = _Shared(self.config)
        while not self._stop:
            with self._cv:
                while not self._q and not self._stop:
                    self._cv.wait()
                if self._stop:
                    break
                task = self._q.popleft()
                self._current = task
            try:
                self._download(task)
            except Exception as exc:  # noqa: BLE001
                task.status = "失败"
                task.error = str(exc)
                qprint(f"✖ 失败：{task.title} — {exc}")
            finally:
                with self._cv:
                    self._current = None

    def _download(self, task: Task) -> None:
        from ..sources.base import Callbacks
        task.status = "下载中"
        tgt = _TARGET_LABEL.get(task.target, task.target)
        qprint(f"\n▶ 开始下载：{task.title}（{task.total_vols} 卷 → {tgt}）")
        src = self._shared.source(task.is_novel)
        book = src.fetch_book(task.book_no)
        index_map = {v.index: v for v in book.volumes}
        titles = {v.index: v.title for v in book.volumes}
        volumes = [index_map[i] for i in task.selected if i in index_map]

        def on_done(vidx, path):
            task.done_vols += 1
            tag = "✓" if path else "⚠"
            qprint(f"   {tag} [{task.title}] {titles.get(vidx, '')}"
                   f"  ({task.done_vols}/{task.total_vols})")

        def on_skip(vidx, filename):
            task.done_vols += 1
            qprint(f"   ⏭ [{task.title}] {titles.get(vidx, '')} 已存在，跳过"
                   f"  ({task.done_vols}/{task.total_vols})")

        storage = self._shared.storage(task.target)
        task.locations = src.download(book, volumes, task.fmt, storage,
                                      Callbacks(on_done=on_done, on_skip=on_skip))
        task.status = "完成"
        qprint(f"✔ 完成：{task.title}（{len(task.locations)} 个文件 → {tgt}）")

    # ---- 收尾 ----
    def wait_all(self, on_tick=None) -> None:
        """阻塞直到队列清空（当前任务 + 所有排队任务下完）。"""
        while self.busy():
            if on_tick:
                on_tick(self.status_summary())
            time.sleep(0.5)

    def shutdown(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def close(self) -> None:
        self.shutdown()
        if self._shared is not None:
            try:
                self._shared.close()
            except Exception:  # noqa: BLE001
                pass
