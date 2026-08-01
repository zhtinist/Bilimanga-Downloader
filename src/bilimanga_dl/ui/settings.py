"""设置（终端）：站点地址、下载输出目录、默认格式、并发数、代理，以及
限速 / 退避重试 / 断点续传 / 确认时是否弹网页 / 调试日志开关。

输出目录由用户指定（未设置则不执行下载）；修改后写回 config/setting.json。
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config, default_download_dir


def open_settings(config: Config, use_terminal: bool = True) -> None:
    """终端逐项修改设置。``use_terminal`` 仅为兼容旧签名。"""
    print("\n===== 设置（直接回车保留当前值）=====")

    print(f"站点地址：{config.site}（固定，不支持修改）")

    cur_out = config.output_dir or f"浏览器下载目录 {default_download_dir()}"
    print(f"下载输出目录 [{cur_out}]（留空=用浏览器下载目录；输入 none 清空恢复默认）:")
    v = input("→ ").strip()
    if v.lower() == "none":
        config.output_dir = ""
    elif v:
        p = Path(v).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
            config.output_dir = str(p)
        except OSError as exc:
            print(f"  无法创建该目录：{exc}（未修改）")

    v = input(f"默认格式 epub/pdf [{config.default_format}]: ").strip().lower()
    if v in ("epub", "pdf"):
        config.default_format = v

    # 下载并发线程（漫画自适应并发 AIMD 的起始数与上限）
    v = input(f"并发-起始线程数 [{config.concurrency_start}]: ").strip()
    if v.isdigit() and int(v) >= 1:
        config.concurrency_start = int(v)
    v = input(f"并发-上限线程数 [{config.concurrency_max}]: ").strip()
    if v.isdigit() and int(v) >= 1:
        config.concurrency_max = int(v)
    config.concurrency_max = max(2, config.concurrency_max)
    config.concurrency_start = max(1, min(config.concurrency_start, config.concurrency_max))

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

    # 百度网盘
    if config.baidu_cookie:
        print(f"百度网盘：已连接（{config.baidu_nickname or '百度用户'}）")
        v = input(f"网盘上传根路径 [{config.baidu_upload_base}]: ").strip()
        if v:
            config.baidu_upload_base = v if v.startswith("/") else "/" + v
        v = input("断开百度账号？输入 yes 断开（清除本地登录态）: ").strip().lower()
        if v == "yes":
            config.baidu_cookie = ""
            config.baidu_nickname = ""
            print("  已断开百度账号。")
    else:
        print("百度网盘：未连接（回主界面输入 c 连接账号）")

    # OneDrive
    if config.onedrive_refresh_token:
        print(f"OneDrive：已连接（{config.onedrive_account or '微软账号'}）")
        v = input(f"OneDrive 上传根路径 [{config.onedrive_upload_base}]: ").strip()
        if v:
            config.onedrive_upload_base = v if v.startswith("/") else "/" + v
        v = input("断开 OneDrive？输入 yes 断开（清除本地登录态）: ").strip().lower()
        if v == "yes":
            config.onedrive_refresh_token = ""
            config.onedrive_account = ""
            print("  已断开 OneDrive。")
    else:
        print("OneDrive：未连接（回主界面输入 o 连接账号）")
    cur_cid = config.onedrive_client_id or "内置公共应用（零注册，直接登录即可）"
    v = input(f"OneDrive client_id [{cur_cid}]（进阶：填你自注册应用的 id；输入 none 清空用内置）: ").strip()
    if v.lower() == "none":
        config.onedrive_client_id = ""
    elif v:
        config.onedrive_client_id = v

    config.save()
    print("设置已保存。")
    print(f"文件将保存到：{config.output_path()}")
