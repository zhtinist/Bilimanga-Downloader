"""打包器插件抽象基类（漫画格式：EPUB / PDF）。

把一个已下载的卷写成成品文件到 ``out_dir``，返回文件路径。轻小说固定 EPUB，
其打包逻辑内置于轻小说源；这里的打包器插件主要服务漫画的格式选择与将来扩展。
新格式 = 继承 :class:`Packager` + ``@packagers.register``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import Book


class Packager(ABC):
    #: 格式标识："epub" | "pdf"。
    fmt: str = ""
    #: 成品扩展名（含点），如 ".epub"。
    ext: str = ""

    @abstractmethod
    def build(self, book: Book, dv, out_dir: Path) -> Path:
        """把漫画卷 ``dv``（:class:`downloader.DownloadedVolume`）写成成品到
        ``out_dir``，返回成品路径。"""
