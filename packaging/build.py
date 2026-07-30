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

# Windows 控制台默认 cp1252 编码无法输出中文，会让脚本在收尾打印时崩溃；
# 统一把标准输出/错误切到 UTF-8（Python 3.7+）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ENTRY = ROOT / "packaging" / "app_entry.py"
NAME = "Bilimanga-Downloader"


def _make_icon(cover: Path):
    """把封面 PNG 转成平台图标：Windows→.ico，macOS→.icns。失败则返回 None（不影响打包）。"""
    try:
        from PIL import Image
        out_dir = ROOT / "build"
        out_dir.mkdir(parents=True, exist_ok=True)
        img = Image.open(cover).convert("RGBA")
        if sys.platform.startswith("win"):
            ico = out_dir / "app.ico"
            img.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            return ico
        if sys.platform == "darwin":
            icns = out_dir / "app.icns"
            try:
                img.save(icns)   # Pillow 在 macOS 上可写 icns
                return icns
            except Exception:
                return None
        return None
    except Exception:
        return None


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
        # 控制台应用：双击在终端里跑交互式命令行下载器（无图形界面）。
        "--console",
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
    # 封面图转成可执行文件图标（有则用，无则跳过，不影响打包）。
    cover = ROOT / "resource" / "app_cover.png"
    if cover.exists():
        icon = _make_icon(cover)
        if icon:
            args += ["--icon", str(icon)]

    import PyInstaller.__main__ as pyi
    pyi.run(args)

    out = ROOT / "dist"
    print("\n打包完成，产物在：", out)
    for p in sorted(out.iterdir()):
        print("  -", p.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
