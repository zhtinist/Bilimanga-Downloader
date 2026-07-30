"""用 PyInstaller 打包成不依赖本地 Python 的双击可执行文件。

跨平台通用：在 macOS 上产出 ``dist/Bilimanga-Downloader.app``，
在 Windows 上产出 ``dist/Bilimanga-Downloader/Bilimanga-Downloader.exe``。

本地用法：
    pip install pyinstaller -r src/requirements.txt
    python3 packaging/build.py

CI 里每个平台各跑一次本脚本即可（见 .github/workflows/release.yml）。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ENTRY = ROOT / "packaging" / "app_entry.py"
NAME = "Bilimanga-Downloader"


def main() -> int:
    try:
        import PyInstaller.__main__  # noqa: F401
    except ImportError:
        print("请先安装 PyInstaller：pip install pyinstaller", file=sys.stderr)
        return 1

    # 清理旧产物
    for d in ("build", "dist"):
        shutil.rmtree(ROOT / d, ignore_errors=True)
    spec = ROOT / f"{NAME}.spec"
    if spec.exists():
        spec.unlink()

    args = [
        str(ENTRY),
        "--name", NAME,
        "--noconfirm",
        "--clean",
        "--windowed",                 # 双击不弹终端窗口（GUI 应用）
        "--onedir",                   # 目录形式：启动快、体积友好
        "--paths", str(SRC),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT),
        # 关键依赖：确保二进制/子模块被完整收集
        "--collect-all", "DrissionPage",
        "--collect-all", "pillow_avif",   # AVIF 解码插件（含 .so/.pyd 二进制）
        "--collect-all", "ebooklib",
        "--collect-submodules", "PIL",
        "--hidden-import", "PIL._avif",
        "--hidden-import", "websocket",
        "--hidden-import", "img2pdf",
        "--hidden-import", "bs4",
        "--hidden-import", "lxml",
    ]
    # macOS：给 .app 一个更友好的 bundle id
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "net.bilimanga.downloader"]

    import PyInstaller.__main__ as pyi
    pyi.run(args)

    out = ROOT / "dist"
    print("\n打包完成，产物在：", out)
    for p in sorted(out.iterdir()):
        print("  -", p.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
