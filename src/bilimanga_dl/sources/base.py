"""内容源插件抽象基类。

一个 Source 封装“某个站点怎么解析 + 怎么把选中卷下成成品并交给存储”。
匹配用类方法（无需实例、无副作用）；解析/下载在实例上（持有 net/config 等）。
新站点 = 继承 :class:`Source` + ``@sources.register``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, List, Optional

from ..models import Book, Volume


class Source(ABC):
    #: 展示名（如“漫画 / bilimanga”）。
    name: str = ""
    #: 类别："manga" | "novel"，用于分目录与格式限制。
    kind: str = ""

    # ---- 匹配与书号（类方法：无需实例）----
    @classmethod
    @abstractmethod
    def matches(cls, raw: str) -> bool:
        """该输入（完整网址 / 书号）是否由本源处理。"""

    @classmethod
    @abstractmethod
    def parse_book_no(cls, raw: str) -> str:
        """从输入中提取书号；无法识别抛 ``ValueError``。"""

    # ---- 解析与下载（实例方法）----
    @abstractmethod
    def fetch_book(self, book_no: str) -> Book:
        """抓详情 + 目录，返回 :class:`Book`（含 volumes）。"""

    @abstractmethod
    def download(self, book: Book, volumes: List[Volume], fmt: str,
                 storage, callbacks: Optional["Callbacks"] = None) -> List[str]:
        """下载选中卷 → 打包 → 交 ``storage`` 保存；返回各卷保存位置。"""

    def close(self) -> None:
        """释放资源（网络会话/浏览器等）。默认无操作。"""


class Callbacks:
    """下载进度回调集合（均可选，首参为卷号 vidx），编排/UI 层传入。"""

    def __init__(self, on_start=None, on_total=None, on_image=None,
                 on_phase=None, on_done=None, on_concurrency=None):
        self.on_start: Optional[Callable] = on_start
        self.on_total: Optional[Callable] = on_total
        self.on_image: Optional[Callable] = on_image
        self.on_phase: Optional[Callable] = on_phase
        self.on_done: Optional[Callable] = on_done
        self.on_concurrency: Optional[Callable] = on_concurrency
