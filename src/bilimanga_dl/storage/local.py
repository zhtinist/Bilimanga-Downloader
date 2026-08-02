"""本地磁盘存储：成品直接写到用户设定的下载目录，按书名分子目录。"""

from __future__ import annotations

from pathlib import Path

from ..downloader import safe_name
from .base import Storage
from ..core.registry import storages


@storages.register
class LocalStorage(Storage):
    name = "local"
    label = "保存到本地"

    def __init__(self, out_root: Path):
        self.out_root = Path(out_root)

    def is_ready(self) -> bool:
        return True

    def status_label(self) -> str:
        return "本地"

    # 本地：stage 即最终目录（<下载目录>/<书名>/），不额外分漫画/小说层。
    def stage_dir(self, category: str, book_title: str) -> Path:
        d = self.out_root / safe_name(book_title)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def commit(self, path: Path, category: str, book_title: str) -> str:
        return str(path)  # 已在最终位置，无需搬运

    def exists(self, category: str, book_title: str, filename: str) -> bool:
        f = self.out_root / safe_name(book_title) / filename
        return f.exists() and f.stat().st_size > 0
