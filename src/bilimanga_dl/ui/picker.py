"""章节选择（终端交互）。

两种选法：
- **光标勾选**：方向键移动、空格勾选/取消、回车确认（questionary checkbox）。
- **输入范围**：如 ``1-9,15,20-25``（沿用 :mod:`select_parser`）。

返回：选中的章号（1 起）升序列表；返回 :data:`BACK` 表示“回上一步”；
返回 ``[]`` 表示未选任何章。非交互终端(无 TTY)自动退回纯文本范围输入。
"""

from __future__ import annotations

import sys
from typing import List, Union

from ..models import Volume
from ..select_parser import parse_selection

BACK = "__back__"          # 回上一步的哨兵
Result = Union[List[int], str]


def _has_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def pick_volumes(volumes: List[Volume], use_terminal: bool = True) -> Result:
    if not volumes:
        return []
    if not _has_tty():
        return _pick_text(volumes)
    try:
        import questionary
    except Exception:  # noqa: BLE001 —— 没装 questionary 就退回文本
        return _pick_text(volumes)

    while True:
        mode = questionary.select(
            "选择方式（↑↓ 移动，回车确认）：",
            choices=[
                "① 光标逐卷勾选（空格选/取消，回车确认）",
                "② 输入范围（如 1-9,15,20-25）",
                "③ 全选",
                "← 返回上一步",
            ],
        ).ask()
        if mode is None or mode.startswith("←"):
            return BACK
        if mode.startswith("①"):
            picked = _pick_checkbox(volumes, questionary)
            if picked is None:
                continue  # 用户取消勾选 → 回到方式菜单
            if not picked:
                questionary.print("未勾选任何章，请重选。")
                continue
            return picked
        if mode.startswith("②"):
            picked = _pick_range(volumes, questionary)
            if picked is None:
                continue
            return picked
        if mode.startswith("③"):
            return [v.index for v in volumes]


def _pick_checkbox(volumes, questionary):
    choices = [questionary.Choice(title=v.summary_line(), value=v.index)
               for v in volumes]
    ans = questionary.checkbox(
        "勾选要下载的卷（空格选/取消，a 全选，回车确认）：",
        choices=choices,
    ).ask()
    if ans is None:
        return None
    return sorted(ans)


def _pick_range(volumes, questionary):
    for v in volumes:
        questionary.print(v.summary_line())
    while True:
        expr = questionary.text(
            "输入章号范围（1-9,15,20-25；直接回车=全选；b=返回）：").ask()
        if expr is None:
            return None
        expr = expr.strip()
        if expr.lower() == "b":
            return None
        if not expr:
            return [v.index for v in volumes]
        try:
            return parse_selection(expr, len(volumes))
        except ValueError as exc:
            questionary.print(f"输入有误：{exc}，请重新输入。")


def _pick_text(volumes) -> Result:
    """无 TTY / 无 questionary 时的纯文本兜底。"""
    for v in volumes:
        print(v.summary_line())
    print("\n示例：1-9,15,20-25（逗号分隔、a-b 区间；直接回车=全选；b=返回）")
    while True:
        expr = input("→ 请输入要下载的章号：").strip()
        if expr.lower() == "b":
            return BACK
        if not expr:
            return [v.index for v in volumes]
        try:
            return parse_selection(expr, len(volumes))
        except ValueError as exc:
            print(f"输入有误：{exc}，请重新输入。")
