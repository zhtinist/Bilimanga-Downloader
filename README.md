# Bilimanga-Downloader

[English](README.en.md) · **中文**

[bilimanga.net](https://www.bilimanga.net/) 漫画下载器：抓取漫画插图，按卷/话下载并打包为 **EPUB** 或 **PDF**。

> **推荐使用命令行 / 本地应用版（本仓库主体）。** 它自带**原生图形界面**（独立程序窗口，不开浏览器、不起本地服务），也可纯命令行使用，最稳定。
> 🐵 油猴脚本版（[`userscript/`](userscript)）**目前不稳定**（受站点结构变动、跨域授权、浏览器差异影响，容易失效），仅作备用，遇到问题请改用本仓库主体。

## 三种使用方式（按推荐程度排列）

| | ⭐ 双击可执行文件 | ⭐ 命令行 / 源码运行 | 🐵 油猴脚本（不稳定，备用） |
|---|---|---|---|
| 面向 | 电脑小白，零环境 | 会用命令行 / 想改代码 | 不想装东西且能接受偶发失效 |
| 环境 | **无需 Python**，只需本机 Chrome/Edge | Python + 本机 Chrome/Edge | 浏览器 + Tampermonkey |
| 获取 | 到 [Releases](../../releases) 下载对应系统的文件，双击即用 | `python3 start.py` | 商店装 Tampermonkey → 装脚本 |
| 界面 | 双击打开**原生窗口**（不用浏览器） | 默认原生窗口，`--cli` 走终端 | 页面右侧浮动按钮 |
| 平台 | macOS / Windows | macOS / Windows | 通用 |

## 一、双击可执行文件（推荐，无需 Python）

1. 到 [Releases](../../releases) 下载对应系统的文件：
   - **macOS**：`Bilimanga-Downloader-macOS.zip`（解压后得到 `Bilimanga-Downloader.app`，双击运行）。
   - **Windows**：`Bilimanga-Downloader-Windows.zip`（解压后双击 `Bilimanga-Downloader.exe`）。
2. 双击后打开一个**独立的程序窗口**（原生界面，不开浏览器、不起本地服务，因此不会触发 macOS「允许接受传入网络连接」的反复弹窗）。
3. 在窗口里：粘贴漫画链接或书号点「解析」→ 勾选要下的章 → 「开始下载」。文件默认存到**浏览器下载目录**（`~/Downloads`），也可在「① 设置」里改成任意目录。

> 前提：本机需装有 **Chrome 或 Edge**（用来过 Cloudflare）。首次解析会启动一次浏览器，约 10–20 秒。
> macOS 首次打开若提示“无法验证开发者”，在「系统设置 → 隐私与安全性」里点「仍要打开」即可。

## 二、命令行 / 源码运行

```bash
python3 start.py            # 默认启动原生图形界面（独立窗口）
python3 start.py --cli      # 交互式命令行菜单
python3 start.py https://www.bilimanga.net/detail/54.html   # 详情页链接，直接下载
python3 start.py 54                                         # 书号，直接下载
python3 start.py --out ~/Downloads/漫画 54                  # 本次临时指定输出目录
python3 start.py --debug    # 调试日志
```

`start.py` 会自动新建独立 conda 环境 `bilimanga-dl`（无 conda 时退回项目内 `.venv`，都不污染系统），装好依赖后启动。

命令行下载共 4 步：**确认漫画 → 解析目录 → 选章（如 `1-9,15,20-25`，回车=全选）→ 选格式**，随后进入流水线逐卷产出。

## 设置项

图形界面「① 设置」或命令行 `--cli` → 设置里可改：

- **站点地址**：默认 `https://www.bilimanga.net`，可改（不再内置多镜像回退，避免误连到别的站点）。
- **下载输出目录**：默认用**浏览器下载目录**（`~/Downloads`），可改成任意目录（成品按书名分子目录存放）。
- 默认格式（EPUB/PDF）、并发数、代理、限速 / 退避重试 / 断点续传、调试日志等。

设置与日志保存在：源码运行时为项目目录；打包运行时为系统应用数据目录
（macOS `~/Library/Application Support/Bilimanga-Downloader`，Windows `%APPDATA%\Bilimanga-Downloader`）。

## 优化 / 特性

- **过 Cloudflare**：用 DrissionPage 驱动本地 Chrome/Edge，靠真实浏览器指纹通过质询。
- **三阶段流水线（下载 → 校对 → 打包）**：逐卷串行下载，某卷下完立即交后台做本地校对 + 打包，与下一卷下载**重叠**进行 —— EPUB 逐卷产出，总耗时≈下载耗时。
- **每卷独立进度条**：选 N 卷即 N 条进度，直观看到每本的下载/校对/打包/完成。
- **自适应并发（每秒错误率采样）**：起步少量并发，每 1 秒按错误率调节（`0 → +1`、`<40% → −1`、`≥40% → −2`）；减并发为优雅缩减，不打断正在下载的线程。
- **AVIF/WebP 自动转 JPEG**：站点插图多为 AVIF（如 54 号书），统一转 JPEG 以兼容 EPUB / PDF，保留原始像素不留白。
- **懒加载修复**：阅读页插图由 JS 懒加载注入，等 `imagecontent` 出现再取并重试，避免漏图。
- **断点续传**：临时图片存 `temp/download/<书名>/<章>/`，中断重跑自动跳过已下；打包后清理临时图。
- **三种输入**（详情页 · 目录页 · 书号）**/ EPUB·PDF 双格式**（PDF 按原图整页排版，不失真不留白）。

## 目录结构

```
Bilimanga-Downloader/
├── start.py              # 入口：默认图形界面，--cli 走终端
├── docs/                 # 运行截图
├── packaging/            # 打包脚本（PyInstaller）与 GitHub Actions 工作流
├── src/
│   ├── requirements.txt
│   └── bilimanga_dl/     # 源码（net / scraper / downloader / build_* / cli / gui / ui …）
├── userscript/           # 🐵 油猴脚本版（备用，不稳定）
└── （运行时生成）config / logs / temp / 你设置的输出目录
```

## 依赖与前提

- 本机需已安装 **Chrome 或 Edge**（DrissionPage 驱动它过 Cloudflare）。
- 源码运行的其余 Python 依赖见 `src/requirements.txt`，`start.py` 自动安装；可执行文件已内置全部依赖。

> 仅供个人学习与备份公开内容使用，请遵守站点条款。
