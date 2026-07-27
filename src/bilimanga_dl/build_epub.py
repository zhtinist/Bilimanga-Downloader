"""用 ebooklib 把一个已下载的卷打包成 EPUB。

排版要点：
- 每张插图独占一页，图片按比例居中铺满，不裁切、不失真。
- 首页封面，随后按话（chapter）分节，生成规范目录（TOC）。
- 写入 Calibre 系列元数据（series/series_index），便于阅读器归类（优化 6）。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ebooklib import epub

from .downloader import DownloadedVolume, safe_name
from .imageutil import image_size
from .logutil import get_logger
from .models import Book

log = get_logger("epub")


_PAGE_CSS = """
@page { margin: 0; padding: 0; }
html, body { margin: 0; padding: 0; text-align: center; background: #ffffff; }
div.page { margin: 0; padding: 0; }
img.full { display: block; margin: 0 auto; max-width: 100%; height: auto; }
"""


def _page_xhtml(img_href: str, w: int, h: int, alt: str) -> str:
    # 不要带 <?xml?> 声明：ebooklib 会在写出时自行添加，且当 content 为
    # Python str 时带声明会导致 lxml 解析失败、body 变空。
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        f"<head><title>{alt}</title>"
        '<link rel="stylesheet" type="text/css" href="style/page.css"/></head>\n'
        '<body><div class="page">'
        f'<img class="full" src="{img_href}" alt="{alt}" />'
        "</div></body></html>"
    )


def build_epub(book: Book, dv: DownloadedVolume, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ebook = epub.EpubBook()

    ident = f"bilimanga-{book.book_no}-{dv.volume.index}"
    ebook.set_identifier(ident)
    title = f"{book.title} - {dv.volume.title}"
    ebook.set_title(title)
    ebook.set_language("zh")
    ebook.add_author(book.author)
    if book.summary:
        ebook.add_metadata("DC", "description", book.summary)
    for tag in book.tags:
        ebook.add_metadata("DC", "subject", tag)
    # Calibre 系列元数据
    ebook.add_metadata(
        None, "meta", "", {"name": "calibre:series", "content": book.title}
    )
    ebook.add_metadata(
        None, "meta", "",
        {"name": "calibre:series_index", "content": str(dv.volume.index)},
    )

    # 样式
    css = epub.EpubItem(
        uid="page_css",
        file_name="style/page.css",
        media_type="text/css",
        content=_PAGE_CSS,
    )
    ebook.add_item(css)

    spine: List = ["nav"]
    toc: List = []
    img_counter = 0

    # 封面：手动添加图片 + 自建封面页（set_cover 自动页在 ebooklib 0.20
    # 的分页扫描中 body 为空会报错，故 create_page=False）。
    if dv.cover and dv.cover.exists():
        ebook.set_cover("cover.jpg", dv.cover.read_bytes(), create_page=False)
        try:
            cw, ch = image_size(dv.cover)
        except Exception:
            cw, ch = 800, 1200
        cover_page = epub.EpubHtml(title="封面", file_name="text/cover.xhtml", lang="zh")
        cover_page.content = _page_xhtml("../cover.jpg", cw, ch, "封面")
        cover_page.add_item(css)
        ebook.add_item(cover_page)
        spine.append(cover_page)

    for dchap in dv.chapters:
        if not dchap.images:
            continue
        chap_first_page = None
        for img_path in dchap.images:
            if not img_path.exists():
                continue
            img_counter += 1
            img_name = f"images/{img_counter:05d}.jpg"
            img_item = epub.EpubItem(
                uid=f"img{img_counter}",
                file_name=img_name,
                media_type="image/jpeg",
                content=img_path.read_bytes(),
            )
            ebook.add_item(img_item)

            try:
                w, h = image_size(img_path)
            except Exception:
                w, h = 800, 1200
            page = epub.EpubHtml(
                title=dchap.title,
                file_name=f"text/p{img_counter:05d}.xhtml",
                lang="zh",
            )
            page.content = _page_xhtml(f"../{img_name}", w, h, dchap.title)
            page.add_item(css)
            ebook.add_item(page)
            spine.append(page)
            if chap_first_page is None:
                chap_first_page = page
        if chap_first_page is not None:
            toc.append(epub.Link(chap_first_page.file_name, dchap.title, chap_first_page.id))

    ebook.toc = tuple(toc)
    ebook.add_item(epub.EpubNcx())
    ebook.add_item(epub.EpubNav())
    ebook.spine = spine

    out_path = out_dir / (safe_name(title) + ".epub")
    log.info("生成 EPUB: %s (%d 页图片)", out_path, img_counter)
    epub.write_epub(str(out_path), ebook)
    return out_path
