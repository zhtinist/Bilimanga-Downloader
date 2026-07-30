"""轻小说（linovelib / 哔哩轻小说）下载 → EPUB。

移植并小幅优化自 ShqWW/bilinovel-download：
- 目录/元数据解析（书名、作者、简介、文库、标签、封面）；
- 逐章逐页抓正文（多页自动翻页），并做 **PUA 字体反混淆**（见 rubbish_secret_map）；
- 诱饵段落清理交给浏览器层 :meth:`Net.get_novel_text`（用计算样式识别）；
- 插图按顺序抽出、去重，第一张作封面；
- 自写 zip 生成文本型 EPUB（文字 + 内嵌插图）。

与漫画版共用 :class:`Net`（真实浏览器过 Cloudflare）。轻小说站点固定为 linovelib。
"""

from __future__ import annotations

import io
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from bs4 import BeautifulSoup
from PIL import Image

from .logutil import get_logger
from .models import Book, Chapter, Volume
from .net import Net
from .rubbish_secret_map import rubbish_secret_map

log = get_logger("novel")

NOVEL_SITE = "https://www.linovelib.com"
IMG_HOST = "https://img3.readpai.com"
COLOR_CHAP_NAME = "插图"
# linovelib 请求过快时的占位提示特征（其实是限流，不是需要登录；退避重试即可）
_LOCK_MARKERS = ("沒有可閱讀的章節內容", "没有可阅读的章节内容", "需要足夠的權限",
                 "需要足够的权限", "審核未通過", "审核未通过")


class NovelRateLimited(RuntimeError):
    """linovelib 暂时限制访问（占位页）。"""

_CHINESE_PUNCT = set(
    "，。！？、；：“”‘’（）《》〈〉【】『』〖〗…—～＋－＝×÷·「」　 "
)

_NOVEL_NO_RE = re.compile(r"/novel/(\d+)")


def parse_novel_no(url_or_no: str) -> str:
    s = (url_or_no or "").strip()
    if s.isdigit():
        return s
    m = _NOVEL_NO_RE.search(s)
    if m:
        return m.group(1)
    nums = re.findall(r"\d+", s)
    if len(nums) == 1:
        return nums[0]
    raise ValueError(f"无法识别轻小说书号：{url_or_no!r}")


def is_novel_url(text: str) -> bool:
    t = (text or "").lower()
    return "linovelib" in t or "/novel/" in t


# ---------------- 反混淆 ----------------
def _deobfuscate_last_p(text_html: str) -> str:
    """linovelib 把真正文塞进最后一个非空 <p>，字符是自定义字体的 PUA 码，需映射回真字。"""
    soup = BeautifulSoup(text_html, "html.parser")
    ps = soup.find_all("p")
    if not ps:
        return str(soup)
    target = None
    for i in range(len(ps) - 1, max(len(ps) - 11, -1), -1):
        if ps[i].get_text().strip():
            target = ps[i]
            break
    if target is None:
        return str(soup)
    out = []
    for ch in target.get_text():
        mapped = rubbish_secret_map.get(ch)
        if mapped is not None:
            out.append(mapped)
        elif ch in _CHINESE_PUNCT:
            out.append(ch)
    target.string = "".join(out)
    return str(soup)


# ---------------- HTML 模板 ----------------
def _container_xml() -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n</container>')


def _cover_xhtml(w: int, h: int) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title></head>\n'
            '<body style="margin:0;padding:0;text-align:center;">\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" height="100%" preserveAspectRatio="xMidYMid meet" '
            f'version="1.1" viewBox="0 0 {w} {h}" width="100%" xmlns:xlink="http://www.w3.org/1999/xlink">\n'
            '<image width="%d" height="%d" xlink:href="../Images/00.jpg"/></svg>\n'
            '</body></html>' % (w, h))


def _text_xhtml(chap_name: str, body: str) -> str:
    return (f'<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>\n'
            f'<title>{chap_name}</title>\n'
            '<style>body{line-height:1.75;} p{text-indent:2em;margin:.6em 0;} '
            'img{display:block;margin:1em auto;max-width:100%;height:auto;}</style>\n'
            f'</head><body>\n<h1>{chap_name}</h1>\n{body}\n</body></html>')


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------- 下载器 ----------------
class NovelDownloader:
    def __init__(self, net: Net, num_thread: int = 4):
        self.net = net
        self.num_thread = max(1, num_thread)

    # ---- 元数据 + 目录 ----
    def fetch_book(self, book_no: str) -> Book:
        main = self.net.get_text(f"{NOVEL_SITE}/novel/{book_no}.html", referer=NOVEL_SITE)
        bf = BeautifulSoup(main, "html.parser")

        def _meta(prop):
            m = bf.find("meta", {"property": prop})
            return m["content"].strip() if m and m.get("content") else ""

        title = _meta("og:novel:book_name") or _text(bf.find("h1")) or f"未知({book_no})"
        author = _meta("og:novel:author") or "未知作者"
        publisher = ""
        label = bf.find("div", class_="book-label")
        if label:
            a = label.find("a", class_="label")
            publisher = _text(a)
        tags = []
        if label:
            span = label.find("span")
            if span:
                tags = [_text(a) for a in span.find_all("a") if _text(a)]
        brief = ""
        dec = bf.find("div", class_="book-dec Jbook-dec") or bf.find("div", class_="book-dec")
        if dec:
            ps = dec.find_all("p")
            brief = _text(ps[0]) if ps else _text(dec)
        cover = ""
        img = bf.find("div", class_="book-img fl")
        if img and img.find("img"):
            cover = img.find("img").get("src") or ""

        book = Book(book_no=book_no, title=title, author=author, cover_url=cover,
                    summary=brief, tags=tags, base_url=NOVEL_SITE,
                    kind="novel", publisher=publisher)
        book.volumes = self._fetch_catalog(book_no)
        log.info("轻小说《%s》共 %d 卷", title, len(book.volumes))
        return book

    def _fetch_catalog(self, book_no: str) -> List[Volume]:
        html = self.net.get_text(f"{NOVEL_SITE}/novel/{book_no}/catalog", referer=NOVEL_SITE)
        bf = BeautifulSoup(html, "html.parser")
        volumes: List[Volume] = []
        for idx, vol in enumerate(bf.find_all("div", class_="volume clearfix"), start=1):
            vname = _text(vol.find("h2", class_="v-line")) or f"第{idx}卷"
            chapters: List[Chapter] = []
            for li in vol.find_all("li", class_="col-4"):
                a = li.find("a")
                if not a:
                    continue
                href = a.get("href") or ""
                url = href if href.startswith("http") else NOVEL_SITE + href
                chapters.append(Chapter(title=_text(li) or _text(a), url=url))
            volumes.append(Volume(index=idx, title=vname, chapters=chapters))
        return volumes

    # ---- 正文（多页 + 反混淆 + 抽图）----
    def _page_text(self, html: str, img_map: dict) -> str:
        obfuscated = "woff2" in html
        bf = BeautifulSoup(html, "html.parser")
        content = bf.find("div", {"id": "TextContent"})
        if content is None:
            return ""
        for sel_id in ("show-more-images", "hidden-images"):
            for e in content.find_all(id=sel_id):
                e.decompose()
        for cls in ("google-auto-placed ap_container", "dag"):
            for e in content.find_all(class_=cls):
                e.decompose()
        text_html = re.sub(r"<!--(.*?)-->", "", str(content), flags=re.DOTALL)

        # 抽取插图，重映射为本地 Images/NN.jpg；第一张(00)作封面不写正文
        for tag in re.findall(r"<img\s[^>]*>", text_html):
            m = re.search(r"[a-zA-Z]{3}/(.*?)\.(jpg|png|jpeg)", tag)
            if not m:
                text_html = text_html.replace(tag, "")
                continue
            img_url = f"{IMG_HOST}/{m.group(1)}.{m.group(2)}"
            if img_url not in img_map:
                img_map[img_url] = str(len(img_map)).zfill(2)
            idx = img_map[img_url]
            if idx == "00":
                text_html = text_html.replace(tag, "")
            else:
                text_html = text_html.replace(
                    tag, f'<img alt="{idx}" src="../Images/{idx}.jpg"/>')

        content = BeautifulSoup(text_html, "html.parser").find("div", id="TextContent")
        # 去掉反爬提示元素 <pNNN>
        for m in re.findall(r"<p(\d+)>", str(content)):
            el = content.find(f"p{m}")
            if el:
                el.decompose()
            break
        body = content.decode_contents().strip("\n")
        cut = body.find("————————————以下为告示")
        if cut != -1:
            body = body[:body.rfind("<", 0, cut) if body.rfind("<", 0, cut) != -1 else cut]
        if obfuscated:
            body = _deobfuscate_last_p(body)
        return body

    def _chap_text(self, chap: Chapter, img_map: dict) -> Tuple[str, bool]:
        """返回 (正文 html, 是否疑似被登录/权限拦截)。"""
        text = ""
        url = chap.url
        base_url = chap.url
        page = 1
        gated = False
        while True:
            html = self.net.get_novel_text(url, referer=NOVEL_SITE)
            if any(m in html for m in _LOCK_MARKERS):
                gated = True
            text += self._page_text(html, img_map)
            nxt = base_url.replace(".html", f"_{page + 1}.html")[len(NOVEL_SITE):]
            if nxt in html:
                page += 1
                url = NOVEL_SITE + nxt
            else:
                break
        return text, gated

    def download_volume(self, book: Book, volume: Volume, out_dir: Path,
                        on_phase: Optional[Callable] = None,
                        on_total: Optional[Callable] = None,
                        on_image: Optional[Callable] = None) -> Optional[Path]:
        """下载一卷 → 生成一个 EPUB，返回路径（无内容返回 None）。"""
        vidx = volume.index
        if on_phase:
            on_phase(vidx, "download")
        img_map: dict = {}
        chapters_out: List[Tuple[str, str]] = []  # (chap_name, body_html)
        any_gated = False
        for chap in volume.chapters:
            if "javascript" in chap.url or "cid" in chap.url:
                log.warning("跳过失效章节链接：%s", chap.title)
                continue
            try:
                body, gated = self._chap_text(chap, img_map)
                any_gated = any_gated or gated
            except Exception as exc:  # noqa: BLE001
                log.warning("章节 %r 抓取失败：%s", chap.title, exc)
                body = ""
            chapters_out.append((chap.title, body))
            time.sleep(0.6)  # 章节间稍作停顿，避免触发 linovelib 限流

        has_text = any(b.strip() for _, b in chapters_out)
        if not has_text and len(img_map) <= 1:
            if any_gated:
                raise NovelRateLimited(
                    "linovelib 暂时限制了访问（返回占位页，通常是请求过于频繁）。"
                    "已自动退避重试仍未成功，请**稍等几分钟再重试**；无需登录。")
            if on_phase:
                on_phase(vidx, "empty")
            return None

        # 下载插图（并发）
        if on_total:
            on_total(vidx, max(len(img_map), 1))
        images: dict = {}  # idx -> bytes(jpeg)
        lock = threading.Lock()

        def _dl(item):
            url, idx = item
            data = None
            # 参考项目做法：轻小说图床(readpai)不做防盗链，纯 requests 最快；失败再退回浏览器。
            try:
                r = self.net.session.get(url, headers={"Referer": NOVEL_SITE}, timeout=30)
                if r.status_code == 200 and r.content:
                    data = r.content
            except Exception:  # noqa: BLE001
                data = None
            if data is None:
                try:
                    data = self.net.get_bytes(url, referer=NOVEL_SITE)
                except Exception as exc:  # noqa: BLE001
                    log.warning("插图 %s 下载失败：%s", idx, exc)
            if data is not None:
                try:
                    with lock:
                        images[idx] = _to_jpeg(data)
                except Exception as exc:  # noqa: BLE001
                    log.warning("插图 %s 转码失败：%s", idx, exc)
            if on_image:
                on_image(vidx)

        if img_map:
            with ThreadPoolExecutor(max_workers=self.num_thread) as pool:
                list(pool.map(_dl, img_map.items()))

        if on_phase:
            on_phase(vidx, "package")
        return _build_epub(book, volume, chapters_out, images, out_dir)


def _to_jpeg(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


def _text(node) -> str:
    return node.get_text(strip=True) if node else ""


# ---------------- EPUB 构建 ----------------
def _build_epub(book: Book, volume: Volume, chapters: List[Tuple[str, str]],
                images: dict, out_dir: Path) -> Path:
    from .downloader import safe_name

    out_dir.mkdir(parents=True, exist_ok=True)
    epub_path = out_dir / (safe_name(f"{book.title} - {volume.title}") + ".epub")

    has_cover = "00" in images
    text_items, spine_items, nav_points = [], [], []
    files: List[Tuple[str, bytes]] = []

    # 文本章节
    chap_no = 0
    for name, body in chapters:
        if not body.strip():
            continue
        fn = f"{chap_no:02d}.xhtml"
        files.append((f"OEBPS/Text/{fn}",
                      _text_xhtml(_esc(name), body).encode("utf-8")))
        text_items.append(f'    <item id="x{chap_no:02d}" href="Text/{fn}" '
                          'media-type="application/xhtml+xml"/>')
        spine_items.append(f'    <itemref idref="x{chap_no:02d}"/>')
        nav_points.append((name, fn))
        chap_no += 1

    # 图片
    img_items = []
    for idx, jpeg in sorted(images.items()):
        files.append((f"OEBPS/Images/{idx}.jpg", jpeg))
        img_items.append(f'    <item id="img{idx}" href="Images/{idx}.jpg" media-type="image/jpeg"/>')

    # 封面
    cover_item = cover_spine = ""
    if has_cover:
        try:
            with Image.open(io.BytesIO(images["00"])) as im:
                w, h = im.size
        except Exception:
            w, h = 600, 800
        files.append(("OEBPS/Text/cover.xhtml", _cover_xhtml(w, h).encode("utf-8")))
        cover_item = ('    <item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>\n'
                      '    <item id="cover-img" href="Images/00.jpg" media-type="image/jpeg" properties="cover-image"/>')
        cover_spine = '    <itemref idref="cover"/>'

    subjects = "\n".join(f"    <dc:subject>{_esc(t)}</dc:subject>" for t in book.tags)
    ncx_points = "\n".join(
        f'    <navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>{_esc(nm)}</text></navLabel>'
        f'<content src="Text/{fn}"/></navPoint>'
        for i, (nm, fn) in enumerate(nav_points))
    toc = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head>\n'
           f'<meta name="dtb:uid" content="linovelib-{book.book_no}-{volume.index}"/></head>\n'
           f'<docTitle><text>{_esc(book.title)}-{_esc(volume.title)}</text></docTitle>\n'
           f'<navMap>\n{ncx_points}\n</navMap></ncx>')

    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package version="3.0" unique-identifier="BookId" xmlns="http://www.idpf.org/2007/opf">\n'
           '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'    <dc:identifier id="BookId">linovelib-{book.book_no}-{volume.index}</dc:identifier>\n'
           f'    <dc:title>{_esc(book.title)}-{_esc(volume.title)}</dc:title>\n'
           '    <dc:language>zh-CN</dc:language>\n'
           f'    <dc:creator>{_esc(book.author)}</dc:creator>\n'
           f'    <dc:publisher>{_esc(book.publisher)}</dc:publisher>\n'
           f'    <dc:description>{_esc(book.summary)}</dc:description>\n'
           f'{subjects}\n'
           f'    <meta name="calibre:series" content="{_esc(book.title)}"/>\n'
           f'    <meta name="calibre:series_index" content="{volume.index}"/>\n'
           '  </metadata>\n  <manifest>\n'
           '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
           f'{cover_item}\n' + "\n".join(text_items) + "\n" + "\n".join(img_items) +
           '\n  </manifest>\n  <spine toc="ncx">\n'
           f'{cover_spine}\n' + "\n".join(spine_items) +
           '\n  </spine>\n</package>')

    files.append(("OEBPS/toc.ncx", toc.encode("utf-8")))
    files.append(("OEBPS/content.opf", opf.encode("utf-8")))
    files.append(("META-INF/container.xml", _container_xml().encode("utf-8")))

    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in files:
            zf.writestr(name, data)
    log.info("生成轻小说 EPUB：%s（%d 章 / %d 图）", epub_path.name, chap_no, len(images))
    return epub_path
