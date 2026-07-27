"""数据结构：书籍 / 卷（章）/ 话。

术语对应 bilimanga.net 的目录层级：
- ``Book``     一本漫画
- ``Volume``   目录里的一个 ``catalog-volume``（用户口中的“章”，带章号+章标题）
- ``Chapter``  卷下面的一话（``chapter-li``），是实际有图片的阅读页
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Chapter:
    title: str
    url: str


@dataclass
class Volume:
    index: int                 # 章号，从 1 开始
    title: str                 # 章标题（catalog-volume 的 h3）
    chapters: List[Chapter] = field(default_factory=list)

    @property
    def chapter_titles(self) -> List[str]:
        return [c.title for c in self.chapters]

    def summary_line(self) -> str:
        """列表展示用：章号 + 章标题 + 包含哪几话。"""
        titles = "、".join(self.chapter_titles) if self.chapters else "（无话）"
        return f"[{self.index}] {self.title}  —— 含 {len(self.chapters)} 话: {titles}"


@dataclass
class Book:
    book_no: str
    title: str
    author: str
    cover_url: str
    summary: str
    tags: List[str] = field(default_factory=list)
    volumes: List[Volume] = field(default_factory=list)
    base_url: str = ""         # 实际连通的镜像域名
