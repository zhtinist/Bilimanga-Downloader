"""原生图形界面（Tkinter）。

不起本地服务、不开浏览器 —— 直接是一个独立窗口，双击可执行文件即用。
这样可避开：①浏览器二次打开卡死；②macOS 对监听端口的可执行文件反复弹
“允许接受传入网络连接”的风险提示。下载逻辑仍复用命令行版的
:class:`Net` / :class:`Scraper` / :class:`Downloader`。

Tkinter 非线程安全：抓取/下载都在后台线程跑，只通过线程安全队列把进度回传，
主线程用 ``after`` 轮询队列刷新界面。
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
from .config import Config, DEFAULT_SITE, TEMP_DOWNLOAD_DIR, default_download_dir
from .downloader import Downloader, safe_name
from .logutil import get_logger
from .models import Book
from .net import Net
from .scraper import Scraper, parse_book_no

log = get_logger("gui")

PHASE_LABEL = {
    "wait": "等待",
    "download": "下载中",
    "validate": "校对",
    "package": "打包",
    "done": "完成",
    "empty": "无内容",
}


class DownloaderGUI:
    def __init__(self, root: tk.Tk, config: Config):
        self.root = root
        self.config = config
        self.net: Optional[Net] = None
        self.scraper: Optional[Scraper] = None
        self.book: Optional[Book] = None
        self.vol_vars: dict = {}       # vidx -> BooleanVar（选章）
        self.prog_rows: dict = {}      # vidx -> treeview item id（进度）
        self.msgq: "queue.Queue" = queue.Queue()
        self.busy = False

        self._build()
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

    # ---------------- 界面搭建 ----------------
    def _build(self):
        self.root.title("bilimanga 漫画下载器")
        self.root.geometry("760x680")
        self.root.minsize(680, 560)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        pad = {"padx": 10, "pady": 6}
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        # ① 设置
        setg = ttk.LabelFrame(main, text="① 设置", padding=10)
        setg.pack(fill="x")
        setg.columnconfigure(1, weight=1)

        ttk.Label(setg, text="站点地址").grid(row=0, column=0, sticky="w", **pad)
        self.var_site = tk.StringVar(value=self.config.site_url or DEFAULT_SITE)
        ttk.Entry(setg, textvariable=self.var_site).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(setg, text="下载目录").grid(row=1, column=0, sticky="w", **pad)
        self.var_out = tk.StringVar(value=self.config.output_dir or "")
        ttk.Entry(setg, textvariable=self.var_out).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(setg, text="浏览…", command=self._browse_out).grid(row=1, column=2, sticky="e", **pad)

        ttk.Label(setg, text="格式").grid(row=2, column=0, sticky="w", **pad)
        rowf = ttk.Frame(setg)
        rowf.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        self.var_fmt = tk.StringVar(value=self.config.default_format or "epub")
        ttk.Radiobutton(rowf, text="EPUB", value="epub", variable=self.var_fmt).pack(side="left")
        ttk.Radiobutton(rowf, text="PDF", value="pdf", variable=self.var_fmt).pack(side="left", padx=(12, 0))
        ttk.Label(rowf, text="  并发").pack(side="left", padx=(20, 4))
        self.var_par = tk.StringVar(value=str(self.config.parallel_chapters))
        ttk.Entry(rowf, textvariable=self.var_par, width=5).pack(side="left")

        ttk.Label(setg, text="代理").grid(row=3, column=0, sticky="w", **pad)
        self.var_proxy = tk.StringVar(value=self.config.proxy or "")
        ttk.Entry(setg, textvariable=self.var_proxy).grid(row=3, column=1, sticky="ew", **pad)
        ttk.Button(setg, text="保存设置", command=self._save_cfg).grid(row=3, column=2, sticky="e", **pad)

        self.var_hint = tk.StringVar()
        ttk.Label(setg, textvariable=self.var_hint, foreground="#666").grid(
            row=4, column=0, columnspan=3, sticky="w", padx=10)
        self._refresh_hint()

        # ② 输入 + 解析
        inp = ttk.LabelFrame(main, text="② 输入漫画（详情页 / 目录页链接 / 书号）", padding=10)
        inp.pack(fill="x", pady=(10, 0))
        inp.columnconfigure(0, weight=1)
        self.var_input = tk.StringVar()
        ent = ttk.Entry(inp, textvariable=self.var_input)
        ent.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ent.bind("<Return>", lambda e: self._on_parse())
        self.btn_parse = ttk.Button(inp, text="解析", command=self._on_parse)
        self.btn_parse.grid(row=0, column=1)

        # ③ 选章
        self.book_frame = ttk.LabelFrame(main, text="③ 选择要下载的章", padding=10)
        self.book_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.var_book = tk.StringVar(value="解析后在此显示书目与章节。")
        ttk.Label(self.book_frame, textvariable=self.var_book, wraplength=700, justify="left").pack(
            anchor="w")

        selbar = ttk.Frame(self.book_frame)
        selbar.pack(fill="x", pady=(6, 4))
        ttk.Button(selbar, text="全选", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(selbar, text="全不选", command=lambda: self._set_all(False)).pack(side="left", padx=(8, 0))
        self.btn_start = ttk.Button(selbar, text="开始下载", command=self._on_download, state="disabled")
        self.btn_start.pack(side="right")

        # 可滚动的章节勾选区
        canvas = tk.Canvas(self.book_frame, height=150, highlightthickness=0)
        vsb = ttk.Scrollbar(self.book_frame, orient="vertical", command=canvas.yview)
        self.vol_inner = ttk.Frame(canvas)
        self.vol_inner.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.vol_inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # ④ 进度
        prog = ttk.LabelFrame(main, text="④ 下载进度", padding=10)
        prog.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(prog, columns=("state", "prog"), show="tree headings", height=6)
        self.tree.heading("#0", text="卷")
        self.tree.heading("state", text="状态")
        self.tree.heading("prog", text="进度")
        self.tree.column("#0", width=380)
        self.tree.column("state", width=90, anchor="center")
        self.tree.column("prog", width=120, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.var_status = tk.StringVar(value="就绪。")
        ttk.Label(main, textvariable=self.var_status, foreground="#333").pack(anchor="w", pady=(8, 0))

    # ---------------- 设置相关 ----------------
    def _refresh_hint(self):
        eff = (self.var_out.get().strip() or str(default_download_dir()))
        self.var_hint.set(f"文件将保存到：{Path(eff).expanduser()}（留空=浏览器下载目录）")

    def _browse_out(self):
        init = self.var_out.get().strip() or str(default_download_dir())
        d = filedialog.askdirectory(initialdir=init, title="选择下载目录")
        if d:
            self.var_out.set(d)
            self._refresh_hint()

    def _save_cfg(self):
        c = self.config
        site = self.var_site.get().strip()
        if site and not site.startswith("http"):
            site = "https://" + site
        c.site_url = (site or DEFAULT_SITE).rstrip("/")
        out = self.var_out.get().strip()
        if out:
            try:
                Path(out).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("目录无效", f"无法创建该目录：{exc}")
                return
        c.output_dir = out
        if self.var_fmt.get() in ("epub", "pdf"):
            c.default_format = self.var_fmt.get()
        par = self.var_par.get().strip()
        if par.isdigit() and int(par) > 0:
            c.parallel_chapters = int(par)
        c.proxy = self.var_proxy.get().strip()
        c.save()
        self._refresh_hint()
        self.var_status.set("设置已保存。")

    # ---------------- 解析 ----------------
    def _on_parse(self):
        if self.busy:
            return
        text = self.var_input.get().strip()
        if not text:
            messagebox.showinfo("提示", "请输入详情页链接、目录页链接或书号。")
            return
        self._save_cfg()  # 解析前先应用站点/代理等设置
        self.busy = True
        self.btn_parse.configure(state="disabled", text="解析中…")
        self.btn_start.configure(state="disabled")
        self.var_status.set("正在解析……（首次需启动浏览器过 Cloudflare，约 10–20 秒）")
        threading.Thread(target=self._parse_worker, args=(text,), daemon=True).start()

    def _parse_worker(self, text: str):
        try:
            book_no = parse_book_no(text)
            self._reset_net()
            self._ensure_net()
            book = self.scraper.fetch_book(book_no)
            if not book.volumes:
                raise RuntimeError("未解析到任何章节，可能页面结构变化或该书需登录。")
            self.msgq.put(("parse_done", book))
        except Exception as exc:  # noqa: BLE001
            self.msgq.put(("parse_err", str(exc)))

    def _render_book(self, book: Book):
        self.book = book
        self.var_book.set(f"《{book.title}》　{book.author}　共 {len(book.volumes)} 章")
        for w in self.vol_inner.winfo_children():
            w.destroy()
        self.vol_vars = {}
        for v in book.volumes:
            var = tk.BooleanVar(value=True)
            self.vol_vars[v.index] = var
            ttk.Checkbutton(
                self.vol_inner, variable=var,
                text=f"{v.index}. {v.title}　（{len(v.chapters)} 话）").pack(anchor="w")
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
        self._save_cfg()
        out_root = self.config.output_path()  # 未配置则默认浏览器下载目录
        index_map = {v.index: v for v in self.book.volumes}
        vols = [index_map[i] for i in selected]

        # 重置进度表
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.prog_rows = {}
        for v in vols:
            iid = self.tree.insert("", "end", text=f"{v.index}. {v.title}",
                                   values=("等待", ""))
            self.prog_rows[v.index] = iid

        self.busy = True
        self.btn_start.configure(state="disabled")
        self.btn_parse.configure(state="disabled")
        self.var_status.set(f"开始下载，输出到：{out_root}")
        threading.Thread(target=self._download_worker, args=(vols, out_root),
                         daemon=True).start()

    def _download_worker(self, vols, out_root):
        book = self.book
        target = out_root / safe_name(book.title)
        try:
            target.mkdir(parents=True, exist_ok=True)
            self._ensure_net()
            downloader = Downloader(self.net, self.scraper, self.config)
            build_fn = build_epub if self.config.default_format == "epub" else build_pdf
            q = self.msgq
            downloader.run_pipeline(
                book, vols, TEMP_DOWNLOAD_DIR, target, build_fn,
                on_start=lambda vi: q.put(("v_phase", vi, "download")),
                on_total=lambda vi, n: q.put(("v_total", vi, n)),
                on_image=lambda vi: q.put(("v_image", vi)),
                on_phase=lambda vi, ph: q.put(("v_phase", vi, ph)),
                on_done=lambda vi, p: q.put(("v_done", vi, p.name if p else "")))
            q.put(("all_done", str(target)))
        except Exception as exc:  # noqa: BLE001
            log.exception("下载失败")
            self.msgq.put(("dl_err", str(exc)))

    # ---------------- 队列轮询（主线程刷新 UI）----------------
    def _drain(self):
        try:
            while True:
                msg = self.msgq.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "parse_done":
            self.busy = False
            self.btn_parse.configure(state="normal", text="解析")
            self.var_status.set("解析完成，请选择要下载的章。")
            self._render_book(msg[1])
        elif kind == "parse_err":
            self.busy = False
            self.btn_parse.configure(state="normal", text="解析")
            self.var_status.set("解析失败。")
            messagebox.showerror("解析失败", msg[1])
        elif kind == "v_total":
            self._set_prog(msg[1], total=msg[2])
        elif kind == "v_image":
            self._set_prog(msg[1], inc=1)
        elif kind == "v_phase":
            self._set_prog(msg[1], phase=msg[2])
        elif kind == "v_done":
            self._set_prog(msg[1], phase="done" if msg[2] else "empty", name=msg[2])
        elif kind == "all_done":
            self.busy = False
            self.btn_start.configure(state="normal")
            self.btn_parse.configure(state="normal")
            self.var_status.set(f"全部完成 → {msg[1]}")
            messagebox.showinfo("完成", f"下载完成，已保存到：\n{msg[1]}")
        elif kind == "dl_err":
            self.busy = False
            self.btn_start.configure(state="normal")
            self.btn_parse.configure(state="normal")
            self.var_status.set("下载出错。")
            messagebox.showerror("下载出错", msg[1])

    _prog_state: dict = {}

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
        self.tree.item(iid, values=(label, prog_txt))

    # ---------------- 关闭 ----------------
    def _on_close(self):
        if self.busy and not messagebox.askokcancel("退出", "下载仍在进行，确定退出？"):
            return
        self._reset_net()
        self.root.destroy()


def _force_repaint(root: tk.Tk) -> None:
    """修复 macOS(尤其旧版 Tk 8.5)首次打开窗口内容空白、不重绘的老问题：
    置顶取焦点 + 轻微改一下窗口尺寸再改回，逼 Tk 触发一次重绘。"""
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
    DownloaderGUI(root, config)
    root.after(120, lambda: _force_repaint(root))
    root.mainloop()
    return 0
