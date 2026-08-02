"""存储去向插件抽象基类。

成品先写到 :meth:`stage_dir` 返回的目录，每卷完成后调用 :meth:`commit`：
- 本地存储：``stage_dir`` 即最终目录，``commit`` 基本无操作；
- 网盘存储（3.0.0）：``stage_dir`` 是临时目录，``commit`` 负责上传并清理本地。

这样下载/打包管线无需感知去向，换云盘只是换一个 Storage 实现。
连接态与展示由 :meth:`is_ready` / :meth:`status_label` 提供（UI 状态 tag 用）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Storage(ABC):
    #: 内部标识："local" | "baidu"。
    name: str = ""
    #: 菜单展示名。
    label: str = ""

    @abstractmethod
    def is_ready(self) -> bool:
        """是否可用（本地恒 True；百度需已连接）。"""

    @abstractmethod
    def status_label(self) -> str:
        """状态 tag 文案。"""

    @abstractmethod
    def stage_dir(self, category: str, book_title: str) -> Path:
        """返回成品写入目录（本地=最终目录；网盘=临时目录）。

        :param category: "漫画" / "小说"。
        """

    @abstractmethod
    def commit(self, path: Path, category: str, book_title: str) -> str:
        """一卷成品写好后调用；返回该成品的最终可读位置。"""

    def exists(self, category: str, book_title: str, filename: str) -> bool:
        """该成品是否已存在于本存储（用于下载前跳过已有卷）。默认 False（不跳过）。"""
        return False
