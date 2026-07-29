"""配置读写。

所有运行时生成的文件都放在**项目根目录**内（不外溢到用户主目录）：
- 设置文件：``<root>/config/setting.json``
- 日志：      ``<root>/logs/``
- 下载输出：  ``<root>/downloads/<书名>/``
使用 :mod:`pathlib` 保证 macOS / Windows 行为一致。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List


# 项目根目录：本文件位于 <root>/src/bilimanga_dl/config.py，向上三级即根目录。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_PATH = CONFIG_DIR / "setting.json"
LOG_DIR = PROJECT_ROOT / "logs"

# 下载目录固定为项目根目录下的 downloads/，不可配置；最终按书名分子目录打包。
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
# 下载过程中的临时图片目录：temp/download/<书名>/<章>/，边下边落盘，避免内存堆积。
TEMP_DOWNLOAD_DIR = PROJECT_ROOT / "temp" / "download"

# 站点的历史/镜像域名，主域名不可用时自动向后尝试。
DEFAULT_MIRRORS: List[str] = [
    "https://www.bilimanga.net",
    "https://www.bilicomic.net",
    "https://www.linovelib.com",
]


@dataclass
class Config:
    # 输出格式（下载目录固定为 DOWNLOADS_DIR，不在此配置）
    default_format: str = "epub"  # epub | pdf

    # 网络
    mirrors: List[str] = field(default_factory=lambda: list(DEFAULT_MIRRORS))
    parallel_chapters: int = 12  # 并发上限（自适应 AIMD 从低起步，最多爬到这个值）
    request_timeout: int = 30

    # 优化开关（3：限速 / 退避重试，二者独立，可同时开启）
    rate_limit_enabled: bool = False   # 默认不限速，尽可能快
    rate_limit_min_ms: int = 200      # 每次请求之间的最小随机间隔
    rate_limit_max_ms: int = 600      # 每次请求之间的最大随机间隔
    retry_enabled: bool = True
    retry_max_attempts: int = 5
    retry_backoff_base: float = 1.5   # 指数退避基数（秒）

    # 断点续传（1）：已完整下载的文件自动跳过
    resume_enabled: bool = True

    # 确认漫画时是否弹出浏览器打开详情页供核对。
    # 关闭则不弹网页，仅在命令行打印作品名字供核对（更安静）。
    confirm_open_browser: bool = False

    # 静默补漏轮数：首轮并发下载后对缺失图的补下轮数。
    # 越大越完整但越慢；设 0 则完全不补漏（最快，可能少量缺页）。默认 1（快且基本齐）。
    retry_missing_rounds: int = 1

    # 代理：留空=自动（用环境变量 HTTP(S)_PROXY，连不上自动改直连）；
    # 也可显式填 "http://127.0.0.1:7890" 强制走该代理。
    proxy: str = ""

    # 用真实浏览器（DrissionPage）过 Cloudflare。现代 Cloudflare 下必须开启；
    # 关闭则退回纯 requests（仅用于本地 mock 测试 / 无浏览器环境）。
    use_browser: bool = True
    browser_headless: bool = True     # 默认无头（无感，不弹自动化窗口）；如遇质询过不去可设 False
    cloudflare_wait: int = 45         # 等待质询自动通过的最长秒数

    # 浏览器可执行文件路径。留空则自动探测常见 Chrome/Edge 安装路径。
    browser_path: str = ""

    # 调试日志：默认关闭；开启后输出到终端并写入 <root>/logs/
    debug: bool = False

    # ---- 读写 ----
    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            # 只保留已知字段，兼容旧版本配置
            known = {f for f in cls().__dict__}
            data = {k: v for k, v in data.items() if k in known}
            cfg = cls(**data)
        else:
            cfg = cls()
            cfg.save()
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def to_dict(self) -> dict:
        return asdict(self)
