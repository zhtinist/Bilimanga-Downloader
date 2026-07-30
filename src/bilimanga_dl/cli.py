"""终端交互界面（唯一界面；无图形窗口）。

设计：一个可前进/回退的**步骤机**，每步 ``console.clear()`` + 顶部面包屑，
不再顺序刷屏。步骤：选类型 → 输链接 → 确认 → 选章 → 选格式 → 下载。

- 漫画：bilimanga.net，真实浏览器过 Cloudflare（:class:`Net` + :class:`Scraper`）。
- 轻小说：默认走**无浏览器手机站引擎**（:class:`MobileNovelDownloader`，快），
  失败时自动回退浏览器引擎（:class:`NovelDownloader`）。
"""

from __future__ import annotations

import sys
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
from .ui.picker import BACK, pick_volumes
from .ui.settings import open_settings

try:
    from rich.console import Console
    from rich.progress import (BarColumn, Progress, SpinnerColumn, TextColumn)
    _console: Optional[Console] = Console()
except Exception:  # rich 不是硬依赖
    _console = None

log = get_logger("cli")

# 步骤机信号
NEXT, GO_BACK, QUIT = "next", "back", "quit"

STEPS = ["输入网址", "确认", "选章", "格式", "下载"]


# ---------------- 通用输出 ----------------
def _print(msg: str = "") -> None:
    if _console:
        _console.print(msg)
    else:
        import re
        print(re.sub(r"\[/?[a-z0-9 #]+\]", "", msg))


def _clear() -> None:
    if _console:
        _console.clear()


def _breadcrumb(cur: int, kind_label: str = "") -> None:
    """顶部面包屑：已完成灰色、当前高亮、未到暗色。"""
    parts = []
    for i, name in enumerate(STEPS):
        if i < cur:
            parts.append(f"[dim]{i + 1}.{name}[/dim]")
        elif i == cur:
            parts.append(f"[bold cyan]➤ {i + 1}.{name}[/bold cyan]")
        else:
            parts.append(f"[grey37]{i + 1}.{name}[/grey37]")
    head = "  ".join(parts)
    if kind_label:
        head = f"[bold]{kind_label}[/bold]   " + head
    if _console:
        _console.rule(head)
    else:
        print("\n" + " > ".join(STEPS) + f"  (当前: {STEPS[cur]})")
    _print("[dim]提示：回车确认 · 输入 b 回上一步 · Ctrl-C 取消[/dim]\n")


# ---------------- questionary 封装（无 TTY 退回 input）----------------
def _tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def _q():
    if not _tty():
        return None
    try:
        import questionary
        return questionary
    except Exception:  # noqa: BLE001
        return None


def _ask_select(msg: str, choices: List[str]) -> Optional[str]:
    q = _q()
    if q:
        return q.select(msg, choices=choices).ask()
    # 文本兜底
    _print(msg)
    for i, c in enumerate(choices, 1):
        _print(f"  {i}) {c}")
    ans = input("→ 请选择序号：").strip()
    if ans.isdigit() and 1 <= int(ans) <= len(choices):
        return choices[int(ans) - 1]
    return None


def _ask_text(msg: str) -> Optional[str]:
    q = _q()
    if q:
        return q.text(msg).ask()
    try:
        return input(msg + " ")
    except EOFError:
        return None


def _pause(msg: str = "按回车继续……") -> None:
    try:
        input(msg)
    except EOFError:
        pass


# ---------------- 会话状态 ----------------
class _State:
    def __init__(self, config: Config):
        self.config = config
        self.is_novel: Optional[bool] = None
        self.book_no: str = ""
        self.book: Optional[Book] = None
        self.selected: List[int] = []
        self.fmt: str = "epub"
        self.net: Optional[Net] = None            # 漫画/轻小说回退浏览器时用
        self.novel_engine = None                  # Mobile 或 NovelDownloader
        self.novel_via_browser = False
        self.exit_app = False                     # 用户在入口选“退出”

    def ensure_net(self) -> Net:
        if self.net is None:
            self.net = Net(self.config)
        return self.net

    def close(self) -> None:
        if self.net is not None:
            try:
                self.net.close()
            except Exception:  # noqa: BLE001
                pass
            self.net = None
        if self.novel_engine is not None and hasattr(self.novel_engine, "close"):
            try:
                self.novel_engine.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------- 输入归类（域名优先，避免书号跨站撞车）----------------
def _classify_input(raw: str, default_is_novel: bool):
    """把用户输入归类为 (is_novel, book_no)。

    关键：**完整网址以域名为准**决定是漫画还是轻小说——
    ``bilinovel.com/novel/3`` 与 ``bilimanga.net/detail/3`` 是两本不同的书，
    只靠书号会撞车。裸书号无法从域名判断，返回的 ``is_novel`` 为
    ``default_is_novel``（交互模式传 None，由调用方追问类型）。
    """
    from .novel import parse_novel_no
    s = (raw or "").strip()
    low = s.lower()
    is_url = ("://" in low) or ("." in low and "/" in low)
    novel_site = "bilinovel.com" in low or "linovelib.com" in low or "linovelib" in low
    manga_site = "bilimanga.net" in low or "bilicomic.net" in low or "bilicomic" in low
    novel_path = "/novel/" in low
    manga_path = "/detail/" in low or "/read/" in low
    is_novel = novel_site or (novel_path and not manga_site)
    is_manga = manga_site or (manga_path and not novel_site)

    if is_novel and not is_manga:
        return True, parse_novel_no(s)
    if is_manga and not is_novel:
        return False, parse_book_no(s)
    if s.isdigit():
        return default_is_novel, s
    if is_url:
        raise ValueError(
            "无法识别该网址属于哪个站点。请粘贴完整网址：\n"
            "  轻小说 https://www.bilinovel.com/novel/3.html\n"
            "  漫画   https://www.bilimanga.net/detail/703.html\n"
            "或直接输入书号。")
    # 非 URL 且非纯数字：按默认类型尽力解析
    return default_is_novel, (parse_novel_no(s) if default_is_novel else parse_book_no(s))


# ---------------- 各步骤 ----------------
def _step_input(st: _State) -> str:
    """合并入口：粘贴任意网址自动识别漫画/轻小说；裸书号再快速追问类型。"""
    _clear()
    _breadcrumb(0)
    _print("粘贴漫画或轻小说的网址（详情页 / 目录页），或输入书号，自动识别类型：")
    _print("  漫画   https://www.bilimanga.net/detail/703.html")
    _print("  轻小说 https://www.bilinovel.com/novel/2139.html")
    _print("[dim]s=设置   q=退出[/dim]")
    raw = _ask_text("→ 网址或书号：")
    if raw is None:
        st.exit_app = True
        return QUIT
    raw = raw.strip()
    if raw.lower() in ("q", "quit", "exit"):
        st.exit_app = True
        return QUIT
    if raw.lower() in ("s", "设置", "setting", "settings"):
        open_settings(st.config, use_terminal=True)
        _pause("\n按回车返回……")
        return GO_BACK
    if not raw:
        _print("[yellow]未输入内容。[/yellow]")
        _pause("按回车重试……")
        return GO_BACK
    try:
        is_novel, book_no = _classify_input(raw, default_is_novel=None)
    except ValueError as exc:
        _print(f"[red]{exc}[/red]")
        _pause("按回车重试……")
        return GO_BACK
    if is_novel is None:
        # 裸书号无法从域名判断，快速追问一次
        kind = _ask_select("这是漫画还是轻小说？（↑↓ 选择）",
                           ["📚 漫画", "📖 轻小说", "← 重新输入"])
        if kind is None or kind.startswith("←"):
            return GO_BACK
        is_novel = kind.startswith("📖")
    st.is_novel = is_novel
    st.book_no = book_no
    return NEXT


def _step_confirm(st: _State) -> str:
    _clear()
    _breadcrumb(1, "轻小说" if st.is_novel else "漫画")
    _print("正在解析……（轻小说秒开；漫画首次需启动浏览器过 Cloudflare，约 10–20 秒）")
    book = _parse_book(st)
    if book is None:
        _print("[red]解析失败。可能是书号错误 / 网络问题 / 站点结构变化。[/red]")
        _pause("按回车返回上一步……")
        return GO_BACK
    st.book = book
    _print(f"\n  作品：[bold]{book.title}[/bold]")
    _print(f"  作者：{book.author}    共 {len(book.volumes)} 卷")
    if book.publisher:
        _print(f"  文库：{book.publisher}")
    ans = _ask_text("\n→ 是这本吗？回车/y 确认，n/b 返回：")
    if ans is None:
        return QUIT
    if ans.strip().lower() in ("", "y", "yes"):
        return NEXT
    return GO_BACK


def _step_select(st: _State) -> str:
    _clear()
    _breadcrumb(2, "轻小说" if st.is_novel else "漫画")
    result = pick_volumes(st.book.volumes, use_terminal=True)
    if result == BACK:
        return GO_BACK
    if not result:
        _print("[yellow]未选择任何卷。[/yellow]")
        _pause("按回车返回……")
        return GO_BACK
    st.selected = result
    return NEXT


def _step_format(st: _State) -> str:
    if st.is_novel:
        st.fmt = "epub"          # 轻小说固定 EPUB，自动跳过
        return NEXT
    _clear()
    _breadcrumb(3, "漫画")
    choice = _ask_select(
        "输出格式（↑↓ 选择）：",
        ["epub（电子书阅读器）", "pdf（按原图整页排版）", "← 返回上一步"],
    )
    if choice is None:
        return QUIT
    if choice.startswith("←"):
        return GO_BACK
    st.fmt = "pdf" if choice.startswith("pdf") else "epub"
    return NEXT


def _step_download(st: _State) -> str:
    _clear()
    _breadcrumb(4, "轻小说" if st.is_novel else "漫画")
    out_root = st.config.output_path()
    target = out_root / safe_name(st.book.title)
    target.mkdir(parents=True, exist_ok=True)
    _print(f"输出目录：[bold]{target}[/bold]\n")

    index_map = {v.index: v for v in st.book.volumes}
    volumes = [index_map[i] for i in st.selected if i in index_map]

    try:
        if st.is_novel:
            outputs = _run_novel_progress(st.novel_engine, st.book, volumes, target)
        else:
            outputs = _run_manga(st, volumes, target)
    except KeyboardInterrupt:
        _print("\n[yellow]已中断，清理临时文件…[/yellow]")
        cleanup_book_temp(TEMP_DOWNLOAD_DIR, st.book.title)
        _pause("按回车返回主菜单……")
        return QUIT
    except Exception as exc:  # noqa: BLE001
        _print(f"\n[red]下载出错：{exc}[/red]")
        cleanup_book_temp(TEMP_DOWNLOAD_DIR, st.book.title)
        _pause("按回车返回主菜单……")
        return QUIT

    _print(f"\n[bold green]✓ 全部完成，共 {len(outputs)} 个文件 → {target}[/bold green]")
    _pause("\n按回车返回主菜单……")
    return QUIT


# ---------------- 解析 / 下载实现 ----------------
def _parse_book(st: _State) -> Optional[Book]:
    if st.is_novel:
        return _parse_novel(st)
    net = st.ensure_net()
    try:
        detail = f"{st.config.site}/detail/{st.book_no}.html"
        net.warm_up(detail)
        return Scraper(net).fetch_book(st.book_no)
    except Exception as exc:  # noqa: BLE001
        log.warning("漫画解析失败：%s", exc)
        return None


def _parse_novel(st: _State) -> Optional[Book]:
    # 优先无浏览器手机站引擎
    from .novel_mobile import MobileNovelDownloader
    try:
        eng = MobileNovelDownloader(num_thread=4, proxy=st.config.proxy or "")
        book = eng.fetch_book(st.book_no)
        if book.volumes:
            st.novel_engine = eng
            st.novel_via_browser = False
            return book
    except Exception as exc:  # noqa: BLE001
        log.warning("手机站引擎解析失败，回退浏览器：%s", exc)
    # 回退：浏览器引擎（桌面站 linovelib）
    try:
        from .novel import NovelDownloader
        net = st.ensure_net()
        eng = NovelDownloader(net)
        book = eng.fetch_book(st.book_no)
        st.novel_engine = eng
        st.novel_via_browser = True
        return book
    except Exception as exc:  # noqa: BLE001
        log.warning("浏览器引擎也失败：%s", exc)
        return None


def _run_novel_progress(engine, book: Book, volumes, target) -> List[Path]:
    """逐卷进度条（手机站引擎与浏览器引擎接口一致，共用此函数）。"""
    outputs: List[Path] = []
    if _console:
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), TextColumn("{task.completed}/{task.total}"),
                      console=_console) as progress:
            for v in volumes:
                task = progress.add_task(f"⬇ {v.title}", total=None)

                def on_total(vi, n, _t=task):
                    progress.update(_t, total=max(n, 1))

                def on_image(vi, _t=task):
                    progress.advance(_t, 1)

                def on_phase(vi, ph, _t=task, _v=v):
                    label = {"download": "下载文本/插图", "package": "打包",
                             "empty": "无内容"}.get(ph, ph)
                    progress.update(_t, description=f"{label}  {_v.title}")

                path = engine.download_volume(book, v, target, on_phase=on_phase,
                                              on_total=on_total, on_image=on_image)
                if path:
                    outputs.append(path)
                    progress.update(task, description=f"[green]✓ {path.name}[/green]")
                else:
                    progress.update(task, description=f"[yellow]⚠ 无内容 {v.title}[/yellow]")
    else:
        for v in volumes:
            print(f"⬇ 下载卷 {v.index}. {v.title}", flush=True)
            path = engine.download_volume(book, v, target)
            print(f"  {'✓ ' + path.name if path else '⚠ 无内容'}", flush=True)
            if path:
                outputs.append(path)
    return outputs


def _run_manga(st: _State, volumes, target) -> List[Path]:
    net = st.ensure_net()
    downloader = Downloader(net, Scraper(net), st.config)
    build_fn = build_epub if st.fmt == "epub" else build_pdf
    try:
        return _run_pipeline_with_progress(
            downloader, st.book, volumes, TEMP_DOWNLOAD_DIR, target, build_fn)
    finally:
        # 正常结束兜底清空可能残留的空临时目录
        book_temp = TEMP_DOWNLOAD_DIR / safe_name(st.book.title)
        try:
            if book_temp.exists() and not any(book_temp.iterdir()):
                book_temp.rmdir()
        except OSError:
            pass


def _run_pipeline_with_progress(downloader: Downloader, book: Book, volumes,
                                temp_dir, out_dir, build_fn):
    """漫画：每个选中卷一条进度条：下载→校对→打包→✓完成。"""
    titles = {v.index: v.title for v in volumes}
    if _console:
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), TextColumn("{task.completed}/{task.total}"),
                      console=_console) as progress:
            tasks = {v.index: progress.add_task(f"⏳ 等待  {v.title}", total=None, start=False)
                     for v in volumes}
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

        return downloader.run_pipeline(book, volumes, temp_dir, out_dir, build_fn,
                                       on_start=on_start, on_done=on_done)


# ---------------- 步骤机驱动 ----------------
def _run_flow(st: _State, start_at: int = 0) -> None:
    """从 start_at 步开始跑步骤机。下载完/退出后返回。"""
    fns = [_step_input, _step_confirm, _step_select, _step_format, _step_download]
    i = start_at
    try:
        while 0 <= i < len(fns):
            sig = fns[i](st)
            if sig == NEXT:
                i += 1
            elif sig == GO_BACK:
                # 轻小说时“格式”步(索引 3)自动跳过，回退要多退一格
                i -= 1
                if i == 3 and st.is_novel:
                    i -= 1
                if i < start_at:
                    i = start_at
            else:  # QUIT
                return
    finally:
        st.close()


def run_download(config: Config, url_or_no: str) -> None:
    """命令行直接下载：按网址域名判类型（裸书号默认漫画），从“确认”步进入。"""
    st = _State(config)
    try:
        st.is_novel, st.book_no = _classify_input(url_or_no, default_is_novel=False)
    except ValueError as exc:
        _print(f"[red]{exc}[/red]")
        return
    _run_flow(st, start_at=1)   # 从“确认”开始


# ---------------- 入口 ----------------
def _print_help() -> None:
    _print("用法：")
    _print("  python3 start.py                进入终端交互界面")
    _print("  python3 start.py <链接或书号>     直接下载")
    _print("  python3 start.py --out <目录>    本次输出到指定目录")
    _print("  python3 start.py --debug         开启调试日志")
    _print("  输入支持：详情页链接 / 目录页链接 / 书号。")


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    config = Config.load()

    debug_on = debug_requested(config.debug)
    log_path = setup_logging(debug_on)
    if debug_on:
        _print(f"[调试] 日志已开启，写入：{log_path}")

    if "--help" in argv or "-h" in argv:
        _print_help()
        return 0
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            config.output_dir = argv[i + 1]
            del argv[i:i + 2]

    argv = [a for a in argv if a not in ("--debug", "--cli", "--ui")]

    # 带 URL 参数：直接下载
    if argv:
        run_download(config, argv[0])
        return 0

    # 无参数：循环进入步骤机（从入口开始，一本下完再回到入口）
    while True:
        st = _State(config)
        _run_flow(st, start_at=0)
        if st.exit_app:            # 用户在入口输入 q 退出
            _print("再见！")
            return 0
