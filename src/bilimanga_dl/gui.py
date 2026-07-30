"""原生图形界面（Tkinter，深色主题）。

不起本地服务、不开浏览器 —— 一个独立窗口，双击可执行文件即用，避开：
①浏览器二次打开卡死；②macOS 对监听端口的可执行文件反复弹网络提示。
下载逻辑复用命令行版的 :class:`Net` / :class:`Scraper` / :class:`Downloader`。

界面分三屏（同窗切换）：
- 主屏：输入链接/书号 → 解析 → 直接在同屏选章 → 开始下载（②③合一）。
- 设置屏：点右上角「⚙ 设置」进入，「← 返回」回主屏。
- 进度屏：点「开始下载」跳转，逐卷进度；完成后「← 返回」再下一本。

深色主题：结构（Frame/Label/Entry/Checkbutton/Radiobutton）用经典 tk 控件并显式
配色 —— macOS 的原生 ttk 会忽略这些背景色，而 tk 控件能可靠上色；按钮/表格/滚动条
用 ttk + clam 主题上色。Tkinter 非线程安全：抓取/下载在后台线程跑，只用线程安全
队列回传，主线程 ``after`` 轮询刷新；布局 grid + 权重，窗口再矮也不裁切按钮。
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .build_epub import build_epub
from .build_pdf import build_pdf
from .config import Config, TEMP_DOWNLOAD_DIR, default_download_dir
from .downloader import Downloader, cleanup_book_temp, safe_name
from .logutil import get_logger
from .models import Book
from .net import Net
from .scraper import Scraper, parse_book_no

log = get_logger("gui")

# ---- 深色配色 ----
BG = "#15151b"
CARD = "#1f1f29"
CARD2 = "#2a2a37"
FG = "#e9e9f0"
MUTED = "#9a9aa8"
ACCENT = "#5b8cff"
ACCENT_ACT = "#4a77e6"
BORDER = "#33333f"
OK = "#3ecb7f"
ERR = "#ff6b6b"
WARN = "#f2b750"

FONT = ("Helvetica", 13)
FONT_SM = ("Helvetica", 12)
FONT_TITLE = ("Helvetica", 17, "bold")
FONT_BOOK = ("Helvetica", 15, "bold")

PHASE_LABEL = {
    "wait": "等待", "download": "下载中", "validate": "校对",
    "package": "打包", "done": "完成", "empty": "无内容",
}
PHASE_COLOR = {
    "wait": MUTED, "download": ACCENT, "validate": WARN,
    "package": WARN, "done": OK, "empty": ERR,
}


class DownloaderGUI:
    def __init__(self, root: tk.Tk, config: Config):
        self.root = root
        self.config = config
        self.net: Optional[Net] = None
        self.scraper: Optional[Scraper] = None
        self.book: Optional[Book] = None
        self.vol_vars: dict = {}
        self.prog_rows: dict = {}
        self._prog_state: dict = {}
        self.msgq: "queue.Queue" = queue.Queue()
        self.busy = False
        self.view = "home"
        self._dl_title = ""

        self._init_style()
        self._build()
        self._show("home")
        self.root.after(120, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 网络对象 ----------------
    def _ensure_net(self):
        if self.net is None:
            self.net = Net(self.config)
            self.scraper = Scraper(self.net)

    def _reset_net(self):
        if self.net is not None:
            try:
                self.net.close()
            except Exception:
                pass
        self.net = None
        self.scraper = None

    # ---------------- 小控件工厂（经典 tk，可靠上色）----------------
    def _frame(self, parent, bg=BG, **kw):
        return tk.Frame(parent, bg=bg, **kw)

    def _label(self, parent, text="", textvariable=None, bg=BG, fg=FG, font=FONT, **kw):
        return tk.Label(parent, text=text, textvariable=textvariable, bg=bg, fg=fg,
                        font=font, **kw)

    def _entry(self, parent, textvariable, **kw):
        return tk.Entry(parent, textvariable=textvariable, bg=CARD, fg=FG, font=FONT,
                        insertbackground=FG, relief="flat", highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=ACCENT, **kw)

    def _check(self, parent, text, var):
        return tk.Checkbutton(parent, text=text, variable=var, bg=CARD, fg=FG,
                              selectcolor=CARD2, activebackground=CARD, activeforeground=FG,
                              anchor="w", font=FONT_SM, highlightthickness=0, bd=0,
                              padx=8, pady=2)

    def _radio(self, parent, text, value, var, bg=BG):
        return tk.Radiobutton(parent, text=text, value=value, variable=var, bg=bg, fg=FG,
                              selectcolor=CARD2, activebackground=bg, activeforeground=FG,
                              font=FONT_SM, highlightthickness=0, bd=0)

    # ---------------- ttk 主题（按钮/表格/滚动条）----------------
    def _init_style(self):
        self.root.configure(bg=BG)
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TButton", background=CARD2, foreground=FG, borderwidth=0,
                     focusthickness=0, padding=(14, 8), font=FONT_SM)
        st.map("TButton", background=[("active", BORDER), ("pressed", BORDER)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                     font=("Helvetica", 13, "bold"), padding=(18, 9), borderwidth=0)
        st.map("Accent.TButton",
               background=[("active", ACCENT_ACT), ("pressed", ACCENT_ACT), ("disabled", BORDER)],
               foreground=[("disabled", MUTED)])
        st.configure("Ghost.TButton", background=BG, foreground=MUTED, padding=(10, 6),
                     borderwidth=0)
        st.map("Ghost.TButton", background=[("active", CARD)], foreground=[("active", FG)])

        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                     borderwidth=0, rowheight=30, font=FONT_SM)
        st.configure("Treeview.Heading", background=CARD2, foreground=MUTED, borderwidth=0,
                     font=("Helvetica", 11))
        st.map("Treeview", background=[("selected", CARD2)], foreground=[("selected", FG)])
        st.configure("Vertical.TScrollbar", background=CARD2, troughcolor=BG,
                     bordercolor=BG, arrowcolor=MUTED, borderwidth=0)
        st.map("Vertical.TScrollbar", background=[("active", BORDER)])

    # ---------------- 布局 ----------------
    def _build(self):
        self.root.title("bilimanga 漫画下载器")
        self.root.geometry("720x640")
        self.root.minsize(600, 460)

        top = self._frame(self.root)
        top.pack(fill="x", padx=16, pady=(14, 6))
        self.logo_holder = self._frame(top)   # 封面缩略图（若有）挂这里
        self.logo_holder.pack(side="left")
        self._label(top, "bilimanga 漫画下载器", font=FONT_TITLE).pack(side="left")
        self.nav_btn = ttk.Button(top, text="⚙ 设置", style="Ghost.TButton",
                                  command=self._nav_click)
        self.nav_btn.pack(side="right")

        self.container = self._frame(self.root)
        self.container.pack(fill="both", expand=True, padx=16, pady=(6, 14))
        self.container.rowconfigure(0, weight=1)
        self.container.columnconfigure(0, weight=1)

        self.home = self._frame(self.container)
        self.settings = self._frame(self.container)
        self.progress = self._frame(self.container)
        for f in (self.home, self.settings, self.progress):
            f.grid(row=0, column=0, sticky="nsew")

        self._build_home()
        self._build_settings()
        self._build_progress()

    def _build_home(self):
        f = self.home
        f.columnconfigure(0, weight=1)
        f.rowconfigure(3, weight=1)

        row = self._frame(f)
        row.grid(row=0, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)
        self.var_input = tk.StringVar()
        ent = self._entry(row, self.var_input)
        ent.grid(row=0, column=0, sticky="ew", ipady=5)
        ent.bind("<Return>", lambda e: self._on_parse())
        self.btn_parse = ttk.Button(row, text="解析", style="Accent.TButton", command=self._on_parse)
        self.btn_parse.grid(row=0, column=1, padx=(10, 0))
        self._label(f, "粘贴详情页 / 目录页链接或书号，例如 54", fg=MUTED, font=FONT_SM).grid(
            row=1, column=0, sticky="w", pady=(6, 12))

        head = self._frame(f)
        head.grid(row=2, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.var_book = tk.StringVar(value="")
        self._label(head, textvariable=self.var_book, font=FONT_BOOK, anchor="w",
                    justify="left", wraplength=520).grid(row=0, column=0, sticky="w")
        selbar = self._frame(head)
        selbar.grid(row=0, column=1, sticky="e")
        ttk.Button(selbar, text="全选", style="Ghost.TButton",
                   command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(selbar, text="全不选", style="Ghost.TButton",
                   command=lambda: self._set_all(False)).pack(side="left")

        card = self._frame(f, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=3, column=0, sticky="nsew", pady=(8, 12))
        self.vol_canvas = tk.Canvas(card, bg=CARD, highlightthickness=0)
        vsb = ttk.Scrollbar(card, orient="vertical", command=self.vol_canvas.yview)
        self.vol_inner = tk.Frame(self.vol_canvas, bg=CARD)
        self.vol_inner.bind("<Configure>",
                            lambda e: self.vol_canvas.configure(scrollregion=self.vol_canvas.bbox("all")))
        self._vol_win = self.vol_canvas.create_window((0, 0), window=self.vol_inner, anchor="nw")
        self.vol_canvas.bind("<Configure>",
                             lambda e: self.vol_canvas.itemconfigure(self._vol_win, width=e.width))
        self.vol_canvas.configure(yscrollcommand=vsb.set)
        self.vol_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.vol_canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.var_empty = tk.StringVar(value="解析后在此选择要下载的章。")
        self.lbl_empty = self._label(self.vol_inner, textvariable=self.var_empty, bg=CARD,
                                     fg=MUTED, font=FONT_SM, anchor="w", justify="left")
        self.lbl_empty.pack(fill="x", padx=12, pady=12)

        bottom = self._frame(f)
        bottom.grid(row=4, column=0, sticky="ew")
        bottom.columnconfigure(2, weight=1)
        self.var_fmt = tk.StringVar(value=self.config.default_format or "epub")
        self._radio(bottom, "EPUB", "epub", self.var_fmt).grid(row=0, column=0)
        self._radio(bottom, "PDF", "pdf", self.var_fmt).grid(row=0, column=1, padx=(14, 0))
        self.btn_start = ttk.Button(bottom, text="开始下载", style="Accent.TButton",
                                    command=self._on_download, state="disabled")
        self.btn_start.grid(row=0, column=3, sticky="e")

    def _build_settings(self):
        f = self.settings
        f.columnconfigure(1, weight=1)
        pad = {"padx": 8, "pady": 8}
        self._label(f, "设置", font=FONT_TITLE).grid(row=0, column=0, columnspan=3,
                                                    sticky="w", pady=(0, 10))

        self._label(f, "站点地址").grid(row=1, column=0, sticky="w", **pad)
        self._label(f, self.config.site, fg=MUTED).grid(row=1, column=1, columnspan=2,
                                                        sticky="w", **pad)

        self._label(f, "下载目录").grid(row=2, column=0, sticky="w", **pad)
        self.var_out = tk.StringVar(value=self.config.output_dir or "")
        self._entry(f, self.var_out).grid(row=2, column=1, sticky="ew", ipady=4, **pad)
        ttk.Button(f, text="浏览…", command=self._browse_out).grid(row=2, column=2, **pad)

        self._label(f, "代理").grid(row=3, column=0, sticky="w", **pad)
        self.var_proxy = tk.StringVar(value=self.config.proxy or "")
        self._entry(f, self.var_proxy).grid(row=3, column=1, columnspan=2, sticky="ew", ipady=4, **pad)

        self.var_hint = tk.StringVar()
        self._label(f, textvariable=self.var_hint, fg=MUTED, font=FONT_SM).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 4))
        self._label(f, "下载线程数自动调节（4 起步），无需设置。",
                    fg=MUTED, font=FONT_SM).grid(row=5, column=0, columnspan=3, sticky="w",
                                                 padx=8, pady=(0, 4))
        self._label(f, "轻小说粘贴 linovelib 链接即可，无需登录；若提示被限流，稍等几分钟重试。",
                    fg=MUTED, font=FONT_SM).grid(row=6, column=0, columnspan=3, sticky="w",
                                                 padx=8, pady=(0, 12))
        self._refresh_hint()

        ttk.Button(f, text="保存设置", style="Accent.TButton", command=self._save_cfg).grid(
            row=7, column=0, columnspan=3, sticky="e", pady=(6, 0), padx=8)

    def _build_progress(self):
        f = self.progress
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        head = self._frame(f)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        head.columnconfigure(0, weight=1)
        self._label(head, "下载进度", font=FONT_TITLE).grid(row=0, column=0, sticky="w")
        self.var_conc = tk.StringVar(value="🧵 并发线程：4")
        self._label(head, textvariable=self.var_conc, fg=ACCENT, font=FONT_SM).grid(
            row=0, column=1, sticky="e")
        wrap = self._frame(f, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        wrap.grid(row=1, column=0, sticky="nsew")
        self.tree = ttk.Treeview(wrap, columns=("state", "prog"), show="tree headings")
        self.tree.heading("#0", text="卷")
        self.tree.heading("state", text="状态")
        self.tree.heading("prog", text="进度")
        self.tree.column("#0", width=360, anchor="w")
        self.tree.column("state", width=90, anchor="center")
        self.tree.column("prog", width=160, anchor="center")
        tvsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tvsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tvsb.pack(side="right", fill="y")
        for ph, col in PHASE_COLOR.items():
            self.tree.tag_configure(ph, foreground=col)

        row = self._frame(f)
        row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        row.columnconfigure(0, weight=1)
        self.var_status = tk.StringVar(value="")
        self._label(row, textvariable=self.var_status, fg=MUTED, font=FONT_SM).grid(
            row=0, column=0, sticky="w")
        self.btn_reveal = ttk.Button(row, text="打开输出目录", style="Ghost.TButton",
                                     command=self._reveal, state="disabled")
        self.btn_reveal.grid(row=0, column=1, sticky="e")

    # ---------------- 视图切换 ----------------
    def _show(self, name: str):
        self.view = name
        {"home": self.home, "settings": self.settings, "progress": self.progress}[name].tkraise()
        if name == "home":
            self.nav_btn.configure(text="⚙ 设置", state="normal")
        elif name == "settings":
            self.nav_btn.configure(text="← 返回", state="normal")
        else:
            self.nav_btn.configure(text="← 返回", state=("disabled" if self.busy else "normal"))

    def _nav_click(self):
        self._show("settings" if self.view == "home" else "home")

    def _on_wheel(self, event):
        if self.view == "home":
            self.vol_canvas.yview_scroll(int(-1 * (event.delta / 40)), "units")

    # ---------------- 设置 ----------------
    def _refresh_hint(self):
        eff = (self.var_out.get().strip() or str(default_download_dir()))
        self.var_hint.set(f"文件将保存到：{Path(eff).expanduser()}（留空 = 浏览器下载目录）")

    def _browse_out(self):
        init = self.var_out.get().strip() or str(default_download_dir())
        d = filedialog.askdirectory(initialdir=init, title="选择下载目录")
        if d:
            self.var_out.set(d)
            self._refresh_hint()

    def _apply_cfg(self) -> bool:
        c = self.config
        # 站点地址固定，仅展示、不支持修改
        out = self.var_out.get().strip()
        if out:
            try:
                Path(out).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("目录无效", f"无法创建该目录：{exc}")
                return False
        c.output_dir = out
        if self.var_fmt.get() in ("epub", "pdf"):
            c.default_format = self.var_fmt.get()
        c.proxy = self.var_proxy.get().strip()
        return True

    def _save_cfg(self):
        if self._apply_cfg():
            self.config.save()
            self._refresh_hint()
            self._show("home")

    # ---------------- 解析 ----------------
    def _on_parse(self):
        if self.busy:
            return
        text = self.var_input.get().strip()
        if not text:
            messagebox.showinfo("提示", "请输入详情页链接、目录页链接或书号。")
            return
        self._apply_cfg()
        self.busy = True
        self.btn_parse.configure(state="disabled", text="解析中…")
        self.btn_start.configure(state="disabled")
        self.var_empty.set("正在解析……首次需启动浏览器过 Cloudflare（约 10–20 秒）。")
        threading.Thread(target=self._parse_worker, args=(text,), daemon=True).start()

    def _parse_worker(self, text: str):
        try:
            from .novel import NovelDownloader, is_novel_url, parse_novel_no
            self._reset_net()
            self._ensure_net()
            if is_novel_url(text):
                book = NovelDownloader(self.net).fetch_book(parse_novel_no(text))
            else:
                book = self.scraper.fetch_book(parse_book_no(text))
            if not book.volumes:
                raise RuntimeError("未解析到任何章节/卷，可能页面结构变化或该书需登录。")
            self.msgq.put(("parse_done", book))
        except Exception as exc:  # noqa: BLE001
            self.msgq.put(("parse_err", str(exc)))

    def _render_book(self, book: Book):
        self.book = book
        self.var_book.set(f"《{book.title}》  {book.author} · 共 {len(book.volumes)} 章")
        for w in self.vol_inner.winfo_children():
            w.destroy()
        self.vol_vars = {}
        for v in book.volumes:
            var = tk.BooleanVar(value=True)
            self.vol_vars[v.index] = var
            self._check(self.vol_inner, f"{v.index}.  {v.title}   （{len(v.chapters)} 话）",
                        var).pack(anchor="w", fill="x", pady=1)
        self.btn_start.configure(state="normal")

    def _set_all(self, val: bool):
        for var in self.vol_vars.values():
            var.set(val)

    # ---------------- 下载 ----------------
    def _on_download(self):
        if self.busy or not self.book:
            return
        selected = [i for i, var in self.vol_vars.items() if var.get()]
        if not selected:
            messagebox.showinfo("提示", "请至少选择一章。")
            return
        self._apply_cfg()
        out_root = self.config.output_path()
        index_map = {v.index: v for v in self.book.volumes}
        vols = [index_map[i] for i in selected]

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.prog_rows = {}
        self._prog_state = {}
        for v in vols:
            iid = self.tree.insert("", "end", text=f"{v.index}. {v.title}",
                                   values=("等待", ""), tags=("wait",))
            self.prog_rows[v.index] = iid

        self.busy = True
        self._dl_title = self.book.title  # 供中断清理定位 temp
        self.btn_reveal.configure(state="disabled")
        self.var_conc.set("🧵 并发线程：4")
        self.var_status.set(f"下载中，输出到：{out_root}")
        self._show("progress")
        threading.Thread(target=self._download_worker, args=(vols, out_root),
                         daemon=True).start()

    def _download_worker(self, vols, out_root):
        book = self.book
        target = out_root / safe_name(book.title)
        q = self.msgq
        try:
            target.mkdir(parents=True, exist_ok=True)
            self._ensure_net()

            # 轻小说：文字型 EPUB，逐卷产出（不走漫画图片流水线）
            if book.kind == "novel":
                from .novel import NovelDownloader
                nd = NovelDownloader(self.net, num_thread=4)
                for v in vols:
                    q.put(("v_phase", v.index, "download"))
                    path = nd.download_volume(
                        book, v, target,
                        on_phase=lambda vi, ph: q.put(("v_phase", vi, ph)),
                        on_total=lambda vi, n: q.put(("v_total", vi, n)),
                        on_image=lambda vi: q.put(("v_image", vi)),
                        on_concurrency=lambda vi, n: q.put(("concurrency", n)))
                    q.put(("v_done", v.index, path.name if path else ""))
                q.put(("all_done", str(target)))
                return

            downloader = Downloader(self.net, self.scraper, self.config)
            build_fn = build_epub if self.config.default_format == "epub" else build_pdf
            downloader.run_pipeline(
                book, vols, TEMP_DOWNLOAD_DIR, target, build_fn,
                on_start=lambda vi: q.put(("v_phase", vi, "download")),
                on_total=lambda vi, n: q.put(("v_total", vi, n)),
                on_image=lambda vi: q.put(("v_image", vi)),
                on_phase=lambda vi, ph: q.put(("v_phase", vi, ph)),
                on_done=lambda vi, p: q.put(("v_done", vi, p.name if p else "")),
                on_concurrency=lambda n: q.put(("concurrency", n)))
            q.put(("all_done", str(target)))
        except Exception as exc:  # noqa: BLE001
            log.exception("下载失败")
            self.msgq.put(("dl_err", str(exc)))

    # ---------------- 队列轮询 ----------------
    def _drain(self):
        try:
            while True:
                self._handle(self.msgq.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "parse_done":
            self.busy = False
            self.btn_parse.configure(state="normal", text="解析")
            self._render_book(msg[1])
        elif kind == "parse_err":
            self.busy = False
            self.btn_parse.configure(state="normal", text="解析")
            self.var_empty.set("解析失败：" + msg[1])
            messagebox.showerror("解析失败", msg[1])
        elif kind == "v_total":
            self._set_prog(msg[1], total=msg[2])
        elif kind == "v_image":
            self._set_prog(msg[1], inc=1)
        elif kind == "v_phase":
            self._set_prog(msg[1], phase=msg[2])
        elif kind == "v_done":
            self._set_prog(msg[1], phase="done" if msg[2] else "empty", name=msg[2])
        elif kind == "concurrency":
            self.var_conc.set(f"🧵 并发线程：{msg[1]}")
        elif kind == "all_done":
            self.busy = False
            self.btn_reveal.configure(state="normal")
            self.nav_btn.configure(state="normal")
            self.var_status.set(f"全部完成 → {msg[1]}")
            messagebox.showinfo("完成", f"下载完成，已保存到：\n{msg[1]}")
        elif kind == "dl_err":
            self.busy = False
            self.nav_btn.configure(state="normal")
            self.var_status.set("下载出错：" + msg[1])
            # 异常中断：清理该书临时图片 + 退出浏览器释放内存
            if self._dl_title:
                cleanup_book_temp(TEMP_DOWNLOAD_DIR, self._dl_title)
            self._reset_net()
            messagebox.showerror("下载出错", msg[1])

    def _set_prog(self, vidx, total=None, inc=0, phase=None, name=None):
        iid = self.prog_rows.get(vidx)
        if not iid:
            return
        st = self._prog_state.setdefault(vidx, {"total": 0, "done": 0, "phase": "wait"})
        if total is not None:
            st["total"] = total
        if inc:
            st["done"] += inc
        if phase is not None:
            st["phase"] = phase
        label = PHASE_LABEL.get(st["phase"], st["phase"])
        if name:
            prog_txt = name
        elif st["total"]:
            prog_txt = f"{st['done']}/{st['total']}"
        else:
            prog_txt = ""
        self.tree.item(iid, values=(label, prog_txt), tags=(st["phase"],))

    def _reveal(self):
        import subprocess
        import sys
        p = self.config.output_path()
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception:
            pass

    # ---------------- 关闭 ----------------
    def _on_close(self):
        if self.busy and not messagebox.askokcancel("退出", "任务仍在进行，确定退出？"):
            return
        # 若在下载中途退出：清理该书临时图片，避免残留占盘
        if self.busy and self._dl_title:
            cleanup_book_temp(TEMP_DOWNLOAD_DIR, self._dl_title)
        self._reset_net()  # 退出浏览器，释放内存
        self.root.destroy()


def _cover_path() -> Optional[Path]:
    """定位封面图 resource/app_cover.png：源码运行在仓库内，打包后在解包目录。"""
    import sys
    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "resource" / "app_cover.png")
    candidates.append(Path(__file__).resolve().parents[2] / "resource" / "app_cover.png")
    for c in candidates:
        if c.exists():
            return c
    return None


def _apply_cover(gui: "DownloaderGUI") -> None:
    """有封面图则：标题左侧显示缩略图 + 设为窗口/程序图标。"""
    p = _cover_path()
    if not p:
        return
    try:
        from PIL import Image, ImageTk
        with Image.open(p) as im:
            im = im.convert("RGBA")
            icon = im.resize((256, 256))
            thumb = im.resize((34, 34))
        gui._icon_img = ImageTk.PhotoImage(icon)
        gui._logo_img = ImageTk.PhotoImage(thumb)
        gui.root.iconphoto(True, gui._icon_img)
        lbl = tk.Label(gui.logo_holder, image=gui._logo_img, bg=BG)
        lbl.pack(side="left", padx=(0, 8))
    except Exception as exc:  # noqa: BLE001
        log.debug("加载封面图失败(忽略): %s", exc)


def _force_repaint(root: tk.Tk) -> None:
    """修复 macOS(尤其旧 Tk 8.5)首次打开窗口内容空白、不重绘的老问题。"""
    try:
        root.update_idletasks()
        root.lift()
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
        root.focus_force()
        w, h = root.winfo_width(), root.winfo_height()
        x, y = root.winfo_x(), root.winfo_y()
        root.geometry(f"{w}x{h + 1}+{x}+{y}")
        root.after(60, lambda: root.geometry(f"{w}x{h}+{x}+{y}"))
    except Exception:  # noqa: BLE001
        pass


def run(config: Config) -> int:
    root = tk.Tk()
    gui = DownloaderGUI(root, config)
    _apply_cover(gui)
    root.after(120, lambda: _force_repaint(root))
    root.mainloop()
    return 0
