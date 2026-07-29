"""设置（终端）：默认格式、并发数、代理，以及限速 / 退避重试 / 断点续传 /
确认时是否弹网页 / 调试日志开关。

下载目录固定为 ``<root>/downloads/``（按书名分子目录），不在此配置。
修改后写回 config/setting.json。
"""

from __future__ import annotations

from ..config import Config, DOWNLOADS_DIR


def open_settings(config: Config, use_terminal: bool = True) -> None:
    """终端逐项修改设置。``use_terminal`` 仅为兼容旧签名。"""
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
    v = input(f"确认漫画时弹出浏览器网页？（关闭则仅在命令行打印作品名字）y/n [{'y' if config.confirm_open_browser else 'n'}]: ").strip().lower()
    if v in ("y", "n"):
        config.confirm_open_browser = (v == "y")
    v = input(f"启用调试日志？ y/n [{'y' if config.debug else 'n'}]: ").strip().lower()
    if v in ("y", "n"):
        config.debug = (v == "y")
    config.save()
    print("设置已保存。")
