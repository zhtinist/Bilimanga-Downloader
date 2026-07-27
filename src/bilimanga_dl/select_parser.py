"""解析章号选择表达式，例如 ``1-9,15,19,20-25``。

- 逗号分隔多个片段
- ``a-b`` 表示闭区间 [a, b]
- 允许空白、中文逗号、中文连字符
- 返回去重升序的整数列表
"""

from __future__ import annotations

from typing import List


def parse_selection(expr: str, max_index: int) -> List[int]:
    """把选择表达式解析为章号列表。

    :param expr: 如 ``"1-9,15,19,20-25"``
    :param max_index: 合法章号的上界（含）
    :raises ValueError: 表达式非法或越界
    """
    if expr is None:
        raise ValueError("选择表达式为空")

    # 归一化中文标点与空白
    normalized = (
        expr.replace("，", ",")
        .replace("－", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("~", "-")
        .replace(" ", "")
        .replace("\t", "")
    )
    if not normalized:
        raise ValueError("选择表达式为空")

    result: set[int] = set()
    for part in normalized.split(","):
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2 or not bounds[0] or not bounds[1]:
                raise ValueError(f"区间格式错误：{part!r}（应形如 20-25）")
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                raise ValueError(f"区间必须为数字：{part!r}")
            if start > end:
                start, end = end, start
            for n in range(start, end + 1):
                result.add(n)
        else:
            try:
                result.add(int(part))
            except ValueError:
                raise ValueError(f"章号必须为数字：{part!r}")

    if not result:
        raise ValueError("未解析出任何章号")

    bad = [n for n in result if n < 1 or n > max_index]
    if bad:
        raise ValueError(
            f"章号超出范围 1-{max_index}: {sorted(bad)}"
        )
    return sorted(result)
