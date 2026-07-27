"""封面确认弹窗：显示封面 + 标题 + 作者 + 简介，让用户确认是否是这本书。

Tk 不可用时回退到终端确认。
"""

from __future__ import annotations

import io
from typing import Optional

from ..models import Book
from .tk_common import center, tk_available


def confirm_book(book: Book, cover_bytes: Optional[bytes],
                 use_terminal: bool = False) -> bool:
    if not use_terminal and tk_available():
        try:
            return _confirm_tk(book, cover_bytes)
        except Exception:
            pass
    return _confirm_terminal(book)


def _confirm_terminal(book: Book) -> bool:
    print("\n===== 请确认漫画信息 =====")
    print(f"标题：{book.title}")
    print(f"作者：{book.author}")
    if book.tags:
        print(f"标签：{'、'.join(book.tags)}")
    summary = book.summary[:200] + ("..." if len(book.summary) > 200 else "")
    print(f"简介：{summary}")
    print(f"共 {len(book.volumes)} 章")
    ans = input("是这本书吗？开始后续操作？[Y/n] ").strip().lower()
    return ans in ("", "y", "yes")


def _confirm_tk(book: Book, cover_bytes: Optional[bytes]) -> bool:
    import tkinter as tk
    from tkinter import ttk

    result = {"ok": False}
    root = tk.Tk()
    root.title("确认漫画")
    center(root, 640, 520)

    photo = None
    if cover_bytes:
        try:
            from PIL import Image, ImageTk

            img = Image.open(io.BytesIO(cover_bytes))
            img.thumbnail((240, 340))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            photo = None

    top = ttk.Frame(root, padding=16)
    top.pack(fill="both", expand=True)

    if photo is not None:
        lbl_img = ttk.Label(top, image=photo)
        lbl_img.image = photo  # 保持引用
        lbl_img.grid(row=0, column=0, rowspan=4, sticky="n", padx=(0, 16))

    ttk.Label(top, text=book.title, font=("", 16, "bold"),
              wraplength=350, justify="left").grid(row=0, column=1, sticky="w")
    ttk.Label(top, text=f"作者：{book.author}").grid(row=1, column=1, sticky="w", pady=(6, 0))
    if book.tags:
        ttk.Label(top, text="标签：" + "、".join(book.tags),
                  wraplength=350, justify="left").grid(row=2, column=1, sticky="w", pady=(4, 0))
    ttk.Label(top, text=f"共 {len(book.volumes)} 章").grid(row=3, column=1, sticky="w", pady=(4, 0))

    summary_frame = ttk.LabelFrame(root, text="简介", padding=8)
    summary_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))
    txt = tk.Text(summary_frame, wrap="word", height=8)
    txt.insert("1.0", book.summary or "（无简介）")
    txt.configure(state="disabled")
    txt.pack(fill="both", expand=True)

    btns = ttk.Frame(root, padding=(16, 0, 16, 16))
    btns.pack(fill="x")

    def on_ok():
        result["ok"] = True
        root.destroy()

    def on_cancel():
        result["ok"] = False
        root.destroy()

    ttk.Button(btns, text="就是这本，继续", command=on_ok).pack(side="right")
    ttk.Button(btns, text="不是，取消", command=on_cancel).pack(side="right", padx=(0, 8))
    root.protocol("WM_DELETE_WINDOW", on_cancel)
    root.mainloop()
    return result["ok"]
