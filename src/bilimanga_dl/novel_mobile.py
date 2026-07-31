"""轻小说（哔哩轻小说）**无浏览器**下载引擎 → EPUB。

借鉴 montaro2017/bili_novel_packer 的关键发现：手机站 ``www.bilinovel.com``
用【安卓手机 UA + Cookie night=0】纯 HTTP 即可访问，**无需真实浏览器过
Cloudflare**——比桌面站(linovelib) + DrissionPage 快一个数量级、几乎不吃内存。

本引擎：
- requests.Session（手机 UA）拉详情/目录/正文，固定最小间隔 + 退避重试；
- 正文取 ``#acontent`` 内的 ``<p>`` 与插图，多页自动翻页（``_2.html`` 规律）；
- 章节链接被 JS 隐藏时从相邻章反推兜底；
- 复用 :mod:`novel` 的 EPUB 构建 / 图片编号 / 转码逻辑。

站点若改版/失效，调用方可回退到 :class:`novel.NovelDownloader`（浏览器路线）。
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from curl_cffi import requests as cffi
from curl_cffi.requests import exceptions as cffi_exc
from bs4 import BeautifulSoup

from .logutil import get_logger
from .models import Book, Chapter, Volume
from .novel import (NovelRateLimited, NovelDownloader, _build_epub, _to_jpeg,
                    _text)
from .ratelimit import RateGate as _RateGate

log = get_logger("novel_mobile")

DOMAIN = "https://www.bilinovel.com"
IMPERSONATE = "chrome"  # curl_cffi 用真实 Chrome 指纹过 CF；bilinovel 手机站对任何 UA 都吐手机版 DOM
# #acontent 里非正文的装饰/诱饵节点：整段丢弃，只留 <p> 与 <img>
_DROP_TAGS = {"div", "ins", "figure", "fig", "script", "style", ".tp", ".bd"}
# 限流/被拦占位页特征
_BLOCK_MARKERS = ("Just a moment", "Attention Required", "Access denied",
                  "需要足夠的權限", "需要足够的权限", "審核未通過", "审核未通过",
                  "沒有可閱讀的章節內容", "没有可阅读的章节内容")


def is_mobile_novel_url(text: str) -> bool:
    t = (text or "").lower()
    return "bilinovel.com" in t or "linovelib" in t or "/novel/" in t


@dataclass
class _VolumeContent:
    """一卷抓取完成后的中间产物：正文（含图片占位）+ 图片编号表 + 是否被限流。"""
    chapters: List[Tuple[str, str]] = field(default_factory=list)  # (章名, 正文html)
    img_map: dict = field(default_factory=dict)                    # 图片URL -> 编号
    any_gated: bool = False

    @property
    def has_text(self) -> bool:
        return any(b.strip() for _, b in self.chapters)


class MobileNovelDownloader:
    """无浏览器手机站引擎。接口对齐 :class:`novel.NovelDownloader`。"""

    def __init__(self, num_thread: int = 4, min_interval: float = 0.7,
                 concurrency: int = 1, timeout: int = 30, proxy: str = ""):
        # 正文抓取默认**串行**（concurrency=1）：手机站对并发很敏感，多连接会被
        # tarpit/429 拖到卡死。图片下载仍用 num_thread 并发（走 CDN，不受此限）。
        self.num_thread = max(1, num_thread)
        self.timeout = timeout
        # (连接超时, 读超时)：避免被限流时单个请求长时间挂起。
        self._to = (8, min(timeout, 20))
        # curl_cffi 会话：真实 Chrome 指纹，长期复用（连接池 + cookie 跨请求保留）。
        self.session = cffi.Session(impersonate=IMPERSONATE)
        self.session.headers.update({
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": "night=0",
            "Referer": DOMAIN,
        })
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self._gate = _RateGate(min_interval, concurrency)

    # ---- 底层抓取（限速 + 429 冷却重试）----
    def _get(self, url: str, tries: int = 8) -> str:
        last = ""
        for attempt in range(tries):
            self._gate.acquire()
            try:
                r = self.session.get(url, timeout=self._to, allow_redirects=True)
                text = r.text
                status = r.status_code
            except cffi_exc.RequestException as exc:
                last = str(exc)
                text = ""
                status = None
            finally:
                self._gate.release()
            if status == 200 and not _blocked(text):
                self._gate.reward()
                return text
            # 429 / 占位页 / 网络错误：命中限流就让全体冷却，避免线程各自空转拖死。
            rate_limited = (status == 429) or _blocked(text) or (status is None)
            if rate_limited:
                cooldown = min(15 + attempt * 8, 45)  # 站点突发后需要一段冷却
                self._gate.penalize(cooldown)
                log.warning("[mobile] 触发限流(%s)，全体冷却 %ds 后重试(%d/%d): %s",
                            status or last, cooldown, attempt + 1, tries, url)
            else:
                backoff = min(1.5 * (2 ** attempt), 15)
                log.warning("[mobile] 抓取失败(HTTP %s)，%.1fs 后重试(%d/%d): %s",
                            status, backoff, attempt + 1, tries, url)
                time.sleep(backoff)
        raise NovelRateLimited(
            f"手机站多次抓取失败（限流未恢复）：{url}")

    # ---- 元数据 + 目录 ----
    def fetch_book(self, book_no: str) -> Book:
        d = BeautifulSoup(self._get(f"{DOMAIN}/novel/{book_no}.html"), "html.parser")

        def _meta(prop):
            m = d.find("meta", {"property": prop})
            return m["content"].strip() if m and m.get("content") else ""

        title = (_text(d.select_one(".book-title")) or _meta("og:novel:book_name")
                 or f"未知({book_no})")
        author = _text(d.select_one(".book-rand-a span")) or _meta("og:novel:author") or "未知作者"
        publisher = _text(d.select_one(".tag-small.orange")) or _meta("og:novel:category")
        summary = _text(d.select_one("#bookSummary content")) or _meta("og:description")
        tags = [_text(e) for e in d.select(".book-cell .book-meta span em") if _text(e)]
        cover_el = d.select_one(".book-layout img") or d.select_one(".book-img img")
        cover = _abs(cover_el.get("src")) if cover_el and cover_el.get("src") else ""

        book = Book(book_no=book_no, title=title, author=author, cover_url=cover,
                    summary=summary, tags=tags, base_url=DOMAIN,
                    kind="novel", publisher=publisher)
        book.volumes = self._fetch_catalog(book_no)
        log.info("轻小说《%s》共 %d 卷（手机站引擎）", title, len(book.volumes))
        return book

    def _fetch_catalog(self, book_no: str) -> List[Volume]:
        c = BeautifulSoup(self._get(f"{DOMAIN}/novel/{book_no}/catalog"), "html.parser")
        volumes: List[Volume] = []
        cur: Optional[Volume] = None
        idx = 0
        for li in c.select(".volume-chapters > li"):
            classes = li.get("class") or []
            if "chapter-bar" in classes:
                idx += 1
                cur = Volume(index=idx, title=li.get_text(strip=True) or f"第{idx}卷",
                             chapters=[])
                volumes.append(cur)
                continue
            if "volume-cover" in classes:
                continue
            if "jsChapter" in classes:
                if cur is None:  # 无卷标题：整本作一卷
                    idx += 1
                    cur = Volume(index=idx, title="", chapters=[])
                    volumes.append(cur)
                a = li.find("a")
                if not a:
                    continue
                href = a.get("href") or ""
                url = "" if "javascript" in href else _abs(href)
                cur.chapters.append(Chapter(title=li.get_text(strip=True) or _text(a),
                                            url=url))
        # 兜底：卷标题里含书名前缀时不强行去除（EPUB 命名会自处理）
        return [v for v in volumes if v.chapters]

    # ---- 正文（多页翻页 + 清洗 + 图片占位）----
    def _chap_text(self, chap: Chapter) -> Tuple[str, bool]:
        if not chap.url:
            return "", False
        text = ""
        base = chap.url
        url = chap.url
        page = 1
        gated = False
        while True:
            html = self._get(url)
            if any(m in html for m in _BLOCK_MARKERS):
                gated = True
            text += self._page_text(html)
            nxt_path = base.replace(".html", f"_{page + 1}.html")[len(DOMAIN):]
            if nxt_path in html:
                page += 1
                url = DOMAIN + nxt_path
            else:
                break
        return text, gated

    def _page_text(self, html: str) -> str:
        d = BeautifulSoup(html, "html.parser")
        content = d.select_one("#acontent") or d.select_one(".bcontent")
        if content is None:
            return ""
        # 丢弃诱饵/装饰节点：类名形如 [a-z]\d{4} 的元素，以及非 p/img 的直接子节点
        for el in content.select("[class]"):
            cls = " ".join(el.get("class") or [])
            if re.fullmatch(r"[a-z]\d{4}", cls or ""):
                el.decompose()
        for el in list(content.find_all(recursive=False)):
            if el.name not in ("p", "img"):
                el.decompose()
        # 段落：保留有文字的 <p>；插图 <img> 用占位标记（复用 novel 的编号逻辑）
        out: List[str] = []
        for node in content.find_all(["p", "img"], recursive=True):
            if node.name == "img":
                src = node.get("data-src") or node.get("src") or ""
                if not src or "<" in src:
                    continue
                out.append(f'<img class="__nv__" src="{_abs(src)}"/>')
            else:
                txt = node.get_text().strip()
                if txt:
                    out.append(f"<p>{_esc(txt)}</p>")
        return "\n".join(out)

    # ---- 下载一卷 → EPUB（三段流水线：抓正文 → 下插图 → 打包）----
    def download_volume(self, book: Book, volume: Volume, out_dir: Path,
                        on_phase: Optional[Callable] = None,
                        on_total: Optional[Callable] = None,
                        on_image: Optional[Callable] = None,
                        on_concurrency: Optional[Callable] = None) -> Optional[Path]:
        vidx = volume.index
        if on_phase:
            on_phase(vidx, "download")

        content = self._fetch_bodies(volume, on_total, on_image)
        if not content.has_text and len(content.img_map) <= 1:
            if content.any_gated:
                raise NovelRateLimited(
                    "手机站暂时限制了访问（占位页，通常是请求过于频繁）。"
                    "已自动退避重试仍未成功，请稍等几分钟再重试。")
            if on_phase:
                on_phase(vidx, "empty")
            return None

        if on_phase:
            on_phase(vidx, "images")
        images = self._download_images(content.img_map, vidx, on_image)

        if on_phase:
            on_phase(vidx, "package")
        return _build_epub(book, volume, content.chapters, images, out_dir)

    # ---- 阶段 1：抓正文（并发受 _gate 控制，默认串行以尊重限流）----
    def _fetch_bodies(self, volume: Volume,
                      on_total: Optional[Callable],
                      on_image: Optional[Callable]) -> "_VolumeContent":
        vidx = volume.index
        jobs = [(i, ch) for i, ch in enumerate(volume.chapters) if ch.url]
        names = [ch.title for _, ch in jobs]
        bodies: dict = {}
        gated_flags: dict = {}
        # 进度条按“章节数”推进（正文抓取是耗时大头），避免一直停在 0/None。
        if on_total:
            on_total(vidx, max(len(jobs), 1))

        def _fetch(job):
            i, ch = job
            try:
                bodies[i], gated_flags[i] = self._chap_text(ch)
            except Exception as exc:  # noqa: BLE001
                log.warning("章节 %r 抓取失败：%s", ch.title, exc)
                bodies[i], gated_flags[i] = "", True
            if on_image:      # 每抓完一章推进一格
                on_image(vidx)

        with ThreadPoolExecutor(max_workers=max(self.num_thread, 1)) as pool:
            list(pool.map(_fetch, jobs))

        ordered = [bodies.get(i, "") for i, _ in jobs]
        ordered, img_map = NovelDownloader._assign_images(ordered)
        return _VolumeContent(chapters=list(zip(names, ordered)),
                              img_map=img_map,
                              any_gated=any(gated_flags.values()))

    # ---- 阶段 2：下插图并转码 JPEG（异源 CDN，与限流无关，可并发）----
    def _download_images(self, img_map: dict, vidx: int,
                         on_image: Optional[Callable]) -> dict:
        images: dict = {}
        if not img_map:
            return images
        lock = threading.Lock()

        def _dl(item, tries=3):
            url, idx = item
            data = None
            # 就地补图（边下边检测）：失败即退避重试，和其它图并发进行。
            for attempt in range(tries):
                try:
                    r = self.session.get(url, timeout=self._to)
                    if r.status_code == 200 and r.content:
                        data = r.content
                        break
                except cffi_exc.RequestException:
                    pass
                if attempt < tries - 1:
                    time.sleep(0.4 * (attempt + 1))
            if data is None:
                log.warning("插图 %s 多次下载失败", idx)
            else:
                try:
                    with lock:
                        images[idx] = _to_jpeg(data)
                except Exception as exc:  # noqa: BLE001
                    log.warning("插图 %s 转码失败：%s", idx, exc)
            if on_image:
                on_image(vidx)

        with ThreadPoolExecutor(max_workers=self.num_thread) as pool:
            list(pool.map(_dl, img_map.items()))
        return images

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------- 小工具 ----------------
def _abs(src: str) -> str:
    """把图片/链接地址规范为绝对 URL。"""
    if not src:
        return ""
    src = src.strip()
    src = src.replace("https://https://", "https://")
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return DOMAIN + src
    return src


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _blocked(text: str) -> bool:
    return bool(text) and any(m in text for m in _BLOCK_MARKERS[:3])
