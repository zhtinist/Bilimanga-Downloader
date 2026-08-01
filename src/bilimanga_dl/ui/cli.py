"""终端交互界面（唯一界面；无图形窗口）。

设计：一个可前进/回退的**步骤机**，每步 ``console.clear()`` + 顶部面包屑，
不再顺序刷屏。步骤：输入网址 → 确认 → 选章 → 选格式 → 下载。

本模块只负责**交互与进度展示**；解析/下载/打包/保存由插件完成——按输入选
内容源（:mod:`sources`），按设置选存储去向（:mod:`storage`），格式选打包器
（:mod:`packagers`），经 :class:`sources.base.Callbacks` 回传进度。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

# 统一 UTF-8：无论从哪个入口进来，都保证终端中文不乱码（Windows 尤其需要）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from ..config import Config, TEMP_DOWNLOAD_DIR
from ..downloader import cleanup_book_temp
from ..core.logutil import debug_requested, get_logger, setup_logging
from ..models import Book
from ..core.net import Net
from ..scraper import parse_book_no
from .picker import BACK, pick_volumes
from .settings import open_settings
from .. import sources as _sources_pkg  # noqa: F401 —— 触发内容源插件注册
from .. import packagers as _packagers_pkg  # noqa: F401
from .. import storage as _storage_pkg  # noqa: F401
from ..core.registry import sources as _source_reg

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


# ---------------- 跨下载复用的共享会话 ----------------
class _Shared:
    """跨多本下载复用的资源：Net（含惰性浏览器）与手机站轻小说引擎。

    下完一本再下一本时不重建、不重新过验证——浏览器一旦启动就长期复用，
    手机站会话/限流桶也沿用（限流冷却状态跨本保留，避免重复触发 429）。
    """

    def __init__(self, config: Config):
        self.config = config
        self._net: Optional[Net] = None
        self._sources: dict = {}       # kind -> Source 实例（会话/引擎跨本复用）
        self._baidu: Optional[tuple] = None   # (connected, nickname) 缓存
        self._onedrive: Optional[tuple] = None  # (connected, account) 缓存

    def ensure_net(self) -> Net:
        if self._net is None:
            self._net = Net(self.config)
        return self._net

    def source(self, is_novel: bool):
        kind = "novel" if is_novel else "manga"
        if kind not in self._sources:
            cls = _source_reg.find(lambda c: getattr(c, "kind", None) == kind)
            self._sources[kind] = cls(self.ensure_net(), self.config)
        return self._sources[kind]

    def storage(self, target: str = "local"):
        """按去向返回存储插件：baidu / onedrive（已连接时）或本地。"""
        if target == "baidu" and self.config.baidu_cookie:
            from ..storage.baidu import BaiduStorage
            return BaiduStorage(self.config)
        if target == "onedrive" and self.config.onedrive_refresh_token:
            from ..storage.onedrive import OneDriveStorage
            return OneDriveStorage(self.config)
        from ..storage.local import LocalStorage
        return LocalStorage(self.config.output_path())

    def baidu_status(self) -> tuple:
        """(是否已连接, 昵称)。启动/首次访问时校验一次存的登录态，失效则清缓存。"""
        if self._baidu is None:
            if self.config.baidu_cookie:
                from ..storage.baidu import BaiduClient
                nick = BaiduClient(self.config.baidu_cookie).verify()
                if nick:
                    if nick != self.config.baidu_nickname:
                        self.config.baidu_nickname = nick
                        self.config.save()
                    self._baidu = (True, nick)
                else:
                    self._baidu = (False, None)   # 登录态失效
            else:
                self._baidu = (False, None)
        return self._baidu

    def connect_baidu(self) -> bool:
        """打开浏览器登录百度，抓登录态并保存；成功发桌面通知。"""
        from ..storage.baidu import capture_login, desktop_notify
        cookie, nick = capture_login(self.ensure_net(),
                                     on_status=lambda m: _print(f"  [dim]{m}[/dim]"))
        if cookie and nick:
            self.config.baidu_cookie = cookie
            self.config.baidu_nickname = nick
            self.config.save()
            self._baidu = (True, nick)
            desktop_notify("百度云已连接", f"账号：{nick}，之后可上传到网盘。")
            return True
        return False

    def onedrive_status(self) -> tuple:
        """(是否已连接, 账号名)。启动/首次访问时用 refresh_token 校验一次，失效则清缓存。"""
        if self._onedrive is None:
            if self.config.onedrive_refresh_token:
                from ..storage.onedrive import OneDriveClient
                cli = OneDriveClient(self.config.onedrive_client_id,
                                     self.config.onedrive_refresh_token)
                acct = cli.verify()
                if acct:
                    # 令牌可能轮换，写回
                    if cli.refresh_token != self.config.onedrive_refresh_token:
                        self.config.onedrive_refresh_token = cli.refresh_token
                    if acct != self.config.onedrive_account:
                        self.config.onedrive_account = acct
                    self.config.save()
                    self._onedrive = (True, acct)
                else:
                    self._onedrive = (False, None)
            else:
                self._onedrive = (False, None)
        return self._onedrive

    def connect_onedrive(self) -> bool:
        """设备码登录 OneDrive：打印网址+验证码，用户在浏览器登录并同意；成功保存。"""
        from ..storage.onedrive import device_code_login, desktop_notify
        refresh, acct = device_code_login(
            self.config.onedrive_client_id,
            on_status=lambda m: _print(f"  [bold yellow]{m}[/bold yellow]"))
        if refresh and acct:
            self.config.onedrive_refresh_token = refresh
            self.config.onedrive_account = acct
            self.config.save()
            self._onedrive = (True, acct)
            desktop_notify("OneDrive 已连接", f"账号：{acct}，之后可上传到 OneDrive。")
            return True
        return False

    def close(self) -> None:
        for s in self._sources.values():
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        self._sources.clear()
        if self._net is not None:
            try:
                self._net.close()
            except Exception:  # noqa: BLE001
                pass
            self._net = None


# ---------------- 单本下载的流程状态 ----------------
class _State:
    def __init__(self, config: Config, shared: _Shared):
        self.config = config
        self.shared = shared
        self.is_novel: Optional[bool] = None
        self.book_no: str = ""
        self.book: Optional[Book] = None
        self.selected: List[int] = []
        self.fmt: str = "epub"
        self.source = None                        # 当前内容源插件
        self.exit_app = False                     # 用户在入口选“退出”

    def ensure_net(self) -> Net:
        return self.shared.ensure_net()

    def close(self) -> None:
        # 单本结束不关闭共享资源（跨本复用）；仅由外层在退出时统一关闭。
        pass


# ---------------- 输入归类（域名优先，避免书号跨站撞车）----------------
def _classify_input(raw: str, default_is_novel: bool):
    """把用户输入归类为 (is_novel, book_no)。

    关键：**完整网址以域名为准**决定是漫画还是轻小说——
    ``bilinovel.com/novel/3`` 与 ``bilimanga.net/detail/3`` 是两本不同的书，
    只靠书号会撞车。裸书号无法从域名判断，返回的 ``is_novel`` 为
    ``default_is_novel``（交互模式传 None，由调用方追问类型）。
    """
    from ..novel import parse_novel_no
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
    connected, nick = st.shared.baidu_status()
    if connected:
        _print(f"[green]☁ 百度云：{nick}[/green]")
    else:
        _print("[grey37]☁ 百度云：未连接[/grey37]（输入 c 连接）")
    od_conn, od_acct = st.shared.onedrive_status()
    if od_conn:
        _print(f"[green]☁ OneDrive：{od_acct}[/green]")
    else:
        _print("[grey37]☁ OneDrive：未连接[/grey37]（输入 o 连接）")
    _print("[dim]s=设置   c=连接百度云   o=连接OneDrive   q=退出[/dim]")
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
    if raw.lower() in ("c", "连接", "百度", "baidu"):
        if st.shared.baidu_status()[0]:
            _print(f"[green]已连接百度云：{st.shared.baidu_status()[1]}[/green]"
                   "（如需换号，可到设置里断开后重连）")
        else:
            _print("即将打开浏览器，请在弹出窗口登录百度账号（登录后自动检测）……")
            ok = st.shared.connect_baidu()
            _print(f"[green]✓ 已连接：{st.shared.baidu_status()[1]}[/green]" if ok
                   else "[yellow]未完成登录（超时/取消）。[/yellow]")
        _pause("\n按回车返回……")
        return GO_BACK
    if raw.lower() in ("o", "onedrive", "od"):
        if st.shared.onedrive_status()[0]:
            _print(f"[green]已连接 OneDrive：{st.shared.onedrive_status()[1]}[/green]"
                   "（如需换号，可到设置里断开后重连）")
        else:
            from ..storage.onedrive import DOC_URL
            _print(f"[cyan]配置/连接教程：{DOC_URL}[/cyan]（已尝试在浏览器打开）")
            try:
                import webbrowser
                webbrowser.open(DOC_URL)
            except Exception:  # noqa: BLE001
                pass
            _print("即将开始 OneDrive 登录，请按提示在浏览器打开网址、输入验证码并登录微软账号……")
            ok = st.shared.connect_onedrive()
            _print(f"[green]✓ 已连接：{st.shared.onedrive_status()[1]}[/green]" if ok
                   else "[yellow]未完成登录（超时/取消）。[/yellow]")
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
    _print("正在解析……（轻小说秒开；漫画偶尔需启动浏览器过 Cloudflare，约 10–20 秒）")
    st.source = st.shared.source(st.is_novel)
    try:
        book = st.source.fetch_book(st.book_no)
    except Exception as exc:  # noqa: BLE001
        log.warning("解析失败：%s", exc)
        book = None
    if book is None or not book.volumes:
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
    # 选择保存去向：本地 + 已连接的网盘（百度 / OneDrive）。只有本地时不弹选择。
    target = "local"
    options = [(f"💾 本地（{st.config.output_path()}）", "local")]
    b_conn, b_nick = st.shared.baidu_status()
    if b_conn:
        options.append((f"☁ 百度网盘（{b_nick}）", "baidu"))
    o_conn, o_acct = st.shared.onedrive_status()
    if o_conn:
        options.append((f"☁ OneDrive（{o_acct}）", "onedrive"))
    if len(options) > 1:
        labels = [o[0] for o in options] + ["← 返回上一步"]
        choice = _ask_select("保存到哪里？（↑↓ 选择）", labels)
        if choice is None:
            return QUIT
        if choice.startswith("←"):
            return GO_BACK
        target = next((t for lbl, t in options if lbl == choice), "local")
    storage = st.shared.storage(target)
    _print(f"保存去向：[bold]{storage.status_label()}[/bold]\n")
    index_map = {v.index: v for v in st.book.volumes}
    volumes = [index_map[i] for i in st.selected if i in index_map]
    try:
        locations = _download_with_progress(st.source, st.book, volumes, st.fmt, storage)
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
    _print(f"\n[bold green]✓ 全部完成，共 {len(locations)} 个文件（{storage.status_label()}）[/bold green]")
    _pause("\n按回车返回主菜单……")
    return QUIT


# ---------------- 统一下载进度（漫画/轻小说共用，经内容源+存储）----------------
def _download_with_progress(source, book: Book, volumes, fmt: str, storage) -> List[str]:
    from ..sources.base import Callbacks
    titles = {v.index: v.title for v in volumes}
    if _console:
        with Progress(SpinnerColumn(),
                      TextColumn("[progress.description]{task.description}"),
                      BarColumn(), TextColumn("{task.completed}/{task.total}"),
                      console=_console) as progress:
            tasks = {v.index: progress.add_task(f"⏳ 等待  {v.title}", total=None,
                                                start=False) for v in volumes}
            conc = progress.add_task("🧵 并发线程：-", total=1, completed=0)

            def on_concurrency(n):
                progress.update(conc, description=f"🧵 并发线程：{n}")

            def on_start(vidx):
                progress.start_task(tasks[vidx])
                progress.update(tasks[vidx], description=f"⬇ 下载  {titles[vidx]}")

            def on_total(vidx, n):
                progress.update(tasks[vidx], total=max(n, 1))

            def on_image(vidx):
                progress.advance(tasks[vidx], 1)

            def on_phase(vidx, ph):
                label = {"download": "⬇ 下载正文", "images": "🖼 下载插图",
                         "validate": "🔍 校对", "package": "📦 打包"}.get(ph)
                if label:
                    progress.update(tasks[vidx], description=f"{label}  {titles[vidx]}")

            def on_done(vidx, path):
                t = tasks[vidx]
                total = progress.tasks[t].total or 1
                if path:
                    progress.update(t, completed=total,
                                    description=f"[green]✓ 完成  {titles[vidx]}[/green]")
                else:
                    progress.update(t, completed=total,
                                    description=f"[yellow]⚠ 无内容  {titles[vidx]}[/yellow]")

            cb = Callbacks(on_start=on_start, on_total=on_total, on_image=on_image,
                           on_phase=on_phase, on_done=on_done,
                           on_concurrency=on_concurrency)
            return source.download(book, volumes, fmt, storage, cb)
    else:
        def on_start(vidx):
            print(f"⬇ 下载卷 {vidx}. {titles[vidx]}", flush=True)

        def on_done(vidx, path):
            print(f"  {'✓ 完成' if path else '⚠ 无内容'}", flush=True)

        cb = Callbacks(on_start=on_start, on_done=on_done)
        return source.download(book, volumes, fmt, storage, cb)


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
    shared = _Shared(config)
    st = _State(config, shared)
    try:
        st.is_novel, st.book_no = _classify_input(url_or_no, default_is_novel=False)
    except ValueError as exc:
        _print(f"[red]{exc}[/red]")
        return
    try:
        _run_flow(st, start_at=1)   # 从“确认”开始
    finally:
        shared.close()


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
    # 开调试时，额外在下载目录写一份 log.txt（持续更新），排查“卡住”最直观。
    log_path = setup_logging(debug_on,
                             extra_dir=config.output_path() if debug_on else None)
    if debug_on:
        _print(f"[调试] 日志已开启，写入：{log_path}")
        _print(f"[调试] 下载目录 log.txt：{config.output_path() / 'log.txt'}")

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

    # 无参数：循环进入步骤机（从入口开始，一本下完再回到入口）。
    # 共享会话跨本复用：下完一本再下别的，不重建会话、不重复过验证。
    shared = _Shared(config)
    try:
        while True:
            st = _State(config, shared)
            _run_flow(st, start_at=0)
            if st.exit_app:            # 用户在入口输入 q 退出
                _print("再见！")
                return 0
    finally:
        shared.close()
