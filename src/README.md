# BilimangaDownloader

[bilimanga.net](https://www.bilimanga.net/) 漫画下载器。跨平台（macOS / Windows）纯 Python 实现：抓取漫画插图，按卷/话下载并打包为 **EPUB** 或 **PDF**。

## 使用（一键启动）

```bash
python3 start.py
```

`start.py` 会**自动**完成：新建独立 conda 环境 `bilimanga-dl`（无 conda 时退回项目内
`.venv`，都不污染系统环境）→ 装依赖 → 进入下载器。命令行只负责这一步启动。

```bash
python3 start.py <URL>     # 直接下载指定漫画
python3 start.py --gui     # 使用图形界面（可选）
python3 start.py --debug   # 开调试日志
python3 start.py --help    # 查看帮助
```

默认是**命令行**流程，共 4 步，逐步有清晰提示：

1. **解析**：粘贴漫画主页 URL（detail / catalog 链接或书号皆可），抓取信息。
2. **确认漫画**：自动在你的默认浏览器打开漫画主页供可视核对，终端回车 / y 确认、n 取消。
3. **选择章**：按 `章号 + 章标题 + 含哪几话` 列出，输入章号表达式勾选，
   示例 `1-9,15,19,20-25`（支持逗号分隔与 `a-b` 区间）。
4. **选择格式**：EPUB / PDF。

随后自动下载并打包。**下载目录固定**为项目根目录下的 `downloads/`，并**按书名分子目录**：
`downloads/<书名>/<书名> - <卷名>.epub`。进度条实时显示，完成后打印输出路径。

> 图形界面（`--gui`）提供同样流程的单窗口版本：封面预览、章节列表点选 / Shift 连选 /
> Ctrl·⌘ 多选 + 文本框输入、进度条。

## 目录结构与生成文件

```
BilimangaDownloader/
├── start.py            # 唯一入口（根目录只有它）
├── src/                # 源代码
│   ├── bilimanga_dl/
│   └── requirements.txt
├── config/setting.json # 设置（首次运行自动生成）
├── logs/               # 调试日志（开启时生成）
└── downloads/<书名>/   # 下载输出
```

所有运行时文件都在项目根目录内，不写入用户主目录。

## 设置

菜单里的「设置」（或 `config/setting.json`）可修改：

- 默认输出格式、并发线程数
- 请求限速开关、失败退避重试开关（两者独立，可同时开启）
- 断点续传开关（已下载文件自动跳过）
- 调试日志开关
- 镜像域名列表（主域名不可用时自动切换）

> 下载目录固定为 `downloads/`（按书名分子目录），不可配置。

## 关于 Cloudflare（重要）

站点使用较强的 Cloudflare 防护，纯 HTTP 库（requests / cloudscraper）已无法通过。
因此本工具**用 DrissionPage 驱动本地真实浏览器**（Chrome/Edge）加载页面来过 Cloudflare：

- **需要本机已安装 Chrome 或 Edge**（`start.py` 会自动安装 Python 依赖 DrissionPage，
  但浏览器本体需你自行装好）。
- 自动化浏览器**默认无头运行（无感，不弹窗）**；插图在浏览器内“导航到图片域名→同源 XHR”
  取字节（requests 会被图床的 JA3 指纹校验挡下），并用**多标签并行**加速。
- 若日志提示「该 IP 被 Cloudflare 硬封禁」或无头一直过不去，把 `browser_headless` 设为
  `false`（有头更易过质询），或更换**住宅代理**节点。
- 设置里可调：`browser_headless`、`cloudflare_wait`（等待秒数）、`parallel_chapters`（并发话数，默认 4）、
  `browser_path`（浏览器路径，留空自动探测）、`proxy`（留空自动，连不上自动直连）。
- 下载过程临时图片放在 `temp/download/<书名>/<章>/`（边下边落盘，不囤内存）；成品在 `downloads/<书名>/`。

## 依赖

见 `src/requirements.txt`。仅供个人学习与备份已购/公开内容使用，请遵守站点条款。
