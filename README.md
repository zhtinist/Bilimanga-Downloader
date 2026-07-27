# Bilimanga-Downloader

[bilimanga.net](https://www.bilimanga.net/) 漫画下载器。跨平台（macOS / Windows）纯 Python 实现：
用真实浏览器过 Cloudflare 抓取漫画插图，按卷/话下载并打包为 **EPUB** 或 **PDF**。

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
python3 start.py --gui      # 图形界面（可选）
python3 start.py --debug    # 调试日志
```

## 使用流程（命令行，共 4 步）

1. **确认漫画**：自动在默认浏览器打开主页供核对，回车 / y 确认（后台已在预热浏览器）。
2. **解析目录**：真实浏览器过 Cloudflare 抓取目录。
3. **选择章**：按 `章号 + 章标题 + 含哪几话` 列出，输入如 `1-9,15,19,20-25` 勾选。
4. **选择格式**：EPUB / PDF，随后多话并行下载并打包。

## 特性

- **过 Cloudflare**：用 DrissionPage 驱动本地 Chrome/Edge；本地连接自动绕过系统代理。
- **多话并行 + 补漏重试**：默认同时下载 4 话，失败的图片自动补下直到齐。
- **断点续传**：临时图片存 `temp/download/<书名>/<章>/`，中断重跑自动跳过已下。
- **规范输出**：EPUB 写入系列/卷号元数据；PDF 按原图整页排版，不失真不留白。
- **镜像切换 / 代理自适应 / 三种输入（详情页·目录页·书号）**。

## 目录结构

```
Bilimanga-Downloader/
├── start.py              # 唯一入口
├── src/
│   ├── requirements.txt
│   └── bilimanga_dl/     # 源码（net / scraper / downloader / build_* / cli / ui …）
├── config/setting.json   # 运行时生成
├── logs/  temp/  downloads/<书名>/   # 运行时生成（已 gitignore）
```

## 依赖与前提

- 本机需已安装 **Chrome 或 Edge**（DrissionPage 驱动它过 Cloudflare）。
- 其余 Python 依赖见 `src/requirements.txt`，`start.py` 自动安装。

> 仅供个人学习与备份公开内容使用，请遵守站点条款。
