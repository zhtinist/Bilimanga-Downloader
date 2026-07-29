# Bilimanga-Downloader

[English](README.en.md) · **中文**

[bilimanga.net](https://www.bilimanga.net/) 漫画下载器：抓取漫画插图，按卷/话下载并打包为 **EPUB** 或 **PDF**。

**两种用法，任选其一：**

| | 命令行版（本仓库主体） | 🐵 油猴脚本版（[`userscript/`](userscript)） |
|---|---|---|
| 面向 | 会用命令行、想要流水线/并发/断点续传 | **电脑小白**，零环境 |
| 运行环境 | 需 Python（`start.py` 自动装依赖）+ 本机 Chrome/Edge | **无需 Python**：装个 Tampermonkey，再一键装脚本 |
| 安装 | `python3 start.py` | 商店装 Tampermonkey → 点 `bilimanga.user.js` 一键安装、自动更新 |
| 平台 | macOS / Windows | macOS / Windows 通用（任意 Chromium/Firefox） |
| 干活的 CPU | 你的电脑 | 你的电脑（脚本同源跑在你自己浏览器里） |

> 油猴脚本**同源跑在 bilimanga 页面里**，直接复用你**已经过了 Cloudflare 的当前会话**，因此不用装任何环境、也没有跨域问题。停在某本漫画的主页/目录页时，页面**右侧会出现一个「⬇ 下载本书」小按钮**，点开直接选章 + 格式下载，免去粘贴链接与确认漫画两步。
> **一键安装**：先装 [Tampermonkey](https://www.tampermonkey.net/) → 点 [安装脚本](https://raw.githubusercontent.com/zhtinist/Bilimanga-Downloader/main/userscript/bilimanga.user.js)（油猴会自动弹出安装页）。详见 [`userscript/README.md`](userscript/README.md)。以下为**命令行版**说明。

## 运行示例（以 703《與妳相戀到生命盡頭》为例）

一条命令走完全流程：确认漫画 → 解析目录 → 选章 → 选格式 → 逐卷下载出书。

![运行流程](docs/run-flow.png)

**每个选中的卷各有一条进度条**，依次经历 `下载 → 🔍校对 → 📦打包 → ✓完成`，逐卷产出；
下载某一卷时其余卷等待，某卷下完立即在后台校对+打包（与下一卷下载重叠），你能实时看到
每个 EPUB 陆续 `✓完成`：

![逐卷进度条](docs/run-progress.png)

## 一键启动

```bash
python3 start.py
```

`start.py` 会自动新建独立 conda 环境 `bilimanga-dl`（无 conda 时退回项目内 `.venv`，都不污染系统），
装好依赖后进入命令行。也可直接带参数：

```bash
python3 start.py https://www.bilimanga.net/detail/703.html   # 详情页链接
python3 start.py https://www.bilimanga.net/read/703/catalog  # 目录页链接
python3 start.py 703                                         # 漫画书号
python3 start.py --debug    # 调试日志
```

> 想要图形界面/零环境？见油猴脚本 [`userscript/`](userscript)——命令行版为纯命令行工具。

## 使用流程（命令行，共 4 步）

1. **确认漫画**：默认仅在命令行打印作品名字供核对，回车 / y 确认；也可在设置里开启
   「弹出浏览器网页」在默认浏览器打开主页核对（后台已在预热浏览器）。
2. **解析目录**：真实浏览器过 Cloudflare 抓取目录。
3. **选择章**：按 `章号 + 章标题 + 含哪几话` 列出，输入如 `1-9,15,19,20-25` 勾选（回车=全选）。
4. **选择格式**：EPUB / PDF，随后进入下载流水线，逐卷产出。

## 优化 / 特性

- **过 Cloudflare**：用 DrissionPage 驱动本地 Chrome/Edge，靠真实浏览器指纹通过质询。
- **三阶段流水线（下载 → 校对 → 打包）**：逐卷串行下载，某卷下完立即交后台做
  本地校对 + 打包，与下一卷的下载**重叠**进行 —— EPUB 逐卷产出，总耗时≈下载耗时。
- **每卷独立进度条**：选 N 卷即 N 条进度条，直观看到每本书的下载/校对/打包/完成。
- **自适应并发（每秒错误率采样）**：起步少量并发，每 1 秒按这一秒错误率调节 ——
  `0 → +1`（顺利就加）、`<40% → −1`（温和减）、`≥40% → −2`（明显被限速则猛减）；
  **减并发是优雅缩减**：只降上限、不打断正在下载的线程，等其自然结束后不再派新活（不重下、不浪费）。
- **懒加载修复**：阅读页插图由 JS 懒加载注入，等 `imagecontent` 出现再取并重试，避免靠后的卷漏图。
- **断点续传**：临时图片存 `temp/download/<书名>/<章>/`，中断重跑自动跳过已下；成品打包后自动清理临时图。
- **本地校对（不联网）**：校对阶段只查缺失/空文件，缺失反馈给下载阶段补下，不重新联网重扫。
- **每卷封面**：EPUB 封面取该卷第一张图（各卷不同）；写入系列/卷号元数据便于阅读器归类。
- **网络不特别设置**：浏览器沿用系统网络环境；仅让本地 CDP 连接绕过代理（避免开代理时连不上浏览器）。
- **镜像切换 / 三种输入（详情页·目录页·书号）/ EPUB·PDF 双格式（PDF 按原图整页排版，不失真不留白）**。

## 目录结构

```
Bilimanga-Downloader/
├── start.py              # 命令行版唯一入口
├── docs/                 # 运行截图
├── src/
│   ├── requirements.txt
│   └── bilimanga_dl/     # 命令行版源码（net / scraper / downloader / build_* / cli / ui …）
├── userscript/           # 🐵 油猴脚本版（bilimanga.user.js，小白零环境）
├── config/setting.json   # 运行时生成
├── logs/  temp/  downloads/<书名>/   # 运行时生成（已 gitignore）
```

## 依赖与前提

- 本机需已安装 **Chrome 或 Edge**（DrissionPage 驱动它过 Cloudflare）。
- 其余 Python 依赖见 `src/requirements.txt`，`start.py` 自动安装。

> 仅供个人学习与备份公开内容使用，请遵守站点条款。
