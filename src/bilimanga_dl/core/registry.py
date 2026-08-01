"""极简插件注册表。

三类插件各一个注册表：内容源 :data:`sources`、打包器 :data:`packagers`、
存储去向 :data:`storages`。插件用 ``@sources.register`` 等装饰器登记自身（登记的是
**类**，按需实例化）。加一个新站点/新网盘 = 新增一个文件并 register，无需改动编排层。
"""

from __future__ import annotations

from typing import Callable, List, Optional, Type, TypeVar

T = TypeVar("T")


class Registry:
    def __init__(self, label: str):
        self.label = label
        self._classes: List[type] = []

    def register(self, cls: Type[T]) -> Type[T]:
        if cls not in self._classes:
            self._classes.append(cls)
        return cls

    def all(self) -> List[type]:
        return list(self._classes)

    def find(self, pred: Callable[[type], bool]) -> Optional[type]:
        for cls in self._classes:
            try:
                if pred(cls):
                    return cls
            except Exception:  # noqa: BLE001 —— 单个插件判定异常不影响其它
                continue
        return None


sources = Registry("source")
packagers = Registry("packager")
storages = Registry("storage")
