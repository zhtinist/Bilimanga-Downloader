// ==UserScript==
// @name         Bilimanga 漫画/轻小说下载器
// @namespace    https://github.com/zhtinist/Bilimanga-Downloader
// @version      2.1.0
// @description  在 bilimanga 漫画 / 哔哩轻小说(bilinovel) 页面里一键把整卷下载成 EPUB / PDF。
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
// @connect      *
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
      await gate.acquire();
      let status = 0, html = "";
      try {
        const resp = await fetchWithTimeout(url, { credentials: "include" }, 20000);
        status = resp.status;
        html = await resp.text();
      } catch (e) {
        lastErr = e;
      } finally {
        gate.release();
      }
      if (status === 200) {
        if (CF_CHALLENGE.some((m) => html.includes(m))) {
          throw new Error("页面被 Cloudflare 拦截，请在当前标签手动通过人机验证后重试。");
        }
        if (html && !isRateLimited(status, html)) {
          gate.reward();
          return html;
        }
      }
      // 429 / 占位限流 / 空响应：全体冷却后重试；其它 HTTP 错误指数退避。
      if (isRateLimited(status, html) || !html) {
        gate.penalize(Math.min(15 + attempt * 8, 45));
      } else {
        await sleep(Math.min(1500 * Math.pow(2, attempt), 10000));
      }
    }
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
    while (true) {
      const html = await fetchText(url);
      text += pageTextNovel(html);
      const nxt = chapUrl.replace(".html", "_" + (page + 1) + ".html").slice(base.length);
      if (html.includes(nxt)) { page += 1; url = base + nxt; }
      else break;
    }
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
    // 正文串行抓取（并发受 gate 控制；正文页对并发敏感，串行 + 429 冷却最稳）。
    await runPool(jobs, 1, async ({ c, i }) => {
      try { bodies[i] = await fetchChapterTextNovel(c.url, book.base); }
      catch (e) { bodies[i] = ""; }
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
    const images = {};
    await runPool(imgUrls, IMAGE_CONCURRENCY, async (u) => {
      // 边下边补：单张就地重试 3 次，命中限流先等全局冷却。
      for (let attempt = 0; attempt < 3; attempt++) {
        await gate.waitCooldown();
        try { images[imgMap[u]] = await fetchImageAsJpeg(u); return; }
        catch (e) { if (attempt < 2) await sleep(500 * (attempt + 1)); }
      }
    });

    prog.setState("打包中…");
    const bytes = buildEpubNovel(book, vol, chapters, images);
    saveFile(bytes, safeName(`${book.title} - ${vol.title}`) + ".epub", "epub");
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

      let done = 0;
      const fetchOne = async (t) => {
        // 单张最多试 3 次（网络抖动/429 常是暂时性）；命中限流先等全局冷却。
        for (let attempt = 0; attempt < 3; attempt++) {
          await gate.waitCooldown();
          try {
            chapterData[t.ci].images[t.ii] = await fetchImageAsJpeg(t.url);
            return;
          } catch (e) {
            if (attempt < 2) await sleep(500 * (attempt + 1));
          }
        }
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
      saveFile(bytes, safeName(`${book.title} - ${vol.title}`) + "." + ext, ext);
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
      <div id="bmd-hint"></div>
      <div id="bmd-progress"></div>
    `;
    document.body.appendChild(panel);

    panel.querySelector(".bmd-close").addEventListener("click", () => togglePanel(false));
    panel.querySelector("#bmd-all").addEventListener("click", () => setAll(true));
    panel.querySelector("#bmd-none").addEventListener("click", () => setAll(false));
    panel.querySelector("#bmd-apply").addEventListener("click", onApplyExpr);
    panel.querySelector("#bmd-start").addEventListener("click", onStart);
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
    setHint("");
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
          prog.error("失败：" + e.message);
          if (String(e.message).includes("Cloudflare")) {
            setHint(e.message);
            break;
          }
        }
      }
    } finally {
      panel.querySelector("#bmd-start").disabled = false;
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
