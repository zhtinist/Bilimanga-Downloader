"""命令行主流程（纯命令行工具；图形界面见浏览器插件 crx/）。"""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from typing import List, Optional

from .build_epub import build_epub
from .build_pdf import build_pdf
from .config import Config, TEMP_DOWNLOAD_DIR
from .downloader import Downloader, cleanup_book_temp, safe_name
from .logutil import debug_requested, get_logger, setup_logging
from .models import Book
from .net import Net
from .scraper import Scraper, parse_book_no
from .ui.picker import pick_volumes
from .ui.settings import open_settings

try:
    from rich.console import Console
    from rich.progress import (BarColumn, Progress, TextColumn,
                               TimeRemainingColumn)
    _console: Optional[Console] = Console()
except Exception:  # rich 不是硬依赖
    _console = None


def _print(msg: str = "") -> None:
    if _console:
        _console.print(msg)
    else:
        # 去掉 rich 标记再打印
        import re
        print(re.sub(r"\[/?[a-z0-9 #]+\]", "", msg))


def _rule(title: str) -> None:
    if _console:
        _console.rule(f"[bold cyan]{title}")
    else:
        print("\n" + "=" * 4 + f" {title} " + "=" * 4)


def _prompt(msg: str) -> str:
    return input(msg)


# ---------------- 各步骤 ----------------
def choose_format(config: Config) -> str:
    _rule("第 4 步 / 共 4 步：选择输出格式")
    default = config.default_format
    _print("可选格式：epub（电子书阅读器）/ pdf（通用，按原图整页排版）")
    while True:
        ans = _prompt(f"→ 输入 epub 或 pdf（直接回车用默认 {default}）：").strip().lower()
        if not ans:
            return default
        if ans in ("epub", "pdf"):
            return ans
        _print("  请输入 epub 或 pdf。")


def run_download(config: Config, url_or_no: str) -> None:
    # 输出目录：已配置用配置值，否则默认浏览器下载目录 ~/Downloads。
    out_root = config.output_path()

    net = Net(config)
    scraper = Scraper(net)

    # 解析书号（本地，无需联网）
    try:
        book_no = parse_book_no(url_or_no)
    except ValueError as exc:
        _print(f"[red]{exc}[/red]")
        net.close()
        return

    # 第 1 步：确认漫画
    _rule("第 1 步 / 共 4 步：确认漫画")
    detail_url = f"{config.site}/detail/{book_no}.html"
    net.warm_up(detail_url)  # 后台启动 Chrome + 预过 Cloudflare，隐藏冷启动耗时

    def _parse_book() -> Optional[Book]:
        _print("正在解析……（首次需启动浏览器过 Cloudflare，约 10–20 秒，可加 --debug 看详情）")
        try:
            b = scraper.fetch_book(book_no)
        except Exception as exc:
            _print(f"[red]获取书籍信息失败：{exc}[/red]")
            return None
        if not b.volumes:
            _print("[red]未解析到任何章节，可能是页面结构变化或该书需要登录。[/red]")
            return None
        return b

    book: Optional[Book] = None
    if config.confirm_open_browser:
        # 弹网页核对：先秒开浏览器详情页，隐藏自动化浏览器冷启动耗时
        try:
            webbrowser.open(detail_url)
            _print(f"  已在浏览器打开主页供你核对：{detail_url}")
        except Exception:
            _print(f"  主页：{detail_url}")
    else:
        # 不弹网页：先解析拿到书名，仅在命令行打印作品名字供核对
        book = _parse_book()
        if book is None:
            net.close()
            return
        _print(f"  作品名字：[bold]{book.title}[/bold]　{book.author}　共 {len(book.volumes)} 章")

    ans = _prompt("→ 是这本吗？回车/y 确认，n 取消：").strip().lower()
    if ans not in ("", "y", "yes"):
        _print("已取消。")
        net.close()
        return

    # 第 2 步：解析目录（命令行核对模式已在上一步解析完成）
    _rule("第 2 步 / 共 4 步：解析目录")
    if book is None:
        book = _parse_book()
        if book is None:
            net.close()
            return
    _print(f"  [bold]{book.title}[/bold]　{book.author}　共 {len(book.volumes)} 章")

    # 第 3 步（选章）
    _rule("第 3 步 / 共 4 步：选择要下载的章")
    selected = pick_volumes(book.volumes, use_terminal=True)
    if not selected:
        _print("未选择任何章，已取消。")
        net.close()
        return

    # 第 4 步：格式
    fmt = choose_format(config)

    # 下载：临时图片放 temp/download/<书名>/，成品放 downloads/<书名>/
    target = out_root / safe_name(book.title)
    target.mkdir(parents=True, exist_ok=True)
    _rule("开始下载（流水线：下载→校对→打包，逐卷产出）")
    _print(f"输出目录：[bold]{target}[/bold]")
    downloader = Downloader(net, scraper, config)
    index_map = {v.index: v for v in book.volumes}
    volumes = [index_map[i] for i in selected]
    build_fn = build_epub if fmt == "epub" else build_pdf

    outputs = []
    interrupted = False
    try:
        outputs = _run_pipeline_with_progress(
            downloader, book, volumes, TEMP_DOWNLOAD_DIR, target, build_fn)
    except KeyboardInterrupt:
        interrupted = True
        _print("\n[yellow]已中断下载，正在清理临时文件…[/yellow]")
    except Exception as exc:  # noqa: BLE001
        interrupted = True
        _print(f"\n[red]下载出错：{exc}，正在清理临时文件…[/red]")
    finally:
        # 无论正常/异常/中断：关闭浏览器释放内存，并清理该书临时图片。
        net.close()
        if interrupted:
            cleanup_book_temp(TEMP_DOWNLOAD_DIR, book.title)
        else:
            # 正常结束：临时目录已在打包时逐卷删除，兜底清掉可能残留的空目录
            book_temp = TEMP_DOWNLOAD_DIR / safe_name(book.title)
            try:
                if book_temp.exists() and not any(book_temp.iterdir()):
                    book_temp.rmdir()
            except OSError:
                pass

    if not interrupted:
        _print(f"\n[bold green]全部完成，共 {len(outputs)} 个文件 → {target}[/bold green]")


def _run_pipeline_with_progress(downloader: Downloader, book: Book, volumes,
                                temp_dir, out_dir, build_fn):
    """每个选中卷一条进度条：下载→校对→打包→✓完成（逐卷产出）。"""
    titles = {v.index: v.title for v in volumes}

    if _console:
        from rich.progress import SpinnerColumn
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=_console,
        ) as progress:
            tasks = {v.index: progress.add_task(f"⏳ 等待  {v.title}", total=None,
                                                start=False) for v in volumes}
            conc_task = progress.add_task("🧵 并发线程：4", total=1, completed=0)

            def on_concurrency(n):
                progress.update(conc_task, description=f"🧵 并发线程：{n}")

            def on_start(vidx):
                progress.start_task(tasks[vidx])
                progress.update(tasks[vidx], description=f"⬇ 下载  {titles[vidx]}")

            def on_total(vidx, n):
                progress.update(tasks[vidx], total=max(n, 1))

            def on_image(vidx):
                progress.advance(tasks[vidx], 1)

            def on_phase(vidx, phase):
                label = {"validate": "🔍 校对", "package": "📦 打包"}.get(phase, phase)
                progress.update(tasks[vidx], description=f"{label}  {titles[vidx]}")

            def on_done(vidx, path):
                t = tasks[vidx]
                total = progress.tasks[t].total or 1
                if path:
                    progress.update(t, completed=total,
                                    description=f"[green]✓ 完成  {path.name}[/green]")
                else:
                    progress.update(t, completed=total,
                                    description=f"[yellow]⚠ 无内容  {titles[vidx]}[/yellow]")

            return downloader.run_pipeline(
                book, volumes, temp_dir, out_dir, build_fn,
                on_start=on_start, on_total=on_total, on_image=on_image,
                on_phase=on_phase, on_done=on_done, on_concurrency=on_concurrency)
    else:
        def on_start(vidx):
            print(f"⬇ 开始下载 卷{vidx}", flush=True)

        def on_done(vidx, path):
            print(f"✓ 完成 卷{vidx}：{path.name if path else '无内容'}", flush=True)

        def on_concurrency(n):
            print(f"🧵 并发线程：{n}", flush=True)

        return downloader.run_pipeline(
            book, volumes, temp_dir, out_dir, build_fn,
            on_start=on_start, on_done=on_done, on_concurrency=on_concurrency)


def _print_help() -> None:
    _print("用法：")
    _print("  python3 start.py                启动原生图形界面（独立窗口）")
    _print("  python3 start.py --cli          进入交互式命令行菜单")
    _print("  python3 start.py <URL>          直接下载指定漫画")
    _print("  python3 start.py --debug        开启调试日志")
    _print("  python3 start.py --out <目录>   本次输出到指定目录（不写回设置）")
    _print("  下载输出目录默认用浏览器下载目录 ~/Downloads，可在「设置」里修改。")
    _print("  输入支持三种：详情页链接 / 目录页链接 / 书号，例如：")
    _print("    python3 start.py https://www.bilimanga.net/detail/703.html")
    _print("    python3 start.py https://www.bilimanga.net/read/703/catalog")
    _print("    python3 start.py 703")


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    config = Config.load()

    # 调试日志：命令行 --debug / 环境变量 / 配置任一开启即生效
    debug_on = debug_requested(config.debug)
    log_path = setup_logging(debug_on)
    if debug_on:
        _print(f"[调试] 日志已开启，写入：{log_path}")

    if "--help" in argv or "-h" in argv:
        _print_help()
        return 0
    # --out <目录>：本次运行临时指定输出目录（不写回设置）
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            config.output_dir = argv[i + 1]
            del argv[i:i + 2]

    force_cli = "--cli" in argv
    # 去掉开关参数，剩下的第一个非开关项当作 URL
    argv = [a for a in argv if a not in ("--debug", "--cli", "--ui")]

    # 命令行：带 URL 参数直接下载
    if argv:
        run_download(config, argv[0])
        return 0

    # 无 URL 参数：默认启动原生图形界面；--cli 才进终端菜单
    if not force_cli:
        try:
            from .gui import run as gui_run
            return gui_run(config)
        except Exception as exc:  # noqa: BLE001 —— 无显示环境等，退回终端菜单
            _print(f"[yellow]图形界面无法启动（{exc}），改用命令行菜单。[/yellow]")

    # 交互式菜单
    while True:
        _rule("bilimanga.net 漫画下载器")
        _print("  1) 下载漫画")
        _print("  2) 设置")
        _print("  3) 退出")
        choice = _prompt("→ 请选择 [1]：").strip() or "1"
        if choice == "1":
            _print("支持以下任一输入（三选一）：")
            _print("  · 详情页链接：https://www.bilimanga.net/detail/703.html")
            _print("  · 目录页链接：https://www.bilimanga.net/read/703/catalog")
            _print("  · 漫画书号：  703")
            url = _prompt("→ 请粘贴链接或书号：").strip()
            if url:
                run_download(config, url)
        elif choice == "2":
            open_settings(config, use_terminal=True)
        elif choice in ("3", "q", "quit", "exit"):
            _print("再见！")
            return 0
        else:
            _print("无效选择，请输入 1 / 2 / 3。")
