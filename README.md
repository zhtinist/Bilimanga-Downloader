# Bilimanga-Downloader

[English](README.en.md) · **中文**

[bilimanga.net](https://www.bilimanga.net/) 漫画 / [哔哩轻小说](https://www.bilinovel.com) 轻小说下载器，按卷打包为 **EPUB** 或 **PDF**。**纯 Python 命令行工具**，跨平台（macOS / Windows / Linux），无需编译。

## 前置条件

- **Python 3.9+**
- 下载**漫画**还需本机装有 **Chrome 或 Edge**（用于过 Cloudflare）；轻小说走手机站直连，一般无需浏览器。

## 快速开始

```bash
python3 start.py                          # 进入终端交互界面
python3 start.py <网址或书号>              # 直接下载
python3 start.py --out <目录> <网址或书号>  # 本次输出到指定目录
python3 start.py --debug                   # 开启调试日志
```

首次运行会**自动创建项目内 `.venv` 并安装依赖**（无需 conda，不污染全局环境）。

也可以**双击启动**：

- macOS：双击 `run.command`
- Windows：双击 `run.bat`

> Windows 双击 `run.bat` 已用 `chcp 65001` 切到 UTF-8，中文不会乱码。

## 使用方法

进入终端界面后，**直接粘贴漫画或轻小说的网址**（详情页 / 目录页均可），程序按域名**自动识别**是漫画还是轻小说，无需先选类型；也可直接输入书号（会追问一次类型）。随后交互式选章（方向键勾选，或输入 `1-9,15` 这样的范围），每一步都能回退。

- 漫画：`https://www.bilimanga.net/detail/703.html` 或书号 `703`
- 轻小说：`https://www.bilinovel.com/novel/2139.html` 或书号 `2139`

下载文件默认存到系统下载目录 `~/Downloads`，可用 `--out` 指定其它目录。入口输入 `s` 进设置、`q` 退出。

## 设置项

- 下载输出目录（默认 `~/Downloads`，或用 `--out` 指定）
- 默认格式 EPUB / PDF（仅漫画需要选，轻小说固定 EPUB）
- 代理、限速 / 退避重试 / 断点续传、调试日志

下载线程数自动调节（终端实时显示当前线程数）。

## 目录结构

```
Bilimanga-Downloader/
├── start.py              # 入口（终端交互界面，自动建 .venv）
├── run.command           # macOS 双击启动
├── run.bat               # Windows 双击启动
├── src/bilimanga_dl/     # 源码
└── resource/             # 资源文件
```

> 仅供个人学习与备份公开内容使用，请遵守站点条款。
