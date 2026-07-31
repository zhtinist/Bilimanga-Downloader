#!/usr/bin/env python3
"""一键启动:自建项目内 .venv → 装依赖到该环境 → 进入下载器。

设计原则:
- **绝不往系统/全局环境装库**。所有依赖装进项目内 ``.venv``,不污染全局。
- 命令行只负责"准备环境 + 启动"这一步。

用法:
    python3 start.py                 # 进入终端交互界面
    python3 start.py <链接或书号>     # 直接下载
    python3 start.py --debug         # 调试日志
    python3 start.py --out <目录>    # 本次输出目录
"""

import os
import subprocess
import sys
from pathlib import Path

# 统一 UTF-8：Windows 终端默认 GBK/cp1252 会让中文乱码。把标准输出/错误切到
# UTF-8（Python 3.7+），并给子进程设 PYTHONUTF8，保证全程中文正常显示。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"                      # 源代码目录
REQUIREMENTS = SRC / "requirements.txt"
IMPORT_CHECK = [
    "requests", "curl_cffi", "bs4", "lxml", "rich", "DrissionPage",
    "ebooklib", "img2pdf", "PIL", "pillow_avif", "questionary",
]
# 已在目标环境里(重入标记),直接跑主程序,避免无限套娃
READY_FLAG = "BILIMANGA_ENV_READY"


def _run(cmd, **kw):
    print("＄", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, **kw)


# ---------- venv ----------
def ensure_local_venv():
    venv_dir = ROOT / ".venv"
    py = (venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    if not py.exists():
        print("正在创建项目内 .venv（不污染全局）……")
        _run([sys.executable, "-m", "venv", str(venv_dir)])
    ensure_deps(str(py))
    return str(py)


# ---------- 依赖 ----------
# 国内镜像:国际源(走代理)不稳时自动切换，通常更快且不依赖代理。
FALLBACK_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
FALLBACK_HOST = "pypi.tuna.tsinghua.edu.cn"


def deps_ok(py):
    code = "import " + ", ".join(IMPORT_CHECK)
    return subprocess.run([py, "-c", code],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def _pip_install(py, args, use_mirror=False):
    # 加大重试与超时，降低大文件下载中断概率
    cmd = [py, "-m", "pip", "install", "--retries", "5", "--timeout", "60"]
    if use_mirror:
        cmd += ["-i", FALLBACK_INDEX, "--trusted-host", FALLBACK_HOST]
    cmd += args
    _run(cmd)


def ensure_deps(py):
    if deps_ok(py):
        return
    print("正在向 .venv 安装依赖(首次运行较慢，请稍候)……")
    # pip 自升级失败不致命，忽略即可
    try:
        _pip_install(py, ["--upgrade", "pip"])
    except subprocess.CalledProcessError:
        pass
    # 先用默认源(可走代理)；网络中断/失败则自动切国内镜像重试
    try:
        _pip_install(py, ["-r", str(REQUIREMENTS)])
    except subprocess.CalledProcessError:
        print("\n下载失败(多为网络中断)，改用国内镜像(清华源)重试……\n")
        _pip_install(py, ["-r", str(REQUIREMENTS)], use_mirror=True)
    if not deps_ok(py):
        print("依赖安装后仍无法导入，请检查网络/代理后重跑 python3 start.py。")
        sys.exit(1)


def main():
    args = sys.argv[1:]

    # 已在目标环境里:直接启动主程序（把 src 加入模块搜索路径）
    if os.environ.get(READY_FLAG) == "1":
        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))
        from bilimanga_dl.cli import main as cli_main
        return cli_main(args)

    # 准备项目内 .venv
    py = ensure_local_venv()

    # 用目标环境的 python 重新启动本程序(带重入标记 + 强制 UTF-8，避免中文乱码)
    env = dict(os.environ, **{READY_FLAG: "1", "PYTHONUTF8": "1",
                              "PYTHONIOENCODING": "utf-8"})
    print(f"环境就绪，正在启动 …… (使用 {py})\n")
    proc = subprocess.run([py, str(ROOT / "start.py"), *args], env=env)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
