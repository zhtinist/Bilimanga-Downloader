"""应用编排入口：集中加载全部插件，再进入终端界面。

对外入口 :func:`main`（start.py 调用）。插件在此统一加载，避免依赖 import 顺序。
"""
from __future__ import annotations

from typing import List, Optional


def load_plugins() -> None:
    """导入各插件包以触发注册（幂等）。"""
    from . import sources, packagers, storage  # noqa: F401


def main(argv: Optional[List[str]] = None) -> int:
    load_plugins()
    from .ui.cli import main as cli_main
    return cli_main(argv)
