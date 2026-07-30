"""解析 bilimanga.net 页面。

页面结构（参考站点实际 DOM 及现有开源项目）：
- 详情页  ``/detail/{no}.html``
    * 标题   ``h1.book-title``
    * 作者   ``span.authorname``
    * 简介   ``section#bookSummary``
    * 标签   ``span.tag-small-group a``
    * 封面   ``.book-cover img`` / ``meta[property=og:image]``
- 目录页  ``/read/{no}/catalog``
    * 每卷（章）  ``div.catalog-volume``  内含 ``h3`` 标题
    * 每话        ``li.chapter-li`` 内含 ``a[href]`` 与 ``span`` 标题
- 阅读页
    * 插图 ``img.imagecontent``，真实地址在 ``data-src``（懒加载）
"""

from __future__ import annotations

import re
from typing import List, Optional

from bs4 import BeautifulSoup

from .logutil import get_logger
from .models import Book, Chapter, Volume
from .net import Net

log = get_logger("scraper")


# 兼容三种输入:
#   详情页  https://www.bilimanga.net/detail/703.html
#   目录页  https://www.bilimanga.net/read/703/catalog
#   书号    703
_BOOK_NO_RE = re.compile(r"(?:detail|read)/(\d+)")


def parse_book_no(url_or_no: str) -> str:
    """从详情页/目录页 URL 或裸书号中提取书号。"""
    s = (url_or_no or "").strip()
    if s.isdigit():
        return s
    m = _BOOK_NO_RE.search(s)
    if m:
        return m.group(1)
    # 兜底:输入里只含一个数字段(如 detail/703.html 的变体)
    nums = re.findall(r"\d+", s)
    if len(nums) == 1:
        return nums[0]
    raise ValueError(
        f"无法识别书号：{url_or_no!r}\n"
        "请输入以下任一形式：\n"
        "  详情页 https://www.bilimanga.net/detail/703.html\n"
        "  目录页 https://www.bilimanga.net/read/703/catalog\n"
        "  书号   703"
    )


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _text(node) -> str:
    return node.get_text(strip=True) if node else ""


class Scraper:
    def __init__(self, net: Net):
        self.net = net

    def _fetch_detail(self, book_no: str):
        """抓详情页，返回 (base_url, html)。单一站点，失败即明确报错。"""
        base = self.net.base_url or self.net.config.site
        try:
            return base, self.net.get_text(
                f"{base}/detail/{book_no}.html", referer=base)
        except Exception as exc:
            log.warning("抓取详情页失败 %s: %s", base, exc)
            raise RuntimeError(
                f"无法打开详情页 {base}/detail/{book_no}.html\n"
                f"原因：{exc}\n"
                "请确认：①书号/链接正确；②站点地址（设置里可改）可访问；"
                "③已装 Chrome/Edge 且能过 Cloudflare。"
            ) from exc

    def fetch_book(self, book_no: str) -> Book:
        # 直接抓详情页（省掉一次首页导航），成功即固定 base。
        base, html = self._fetch_detail(book_no)
        self.net.base_url = base
        soup = _soup(html)

        title = _text(soup.find("h1", class_="book-title")) or _text(soup.find("h1"))
        author = _text(soup.find("span", class_="authorname"))
        if not author:
            # 兜底：部分模板作者在 .authorname a 或 meta
            a = soup.select_one(".author a, .authorname a")
            author = _text(a)

        summary_node = soup.find("section", id="bookSummary") or soup.find(
            id="bookSummary"
        )
        summary = _text(summary_node)

        cover_url = self._extract_cover(soup, base)

        tags: List[str] = []
        tag_group = soup.find("span", class_="tag-small-group")
        if tag_group:
            tags = [_text(a) for a in tag_group.find_all("a") if _text(a)]

        book = Book(
            book_no=book_no,
            title=title or f"未知书名({book_no})",
            author=author or "未知作者",
            cover_url=cover_url,
            summary=summary,
            tags=tags,
            base_url=base,
        )
        log.debug("详情页解析: 标题=%r 作者=%r 标签=%s 封面=%r",
                  book.title, book.author, book.tags, book.cover_url)
        book.volumes = self._fetch_catalog(book_no, base)
        log.info("共解析到 %d 章", len(book.volumes))
        return book

    def _extract_cover(self, soup: BeautifulSoup, base: str) -> str:
        for sel in (".book-cover img", ".book-img img", "img.book-cover"):
            node = soup.select_one(sel)
            if node:
                src = node.get("data-src") or node.get("src")
                if src:
                    return self._abs(src, base)
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return self._abs(og["content"], base)
        return ""

    def _fetch_catalog(self, book_no: str, base: str) -> List[Volume]:
        catalog_url = f"{base}/read/{book_no}/catalog"
        html = self.net.get_text(catalog_url, referer=f"{base}/detail/{book_no}.html")
        soup = _soup(html)

        volumes: List[Volume] = []
        vol_nodes = soup.find_all("div", class_="catalog-volume")
        for idx, vol_node in enumerate(vol_nodes, start=1):
            title = _text(vol_node.find("h3")) or f"第{idx}章"
            chapters: List[Chapter] = []
            for li in vol_node.find_all("li", class_="chapter-li"):
                a = li.find("a", href=True)
                if not a:
                    continue
                href = a["href"]
                # 无效/占位链接（未解锁等）跳过
                if "javascript" in href or href.strip() in ("#", ""):
                    continue
                name = _text(a.find("span")) or _text(a)
                chapters.append(Chapter(title=name, url=self._abs(href, base)))
            log.debug("第 %d 章 %r 含 %d 话", idx, title, len(chapters))
            volumes.append(Volume(index=idx, title=title, chapters=chapters))
        return volumes

    def fetch_chapter_images(self, chapter: Chapter, base: str) -> List[str]:
        """返回一话中所有插图的真实 URL（顺序即阅读顺序）。

        阅读页图片由 JS 懒加载注入，读太早会拿到 0 张，故等待 ``imagecontent``
        出现；若仍为空则重试几次（换新导航），避免整话漏下。
        """
        for attempt in range(3):
            html = self.net.get_text(chapter.url, referer=base, wait_for="imagecontent")
            soup = _soup(html)
            urls: List[str] = []
            for img in soup.find_all("img", class_="imagecontent"):
                src = img.get("data-src") or img.get("src")
                if src and not src.startswith("data:"):
                    urls.append(self._abs(src, base))
            if urls:
                log.debug("话 %r 解析到 %d 张图片", chapter.title, len(urls))
                return urls
            log.debug("话 %r 第 %d 次解析到 0 张，重试", chapter.title, attempt + 1)
        log.warning("话 %r 多次尝试仍解析到 0 张图片", chapter.title)
        return []

    @staticmethod
    def _abs(url: str, base: str) -> str:
        url = url.strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return base + url
        return base + "/" + url
