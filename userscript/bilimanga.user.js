// ==UserScript==
// @name         Bilimanga 漫画/轻小说下载器
// @namespace    https://github.com/zhtinist/Bilimanga-Downloader
// @version      3.0.2
// @description  在 bilimanga 漫画 / 哔哩轻小说(bilinovel) 页面里一键把整卷下载成 EPUB / PDF，可存本地或上传到你的百度网盘。
// @author       HTZHU
// @license      MIT
// @homepageURL  https://github.com/zhtinist/Bilimanga-Downloader
// @supportURL   https://github.com/zhtinist/Bilimanga-Downloader/issues
// @icon         https://www.bilimanga.net/favicon.ico
// @match        https://www.bilimanga.net/*
// @match        https://www.bilicomic.net/*
// @match        https://www.bilinovel.com/*
// @match        https://m.bilinovel.com/*
// @match        https://www.linovelib.com/*
// @grant        GM_download
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @grant        GM_getValue
// @grant        GM_setValue
// 明确声明图片 CDN、站点域名与百度网盘域名：安装时一次性授权、之后下载/上传不再逐次弹跨域验证。
// （Tampermonkey 会匹配子域，故 motiezw.com 覆盖 i.motiezw.com、readpai.com 覆盖 img3.readpai.com；
//   baidu.com 覆盖 pan.baidu.com、pcs.baidu.com 覆盖 c.pcs.baidu.com——网盘用你浏览器里已登录的 cookie）
// @connect      motiezw.com
// @connect      readpai.com
// @connect      bilimanga.net
// @connect      bilicomic.net
// @connect      bilinovel.com
// @connect      linovelib.com
// @connect      baidu.com
// @connect      pcs.baidu.com
// @run-at       document-idle
// @updateURL    https://raw.githubusercontent.com/zhtinist/Bilimanga-Downloader/main/userscript/bilimanga.user.js
// @downloadURL  https://raw.githubusercontent.com/zhtinist/Bilimanga-Downloader/main/userscript/bilimanga.user.js
// ==/UserScript==

/*
 * 单文件用户脚本：脚本同源运行在 bilimanga 页面内，直接复用已过 Cloudflare 的会话，
 * 页面与图片均走同源 fetch（携带 cookie）；个别跨子域图片用 GM_xmlhttpRequest 兜底。
 * 核心逻辑（解析、自写 ZIP→EPUB、自写 PDF、图片转 JPEG）与命令行版一致。
 */

(function () {
  "use strict";

  // =====================================================================
  // 一、工具
  // =====================================================================
  const textEncoder = new TextEncoder();
  const enc = (s) => textEncoder.encode(s);

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // 给任意 Promise 套一个总超时兜底：无论卡在网络、图片解码还是 canvas 编码，
  // 到点就 reject，保证并发池的 worker 一定会往下走，绝不永久卡住。
  function withTimeout(promise, ms, label) {
    let t;
    const guard = new Promise((_, rej) => {
      t = setTimeout(() => rej(new Error((label || "操作") + "超时")), ms);
    });
    return Promise.race([promise, guard]).finally(() => clearTimeout(t));
  }

  // ===================== 调试日志 =====================
  // 勾选调试后：让用户选 log.txt 落点，边下边把日志“持续写盘”。关键点——即便某个
  // await 卡死，事件循环并没有停，setInterval 定时刷盘照常执行，能把日志记到卡住的
  // 那一刻，便于定位是哪一章/哪张图/哪个请求卡住。不支持 File System Access 或用户
  // 取消时，退回“结束时把缓冲整体下载成 log.txt”。
  let DBG = false;
  let logBuffer = [];
  let logHandle = null;        // FileSystemFileHandle
  let logDirty = false;
  let logFlushing = false;
  let logFlushTimer = null;
  const t0log = Date.now();

  function dlog() {
    if (!DBG) return;
    const args = Array.prototype.slice.call(arguments);
    const dt = ((Date.now() - t0log) / 1000).toFixed(2);
    const line = `[+${dt}s] ` + args.map(
      (a) => (typeof a === "string" ? a : (() => { try { return JSON.stringify(a); } catch (e) { return String(a); } })())
    ).join(" ");
    logBuffer.push(line);
    logDirty = true;
    try { console.debug("[bmd]", line); } catch (e) {}
  }

  async function flushLog() {
    if (logFlushing || !logDirty || !logHandle) return;
    logFlushing = true;
    logDirty = false;
    try {
      const w = await logHandle.createWritable();
      await w.write(logBuffer.join("\n") + "\n");
      await w.close();
    } catch (e) {
      logDirty = true;      // 写失败下轮重试
    } finally {
      logFlushing = false;
    }
  }

  async function startDebugLog() {
    logBuffer = [];
    logDirty = false;
    logHandle = null;
    // showSaveFilePicker 必须在用户手势里同步发起（onStart 由点击触发，此处之前无 await）
    if (typeof window.showSaveFilePicker === "function") {
      try {
        logHandle = await window.showSaveFilePicker({
          suggestedName: "log.txt",
          types: [{ description: "文本日志", accept: { "text/plain": [".txt"] } }],
        });
      } catch (e) { logHandle = null; }   // 用户取消 → 退回结束时下载
    }
    if (logFlushTimer) clearInterval(logFlushTimer);
    logFlushTimer = setInterval(() => { flushLog(); }, 1500);
    dlog("=== 调试日志开始 ===");
    dlog("UA", navigator.userAgent);
    dlog("持续写盘方式", logHandle ? "File System Access（实时写 log.txt）" : "内存缓冲（结束时下载 log.txt）");
  }

  async function stopDebugLog() {
    dlog("=== 调试日志结束 ===");
    logDirty = true;
    await flushLog();
    if (logFlushTimer) { clearInterval(logFlushTimer); logFlushTimer = null; }
    if (!logHandle) {          // 没拿到文件句柄：把缓冲整体存成 log.txt 到下载目录
      try { saveText(logBuffer.join("\n") + "\n", "log.txt"); } catch (e) {}
    }
  }

  function saveText(text, filename) {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    if (typeof GM_download === "function") {
      try {
        GM_download({ url, name: filename, saveAs: false,
          onerror: () => anchorDownload(url, filename),
          ontimeout: () => anchorDownload(url, filename) });
      } catch (e) { anchorDownload(url, filename); }
    } else { anchorDownload(url, filename); }
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  // 带超时的 fetch：卡住的请求会自动 abort，避免拖死并发池里的一个 worker。
  function fetchWithTimeout(url, opts, ms) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms || 20000);
    return fetch(url, Object.assign({ signal: ctrl.signal }, opts || {})).finally(() =>
      clearTimeout(timer)
    );
  }

  function escXml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&apos;");
  }

  function escHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function safeName(name) {
    const cleaned = String(name || "")
      .replace(/[?*"<>|:/\\\x00-\x1f]/g, "_")
      .trim()
      .replace(/^\.+|\.+$/g, "");
    return cleaned || "untitled";
  }

  function pad5(n) {
    return String(n).padStart(5, "0");
  }

  // 选择表达式 1-9,15,20-25（照抄 select_parser.py）
  function parseSelection(expr, maxIndex) {
    const normalized = (expr || "")
      .replace(/[０-９]/g, (d) => String.fromCharCode(d.charCodeAt(0) - 0xfee0)) // 全角数字→半角
      .replace(/，/g, ",")
      .replace(/[－–—~]/g, "-")
      .replace(/\s/g, "");
    if (!normalized) throw new Error("选择表达式为空");
    const result = new Set();
    for (const part of normalized.split(",")) {
      if (!part) continue;
      if (part.includes("-")) {
        const bounds = part.split("-");
        if (bounds.length !== 2 || !bounds[0] || !bounds[1]) {
          throw new Error(`区间格式错误：${part}（应形如 20-25）`);
        }
        let start = parseInt(bounds[0], 10);
        let end = parseInt(bounds[1], 10);
        if (Number.isNaN(start) || Number.isNaN(end)) {
          throw new Error(`区间必须为数字：${part}`);
        }
        if (start > end) [start, end] = [end, start];
        for (let n = start; n <= end; n++) result.add(n);
      } else {
        const n = parseInt(part, 10);
        if (Number.isNaN(n)) throw new Error(`章号必须为数字：${part}`);
        result.add(n);
      }
    }
    const bad = [...result].filter((n) => n < 1 || n > maxIndex);
    if (bad.length) {
      throw new Error(`章号超出范围 1-${maxIndex}: ${bad.sort((a, b) => a - b)}`);
    }
    return [...result].sort((a, b) => a - b);
  }

  // =====================================================================
  // 二、站点解析（详情 / 目录 / 阅读页），照抄 scraper.py
  // =====================================================================
  const CF_CHALLENGE = [
    "Just a moment",
    "Checking your browser",
    "Enable JavaScript and cookies",
    "Verifying you are human",
  ];

  function absUrl(url, base) {
    url = (url || "").trim();
    if (!url) return "";
    if (url.startsWith("http://") || url.startsWith("https://")) return url;
    if (url.startsWith("//")) return "https:" + url;
    if (url.startsWith("/")) return base + url;
    return base + "/" + url;
  }

  function isChallenge(html) {
    return !html || CF_CHALLENGE.some((m) => html.includes(m));
  }

  // 软限流占位页特征（429 或站点返回的“审核未通过/需要权限”页）。
  const RATE_MARKERS = [
    "需要足夠的權限", "需要足够的权限", "審核未通過", "审核未通过",
    "沒有可閱讀的章節內容", "没有可阅读的章节内容",
  ];
  function isRateLimited(status, html) {
    return status === 429 || (!!html && RATE_MARKERS.some((m) => html.includes(m)));
  }

  // 限流闸门（照搬命令行版 RateGate）：正文/页面请求串行 + 最小间隔，命中限流即
  // 让**全体**请求一起冷却并自适应拉长间隔；图片请求只等冷却、不串行。
  // 浏览器里本就带真实指纹+cookie（不必过 CF），这里唯一要防的就是按 IP 的 429。
  const gate = (() => {
    let busy = false, nextAt = 0, cooldownUntil = 0, minGap = 250;
    const BASEGAP = 250, MAXGAP = 6000;
    async function acquire() {          // 正文/页面：串行 + 间隔 + 冷却
      while (busy) await sleep(25);
      busy = true;
      let wait;
      while ((wait = Math.max(nextAt - Date.now(), cooldownUntil - Date.now())) > 0) {
        await sleep(Math.min(wait, 1000));
      }
      nextAt = Date.now() + minGap;
    }
    function release() { busy = false; }
    function penalize(sec) {            // 命中限流：全体冷却 + 拉长间隔
      cooldownUntil = Math.max(cooldownUntil, Date.now() + sec * 1000);
      minGap = Math.min(minGap * 1.5 + 100, MAXGAP);
    }
    function reward() { if (minGap > BASEGAP) minGap = Math.max(BASEGAP, minGap * 0.9); }
    async function waitCooldown() {     // 图片：只等全局冷却，不串行
      let wait;
      while ((wait = cooldownUntil - Date.now()) > 0) await sleep(Math.min(wait, 1000));
    }
    return { acquire, release, penalize, reward, waitCooldown };
  })();

  // 页面抓取：走限流闸门；命中 429/占位页则全体冷却后重试；真 CF 质询交用户手动过。
  async function fetchText(url, tries = 8) {
    let lastErr = null;
    for (let attempt = 0; attempt < tries; attempt++) {
      dlog("GET", url, "第" + (attempt + 1) + "/" + tries + "次");
      await gate.acquire();
      let status = 0, html = "";
      try {
        const resp = await fetchWithTimeout(url, { credentials: "include" }, 20000);
        status = resp.status;
        html = await resp.text();
      } catch (e) {
        lastErr = e;
        dlog("  请求异常", url, e && e.message);
      } finally {
        gate.release();
      }
      if (status === 200) {
        if (CF_CHALLENGE.some((m) => html.includes(m))) {
          dlog("  命中 Cloudflare 质询", url);
          throw new Error("页面被 Cloudflare 拦截，请在当前标签手动通过人机验证后重试。");
        }
        if (html && !isRateLimited(status, html)) {
          gate.reward();
          dlog("  200 OK", url, "字节=" + html.length);
          return html;
        }
      }
      // 429 / 占位限流 / 空响应：全体冷却后重试；其它 HTTP 错误指数退避。
      if (isRateLimited(status, html) || !html) {
        const cd = Math.min(15 + attempt * 8, 45);
        gate.penalize(cd);
        dlog("  限流/空响应", url, "status=" + status, "冷却" + cd + "s后重试");
      } else {
        const bk = Math.min(1500 * Math.pow(2, attempt), 10000);
        dlog("  HTTP 错误", url, "status=" + status, "退避" + bk + "ms");
        await sleep(bk);
      }
    }
    dlog("  放弃（多次抓取失败）", url);
    throw lastErr || new Error(`多次抓取失败（限流未恢复）：${url}`);
  }

  function parseDoc(html) {
    return new DOMParser().parseFromString(html, "text/html");
  }

  function textOf(node) {
    return node ? (node.textContent || "").trim() : "";
  }

  function extractCover(doc, base) {
    for (const sel of [".book-cover img", ".book-img img", "img.book-cover"]) {
      const node = doc.querySelector(sel);
      if (node) {
        const src = node.getAttribute("data-src") || node.getAttribute("src");
        if (src) return absUrl(src, base);
      }
    }
    const og = doc.querySelector('meta[property="og:image"]');
    if (og && og.getAttribute("content")) return absUrl(og.getAttribute("content"), base);
    return "";
  }

  async function fetchBook(bookNo, base) {
    const html = await fetchText(`${base}/detail/${bookNo}.html`);
    const doc = parseDoc(html);

    const title =
      textOf(doc.querySelector("h1.book-title")) || textOf(doc.querySelector("h1"));
    let author = textOf(doc.querySelector("span.authorname"));
    if (!author) author = textOf(doc.querySelector(".author a, .authorname a"));
    const summary = textOf(doc.querySelector("#bookSummary"));
    const coverUrl = extractCover(doc, base);

    let tags = [];
    const tagGroup = doc.querySelector("span.tag-small-group");
    if (tagGroup) {
      tags = Array.from(tagGroup.querySelectorAll("a")).map((a) => textOf(a)).filter(Boolean);
    }

    const volumes = await fetchCatalog(bookNo, base);
    return {
      bookNo,
      base,
      title: title || `未知书名(${bookNo})`,
      author: author || "未知作者",
      coverUrl,
      summary,
      tags,
      volumes,
    };
  }

  async function fetchCatalog(bookNo, base) {
    const html = await fetchText(`${base}/read/${bookNo}/catalog`);
    const doc = parseDoc(html);
    const volumes = [];
    doc.querySelectorAll("div.catalog-volume").forEach((volNode, i) => {
      const index = i + 1;
      const title = textOf(volNode.querySelector("h3")) || `第${index}章`;
      const chapters = [];
      volNode.querySelectorAll("li.chapter-li").forEach((li) => {
        const a = li.querySelector("a[href]");
        if (!a) return;
        const href = a.getAttribute("href") || "";
        if (href.includes("javascript") || href.trim() === "#" || href.trim() === "") return;
        const name = textOf(a.querySelector("span")) || textOf(a);
        chapters.push({ title: name, url: absUrl(href, base) });
      });
      volumes.push({ index, title, chapters });
    });
    return volumes;
  }

  // ===================================================================
  // 二·B、轻小说（哔哩轻小说 手机站 bilinovel）解析 —— 与命令行版 novel_mobile.py 一致。
  // 手机站正文干净（无 PUA 字体混淆）：阅读页取 #acontent 内的 <p> 与 <img data-src>。
  // ===================================================================
  function isNovelPath() {
    return /\/novel\/\d+/.test(location.pathname);
  }

  function pageTextNovel(html) {
    const doc = parseDoc(html);
    const content = doc.querySelector("#acontent") || doc.querySelector(".bcontent");
    if (!content) return "";
    // 丢弃诱饵/装饰节点：类名形如 [a-z]\d{4} 的元素，以及非 p/img 的直接子节点。
    content.querySelectorAll("[class]").forEach((el) => {
      const cls = (el.getAttribute("class") || "").trim();
      if (/^[a-z]\d{4}$/.test(cls)) el.remove();
    });
    Array.from(content.children).forEach((el) => {
      const tag = el.tagName.toLowerCase();
      if (tag !== "p" && tag !== "img") el.remove();
    });
    const out = [];
    content.querySelectorAll("p, img").forEach((node) => {
      if (node.tagName.toLowerCase() === "img") {
        const src = node.getAttribute("data-src") || node.getAttribute("src") || "";
        if (!src || src.includes("<")) return;
        out.push('<img class="__nv__" src="' + absUrl(src, location.origin) + '"/>');
      } else {
        const t = (node.textContent || "").trim();
        if (t) out.push("<p>" + escHtml(t) + "</p>");
      }
    });
    return out.join("\n");
  }

  async function fetchChapterTextNovel(chapUrl, base) {
    let text = "", url = chapUrl, page = 1;
    // 安全上限：正常分页不会超过几页；给个硬上限，杜绝“翻页条件误判”导致的死循环。
    const MAX_PAGES = 30;
    while (page <= MAX_PAGES) {
      dlog("  正文分页", "第" + page + "页", url);
      const html = await fetchText(url);
      text += pageTextNovel(html);
      const nxt = chapUrl.replace(".html", "_" + (page + 1) + ".html").slice(base.length);
      if (html.includes(nxt)) { page += 1; url = base + nxt; }
      else break;
    }
    if (page > MAX_PAGES) dlog("  ⚠ 分页达到上限 " + MAX_PAGES + "，强制停止", chapUrl);
    return text;
  }

  async function fetchCatalogNovel(bookNo, base) {
    const html = await fetchText(base + "/novel/" + bookNo + "/catalog");
    const doc = parseDoc(html);
    const volumes = [];
    let cur = null, idx = 0;
    doc.querySelectorAll(".volume-chapters > li").forEach((li) => {
      const cls = li.className || "";
      if (cls.includes("chapter-bar")) {
        idx += 1;
        cur = { index: idx, title: (li.textContent || "").trim() || ("第" + idx + "卷"), chapters: [] };
        volumes.push(cur);
        return;
      }
      if (cls.includes("volume-cover")) return;
      if (cls.includes("jsChapter")) {
        if (!cur) { idx += 1; cur = { index: idx, title: "", chapters: [] }; volumes.push(cur); }
        const a = li.querySelector("a");
        if (!a) return;
        const href = a.getAttribute("href") || "";
        const url = href.includes("javascript") ? "" : absUrl(href, base);
        cur.chapters.push({ title: (li.textContent || "").trim() || textOf(a), url });
      }
    });
    return volumes.filter((v) => v.chapters.length);
  }

  async function fetchBookNovel(bookNo, base) {
    const html = await fetchText(base + "/novel/" + bookNo + ".html");
    const doc = parseDoc(html);
    const meta = (p) => {
      const m = doc.querySelector('meta[property="' + p + '"]');
      return m && m.getAttribute("content") ? m.getAttribute("content").trim() : "";
    };
    const title = textOf(doc.querySelector(".book-title")) || meta("og:novel:book_name") || ("未知(" + bookNo + ")");
    const author = textOf(doc.querySelector(".book-rand-a span")) || meta("og:novel:author") || "未知作者";
    const publisher = textOf(doc.querySelector(".tag-small.orange")) || meta("og:novel:category");
    const summary = textOf(doc.querySelector("#bookSummary content")) || meta("og:description");
    const tags = Array.from(doc.querySelectorAll(".book-cell .book-meta span em")).map((e) => textOf(e)).filter(Boolean);
    const coverEl = doc.querySelector(".book-layout img") || doc.querySelector(".book-img img");
    const coverUrl = coverEl ? absUrl(coverEl.getAttribute("src") || "", base) : "";
    const volumes = await fetchCatalogNovel(bookNo, base);
    return { bookNo, base, kind: "novel", title, author, publisher, summary, coverUrl, tags, volumes };
  }

  function pad2(n) { return String(n).padStart(2, "0"); }

  function textXhtmlNovel(title, body) {
    return '<?xml version="1.0" encoding="utf-8"?>\n' +
      '<html xmlns="http://www.w3.org/1999/xhtml"><head>\n' +
      `<title>${escXml(title)}</title>\n` +
      "<style>body{line-height:1.75;} p{text-indent:2em;margin:.6em 0;} " +
      "img{display:block;margin:1em auto;max-width:100%;height:auto;}</style>\n" +
      `</head><body>\n<h1>${escXml(title)}</h1>\n${body}\n</body></html>`;
  }

  function coverXhtmlNovel(w, h) {
    return '<?xml version="1.0" encoding="UTF-8"?>\n' +
      '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title></head>\n' +
      '<body style="margin:0;padding:0;text-align:center;">\n' +
      `<svg xmlns="http://www.w3.org/2000/svg" height="100%" preserveAspectRatio="xMidYMid meet" version="1.1" viewBox="0 0 ${w} ${h}" width="100%" xmlns:xlink="http://www.w3.org/1999/xlink">\n` +
      `<image width="${w}" height="${h}" xlink:href="../Images/00.jpg"/></svg>\n</body></html>`;
  }

  function buildEpubNovel(book, vol, chapters, images) {
    const entries = [];
    entries.push({ name: "mimetype", data: enc("application/epub+zip") });
    entries.push({ name: "META-INF/container.xml", data: enc(
      '<?xml version="1.0" encoding="UTF-8"?>\n' +
      '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n' +
      '  <rootfiles>\n    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n  </rootfiles>\n</container>') });

    const textItems = [], spineItems = [], navPoints = [];
    let chapNo = 0;
    for (const c of chapters) {
      if (!c.body.trim()) continue;
      const fn = pad2(chapNo) + ".xhtml";
      entries.push({ name: `OEBPS/Text/${fn}`, data: enc(textXhtmlNovel(c.title, c.body)) });
      textItems.push(`    <item id="x${pad2(chapNo)}" href="Text/${fn}" media-type="application/xhtml+xml"/>`);
      spineItems.push(`    <itemref idref="x${pad2(chapNo)}"/>`);
      navPoints.push({ name: c.title, fn });
      chapNo += 1;
    }
    const imgItems = [];
    for (const idx of Object.keys(images).sort()) {
      entries.push({ name: `OEBPS/Images/${idx}.jpg`, data: images[idx].jpeg });
      imgItems.push(`    <item id="img${idx}" href="Images/${idx}.jpg" media-type="image/jpeg"/>`);
    }
    let coverItem = "", coverSpine = "";
    if (images["00"]) {
      const w = images["00"].width || 600, h = images["00"].height || 800;
      entries.push({ name: "OEBPS/Text/cover.xhtml", data: enc(coverXhtmlNovel(w, h)) });
      coverItem = '    <item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>\n' +
        '    <item id="cover-img" href="Images/00.jpg" media-type="image/jpeg" properties="cover-image"/>';
      coverSpine = '    <itemref idref="cover"/>';
    }
    const subjects = (book.tags || []).map((t) => `    <dc:subject>${escXml(t)}</dc:subject>`).join("\n");
    const ncxPoints = navPoints.map((p, i) =>
      `    <navPoint id="n${i}" playOrder="${i + 1}"><navLabel><text>${escXml(p.name)}</text></navLabel><content src="Text/${p.fn}"/></navPoint>`).join("\n");
    const toc = '<?xml version="1.0" encoding="utf-8"?>\n' +
      '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head>\n' +
      `<meta name="dtb:uid" content="linovelib-${book.bookNo}-${vol.index}"/></head>\n` +
      `<docTitle><text>${escXml(book.title)}-${escXml(vol.title)}</text></docTitle>\n` +
      `<navMap>\n${ncxPoints}\n</navMap></ncx>`;
    const opf = '<?xml version="1.0" encoding="utf-8"?>\n' +
      '<package version="3.0" unique-identifier="BookId" xmlns="http://www.idpf.org/2007/opf">\n' +
      '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n' +
      `    <dc:identifier id="BookId">linovelib-${book.bookNo}-${vol.index}</dc:identifier>\n` +
      `    <dc:title>${escXml(book.title)}-${escXml(vol.title)}</dc:title>\n` +
      "    <dc:language>zh-CN</dc:language>\n" +
      `    <dc:creator>${escXml(book.author)}</dc:creator>\n` +
      `    <dc:publisher>${escXml(book.publisher || "")}</dc:publisher>\n` +
      `    <dc:description>${escXml(book.summary || "")}</dc:description>\n` +
      (subjects ? subjects + "\n" : "") +
      `    <meta name="calibre:series" content="${escXml(book.title)}"/>\n` +
      `    <meta name="calibre:series_index" content="${vol.index}"/>\n` +
      "  </metadata>\n  <manifest>\n" +
      '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n' +
      (coverItem ? coverItem + "\n" : "") +
      textItems.join("\n") + "\n" + imgItems.join("\n") +
      "\n  </manifest>\n  <spine toc=\"ncx\">\n" +
      (coverSpine ? coverSpine + "\n" : "") +
      spineItems.join("\n") + "\n  </spine>\n</package>";
    entries.push({ name: "OEBPS/toc.ncx", data: enc(toc) });
    entries.push({ name: "OEBPS/content.opf", data: enc(opf) });
    return buildZip(entries);
  }

  async function processVolumeNovel(book, vol, prog) {
    prog.setState("下载正文…");
    const jobs = vol.chapters.map((c, i) => ({ c, i })).filter((j) => !j.c.url.includes("javascript"));
    const bodies = new Array(vol.chapters.length).fill("");
    let done = 0;
    dlog("▶ 开始卷", vol.index, vol.title, "章数=" + jobs.length);
    // 正文串行抓取（并发受 gate 控制；正文页对并发敏感，串行 + 429 冷却最稳）。
    await runPool(jobs, 1, async ({ c, i }) => {
      dlog("正文 第" + (done + 1) + "/" + jobs.length + "章 开始", c.title, c.url);
      const ts = Date.now();
      try {
        bodies[i] = await fetchChapterTextNovel(c.url, book.base);
        dlog("正文 章完成", c.title, "字节=" + bodies[i].length, "耗时" + ((Date.now() - ts) / 1000).toFixed(1) + "s");
      } catch (e) {
        bodies[i] = "";
        dlog("正文 章失败", c.title, e && e.message);
      }
      done += 1;
      prog.setRatio(done / (jobs.length * 1.3));
      prog.setState(`下载正文 ${done}/${jobs.length}`);
    });

    // 按章节顺序统一给插图编号（第一张 00 作封面）
    const ordered = jobs.map((j) => bodies[j.i] || "");
    const imgMap = {};
    for (const b of ordered) {
      const re = /<img class="__nv__" src="([^"]+)"\/>/g;
      let m;
      while ((m = re.exec(b))) if (!(m[1] in imgMap)) imgMap[m[1]] = pad2(Object.keys(imgMap).length);
    }
    const rewrite = (b) => b.replace(/<img class="__nv__" src="([^"]+)"\/>/g, (mm, u) => {
      const idx = imgMap[u];
      return idx === "00" ? "" : `<img alt="${idx}" src="../Images/${idx}.jpg"/>`;
    });
    const chapters = jobs.map((j, k) => ({ title: j.c.title, body: rewrite(ordered[k]) }));
    const imgUrls = Object.keys(imgMap);
    if (!chapters.some((c) => c.body.trim()) && imgUrls.length <= 1) {
      prog.error("未获取到正文（可能被临时限流，稍后重试）");
      return;
    }

    prog.setState(`下载插图 ${imgUrls.length} 张…`);
    dlog("插图 共", imgUrls.length, "张");
    const images = {};
    let imgDone = 0;
    await runPool(imgUrls, IMAGE_CONCURRENCY, async (u) => {
      const idx = imgMap[u];
      // 边下边补：单张就地重试 3 次，命中限流先等全局冷却。每次取图套 40s 总超时，
      // 卡死的请求会被 abort 并重试，不拖死并发池。
      for (let attempt = 0; attempt < 3; attempt++) {
        await gate.waitCooldown();
        dlog("插图 取", idx, "第" + (attempt + 1) + "次", u);
        try {
          images[idx] = await withTimeout(fetchImageAsJpeg(u), 40000, "下载插图");
          imgDone += 1;
          dlog("插图 OK", idx, images[idx].width + "x" + images[idx].height, "(" + imgDone + "/" + imgUrls.length + ")");
          return;
        } catch (e) {
          dlog("插图 失败", idx, "第" + (attempt + 1) + "次", e && e.message);
          if (attempt < 2) await sleep(500 * (attempt + 1));
        }
      }
      dlog("插图 放弃", idx, u);
    });

    prog.setState("打包中…");
    dlog("打包中…", "章=" + chapters.length, "图=" + Object.keys(images).length);
    const bytes = buildEpubNovel(book, vol, chapters, images);
    dlog("打包完成", "字节=" + bytes.length, "去向=" + saveTarget);
    await dispatchSave(book, bytes, safeName(`${book.title} - ${vol.title}`) + ".epub", "epub", prog);
    dlog("✔ 卷完成", vol.index, vol.title);
    prog.done();
  }

  function imageUrlsFromDoc(doc, base) {
    const urls = [];
    doc.querySelectorAll("img.imagecontent").forEach((img) => {
      const src = img.getAttribute("data-src") || img.getAttribute("src");
      if (src && !src.startsWith("data:")) urls.push(absUrl(src, base));
    });
    return urls;
  }

  // 阅读页图片地址：先用原始 HTML 取 data-src；若为 0 张，用隐藏 iframe 让页面
  // JS 完成懒加载注入后再读渲染后的 DOM（同源 iframe 可直接访问）。
  async function fetchChapterImages(chapterUrl, base) {
    try {
      const html = await fetchText(chapterUrl);
      const urls = imageUrlsFromDoc(parseDoc(html), base);
      if (urls.length > 0) return urls;
    } catch (e) {
      if (String(e.message).includes("Cloudflare")) throw e;
    }
    return fetchChapterImagesViaIframe(chapterUrl, base);
  }

  function fetchChapterImagesViaIframe(chapterUrl, base) {
    return new Promise((resolve) => {
      const iframe = document.createElement("iframe");
      iframe.style.cssText = "position:fixed;left:-9999px;top:-9999px;width:600px;height:800px;border:0;";
      let settled = false;
      const finish = (urls) => {
        if (settled) return;
        settled = true;
        try {
          iframe.remove();
        } catch (e) {}
        resolve(urls);
      };
      iframe.addEventListener("load", async () => {
        const deadline = Date.now() + 15000;
        while (Date.now() < deadline) {
          let urls = [];
          try {
            const idoc = iframe.contentDocument;
            if (idoc) urls = imageUrlsFromDoc(idoc, base);
          } catch (e) {}
          if (urls.length > 0) return finish(urls);
          await sleep(800);
        }
        finish([]);
      });
      iframe.src = chapterUrl;
      document.body.appendChild(iframe);
      setTimeout(() => finish([]), 20000); // 兜底超时
    });
  }

  // =====================================================================
  // 三、图片抓取与转码（同源 fetch → GM_xmlhttpRequest 兜底 → 转 JPEG）
  // =====================================================================
  function gmFetchBytes(url) {
    return new Promise((resolve, reject) => {
      if (typeof GM_xmlhttpRequest !== "function") {
        return reject(new Error("GM_xmlhttpRequest 不可用"));
      }
      GM_xmlhttpRequest({
        method: "GET",
        url,
        responseType: "arraybuffer",
        anonymous: false,
        // 关键：必须设超时。否则图床 hold 住连接不返回时，本请求永不结算，
        // 会把并发池的 worker 一个个占死，最终进度条卡在半路不动（尤其轻小说插图
        // 来自跨域图床、每张都走这里）。超时后 ontimeout 触发 → 交由上层重试/放弃。
        timeout: 30000,
        // 图床常有防盗链：显式带上本站 Referer（同 Python 里“导航到图片同源再取字节”的效果）
        headers: { Referer: location.origin + "/" },
        onload: (resp) => {
          if (resp.status >= 200 && resp.status < 300 && resp.response) {
            resolve(new Uint8Array(resp.response));
          } else {
            reject(new Error("HTTP " + resp.status));
          }
        },
        onerror: () => reject(new Error("GM 请求失败")),
        ontimeout: () => reject(new Error("GM 请求超时")),
      });
    });
  }

  async function fetchImageBytes(url) {
    try {
      const resp = await fetchWithTimeout(url, { credentials: "include" }, 25000);
      if (resp.ok) return new Uint8Array(await resp.arrayBuffer());
    } catch (e) {
      // 跨域被拦 / 超时：走 GM 兜底
    }
    return gmFetchBytes(url);
  }

  // 把已解码的位图铺白底后编码成 JPEG。优先 OffscreenCanvas；缺失（部分浏览器/
  // 脚本管理器不支持 OffscreenCanvas.convertToBlob）时退回普通 <canvas>，提升兼容性。
  async function bitmapToJpeg(bitmap) {
    const w = bitmap.width;
    const h = bitmap.height;
    let jpeg;
    if (typeof OffscreenCanvas !== "undefined" && OffscreenCanvas.prototype.convertToBlob) {
      const canvas = new OffscreenCanvas(w, h);
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, w, h);
      ctx.drawImage(bitmap, 0, 0);
      const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.9 });
      jpeg = new Uint8Array(await blob.arrayBuffer());
    } else {
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, w, h);
      ctx.drawImage(bitmap, 0, 0);
      const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.9));
      if (!blob) throw new Error("canvas 编码失败");
      jpeg = new Uint8Array(await blob.arrayBuffer());
    }
    return { jpeg, width: w, height: h };
  }

  async function fetchImageAsJpeg(url) {
    const raw = await fetchImageBytes(url);
    const blob = new Blob([raw]);
    let bitmap;
    try {
      bitmap = await createImageBitmap(blob);
    } catch (e) {
      throw new Error("图片解码失败：" + url);
    }
    try {
      return await bitmapToJpeg(bitmap);
    } finally {
      if (bitmap.close) bitmap.close();
    }
  }

  // =====================================================================
  // 四、纯 JS 自写 ZIP（store 无压缩，供 EPUB）
  // =====================================================================
  const CRC_TABLE = (() => {
    const table = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[n] = c >>> 0;
    }
    return table;
  })();

  function crc32(bytes) {
    let crc = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) {
      crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
    }
    return (crc ^ 0xffffffff) >>> 0;
  }

  function buildZip(entries) {
    const localParts = [];
    const central = [];
    let offset = 0;
    for (const entry of entries) {
      const nameBytes = enc(entry.name);
      const data = entry.data;
      const crc = crc32(data);
      const size = data.length;

      const header = new Uint8Array(30 + nameBytes.length);
      const hv = new DataView(header.buffer);
      hv.setUint32(0, 0x04034b50, true);
      hv.setUint16(4, 20, true);
      hv.setUint16(6, 0x0800, true);
      hv.setUint16(8, 0, true);
      hv.setUint16(10, 0, true);
      hv.setUint16(12, 0x21, true);
      hv.setUint32(14, crc, true);
      hv.setUint32(18, size, true);
      hv.setUint32(22, size, true);
      hv.setUint16(26, nameBytes.length, true);
      hv.setUint16(28, 0, true);
      header.set(nameBytes, 30);
      localParts.push(header, data);

      const cd = new Uint8Array(46 + nameBytes.length);
      const cv = new DataView(cd.buffer);
      cv.setUint32(0, 0x02014b50, true);
      cv.setUint16(4, 20, true);
      cv.setUint16(6, 20, true);
      cv.setUint16(8, 0x0800, true);
      cv.setUint16(10, 0, true);
      cv.setUint16(12, 0, true);
      cv.setUint16(14, 0x21, true);
      cv.setUint32(16, crc, true);
      cv.setUint32(20, size, true);
      cv.setUint32(24, size, true);
      cv.setUint16(28, nameBytes.length, true);
      cv.setUint16(30, 0, true);
      cv.setUint16(32, 0, true);
      cv.setUint16(34, 0, true);
      cv.setUint16(36, 0, true);
      cv.setUint32(38, 0, true);
      cv.setUint32(42, offset, true);
      cd.set(nameBytes, 46);
      central.push(cd);

      offset += header.length + data.length;
    }

    const centralOffset = offset;
    let centralSize = 0;
    for (const cd of central) centralSize += cd.length;

    const eocd = new Uint8Array(22);
    const ev = new DataView(eocd.buffer);
    ev.setUint32(0, 0x06054b50, true);
    ev.setUint16(4, 0, true);
    ev.setUint16(6, 0, true);
    ev.setUint16(8, entries.length, true);
    ev.setUint16(10, entries.length, true);
    ev.setUint32(12, centralSize, true);
    ev.setUint32(16, centralOffset, true);
    ev.setUint16(20, 0, true);

    const total = centralOffset + centralSize + eocd.length;
    const out = new Uint8Array(total);
    let p = 0;
    for (const part of localParts) {
      out.set(part, p);
      p += part.length;
    }
    for (const cd of central) {
      out.set(cd, p);
      p += cd.length;
    }
    out.set(eocd, p);
    return out;
  }

  // =====================================================================
  // 五、纯 JS 自写 EPUB（照抄 build_epub.py 排版）
  // =====================================================================
  const PAGE_CSS =
    "@page { margin: 0; padding: 0; }\n" +
    "html, body { margin: 0; padding: 0; text-align: center; background: #ffffff; }\n" +
    "div.page { margin: 0; padding: 0; }\n" +
    "img.full { display: block; margin: 0 auto; max-width: 100%; height: auto; }\n";

  function pageXhtml(imgHref, title) {
    return (
      '<?xml version="1.0" encoding="utf-8"?>\n' +
      '<html xmlns="http://www.w3.org/1999/xhtml">\n' +
      `<head><title>${escXml(title)}</title>` +
      '<link rel="stylesheet" type="text/css" href="../style/page.css"/></head>\n' +
      '<body><div class="page">' +
      `<img class="full" src="${imgHref}" alt="${escXml(title)}"/>` +
      "</div></body></html>"
    );
  }

  function buildEpub(book, vol, chapters) {
    const title = `${book.title} - ${vol.title}`;
    const ident = `bilimanga-${book.bookNo}-${vol.index}`;

    const entries = [];
    entries.push({ name: "mimetype", data: enc("application/epub+zip") });
    entries.push({
      name: "META-INF/container.xml",
      data: enc(
        '<?xml version="1.0" encoding="utf-8"?>\n' +
          '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n' +
          "  <rootfiles>\n" +
          '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n' +
          "  </rootfiles>\n" +
          "</container>\n"
      ),
    });
    entries.push({ name: "OEBPS/style/page.css", data: enc(PAGE_CSS) });

    const manifestItems = [];
    const spineItems = [];
    const navLis = [];
    const ncxPoints = [];
    let counter = 0;
    let firstImage = null;

    for (const chap of chapters) {
      if (!chap.images || chap.images.length === 0) continue;
      let chapFirstPage = null;
      for (const img of chap.images) {
        counter += 1;
        const imgName = `images/${pad5(counter)}.jpg`;
        const pageName = `text/p${pad5(counter)}.xhtml`;
        entries.push({ name: `OEBPS/${imgName}`, data: img.jpeg });
        entries.push({ name: `OEBPS/${pageName}`, data: enc(pageXhtml(`../${imgName}`, chap.title)) });
        manifestItems.push(`    <item id="img${counter}" href="${imgName}" media-type="image/jpeg"/>`);
        manifestItems.push(
          `    <item id="page${counter}" href="${pageName}" media-type="application/xhtml+xml"/>`
        );
        spineItems.push(`    <itemref idref="page${counter}"/>`);
        if (!chapFirstPage) chapFirstPage = pageName;
        if (!firstImage) firstImage = img;
      }
      if (chapFirstPage) {
        navLis.push(`      <li><a href="${chapFirstPage}">${escXml(chap.title)}</a></li>`);
        const order = ncxPoints.length + 1;
        ncxPoints.push(
          `    <navPoint id="nav${order}" playOrder="${order}">\n` +
            `      <navLabel><text>${escXml(chap.title)}</text></navLabel>\n` +
            `      <content src="${chapFirstPage}"/>\n` +
            `    </navPoint>`
        );
      }
    }

    let coverManifest = "";
    let coverMeta = "";
    if (firstImage) {
      entries.push({ name: "OEBPS/images/cover.jpg", data: firstImage.jpeg });
      coverManifest =
        '    <item id="cover-img" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>\n';
      coverMeta = '    <meta name="cover" content="cover-img"/>\n';
    }

    const subjects = (book.tags || [])
      .map((t) => `    <dc:subject>${escXml(t)}</dc:subject>`)
      .join("\n");

    const opf =
      '<?xml version="1.0" encoding="utf-8"?>\n' +
      '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n' +
      '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n' +
      `    <dc:identifier id="bookid">${escXml(ident)}</dc:identifier>\n` +
      `    <dc:title>${escXml(title)}</dc:title>\n` +
      "    <dc:language>zh</dc:language>\n" +
      `    <dc:creator>${escXml(book.author)}</dc:creator>\n` +
      (book.summary ? `    <dc:description>${escXml(book.summary)}</dc:description>\n` : "") +
      (subjects ? subjects + "\n" : "") +
      `    <meta name="calibre:series" content="${escXml(book.title)}"/>\n` +
      `    <meta name="calibre:series_index" content="${vol.index}"/>\n` +
      coverMeta +
      "  </metadata>\n" +
      "  <manifest>\n" +
      '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n' +
      '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n' +
      '    <item id="css" href="style/page.css" media-type="text/css"/>\n' +
      coverManifest +
      manifestItems.join("\n") +
      "\n  </manifest>\n" +
      '  <spine toc="ncx">\n' +
      spineItems.join("\n") +
      "\n  </spine>\n" +
      "</package>\n";
    entries.push({ name: "OEBPS/content.opf", data: enc(opf) });

    const nav =
      '<?xml version="1.0" encoding="utf-8"?>\n' +
      '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n' +
      `<head><title>${escXml(title)}</title></head>\n` +
      "<body>\n" +
      '  <nav epub:type="toc" id="toc">\n' +
      "    <h1>目录</h1>\n    <ol>\n" +
      navLis.join("\n") +
      "\n    </ol>\n  </nav>\n</body>\n</html>\n";
    entries.push({ name: "OEBPS/nav.xhtml", data: enc(nav) });

    const ncx =
      '<?xml version="1.0" encoding="utf-8"?>\n' +
      '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n' +
      "  <head>\n" +
      `    <meta name="dtb:uid" content="${escXml(ident)}"/>\n` +
      '    <meta name="dtb:depth" content="1"/>\n' +
      '    <meta name="dtb:totalPageCount" content="0"/>\n' +
      '    <meta name="dtb:maxPageNumber" content="0"/>\n' +
      "  </head>\n" +
      `  <docTitle><text>${escXml(title)}</text></docTitle>\n` +
      "  <navMap>\n" +
      ncxPoints.join("\n") +
      "\n  </navMap>\n</ncx>\n";
    entries.push({ name: "OEBPS/toc.ncx", data: enc(ncx) });

    return buildZip(entries);
  }

  // =====================================================================
  // 六、纯 JS 自写 PDF（照抄 build_pdf.py，DCTDecode 无损嵌 JPEG）
  // =====================================================================
  function pdfNum(n) {
    let s = n.toFixed(2).replace(/\.?0+$/, "");
    return s === "" ? "0" : s;
  }

  function buildPdf(images) {
    if (!images || images.length === 0) throw new Error("没有可用于生成 PDF 的图片");
    const chunks = [];
    let length = 0;
    const offsets = [];
    const push = (data) => {
      const bytes = typeof data === "string" ? enc(data) : data;
      chunks.push(bytes);
      length += bytes.length;
    };
    const startObj = (num) => {
      offsets[num] = length;
      push(`${num} 0 obj\n`);
    };
    const endObj = () => push("endobj\n");

    push("%PDF-1.7\n");
    push(new Uint8Array([0x25, 0xe2, 0xe3, 0xcf, 0xd3, 0x0a]));

    const n = images.length;
    const pageNums = [];
    for (let i = 0; i < n; i++) pageNums.push(3 + i * 3);

    startObj(1);
    push("<< /Type /Catalog /Pages 2 0 R >>\n");
    endObj();

    startObj(2);
    push(`<< /Type /Pages /Kids [${pageNums.map((p) => `${p} 0 R`).join(" ")}] /Count ${n} >>\n`);
    endObj();

    for (let i = 0; i < n; i++) {
      const img = images[i];
      const pageNum = 3 + i * 3;
      const contentNum = pageNum + 1;
      const imgNum = pageNum + 2;
      const wS = pdfNum((img.width * 72) / 96);
      const hS = pdfNum((img.height * 72) / 96);

      startObj(pageNum);
      push(
        `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${wS} ${hS}] ` +
          `/Resources << /XObject << /Im0 ${imgNum} 0 R >> >> ` +
          `/Contents ${contentNum} 0 R >>\n`
      );
      endObj();

      const content = `q\n${wS} 0 0 ${hS} 0 0 cm\n/Im0 Do\nQ\n`;
      const contentBytes = enc(content);
      startObj(contentNum);
      push(`<< /Length ${contentBytes.length} >>\nstream\n`);
      push(contentBytes);
      push("\nendstream\n");
      endObj();

      startObj(imgNum);
      push(
        `<< /Type /XObject /Subtype /Image /Width ${img.width} /Height ${img.height} ` +
          `/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode ` +
          `/Length ${img.jpeg.length} >>\nstream\n`
      );
      push(img.jpeg);
      push("\nendstream\n");
      endObj();
    }

    const totalObjects = 2 + n * 3;
    const xrefOffset = length;
    push(`xref\n0 ${totalObjects + 1}\n`);
    push("0000000000 65535 f \n");
    for (let i = 1; i <= totalObjects; i++) {
      push(`${String(offsets[i] || 0).padStart(10, "0")} 00000 n \n`);
    }
    push(`trailer\n<< /Size ${totalObjects + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`);

    const out = new Uint8Array(length);
    let p = 0;
    for (const c of chunks) {
      out.set(c, p);
      p += c.length;
    }
    return out;
  }

  // =====================================================================
  // 六・五、百度网盘上传（用你浏览器里已登录的百度 cookie，走非官方网页/PCS 接口）
  //   分片上传需按 4MB 块算 md5，而浏览器 crypto.subtle 不支持 md5，故内置 md5。
  //   ⚠️ 非官方接口、违反百度网盘服务条款，百度改版/风控可能随时失效；仅供把自己
  //   下载的内容备份到自己的网盘。
  // =====================================================================
  const BAIDU_BLOCK = 4 * 1024 * 1024;
  const BAIDU_BASE_KEY = "bmd_baidu_base";
  let saveTarget = "local";        // local | baidu，由面板选择

  function baiduBase() {
    try {
      return typeof GM_getValue === "function"
        ? GM_getValue(BAIDU_BASE_KEY, "/bilidownloader") : "/bilidownloader";
    } catch (e) { return "/bilidownloader"; }
  }
  function setBaiduBase(v) {
    try { if (typeof GM_setValue === "function") GM_setValue(BAIDU_BASE_KEY, v); } catch (e) {}
  }
  function baiduRemotePath(book, filename) {
    const cat = book.kind === "novel" ? "小说" : "漫画";
    let base = (baiduBase() || "/bilidownloader").replace(/\/+$/, "");
    if (!base.startsWith("/")) base = "/" + base;
    return `${base}/${cat}/${safeName(book.title)}/${filename}`;
  }

  function gmReq(opts) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest(Object.assign({
        timeout: 180000,
        onload: (r) => resolve(r),
        onerror: () => reject(new Error("网络请求失败")),
        ontimeout: () => reject(new Error("请求超时")),
      }, opts));
    });
  }
  function formBody(obj) {
    return Object.keys(obj)
      .map((k) => encodeURIComponent(k) + "=" + encodeURIComponent(obj[k])).join("&");
  }

  // 校验百度登录态并取昵称 + bdstoken；未登录/失败返回 null。
  async function baiduVerify() {
    try {
      const url = "https://pan.baidu.com/api/gettemplatevariable?clienttype=0&app_id=250528&fields="
        + encodeURIComponent('["username","bdstoken"]');
      const r = JSON.parse((await gmReq({ method: "GET", url })).responseText);
      if (r.errno === 0 && r.result) {
        return { nickname: r.result.username || "百度用户", bdstoken: r.result.bdstoken };
      }
    } catch (e) {}
    return null;
  }

  // 内置 md5：输入 Uint8Array，输出小写 hex（已与 python hashlib 对拍一致）。
  function md5bytes(bytes) {
    function add32(a, b) { return (a + b) & 0xffffffff; }
    function cmn(q, a, b, x, s, t) {
      a = add32(add32(a, q), add32(x, t));
      return add32((a << s) | (a >>> (32 - s)), b);
    }
    function ff(a,b,c,d,x,s,t){ return cmn((b & c) | (~b & d), a, b, x, s, t); }
    function gg(a,b,c,d,x,s,t){ return cmn((b & d) | (c & ~d), a, b, x, s, t); }
    function hh(a,b,c,d,x,s,t){ return cmn(b ^ c ^ d, a, b, x, s, t); }
    function ii(a,b,c,d,x,s,t){ return cmn(c ^ (b | ~d), a, b, x, s, t); }
    function blk(bb, off) {
      const m = [];
      for (let i = 0; i < 16; i++) {
        m[i] = bb[off+i*4] + (bb[off+i*4+1]<<8) + (bb[off+i*4+2]<<16) + (bb[off+i*4+3]<<24);
      }
      return m;
    }
    function cycle(x, k) {
      let a=x[0], b=x[1], c=x[2], d=x[3];
      a=ff(a,b,c,d,k[0],7,-680876936); d=ff(d,a,b,c,k[1],12,-389564586); c=ff(c,d,a,b,k[2],17,606105819); b=ff(b,c,d,a,k[3],22,-1044525330);
      a=ff(a,b,c,d,k[4],7,-176418897); d=ff(d,a,b,c,k[5],12,1200080426); c=ff(c,d,a,b,k[6],17,-1473231341); b=ff(b,c,d,a,k[7],22,-45705983);
      a=ff(a,b,c,d,k[8],7,1770035416); d=ff(d,a,b,c,k[9],12,-1958414417); c=ff(c,d,a,b,k[10],17,-42063); b=ff(b,c,d,a,k[11],22,-1990404162);
      a=ff(a,b,c,d,k[12],7,1804603682); d=ff(d,a,b,c,k[13],12,-40341101); c=ff(c,d,a,b,k[14],17,-1502002290); b=ff(b,c,d,a,k[15],22,1236535329);
      a=gg(a,b,c,d,k[1],5,-165796510); d=gg(d,a,b,c,k[6],9,-1069501632); c=gg(c,d,a,b,k[11],14,643717713); b=gg(b,c,d,a,k[0],20,-373897302);
      a=gg(a,b,c,d,k[5],5,-701558691); d=gg(d,a,b,c,k[10],9,38016083); c=gg(c,d,a,b,k[15],14,-660478335); b=gg(b,c,d,a,k[4],20,-405537848);
      a=gg(a,b,c,d,k[9],5,568446438); d=gg(d,a,b,c,k[14],9,-1019803690); c=gg(c,d,a,b,k[3],14,-187363961); b=gg(b,c,d,a,k[8],20,1163531501);
      a=gg(a,b,c,d,k[13],5,-1444681467); d=gg(d,a,b,c,k[2],9,-51403784); c=gg(c,d,a,b,k[7],14,1735328473); b=gg(b,c,d,a,k[12],20,-1926607734);
      a=hh(a,b,c,d,k[5],4,-378558); d=hh(d,a,b,c,k[8],11,-2022574463); c=hh(c,d,a,b,k[11],16,1839030562); b=hh(b,c,d,a,k[14],23,-35309556);
      a=hh(a,b,c,d,k[1],4,-1530992060); d=hh(d,a,b,c,k[4],11,1272893353); c=hh(c,d,a,b,k[7],16,-155497632); b=hh(b,c,d,a,k[10],23,-1094730640);
      a=hh(a,b,c,d,k[13],4,681279174); d=hh(d,a,b,c,k[0],11,-358537222); c=hh(c,d,a,b,k[3],16,-722521979); b=hh(b,c,d,a,k[6],23,76029189);
      a=hh(a,b,c,d,k[9],4,-640364487); d=hh(d,a,b,c,k[12],11,-421815835); c=hh(c,d,a,b,k[15],16,530742520); b=hh(b,c,d,a,k[2],23,-995338651);
      a=ii(a,b,c,d,k[0],6,-198630844); d=ii(d,a,b,c,k[7],10,1126891415); c=ii(c,d,a,b,k[14],15,-1416354905); b=ii(b,c,d,a,k[5],21,-57434055);
      a=ii(a,b,c,d,k[12],6,1700485571); d=ii(d,a,b,c,k[3],10,-1894986606); c=ii(c,d,a,b,k[10],15,-1051523); b=ii(b,c,d,a,k[1],21,-2054922799);
      a=ii(a,b,c,d,k[8],6,1873313359); d=ii(d,a,b,c,k[15],10,-30611744); c=ii(c,d,a,b,k[6],15,-1560198380); b=ii(b,c,d,a,k[13],21,1309151649);
      a=ii(a,b,c,d,k[4],6,-145523070); d=ii(d,a,b,c,k[11],10,-1120210379); c=ii(c,d,a,b,k[2],15,718787259); b=ii(b,c,d,a,k[9],21,-343485551);
      x[0]=add32(a,x[0]); x[1]=add32(b,x[1]); x[2]=add32(c,x[2]); x[3]=add32(d,x[3]);
    }
    const n = bytes.length;
    const state = [1732584193, -271733879, -1732584194, 271733878];
    let i;
    for (i = 64; i <= n; i += 64) cycle(state, blk(bytes, i - 64));
    const rem = n % 64;
    const tail = new Uint8Array(rem < 56 ? 64 : 128);
    for (let j = 0; j < rem; j++) tail[j] = bytes[n - rem + j];
    tail[rem] = 0x80;
    const bits = n * 8;
    const lp = tail.length - 8;
    tail[lp] = bits & 0xff; tail[lp+1] = (bits>>>8)&0xff; tail[lp+2] = (bits>>>16)&0xff; tail[lp+3] = (bits>>>24)&0xff;
    const hi = Math.floor(n / 0x20000000);
    tail[lp+4] = hi & 0xff; tail[lp+5] = (hi>>>8)&0xff; tail[lp+6] = (hi>>>16)&0xff; tail[lp+7] = (hi>>>24)&0xff;
    for (i = 0; i < tail.length; i += 64) cycle(state, blk(tail, i));
    const hex = "0123456789abcdef";
    let out = "";
    for (let j = 0; j < 4; j++) {
      const v = state[j];
      for (let k = 0; k < 4; k++) {
        const byte = (v >>> (k * 8)) & 0xff;
        out += hex[(byte >> 4) & 0xf] + hex[byte & 0xf];
      }
    }
    return out;
  }

  // 分片上传成品到网盘 remotePath（precreate→superfile2 逐块→create 合并）。
  async function baiduUpload(bytes, remotePath, prog) {
    const info = await baiduVerify();
    if (!info) throw new Error("百度网盘未登录：请先在浏览器登录 pan.baidu.com 再上传");
    const size = bytes.length;
    const nblocks = Math.max(1, Math.ceil(size / BAIDU_BLOCK));
    const blockList = [];
    for (let i = 0; i < nblocks; i++) {
      blockList.push(md5bytes(bytes.subarray(i * BAIDU_BLOCK, Math.min(size, (i + 1) * BAIDU_BLOCK))));
    }
    const common = "channel=chunlei&web=1&app_id=250528&clienttype=0&bdstoken=" + encodeURIComponent(info.bdstoken);

    const pre = JSON.parse((await gmReq({
      method: "POST", url: "https://pan.baidu.com/api/precreate?" + common,
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      data: formBody({ path: remotePath, size, isdir: 0, autoinit: 1, rtype: 3, block_list: JSON.stringify(blockList) }),
    })).responseText);
    if (pre.errno !== 0) throw new Error("precreate 失败 errno=" + pre.errno);
    const uploadid = pre.uploadid || "";
    let toUpload = Array.isArray(pre.block_list) ? pre.block_list : blockList.map((_, i) => i);
    dlog("网盘 precreate OK", remotePath, "总块=" + nblocks, "待传=" + toUpload.length);

    for (let k = 0; k < toUpload.length; k++) {
      const seq = toUpload[k];
      const chunk = bytes.subarray(seq * BAIDU_BLOCK, Math.min(size, (seq + 1) * BAIDU_BLOCK));
      const fd = new FormData();
      fd.append("file", new Blob([chunk], { type: "application/octet-stream" }), "blob");
      const url = "https://c.pcs.baidu.com/rest/2.0/pcs/superfile2?method=upload&app_id=250528"
        + "&channel=chunlei&clienttype=0&web=1&path=" + encodeURIComponent(remotePath)
        + "&uploadid=" + encodeURIComponent(uploadid) + "&partseq=" + seq;
      dlog("网盘 传块", seq, "(" + (k + 1) + "/" + toUpload.length + ")");
      const jr = JSON.parse((await gmReq({ method: "POST", url, data: fd })).responseText);
      if (!jr.md5) { dlog("网盘 传块失败", seq, jr); throw new Error("分块 " + seq + " 上传失败"); }
      prog.setRatio((k + 1) / toUpload.length);
      prog.setState(`上传网盘 ${k + 1}/${toUpload.length} 块`);
    }

    const cr = JSON.parse((await gmReq({
      method: "POST", url: "https://pan.baidu.com/api/create?" + common,
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      data: formBody({ path: remotePath, size, isdir: 0, block_list: JSON.stringify(blockList), uploadid, rtype: 3 }),
    })).responseText);
    if (cr.errno !== 0) throw new Error("create 合并失败 errno=" + cr.errno);
    return remotePath;
  }

  // 统一保存出口：按面板选择存本地或传网盘。
  async function dispatchSave(book, bytes, filename, ext, prog) {
    if (saveTarget === "baidu") {
      const remote = baiduRemotePath(book, filename);
      dlog("保存 → 百度网盘", remote, "字节=" + bytes.length);
      prog.setState("上传网盘…");
      await baiduUpload(bytes, remote, prog);
      dlog("保存 网盘完成", remote);
      prog.setState("已上传网盘 ☁");
    } else {
      dlog("保存 → 本地", filename, "字节=" + bytes.length);
      saveFile(bytes, filename, ext);
    }
  }

  // =====================================================================
  // 七、保存文件
  // =====================================================================
  function anchorDownload(url, filename) {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function saveFile(bytes, filename, ext) {
    const type = ext === "pdf" ? "application/pdf" : "application/epub+zip";
    const blob = new Blob([bytes], { type });
    const url = URL.createObjectURL(blob);
    if (typeof GM_download === "function") {
      try {
        GM_download({
          url,
          name: filename,
          saveAs: false,
          onerror: () => anchorDownload(url, filename),
          ontimeout: () => anchorDownload(url, filename),
        });
      } catch (e) {
        anchorDownload(url, filename);
      }
    } else {
      anchorDownload(url, filename);
    }
    setTimeout(() => URL.revokeObjectURL(url), 120000);
  }

  // =====================================================================
  // 八、并发池 + 逐卷处理
  // =====================================================================
  const IMAGE_CONCURRENCY = 4;

  async function runPool(items, limit, worker) {
    let idx = 0;
    const runners = [];
    const n = Math.min(limit, items.length);
    for (let k = 0; k < n; k++) {
      runners.push(
        (async () => {
          while (idx < items.length) {
            const i = idx++;
            await worker(items[i]);
          }
        })()
      );
    }
    await Promise.all(runners);
  }

  async function processVolume(book, vol, fmt, prog) {
    prog.setState("解析话列表…");
    // 并发解析各话的图片地址（限流 4，兼顾速度与不触发 Cloudflare 限速）；
    // 按原顺序写回，保证阅读顺序不乱。
    let chapterData = new Array(vol.chapters.length);

    // 释放已解码图片占用的内存（每张是较大的 JPEG Uint8Array）。无论正常完成、
    // 报错还是中途中断，都在 finally 里调用，避免内存随着多卷连下不断累积。
    const release = () => {
      if (!chapterData) return;
      for (const c of chapterData) {
        if (c && c.images) c.images.length = 0;
      }
      chapterData = null;
    };

    try {
      await runPool(
        vol.chapters.map((chap, i) => ({ chap, i })),
        4,
        async ({ chap, i }) => {
          const urls = await fetchChapterImages(chap.url, book.base);
          chapterData[i] = { title: chap.title, urls, images: new Array(urls.length).fill(null) };
        }
      );
      const total = chapterData.reduce((s, c) => s + c.urls.length, 0);
      if (total === 0) {
        prog.error("未解析到图片");
        return;
      }

      prog.setState(`下载中 0/${total}`);
      const tasks = [];
      chapterData.forEach((c, ci) => c.urls.forEach((u, ii) => tasks.push({ ci, ii, url: u })));

      dlog("▶ 开始卷", vol.index, vol.title, "图片总数=" + total);
      let done = 0;
      const fetchOne = async (t) => {
        // 单张最多试 3 次（网络抖动/429 常是暂时性）；命中限流先等全局冷却。
        // 每次取图套 40s 总超时，卡死的请求会被放弃并重试，不拖死并发池。
        for (let attempt = 0; attempt < 3; attempt++) {
          await gate.waitCooldown();
          dlog("图片 取", t.ci + ":" + t.ii, "第" + (attempt + 1) + "次", t.url);
          try {
            chapterData[t.ci].images[t.ii] = await withTimeout(fetchImageAsJpeg(t.url), 40000, "下载图片");
            dlog("图片 OK", t.ci + ":" + t.ii);
            return;
          } catch (e) {
            dlog("图片 失败", t.ci + ":" + t.ii, "第" + (attempt + 1) + "次", e && e.message);
            if (attempt < 2) await sleep(500 * (attempt + 1));
          }
        }
        dlog("图片 放弃", t.ci + ":" + t.ii, t.url);
      };
      await runPool(tasks, IMAGE_CONCURRENCY, async (t) => {
        await fetchOne(t);
        done += 1;
        prog.setRatio(done / total);
        prog.setState(`下载中 ${done}/${total}`);
      });

      // 补漏轮：仍缺失的再单线程重试一遍，尽量补齐（同命令行版的“最终补齐”）
      const missing = tasks.filter((t) => !chapterData[t.ci].images[t.ii]);
      if (missing.length) {
        dlog("补漏", missing.length, "张");
        prog.setState(`补漏 ${missing.length} 张…`);
        for (const t of missing) await fetchOne(t);
      }

      const chapters = chapterData.map((c) => ({ title: c.title, images: c.images.filter(Boolean) }));
      if (!chapters.some((c) => c.images.length > 0)) {
        prog.error("图片全部下载失败");
        return;
      }

      prog.setState("打包中…");
      let bytes;
      let ext;
      if (fmt === "pdf") {
        const flat = [];
        chapters.forEach((c) => c.images.forEach((im) => flat.push(im)));
        bytes = buildPdf(flat);
        ext = "pdf";
      } else {
        bytes = buildEpub(book, vol, chapters);
        ext = "epub";
      }

      // 打包字节已生成，解码图片可以立刻释放，降低多卷连下时的内存峰值
      chapters.length = 0;
      release();
      dlog("打包完成", ext, "字节=" + bytes.length, "去向=" + saveTarget);
      await dispatchSave(book, bytes, safeName(`${book.title} - ${vol.title}`) + "." + ext, ext, prog);
      dlog("✔ 卷完成", vol.index, vol.title);
      bytes = null;
      prog.done();
    } finally {
      release();
    }
  }

  // =====================================================================
  // 九、UI（悬浮按钮 + 面板）
  // =====================================================================
  GM_addStyle(`
    #bmd-fab{position:fixed;right:0;top:40%;z-index:2147483000;background:#2f6fed;color:#fff;
      border:none;border-radius:10px 0 0 10px;padding:14px 8px;font-size:13px;cursor:pointer;
      writing-mode:vertical-rl;letter-spacing:2px;line-height:1.2;
      box-shadow:-2px 2px 12px rgba(0,0,0,.22);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}
    #bmd-fab:hover{background:#245bd0;}
    #bmd-panel{position:fixed;right:20px;top:60px;z-index:2147483000;width:420px;max-width:calc(100vw - 40px);
      max-height:80vh;overflow:auto;background:#fff;color:#1f2328;border:1px solid #e3e6ea;border-radius:12px;
      box-shadow:0 10px 40px rgba(0,0,0,.25);padding:16px;
      font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6;}
    #bmd-panel.bmd-hidden{display:none;}
    #bmd-panel h3{margin:0 0 10px;font-size:16px;}
    #bmd-panel .bmd-close{position:absolute;right:12px;top:10px;cursor:pointer;color:#888;border:none;background:none;font-size:18px;}
    #bmd-panel input[type=text]{width:100%;padding:8px 10px;border:1px solid #e3e6ea;border-radius:8px;box-sizing:border-box;}
    #bmd-panel .bmd-btn{border:1px solid #e3e6ea;background:#fff;border-radius:8px;padding:6px 12px;cursor:pointer;font-size:13px;}
    #bmd-panel .bmd-btn:hover{background:#f0f2f5;}
    #bmd-panel .bmd-btn.primary{background:#2f6fed;border-color:#2f6fed;color:#fff;}
    #bmd-panel .bmd-btn.primary:hover{background:#245bd0;}
    #bmd-panel .bmd-btn:disabled{opacity:.5;cursor:not-allowed;}
    #bmd-meta{margin:6px 0;color:#444;}
    #bmd-meta b{color:#1f2328;}
    .bmd-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0;}
    #bmd-expr{flex:1;min-width:120px;}
    #bmd-list{max-height:240px;overflow:auto;border:1px solid #eee;border-radius:8px;padding:6px 8px;margin:8px 0;}
    #bmd-list label{display:block;padding:3px 0;cursor:pointer;}
    #bmd-list .idx{color:#888;display:inline-block;min-width:28px;}
    #bmd-list .cnt{color:#999;font-size:12px;}
    #bmd-progress{display:flex;flex-direction:column;gap:8px;margin-top:8px;}
    .bmd-prog{border:1px solid #e3e6ea;border-radius:8px;padding:8px 10px;}
    .bmd-prog .ph{display:flex;justify-content:space-between;margin-bottom:5px;}
    .bmd-prog .st{color:#888;font-size:12px;}
    .bmd-prog .st.done{color:#1a9c5b;}
    .bmd-prog .st.err{color:#c0392b;}
    .bmd-bar{height:7px;background:#eceff3;border-radius:5px;overflow:hidden;}
    .bmd-bar>i{display:block;height:100%;width:0;background:#2f6fed;transition:width .2s;}
    #bmd-hint{font-size:13px;color:#c0392b;white-space:pre-wrap;margin-top:6px;}
  `);

  let panel = null;
  let currentBook = null;
  let bookNo = "";
  let siteKind = "manga";   // manga | novel

  function buildPanel() {
    panel = document.createElement("div");
    panel.id = "bmd-panel";
    panel.className = "bmd-hidden";
    panel.innerHTML = `
      <button class="bmd-close" title="关闭">×</button>
      <h3>下载本书</h3>
      <div id="bmd-meta">正在读取本书目录…</div>
      <div class="bmd-row">
        <button class="bmd-btn" id="bmd-all">全选</button>
        <button class="bmd-btn" id="bmd-none">清空</button>
        <input type="text" id="bmd-expr" placeholder="表达式 1-9,15,20-25" />
        <button class="bmd-btn" id="bmd-apply">应用</button>
      </div>
      <div id="bmd-list"></div>
      <div class="bmd-row">
        <label><input type="radio" name="bmd-fmt" value="epub" checked> EPUB</label>
        <label><input type="radio" name="bmd-fmt" value="pdf"> PDF</label>
        <button class="bmd-btn primary" id="bmd-start" style="margin-left:auto">开始下载</button>
      </div>
      <div class="bmd-row">
        <span style="color:#888">保存到：</span>
        <label><input type="radio" name="bmd-dest" value="local" checked> 💾 本地</label>
        <label><input type="radio" name="bmd-dest" value="baidu"> ☁ 百度网盘</label>
        <span id="bmd-baidu-status" style="font-size:12px;color:#888"></span>
      </div>
      <div class="bmd-row bmd-hidden" id="bmd-baidu-row">
        <span style="color:#888;white-space:nowrap">网盘根路径</span>
        <input type="text" id="bmd-baidu-base" placeholder="/bilidownloader" />
      </div>
      <div class="bmd-row">
        <label title="记录每次请求/每章/每张图，卡住时也能定位；日志写到 log.txt">
          <input type="checkbox" id="bmd-debug"> 🐞 调试日志（出问题时勾选，生成 log.txt）
        </label>
      </div>
      <div id="bmd-hint"></div>
      <div id="bmd-progress"></div>
    `;
    document.body.appendChild(panel);

    panel.querySelector(".bmd-close").addEventListener("click", () => togglePanel(false));
    panel.querySelector("#bmd-all").addEventListener("click", () => setAll(true));
    panel.querySelector("#bmd-none").addEventListener("click", () => setAll(false));
    panel.querySelector("#bmd-apply").addEventListener("click", onApplyExpr);
    panel.querySelector("#bmd-start").addEventListener("click", onStart);
    panel.querySelectorAll('input[name="bmd-dest"]').forEach(
      (r) => r.addEventListener("change", onDestChange));
    const baseInput = panel.querySelector("#bmd-baidu-base");
    baseInput.value = baiduBase();
    baseInput.addEventListener("change", () => {
      let v = (baseInput.value || "").trim() || "/bilidownloader";
      if (!v.startsWith("/")) v = "/" + v;
      baseInput.value = v;
      setBaiduBase(v);
    });
  }

  // 选择「百度网盘」时校验登录态：已登录显示昵称并展开根路径；未登录回退本地。
  async function onDestChange() {
    const dest = panel.querySelector('input[name="bmd-dest"]:checked').value;
    const baseRow = panel.querySelector("#bmd-baidu-row");
    const statusEl = panel.querySelector("#bmd-baidu-status");
    if (dest !== "baidu") {
      baseRow.classList.add("bmd-hidden");
      statusEl.textContent = "";
      return;
    }
    statusEl.style.color = "#888";
    statusEl.textContent = "检测登录…";
    const info = await baiduVerify();
    if (info) {
      statusEl.style.color = "#2e7d32";
      statusEl.textContent = "已登录：" + info.nickname;
      baseRow.classList.remove("bmd-hidden");
    } else {
      statusEl.style.color = "#c62828";
      statusEl.textContent = "未登录，请先在浏览器登录 pan.baidu.com 后再试";
      panel.querySelector('input[name="bmd-dest"][value="local"]').checked = true;
      baseRow.classList.add("bmd-hidden");
    }
  }

  function setHint(msg) {
    panel.querySelector("#bmd-hint").textContent = msg || "";
  }

  function checkboxes() {
    return Array.from(panel.querySelectorAll("#bmd-list input[type=checkbox]"));
  }
  function setAll(v) {
    checkboxes().forEach((cb) => (cb.checked = v));
  }
  function onApplyExpr() {
    if (!currentBook) return;
    try {
      const picked = new Set(parseSelection(panel.querySelector("#bmd-expr").value, currentBook.volumes.length));
      checkboxes().forEach((cb) => (cb.checked = picked.has(parseInt(cb.dataset.index, 10))));
      setHint("");
    } catch (e) {
      setHint(e.message);
    }
  }

  function renderBook(book) {
    const unit = book.kind === "novel" ? "卷" : "章";
    const cunit = book.kind === "novel" ? "章" : "话";
    panel.querySelector("#bmd-meta").innerHTML =
      `<b>${escHtml(book.title)}</b><br>作者：${escHtml(book.author)}　共 ${book.volumes.length} ${unit}`;
    const list = panel.querySelector("#bmd-list");
    list.innerHTML = "";
    book.volumes.forEach((vol) => {
      const label = document.createElement("label");
      label.innerHTML =
        `<input type="checkbox" data-index="${vol.index}" checked> ` +
        `<span class="idx">${vol.index}.</span>${escHtml(vol.title)} ` +
        `<span class="cnt">（${vol.chapters.length} ${cunit}）</span>`;
      list.appendChild(label);
    });
  }

  async function loadCurrentBook() {
    setHint("");
    currentBook = null;
    panel.querySelector("#bmd-meta").textContent = "正在读取本书目录…";
    try {
      const book = siteKind === "novel"
        ? await fetchBookNovel(bookNo, location.origin)
        : await fetchBook(bookNo, location.origin);
      currentBook = book;
      renderBook(book);
      if (book.volumes.length === 0) setHint("未解析到任何卷/章，请确认页面是否正确。");
    } catch (e) {
      panel.querySelector("#bmd-meta").textContent = "";
      setHint("读取失败：" + e.message);
    }
  }

  function createProgress(title) {
    const el = document.createElement("div");
    el.className = "bmd-prog";
    el.innerHTML =
      `<div class="ph"><span>${escHtml(title)}</span><span class="st">等待中</span></div>` +
      `<div class="bmd-bar"><i></i></div>`;
    panel.querySelector("#bmd-progress").appendChild(el);
    const st = el.querySelector(".st");
    const bar = el.querySelector(".bmd-bar > i");
    return {
      setState: (t) => (st.textContent = t),
      setRatio: (r) => (bar.style.width = Math.round(r * 100) + "%"),
      done: () => {
        st.textContent = "完成";
        st.className = "st done";
        bar.style.width = "100%";
      },
      error: (t) => {
        st.textContent = t;
        st.className = "st err";
      },
    };
  }

  async function onStart() {
    if (!currentBook) {
      setHint("请先解析。");
      return;
    }
    const selected = checkboxes()
      .filter((cb) => cb.checked)
      .map((cb) => parseInt(cb.dataset.index, 10));
    if (selected.length === 0) {
      setHint("请至少选择一章。");
      return;
    }
    const fmt = panel.querySelector('input[name="bmd-fmt"]:checked').value;
    saveTarget = panel.querySelector('input[name="bmd-dest"]:checked').value;
    if (saveTarget === "baidu") {
      let v = (panel.querySelector("#bmd-baidu-base").value || "").trim() || "/bilidownloader";
      if (!v.startsWith("/")) v = "/" + v;
      setBaiduBase(v);
    }
    // 调试日志：必须在这里（用户点击手势内、任何 await 之前）发起文件选择框。
    DBG = panel.querySelector("#bmd-debug").checked;
    if (DBG) {
      await startDebugLog();
      dlog("书", currentBook.title, currentBook.kind, "选中卷", selected.join(","), "格式=" + fmt, "去向=" + saveTarget);
    }
    setHint(DBG ? (logHandle ? "调试中：日志实时写入你选的 log.txt" : "调试中：日志将在结束时下载为 log.txt") : "");
    panel.querySelector("#bmd-start").disabled = true;
    panel.querySelector("#bmd-progress").innerHTML = "";

    const book = currentBook;
    const volumes = book.volumes.filter((v) => selected.includes(v.index));
    try {
      for (const vol of volumes) {
        const prog = createProgress(`${vol.index}. ${vol.title}`);
        try {
          if (book.kind === "novel") await processVolumeNovel(book, vol, prog);
          else await processVolume(book, vol, fmt, prog);
        } catch (e) {
          dlog("✖ 卷异常", vol.index, vol.title, e && e.message);
          prog.error("失败：" + e.message);
          if (String(e.message).includes("Cloudflare")) {
            setHint(e.message);
            break;
          }
        }
      }
    } finally {
      panel.querySelector("#bmd-start").disabled = false;
      if (DBG) { await stopDebugLog(); DBG = false; }
    }
  }

  function togglePanel(show) {
    if (!panel) buildPanel();
    const visible = show === undefined ? panel.classList.contains("bmd-hidden") : show;
    panel.classList.toggle("bmd-hidden", !visible);
    if (visible && !currentBook) loadCurrentBook(); // 首次展开：直接加载当前这本书
  }

  // 从当前 URL 提取书号，并判断是漫画还是轻小说。
  function detectBookNo() {
    const nv = /\/novel\/(\d+)/.exec(location.pathname);
    if (nv) { siteKind = "novel"; return nv[1]; }
    const m = /\/(?:detail|read)\/(\d+)/.exec(location.pathname);
    if (m) { siteKind = "manga"; return m[1]; }
    return "";
  }

  function init() {
    bookNo = detectBookNo();
    if (!bookNo) return; // 非漫画/轻小说页面不显示按钮
    const fab = document.createElement("button");
    fab.id = "bmd-fab";
    fab.textContent = "⬇ 下载本书";
    fab.addEventListener("click", () => togglePanel());
    document.body.appendChild(fab);
  }

  if (document.body) init();
  else window.addEventListener("DOMContentLoaded", init);
})();
