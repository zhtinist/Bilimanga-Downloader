"""章节选择（终端）。

打印“章号 + 章标题 + 含哪几话”列表，读入表达式（示例 1-9,15,19,20-25）。
返回选中的章号（1 起）升序列表。
"""

from __future__ import annotations

from typing import List

from ..models import Volume
from ..select_parser import parse_selection


def pick_volumes(volumes: List[Volume], use_terminal: bool = True) -> List[int]:
    """终端选章。``use_terminal`` 仅为兼容旧签名，恒为终端交互。"""
    if not volumes:
        return []
    for v in volumes:
        print(v.summary_line())
    print("\n示例输入格式：1-9,15,19,20-25   （逗号分隔、a-b 区间；直接回车=全选）")
    while True:
        expr = input("→ 请输入要下载的章号：").strip()
        if not expr:
            return [v.index for v in volumes]
        try:
            return parse_selection(expr, len(volumes))
        except ValueError as exc:
            print(f"输入有误：{exc}，请重新输入。")
