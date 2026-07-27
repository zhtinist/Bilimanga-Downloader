"""章节选择弹窗。

- 列表按“章号 + 章标题 + 含哪几话”展示。
- 列表框为 EXTENDED 多选：单击选择、Shift 连选一段、Ctrl/Cmd 点选多个。
- 底部文本框可直接输入表达式（示例 1-9,15,19,20-25），与列表框双向同步。

Tk 不可用时回退到终端：打印列表 + 输入表达式。
返回选中的章号（1 起）升序列表。
"""

from __future__ import annotations

from typing import List

from ..models import Volume
from ..select_parser import parse_selection
from .tk_common import center, tk_available


def pick_volumes(volumes: List[Volume], use_terminal: bool = False) -> List[int]:
    if not volumes:
        return []
    if not use_terminal and tk_available():
        try:
            return _pick_tk(volumes)
        except Exception:
            pass
    return _pick_terminal(volumes)


def _indices_to_expr(indices: List[int]) -> str:
    """把 [1,2,3,5,7,8] 压缩成 '1-3,5,7-8'。"""
    if not indices:
        return ""
    indices = sorted(set(indices))
    parts = []
    start = prev = indices[0]
    for n in indices[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = n
    parts.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(parts)


def _pick_terminal(volumes: List[Volume]) -> List[int]:
    for v in volumes:
        print(v.summary_line())
    print("\n示例输入格式：1-9,15,19,20-25   （逗号分隔、a-b 区间；直接回车=全选）")
    while True:
        expr = input("→ 请输入要下载的章号：").strip()
        if not expr:
            return [v.index for v in volumes]
        try:
            return parse_selection(expr, len(volumes))
        except ValueError as exc:
            print(f"输入有误：{exc}，请重新输入。")


def _pick_tk(volumes: List[Volume]) -> List[int]:
    import tkinter as tk
    from tkinter import ttk, messagebox

    result: dict = {"indices": None}
    max_index = len(volumes)

    root = tk.Tk()
    root.title("选择要下载的章")
    center(root, 760, 560)

    ttk.Label(
        root,
        text="点击选择；按住 Shift 连选一段；按住 Ctrl/⌘ 点选多个。",
        padding=(12, 10, 12, 4),
    ).pack(anchor="w")

    list_frame = ttk.Frame(root, padding=(12, 0))
    list_frame.pack(fill="both", expand=True)

    scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
    listbox = tk.Listbox(
        list_frame,
        selectmode=tk.EXTENDED,
        yscrollcommand=scrollbar.set,
        activestyle="none",
    )
    scrollbar.config(command=listbox.yview)
    scrollbar.pack(side="right", fill="y")
    listbox.pack(side="left", fill="both", expand=True)

    for v in volumes:
        listbox.insert(tk.END, v.summary_line())

    # 文本框区
    entry_frame = ttk.Frame(root, padding=12)
    entry_frame.pack(fill="x")
    ttk.Label(entry_frame, text="或输入章号（示例 1-9,15,19,20-25）：").pack(anchor="w")
    entry_row = ttk.Frame(entry_frame)
    entry_row.pack(fill="x", pady=(4, 0))
    expr_var = tk.StringVar()
    entry = ttk.Entry(entry_row, textvariable=expr_var)
    entry.pack(side="left", fill="x", expand=True)

    def apply_expr():
        expr = expr_var.get().strip()
        if not expr:
            return
        try:
            indices = parse_selection(expr, max_index)
        except ValueError as exc:
            messagebox.showerror("输入有误", str(exc), parent=root)
            return
        listbox.selection_clear(0, tk.END)
        for n in indices:
            listbox.selection_set(n - 1)
        listbox.see(indices[0] - 1)

    def on_listbox_select(_event=None):
        sel = [i + 1 for i in listbox.curselection()]
        expr_var.set(_indices_to_expr(sel))

    listbox.bind("<<ListboxSelect>>", on_listbox_select)
    entry.bind("<Return>", lambda e: apply_expr())

    ttk.Button(entry_row, text="应用到列表", command=apply_expr).pack(side="left", padx=(8, 0))

    # 底部按钮
    btns = ttk.Frame(root, padding=(12, 0, 12, 12))
    btns.pack(fill="x")

    def select_all():
        listbox.selection_set(0, tk.END)
        on_listbox_select()

    def clear_all():
        listbox.selection_clear(0, tk.END)
        expr_var.set("")

    def on_ok():
        sel = [i + 1 for i in listbox.curselection()]
        if not sel:
            messagebox.showwarning("未选择", "请至少选择一章。", parent=root)
            return
        result["indices"] = sel
        root.destroy()

    def on_cancel():
        result["indices"] = None
        root.destroy()

    ttk.Button(btns, text="全选", command=select_all).pack(side="left")
    ttk.Button(btns, text="清空", command=clear_all).pack(side="left", padx=(8, 0))
    ttk.Button(btns, text="开始下载", command=on_ok).pack(side="right")
    ttk.Button(btns, text="取消", command=on_cancel).pack(side="right", padx=(0, 8))

    root.protocol("WM_DELETE_WINDOW", on_cancel)
    root.mainloop()

    if result["indices"] is None:
        return []
    return result["indices"]
