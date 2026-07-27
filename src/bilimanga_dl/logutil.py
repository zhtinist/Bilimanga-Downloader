"""日志工具。

默认完全静默（只挂 NullHandler，不产生任何输出）。
调试时通过 :func:`setup_logging` 打开：同时写终端(stderr)与日志文件。

打开方式（任一即可）：
- 命令行  ``python3 main.py --debug ...``
- 环境变量 ``BILIMANGA_DEBUG=1``
- 设置面板勾选 / config.json 里 ``"debug": true``
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import LOG_DIR

LOGGER_NAME = "bilimanga_dl"

# 库被导入时先挂 NullHandler，避免 "No handlers could be found" 且默认静默。
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


def get_logger(module: Optional[str] = None) -> logging.Logger:
    """获取带命名空间的子 logger，如 get_logger('net')。"""
    if module:
        return logging.getLogger(f"{LOGGER_NAME}.{module}")
    return logging.getLogger(LOGGER_NAME)


def setup_logging(enabled: bool, to_file: bool = True) -> Optional[Path]:
    """配置日志。返回日志文件路径（未开启或不写文件时为 None）。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False  # 不冒泡到 Python 根 logger
    # 清掉旧 handler，避免重复配置时叠加
    for h in list(logger.handlers):
        logger.removeHandler(h)

    if not enabled:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)  # 实际静默
        return None

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%H:%M:%S"
    )

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG)
    logger.addHandler(stream)

    log_path: Optional[Path] = None
    if to_file:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOG_DIR / f"bilimanga_{datetime.now():%Y%m%d_%H%M%S}.log"
            fileh = logging.FileHandler(log_path, encoding="utf-8")
            fileh.setFormatter(fmt)
            fileh.setLevel(logging.DEBUG)
            logger.addHandler(fileh)
        except OSError:
            log_path = None

    logger.debug("调试日志已开启，日志文件：%s", log_path)
    return log_path


def debug_requested(config_debug: bool = False) -> bool:
    """综合命令行/环境变量/配置判断是否开启调试日志。"""
    if config_debug:
        return True
    env = os.environ.get("BILIMANGA_DEBUG", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    return "--debug" in sys.argv
