"""设置面板：默认格式、线程数，以及限速 / 退避重试 / 断点续传 / 调试日志开关。

下载目录固定为 ``<root>/downloads/``（按书名分子目录），不在此配置。
Tk 不可用时回退到终端逐项修改。修改后写回 config/setting.json。
"""

from __future__ import annotations

from ..config import Config, DOWNLOADS_DIR
from .tk_common import center, tk_available


def open_settings(config: Config, use_terminal: bool = False) -> None:
    if not use_terminal and tk_available():
        try:
            _settings_tk(config)
            return
        except Exception:
            pass
    _settings_terminal(config)


def _settings_terminal(config: Config) -> None:
    print("\n===== 设置（直接回车保留当前值）=====")
    print(f"（下载目录固定为：{DOWNLOADS_DIR}，按书名分子目录，不可修改）")
    v = input(f"默认格式 epub/pdf [{config.default_format}]: ").strip().lower()
    if v in ("epub", "pdf"):
        config.default_format = v
    v = input(f"并发数（同时下载几话）[{config.parallel_chapters}]: ").strip()
    if v.isdigit() and int(v) > 0:
        config.parallel_chapters = int(v)
    cur_proxy = config.proxy or "自动(用环境变量，连不上自动直连)"
    v = input(f"代理 [{cur_proxy}]（输入 none 表示强制直连；直接回车不改）: ").strip()
    if v.lower() == "none":
        config.proxy = ""
    elif v:
        config.proxy = v
    v = input(f"启用限速？ y/n [{'y' if config.rate_limit_enabled else 'n'}]: ").strip().lower()
    if v in ("y", "n"):
        config.rate_limit_enabled = (v == "y")
    v = input(f"启用失败退避重试？ y/n [{'y' if config.retry_enabled else 'n'}]: ").strip().lower()
    if v in ("y", "n"):
        config.retry_enabled = (v == "y")
    v = input(f"启用断点续传？ y/n [{'y' if config.resume_enabled else 'n'}]: ").strip().lower()
    if v in ("y", "n"):
        config.resume_enabled = (v == "y")
    v = input(f"启用调试日志？ y/n [{'y' if config.debug else 'n'}]: ").strip().lower()
    if v in ("y", "n"):
        config.debug = (v == "y")
    config.save()
    print("设置已保存。")


def _settings_tk(config: Config) -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("设置")
    center(root, 560, 380)
    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)

    row = 0
    ttk.Label(frm, text="下载目录（固定，按书名分子目录）：").grid(
        row=row, column=0, sticky="w")
    ttk.Label(frm, text=str(DOWNLOADS_DIR), foreground="#2456a6").grid(
        row=row, column=1, sticky="w")

    row += 1
    ttk.Label(frm, text="默认输出格式：").grid(row=row, column=0, sticky="w", pady=(10, 0))
    fmt_var = tk.StringVar(value=config.default_format)
    ttk.Combobox(frm, textvariable=fmt_var, values=["epub", "pdf"],
                 state="readonly", width=10).grid(row=row, column=1, sticky="w", pady=(10, 0))

    row += 1
    ttk.Label(frm, text="并发数（同时下载几话）：").grid(row=row, column=0, sticky="w", pady=(10, 0))
    thread_var = tk.IntVar(value=config.parallel_chapters)
    ttk.Spinbox(frm, from_=1, to=32, textvariable=thread_var, width=8).grid(
        row=row, column=1, sticky="w", pady=(10, 0))

    row += 1
    ttk.Label(frm, text="代理（留空=自动，连不上自动直连）：").grid(
        row=row, column=0, sticky="w", pady=(10, 0))
    proxy_var = tk.StringVar(value=config.proxy)
    ttk.Entry(frm, textvariable=proxy_var, width=26).grid(
        row=row, column=1, sticky="w", pady=(10, 0))

    row += 1
    rl_var = tk.BooleanVar(value=config.rate_limit_enabled)
    ttk.Checkbutton(frm, text="启用请求限速（降低触发反爬概率）",
                    variable=rl_var).grid(row=row, column=0, columnspan=2,
                                          sticky="w", pady=(14, 0))
    row += 1
    retry_var = tk.BooleanVar(value=config.retry_enabled)
    ttk.Checkbutton(frm, text="启用失败退避重试",
                    variable=retry_var).grid(row=row, column=0, columnspan=2,
                                             sticky="w", pady=(6, 0))
    row += 1
    resume_var = tk.BooleanVar(value=config.resume_enabled)
    ttk.Checkbutton(frm, text="启用断点续传（已下载文件自动跳过）",
                    variable=resume_var).grid(row=row, column=0, columnspan=2,
                                              sticky="w", pady=(6, 0))
    row += 1
    debug_var = tk.BooleanVar(value=config.debug)
    ttk.Checkbutton(frm, text="启用调试日志（输出到终端并写入 <root>/logs/）",
                    variable=debug_var).grid(row=row, column=0, columnspan=2,
                                             sticky="w", pady=(6, 0))

    frm.columnconfigure(1, weight=1)

    btns = ttk.Frame(root, padding=(16, 0, 16, 16))
    btns.pack(fill="x")

    def on_save():
        config.default_format = fmt_var.get()
        config.parallel_chapters = max(1, int(thread_var.get()))
        config.proxy = proxy_var.get().strip()
        config.rate_limit_enabled = bool(rl_var.get())
        config.retry_enabled = bool(retry_var.get())
        config.resume_enabled = bool(resume_var.get())
        config.debug = bool(debug_var.get())
        config.save()
        root.destroy()

    ttk.Button(btns, text="保存", command=on_save).pack(side="right")
    ttk.Button(btns, text="取消", command=root.destroy).pack(side="right", padx=(0, 8))
    root.mainloop()
