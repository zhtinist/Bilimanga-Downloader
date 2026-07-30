# Bilimanga-Downloader

[English](README.en.md) · **中文**

[bilimanga.net](https://www.bilimanga.net/) 漫画 / [linovelib](https://www.linovelib.com) 轻小说下载器，按卷打包为 **EPUB** 或 **PDF**。

## 选择使用方式

| | ⭐ 双击可执行文件 | 命令行 / 源码 | 🐵 油猴脚本 |
|---|---|---|---|
| 适合 | 想直接就用 | 想改代码 / 自动化 | 想在浏览器里顺手下 |
| 获取 | 到 [Releases](../../releases) 下载对应系统的文件，双击 | `python3 start.py` | 装 Tampermonkey → 装脚本 |
| 界面 | 原生窗口 | 默认原生窗口，`--cli` 走终端 | 页面右侧浮动按钮 |
| 稳定性 | 稳 | 稳 | 一般（备用） |

推荐前两种；油猴脚本作为备用。本机需装有 **Chrome 或 Edge**。

## 双击可执行文件

到 [Releases](../../releases) 下载解压后双击：macOS 为 `Bilimanga-Downloader.app`，Windows 为 `Bilimanga-Downloader.exe`。打开后：粘贴链接或书号 → 解析 → 勾选要下的卷 → 开始下载。

- 漫画：`https://www.bilimanga.net/detail/54.html` 或书号 `54`
- 轻小说：`https://www.linovelib.com/novel/2139.html`

文件默认存到浏览器下载目录（`~/Downloads`），可在「设置」里改。

## 命令行 / 源码

```bash
python3 start.py            # 图形界面
python3 start.py --cli      # 终端菜单
python3 start.py <链接或书号>  # 直接下载
python3 start.py --out <目录> <链接或书号>   # 本次输出到指定目录
```

## 设置项

- 下载输出目录（默认浏览器下载目录）
- 默认格式 EPUB / PDF
- 代理、限速 / 退避重试 / 断点续传、调试日志

站点地址固定展示；下载线程数自动调节（界面实时显示当前线程数）。

## 目录结构

```
Bilimanga-Downloader/
├── start.py              # 入口（默认图形界面，--cli 走终端）
├── packaging/            # 打包脚本与 GitHub Actions 工作流
├── src/bilimanga_dl/     # 源码
├── userscript/           # 🐵 油猴脚本版
└── resource/             # 封面图 app_cover.png
```

> 仅供个人学习与备份公开内容使用，请遵守站点条款。
