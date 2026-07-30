#!/bin/bash
# macOS 双击启动：在终端里运行命令行下载器。
# 首次运行会自动创建 .venv 并安装依赖（需本机已装 python3）。
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec python3 start.py "$@"
