"""打包后可执行文件的入口：启动本地网页 UI（双击即用，不依赖本地 Python）。

- 打包环境（PyInstaller，``sys.frozen``）：``bilimanga_dl`` 已随包内置，直接导入。
- 从源码直接跑本文件（调试打包逻辑）：把仓库的 ``src`` 加入模块搜索路径。
"""

import os
import sys


def _bootstrap_path() -> None:
    if getattr(sys, "frozen", False):
        return  # 打包环境：包已内置
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "..", "src")
    if os.path.isdir(src) and src not in sys.path:
        sys.path.insert(0, src)


def main() -> int:
    _bootstrap_path()
    from bilimanga_dl.config import Config
    from bilimanga_dl.logutil import debug_requested, setup_logging
    from bilimanga_dl.webui import serve

    config = Config.load()
    setup_logging(debug_requested(config.debug))
    serve(config, open_browser=True)
    return 0


def _report_crash(text: str) -> None:
    """无控制台的 GUI 打包版启动失败时，落盘 + 弹原生对话框，避免“双击没反应”。"""
    try:
        from bilimanga_dl.config import PROJECT_ROOT
        log_path = PROJECT_ROOT / "crash.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(text, encoding="utf-8")
        hint = f"\n\n详情已写入：{log_path}"
    except Exception:
        hint = ""
    msg = "启动失败：\n" + text.strip().splitlines()[-1] + hint
    try:
        if sys.platform == "darwin":
            import subprocess
            subprocess.run(["osascript", "-e",
                            f'display dialog {msg!r} buttons {{"OK"}}'], check=False)
        elif sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "Bilimanga-Downloader", 0)
        else:
            sys.stderr.write(msg + "\n")
    except Exception:
        sys.stderr.write(msg + "\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        import traceback
        _report_crash(traceback.format_exc())
        raise SystemExit(1)
