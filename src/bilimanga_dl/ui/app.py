"""Tkinter 图形主界面。

布局参考 ShqWW/bilinovel-download:封面 + 书籍信息 + URL 解析 +
章节勾选(支持 Shift 连选 / Ctrl·⌘ 多选 + 文本框输入)+ 格式选择 +
下载目录 + 进度条,全部集中在一个窗口。

耗时操作(解析、下载)放到后台线程,通过 ``root.after`` 回主线程更新 UI。
"""

from __future__ import annotations

import io
import queue
import threading
from pathlib import Path
from typing import List, Optional

from ..build_epub import build_epub
from ..build_pdf import build_pdf
from ..config import Config, DOWNLOADS_DIR
from ..downloader import Downloader, safe_name
from ..logutil import get_logger
from ..models import Book
from ..net import Net
from ..scraper import Scraper, parse_book_no
from ..select_parser import parse_selection
from .picker import _indices_to_expr
from .settings import open_settings

log = get_logger("gui")


class BilimangaApp:
    def __init__(self, config: Config):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.config = config
        self.book: Optional[Book] = None
        self.net: Optional[Net] = None
        self.scraper: Optional[Scraper] = None
        self._cover_photo = None
        self._busy = False
        self._ui_queue: "queue.Queue" = queue.Queue()

        self.root = tk.Tk()
        self.root.title("Bilimanga 漫画下载器")
        self.root.minsize(820, 640)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._drain_queue)

    # ---------------- 构建界面 ----------------
    def _build(self):
        tk, ttk = self.tk, self.ttk

        # 顶部:URL + 解析 + 设置
        top = ttk.Frame(self.root, padding=(12, 12, 12, 6))
        top.pack(fill="x")
        ttk.Label(top, text="漫画主页 URL：").pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(top, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(4, 8))
        self.url_entry.bind("<Return>", lambda e: self._on_parse())
        self.parse_btn = ttk.Button(top, text="解析", command=self._on_parse)
        self.parse_btn.pack(side="left")
        ttk.Button(top, text="设置", command=self._on_settings).pack(side="left", padx=(8, 0))

        # 书籍信息:封面 + 文本
        info = ttk.Frame(self.root, padding=(12, 6))
        info.pack(fill="x")
        self.cover_label = ttk.Label(info, text="（封面）", width=20, anchor="center",
                                     relief="groove")
        self.cover_label.grid(row=0, column=0, rowspan=4, sticky="n", padx=(0, 12))
        self.title_var = tk.StringVar(value="尚未解析")
        self.author_var = tk.StringVar(value="")
        self.tags_var = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.title_var, font=("", 15, "bold"),
                  wraplength=520, justify="left").grid(row=0, column=1, sticky="w")
        ttk.Label(info, textvariable=self.author_var).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(info, textvariable=self.tags_var, wraplength=520,
                  justify="left").grid(row=2, column=1, sticky="w", pady=(2, 0))
        self.summary_text = tk.Text(info, height=4, wrap="word")
        self.summary_text.grid(row=3, column=1, sticky="we", pady=(6, 0))
        self.summary_text.configure(state="disabled")
        info.columnconfigure(1, weight=1)

        # 章节选择
        mid = ttk.LabelFrame(self.root, text="选择要下载的章（点击选择 / Shift 连选 / Ctrl·⌘ 多选）",
                             padding=(10, 8))
        mid.pack(fill="both", expand=True, padx=12, pady=6)
        list_row = ttk.Frame(mid)
        list_row.pack(fill="both", expand=True)
        sb = ttk.Scrollbar(list_row, orient="vertical")
        self.listbox = tk.Listbox(list_row, selectmode=tk.EXTENDED,
                                  yscrollcommand=sb.set, activestyle="none")
        sb.config(command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_listbox_select)

        expr_row = ttk.Frame(mid)
        expr_row.pack(fill="x", pady=(8, 0))
        ttk.Label(expr_row, text="或输入章号（示例 1-9,15,19,20-25）：").pack(side="left")
        self.expr_var = tk.StringVar()
        expr_entry = ttk.Entry(expr_row, textvariable=self.expr_var, width=28)
        expr_entry.pack(side="left", padx=(4, 4))
        expr_entry.bind("<Return>", lambda e: self._apply_expr())
        ttk.Button(expr_row, text="应用", command=self._apply_expr).pack(side="left")
        ttk.Button(expr_row, text="全选", command=self._select_all).pack(side="left", padx=(8, 0))
        ttk.Button(expr_row, text="清空", command=self._clear_all).pack(side="left", padx=(4, 0))

        # 底部:格式 + 目录 + 进度 + 下载
        bottom = ttk.Frame(self.root, padding=(12, 6, 12, 12))
        bottom.pack(fill="x")

        fmt_row = ttk.Frame(bottom)
        fmt_row.pack(fill="x")
        ttk.Label(fmt_row, text="输出格式：").pack(side="left")
        self.fmt_var = tk.StringVar(value=self.config.default_format)
        ttk.Radiobutton(fmt_row, text="EPUB", value="epub", variable=self.fmt_var).pack(side="left")
        ttk.Radiobutton(fmt_row, text="PDF", value="pdf", variable=self.fmt_var).pack(side="left", padx=(6, 0))

        dir_row = ttk.Frame(bottom)
        dir_row.pack(fill="x", pady=(6, 0))
        ttk.Label(dir_row, text="下载到（固定，按书名分子目录）：").pack(side="left")
        ttk.Label(dir_row, text=str(DOWNLOADS_DIR), foreground="#2456a6",
                  wraplength=520, justify="left").pack(side="left", padx=(4, 6))

        prog_row = ttk.Frame(bottom)
        prog_row.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(prog_row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.download_btn = ttk.Button(prog_row, text="开始下载", command=self._on_download)
        self.download_btn.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="就绪。输入漫画主页 URL 后点“解析”。")
        ttk.Label(bottom, textvariable=self.status_var, foreground="#555").pack(
            anchor="w", pady=(6, 0))

    # ---------------- 线程 → UI 通信 ----------------
    def _post(self, fn):
        """把一个无参回调放进队列,主线程执行。"""
        self._ui_queue.put(fn)

    def _drain_queue(self):
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("UI 回调异常")
        except queue.Empty:
            pass
        self.root.after(80, self._drain_queue)

    def _set_status(self, text):
        self._post(lambda: self.status_var.set(text))

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._post(lambda: (self.parse_btn.config(state=state),
                            self.download_btn.config(state=state)))

    # ---------------- 事件 ----------------
    def _on_settings(self):
        open_settings(self.config)
        # 回填可能变化的默认格式
        self.fmt_var.set(self.config.default_format)

    def _on_parse(self):
        if self._busy:
            return
        url = self.url_var.get().strip()
        if not url:
            self._messagebox("提示", "请输入漫画主页 URL。", "info")
            return
        self._set_busy(True)
        self._set_status("正在解析……")
        threading.Thread(target=self._parse_worker, args=(url,), daemon=True).start()

    def _parse_worker(self, url: str):
        try:
            book_no = parse_book_no(url)
            if self.net is None:
                self.net = Net(self.config)
                self.scraper = Scraper(self.net)
            book = self.scraper.fetch_book(book_no)
            cover_bytes = None
            if book.cover_url:
                try:
                    cover_bytes = self.net.get_bytes(book.cover_url, referer=book.base_url)
                except Exception:
                    cover_bytes = None
            self._post(lambda: self._fill_book(book, cover_bytes))
            self._set_status(f"解析完成:{book.title} · 共 {len(book.volumes)} 章")
        except Exception as exc:
            log.exception("解析失败")
            self._set_status("解析失败")
            self._messagebox("解析失败", str(exc), "error")
        finally:
            self._set_busy(False)

    def _fill_book(self, book: Book, cover_bytes):
        self.book = book
        self.title_var.set(book.title)
        self.author_var.set(f"作者：{book.author}")
        self.tags_var.set("标签：" + "、".join(book.tags) if book.tags else "")
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", book.summary or "（无简介）")
        self.summary_text.configure(state="disabled")
        # 封面
        self._cover_photo = None
        self.cover_label.config(image="", text="（无封面）")
        if cover_bytes:
            try:
                from PIL import Image, ImageTk
                img = Image.open(io.BytesIO(cover_bytes))
                img.thumbnail((180, 260))
                self._cover_photo = ImageTk.PhotoImage(img)
                self.cover_label.config(image=self._cover_photo, text="")
            except Exception:
                pass
        # 章节列表
        self.listbox.delete(0, "end")
        for v in book.volumes:
            self.listbox.insert("end", v.summary_line())
        self.expr_var.set("")

    def _on_listbox_select(self, _e=None):
        sel = [i + 1 for i in self.listbox.curselection()]
        self.expr_var.set(_indices_to_expr(sel))

    def _apply_expr(self):
        if not self.book:
            return
        expr = self.expr_var.get().strip()
        if not expr:
            return
        try:
            indices = parse_selection(expr, len(self.book.volumes))
        except ValueError as exc:
            self._messagebox("输入有误", str(exc), "error")
            return
        self.listbox.selection_clear(0, "end")
        for n in indices:
            self.listbox.selection_set(n - 1)
        self.listbox.see(indices[0] - 1)

    def _select_all(self):
        self.listbox.selection_set(0, "end")
        self._on_listbox_select()

    def _clear_all(self):
        self.listbox.selection_clear(0, "end")
        self.expr_var.set("")

    def _on_download(self):
        if self._busy:
            return
        if not self.book:
            self._messagebox("提示", "请先解析一本漫画。", "info")
            return
        sel = [i + 1 for i in self.listbox.curselection()]
        if not sel:
            self._messagebox("提示", "请至少选择一章。", "info")
            return
        fmt = self.fmt_var.get()
        self._set_busy(True)
        threading.Thread(target=self._download_worker, args=(sel, fmt),
                         daemon=True).start()

    def _download_worker(self, selected: List[int], fmt: str):
        try:
            # 固定输出到 <root>/downloads/<书名>/
            target = DOWNLOADS_DIR / safe_name(self.book.title)
            target.mkdir(parents=True, exist_ok=True)
            work_dir = target / ".cache"
            downloader = Downloader(self.net, self.scraper, self.config)
            index_map = {v.index: v for v in self.book.volumes}
            outputs = []
            for vol_index in selected:
                volume = index_map[vol_index]
                self._set_status(f"下载中:[{volume.index}] {volume.title}")

                def cb(desc, done, total, _v=volume):
                    self._post(lambda: self._update_progress(desc, done, total))

                dv = downloader.download_volume(self.book, volume, work_dir, progress_cb=cb)
                if not any(dc.images for dc in dv.chapters):
                    self._set_status(f"跳过(无图片):{volume.title}")
                    continue
                self._set_status(f"打包中:{volume.title}")
                out = build_epub(self.book, dv, target) if fmt == "epub" \
                    else build_pdf(self.book, dv, target)
                outputs.append(out)
            self._set_status(f"全部完成,共 {len(outputs)} 个文件 → {target}")
            self._messagebox("完成", f"已生成 {len(outputs)} 个文件:\n" +
                             "\n".join(str(o) for o in outputs), "info")
        except Exception as exc:
            log.exception("下载失败")
            self._set_status("下载失败")
            self._messagebox("下载失败", str(exc), "error")
        finally:
            self._set_busy(False)
            self._post(lambda: self.progress.config(value=0))

    def _update_progress(self, desc, done, total):
        if total:
            self.progress.config(maximum=total, value=done)
        self.status_var.set(f"{desc}  {done}/{total}")

    # ---------------- 工具 ----------------
    def _messagebox(self, title, msg, kind="info"):
        """线程安全地弹消息框;yesno 返回布尔。"""
        from tkinter import messagebox
        result = {"v": None}
        done = threading.Event()

        def show():
            if kind == "error":
                messagebox.showerror(title, msg, parent=self.root)
            elif kind == "yesno":
                result["v"] = messagebox.askyesno(title, msg, parent=self.root)
            else:
                messagebox.showinfo(title, msg, parent=self.root)
            done.set()

        if threading.current_thread() is threading.main_thread():
            show()
        else:
            self._post(show)
            done.wait()
        return result["v"]

    def _on_close(self):
        if self.net:
            try:
                self.net.close()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def launch(config: Config) -> None:
    BilimangaApp(config).run()
