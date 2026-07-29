# Bilimanga 漫画下载器（油猴脚本）

在 bilimanga 漫画页面里一键把整卷下载成 **EPUB / PDF** 的 Tampermonkey 用户脚本。
无需安装 Python 或任何环境，装好油猴、点一下右侧按钮即可，Mac / Windows 通用。

脚本同源运行在 bilimanga 页面内，直接复用当前浏览器已经通过 Cloudflare 的会话与
cookie，页面与图片都走同源请求，比外部工具更稳，也不需要额外授权域名。

## 安装

1. 先给 Chrome / Edge 安装 **Tampermonkey** 扩展（从对应浏览器的扩展商店搜索安装）。
2. 打开脚本文件的 **raw 链接**（GitHub 上点开 `userscript/bilimanga.user.js` → 右上角 `Raw`）：

   ```
   https://raw.githubusercontent.com/zhtinist/Bilimanga-Downloader/main/userscript/bilimanga.user.js
   ```

   Tampermonkey 会自动弹出安装页，点「安装」即可。
   也可以在 Tampermonkey 面板里「添加新脚本」，把本文件内容整个粘进去保存。

> 若你 fork 到自己的仓库，请把脚本头部 `@updateURL` / `@downloadURL` 里的
> GitHub 地址改成你自己的 raw 地址，才能享受自动更新提示。

## 使用

1. 打开任意一本漫画的**详情页**（`/detail/{书号}.html`）、**目录页**
   （`/read/{书号}/catalog`）或**阅读页**（`/read/{书号}/...`）。
2. 页面**右侧边缘**会出现一个竖排小按钮「⬇ 下载本书」。点它展开面板。
3. 面板已经默认是当前这本书，自动读取目录并列出全部章节：
   - 勾选要下载的章（默认全选）；可用「全选 / 清空」，或用表达式框填
     `1-9,15,20-25` 后点「应用」精确选择。
   - 选择输出格式 **EPUB** 或 **PDF**。
   - 点「开始下载」。每一卷一条进度（下载 → 打包 → 完成），完成后自动保存为
     `书名 - 卷名.epub` / `书名 - 卷名.pdf` 到浏览器下载目录。

排版与命令行版一致：每张插图独占一页、居中铺满不裁切；EPUB 每话一个目录条目、
带封面与 Calibre 系列信息；PDF 无损内嵌 JPEG、页面尺寸贴合图片像素。

## 已知限制 / 可能需要微调的点

- **需在真机浏览器实测**。站点 DOM 结构若调整，解析用的选择器
  （`h1.book-title`、`span.authorname`、`#bookSummary`、`.book-cover img`、
  `div.catalog-volume` / `li.chapter-li`、阅读页 `img.imagecontent` 的 `data-src`）
  可能需要按实际页面微调。
- **Cloudflare 人机验证**：脚本依赖你当前标签页已经过验证的会话。若下载中提示被
  Cloudflare 拦截，请在当前标签手动通过一次人机验证后重试。
- **图片懒加载**：脚本先用抓取到的原始 HTML 里的 `data-src`；个别话如果地址是由
  页面 JS 注入、原始 HTML 里取不到，会自动用一个隐藏 `iframe` 加载该阅读页、等
  渲染后再读地址。极少数强依赖脚本的页面仍可能漏图，可重试。
- **跨子域图片**：图片多与站点同源，直接 `fetch` 即可；若个别图片位于其它子域被
  浏览器拦截，脚本会自动改用 `GM_xmlhttpRequest` 兜底（因此脚本申请了 `@connect *`
  权限，Tampermonkey 首次可能提示允许跨域请求，选允许即可）。
- **保存方式**：优先用 `GM_download` 静默保存到下载目录；若被禁用会回退为普通的
  浏览器下载（`<a download>`），效果一致。
- 大卷（图片很多）打包时会占用一定内存，属正常现象；下载并发限制为 5，避免触发
  站点限流（429）。
