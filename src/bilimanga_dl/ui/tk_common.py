"""Tkinter 公共工具：检测可用性、居中窗口。"""

from __future__ import annotations


def tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    try:
        root = _new_root()
        root.destroy()
        return True
    except Exception:
        return False


def _new_root():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    return root


def center(win, width: int, height: int) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = (sw - width) // 2
    y = (sh - height) // 3
    win.geometry(f"{width}x{height}+{x}+{y}")
