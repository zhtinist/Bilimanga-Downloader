# Bilimanga-Downloader

[English](README.en.md) · **中文**

[bilimanga.net](https://www.bilimanga.net/) 漫画 / [linovelib](https://www.linovelib.com) 轻小说下载器，按卷打包为 **EPUB** 或 **PDF**。

## 选择使用方式

| | ⭐ 命令行 / 源码 | 双击可执行文件 | 🐵 油猴脚本 |
|---|---|---|---|
| 适合 | 想直接就用 / 改代码 / 自动化 | 想直接双击运行 | 想在浏览器里顺手下 |
| 获取 | `python3 start.py` | 到 [Releases](../../releases) 下载对应系统的文件，双击 | 装 Tampermonkey → 装脚本 |
| 界面 | 终端交互界面 | 原生窗口 | 页面右侧浮动按钮 |
| 状态 | 主推 | 暂时搁置（macOS 权限问题） | 暂停维护（备用） |

当前主推命令行 / 源码；双击可执行文件暂时搁置，油猴脚本暂停维护。本机需装有 **Chrome 或 Edge**。

## 命令行 / 源码

无需 conda，`python3 start.py` 会自动创建项目内 `.venv` 并安装依赖。

```bash
python3 start.py                          # 进入终端交互界面
python3 start.py <链接或书号>              # 直接下载
python3 start.py --out <目录> <链接或书号>  # 本次输出到指定目录
```

进入终端交互界面后：直接粘贴漫画或轻小说的网址（按域名自动识别类型，无需先选；也可输入书号，会追问一次类型），接着交互式选章（方向键勾选或输入范围），可逐步回退。

- 漫画：`https://www.bilimanga.net/detail/54.html` 或书号 `54`
- 轻小说：`https://www.linovelib.com/novel/2139.html`

文件默认存到浏览器下载目录（`~/Downloads`），可用 `--out` 指定。

## 设置项

- 下载输出目录（默认浏览器下载目录，或用 `--out` 指定）
- 默认格式 EPUB / PDF
- 代理、限速 / 退避重试 / 断点续传、调试日志

下载线程数自动调节（终端实时显示当前线程数）。轻小说默认走手机站直连（更快），异常时回退浏览器；漫画仍需本机 Chrome / Edge（过 Cloudflare）。

## 目录结构

```
Bilimanga-Downloader/
├── start.py              # 入口（终端交互界面）
├── packaging/            # 打包脚本与 GitHub Actions 工作流
├── src/bilimanga_dl/     # 源码
├── userscript/           # 🐵 油猴脚本版（暂停维护）
└── resource/             # 封面图 app_cover.png
```

> 仅供个人学习与备份公开内容使用，请遵守站点条款。
