"""网络层。

现代 Cloudflare(托管质询 / Turnstile)已让纯 HTTP 库(requests / cloudscraper)
基本失效。参考 ShqWW/bilinovel-download 的做法，本层**以真实浏览器为主引擎**：

1. 用 DrissionPage 驱动本地 Chrome/Edge 加载页面，靠真实浏览器指纹过 Cloudflare，
   拿到 HTML 与 ``cf_clearance`` 等 cookie。
2. 图片下载优先用 requests 复用浏览器的 cookie + UA 并发抓取（快）；
   若被拦，则回退到浏览器页面上下文内用同步 XHR 抓取（稳，串行）。

另外实现：镜像域名自动切换、限速、指数退避重试、代理连不上自动直连。

``config.use_browser=False`` 时退回纯 requests（供本地 mock 测试 / 无浏览器环境）。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import random
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests


def ensure_local_no_proxy() -> None:
    """让本地地址绕过代理。

    否则当系统设了 HTTP(S)_PROXY / ALL_PROXY 时，DrissionPage 连本地 Chrome
    调试端口的 websocket、以及 requests 访问 localhost，都会被塞进代理导致失败
    （表现为 “Connection to remote host was lost” 或 502）。
    """
    locals_ = ["127.0.0.1", "localhost", "::1"]
    for key in ("NO_PROXY", "no_proxy"):
        cur = os.environ.get(key, "")
        parts = [p.strip() for p in cur.split(",") if p.strip()]
        for h in locals_:
            if h not in parts:
                parts.append(h)
        os.environ[key] = ",".join(parts)

from .config import Config
from .imageutil import save_as_jpeg
from .logutil import get_logger
from .ratelimit import RateGate

log = get_logger("net")


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 手机 UA：实测 bilimanga 手机站直连（目录/阅读页/图片 CDN）用它即可，无需浏览器。
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)


def _origin(url: str) -> str:
    p = urlparse(url or "")
    return f"{p.scheme}://{p.netloc}" if p.scheme else ""


def _image_headers(referer: Optional[str]) -> dict:
    """图片 CDN 需要完整的浏览器指纹头，否则 Cloudflare 返回 403（非 JS 质询）。"""
    h = {
        "User-Agent": MOBILE_UA,
        "Accept": "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if referer:
        h["Referer"] = referer
        o = _origin(referer)
        if o:
            h["Origin"] = o
    return h

# 软性质询特征（浏览器会自动解算）。只用 interstitial 独有的串，
# 不用 challenge-platform / __cf_chl（这些是 Cloudflare 的 beacon，正常页也可能带，会误判）。
_CF_CHALLENGE = (
    "Just a moment",
    "Checking your browser before accessing",
    "Enable JavaScript and cookies to continue",
    "Verifying you are human",
)
# 硬封禁特征（等待无用，需换 IP/代理）
_CF_BLOCKED = (
    "Sorry, you have been blocked",
    "Attention Required! | Cloudflare",
)


class CloudflareBlocked(RuntimeError):
    """无法绕过 Cloudflare 时抛出。"""


class BrowserUnavailable(RuntimeError):
    """需要浏览器但无法启动时抛出。"""


def _looks_like_cloudflare(text: str, status: int) -> bool:
    """requests 响应是否疑似被 Cloudflare 拦截。"""
    if status in (403, 503) and text:
        return any(m in text for m in (_CF_CHALLENGE + _CF_BLOCKED))
    return False


def _html_state(html: str) -> str:
    """判断浏览器取到的 HTML 处于何种状态：ok / challenge / blocked。"""
    if not html:
        return "challenge"
    if any(m in html for m in _CF_BLOCKED):
        return "blocked"
    if any(m in html for m in _CF_CHALLENGE):
        return "challenge"
    return "ok"


def _free_port() -> int:
    """取一个空闲本地端口给 Chrome 调试用（避免与已开浏览器/上次实例冲突）。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _detect_browser_path() -> Optional[str]:
    """跨平台探测常见 Chrome / Edge 安装路径。"""
    candidates = []
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif sys.platform.startswith("win"):
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                return found
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("chrome", "google-chrome", "msedge", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


# 扫描阅读页：返回 [图片总数, 已加载数, data-src 列表]
_SCAN_JS = r"""
var g = document.querySelectorAll('img.imagecontent');
var loaded = 0, urls = [];
for (var i = 0; i < g.length; i++) {
  if (g[i].complete && g[i].naturalWidth > 0) loaded++;
  urls.push(g[i].getAttribute('data-src') || g[i].getAttribute('src') || '');
}
return [g.length, loaded, urls];
"""

# 同源限流并发抓取：LIMIT 个 worker 从共享队列取 URL 并发 fetch（模仿浏览器 <img>
# 的 ~6 并发节流，避免一次性 burst 触发 429），一次 run_js 抓完一整话。
# run_js 不支持 list，故用 JSON 字符串传参；标签须已在图片同源上。
_POOL_JS = r"""
const urls = JSON.parse(arguments[0]);
const LIMIT = 5, TIMEOUT = 15000;
return (async () => {
  const out = new Array(urls.length).fill(null);
  let idx = 0;
  async function one(u) {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), TIMEOUT);
    try {
      const r = await fetch(u, {credentials: 'include', signal: c.signal});
      if (!r.ok) return 'ERR' + r.status;
      const b = await r.arrayBuffer();
      const a = new Uint8Array(b);
      let s = ''; const C = 0x8000;
      for (let j = 0; j < a.length; j += C) s += String.fromCharCode.apply(null, a.subarray(j, j + C));
      return btoa(s);
    } catch (e) { return 'ERR:' + e; } finally { clearTimeout(t); }
  }
  async function worker() {
    while (idx < urls.length) { const i = idx++; out[i] = await one(urls[i]); }
  }
  await Promise.all(Array.from({length: LIMIT}, worker));
  return out;
})();
"""

# 在页面上下文内用同步 XHR 抓取二进制并 base64 返回（复用浏览器 cookie + 指纹）
_XHR_JS = r"""
var url = arguments[0];
try {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url, false);
    xhr.overrideMimeType('text/plain; charset=x-user-defined');
    xhr.send(null);
    if (xhr.status !== 200) return 'ERR:' + xhr.status;
    var res = xhr.responseText, out = '';
    for (var i = 0; i < res.length; i++) out += String.fromCharCode(res.charCodeAt(i) & 0xff);
    return btoa(out);
} catch (e) { return 'ERR:' + e; }
"""



class BrowserEngine:
    """DrissionPage 真实浏览器封装。

    - 页面(HTML)用主标签串行加载，靠真实浏览器指纹过 Cloudflare。
    - 图片下载:图片域名(如 i.motiezw.com)对 requests 返回 403(JA3 指纹校验)，
      唯一可行的是**在浏览器里直接导航到图片 URL → 同源 XHR 取字节**。
      为提速，维护一个标签池，多标签并行下载(即多线程)。
    """

    def __init__(self, config: Config):
        self.config = config
        self._browser = None
        self._tab = None                 # 首个标签
        self._lock = threading.RLock()
        self._pool: Optional[queue.Queue] = None  # 标签池（页面与图片共用）
        self._pool_lock = threading.Lock()
        self._tab_count = 0
        self._max_tabs = max(1, config.parallel_chapters)  # 标签上限=并发上限
        self._force_headful = False   # 登录时临时用有头浏览器

    def _ensure(self):
        if self._tab is not None:
            return
        try:
            from DrissionPage import Chromium, ChromiumOptions  # type: ignore
        except Exception as exc:
            raise BrowserUnavailable(
                "需要 DrissionPage 驱动真实浏览器来通过 Cloudflare，但导入失败："
                f"{exc}\n请先 pip install DrissionPage 并安装 Chrome/Edge。"
            )
        browser_path = self.config.browser_path or _detect_browser_path()
        if not browser_path:
            raise BrowserUnavailable(
                "未找到本地 Chrome/Edge 浏览器。请安装 Chrome，或在设置里填 browser_path。"
            )
        co = ChromiumOptions().set_browser_path(browser_path)
        # 固定的独立用户资料目录：持久化登录态与 cf_clearance（下次更快；轻小说登录一次即可）。
        # 注意不能用 auto_port()——它会另配临时资料目录，且与 set_user_data_path 叠加会清掉端口。
        # 因此这里显式指定一个空闲端口 + 固定资料目录。
        try:
            from .config import PROJECT_ROOT
            profile = PROJECT_ROOT / "browser_profile"
            profile.mkdir(parents=True, exist_ok=True)
            co.set_user_data_path(str(profile))
        except Exception as exc:  # noqa: BLE001
            log.debug("设置持久化用户目录失败(忽略): %s", exc)
        co.set_local_port(_free_port())
        headless = self.config.browser_headless and not self._force_headful
        if headless:
            co.headless(True)
        co.set_argument("--no-first-run")
        co.set_argument("--no-default-browser-check")
        # 网络：不做任何特别设置，浏览器沿用它自身/系统的网络环境即可
        # （不 set_proxy、不 --no-proxy-server，实测这样最快且最稳）。
        log.info("正在启动本地浏览器过 Cloudflare（%s，headless=%s）……",
                 browser_path, headless)
        self._browser = Chromium(co)
        self._tab = self._browser.latest_tab

    # ---- 统一标签池（按需懒增长到上限，配合自适应并发） ----
    def _ensure_pool(self):
        if self._pool is not None:
            return
        with self._pool_lock:
            if self._pool is not None:
                return
            self._ensure()
            self._pool = queue.Queue()
            self._pool.put(self._tab)
            self._tab_count = 1
            log.debug("[browser] 标签池初始化(上限 %d)", self._max_tabs)

    def _borrow(self):
        self._ensure_pool()
        with self._pool_lock:
            if not self._pool.empty():
                return self._pool.get_nowait()
            if self._tab_count < self._max_tabs:
                try:
                    tab = self._browser.new_tab()
                    self._tab_count += 1
                    return tab
                except Exception:
                    pass
        return self._pool.get()  # 已达上限：等待归还

    def _release(self, tab):
        if self._pool is not None:
            self._pool.put(tab)

    @staticmethod
    def _origin(u: str) -> str:
        p = urlparse(u or "")
        return f"{p.scheme}://{p.netloc}"

    def get_html(self, url: str, wait_for: Optional[str] = None) -> str:
        tab = self._borrow()
        try:
            log.debug("[browser] GET %s", url)
            tab.get(url)
            deadline = time.time() + self.config.cloudflare_wait
            html = tab.html or ""
            state = _html_state(html)
            while state == "challenge" and time.time() < deadline:
                time.sleep(1.5)
                html = tab.html or ""
                state = _html_state(html)
            if state == "blocked":
                raise CloudflareBlocked(
                    f"该 IP 被 Cloudflare 硬封禁: {url}（请更换住宅代理节点后重试）"
                )
            if state == "challenge":
                raise CloudflareBlocked(
                    f"等待 {self.config.cloudflare_wait}s 仍未通过 Cloudflare: {url}"
                )
            # 等待 JS 注入的目标内容出现（如阅读页的 imagecontent 懒加载图片）
            if wait_for and wait_for not in html:
                wdeadline = time.time() + 15
                while wait_for not in html and time.time() < wdeadline:
                    time.sleep(0.5)
                    html = tab.html or ""
            log.debug("[browser] ← %s state=%s len=%d wait_for=%s hit=%s",
                      url, state, len(html), wait_for, (wait_for in html) if wait_for else "-")
            return html
        finally:
            self._release(tab)

    def get_novel_html(self, url: str) -> str:
        """轻小说阅读页专用：过 CF + 等正文渲染，并按参考项目做“诱饵段落”清理。

        linovelib 会注入一批 ``position:absolute`` 的假 ``<p>``（带 data-* 属性）来污染
        抓取。这些只能靠**真实浏览器的计算样式**识别：找到定位为 absolute 的 p，取其
        data-* 键值，把所有同键值的 p 从 DOM 里删掉；再清掉残留 data-* 属性。
        """
        from bs4 import BeautifulSoup
        # linovelib 请求过快时会返回“需要足夠的權限/審核未通過”这类占位页（其实是限流，
        # 并非需要登录）。这里像参考项目对付 Cloudflare 那样：检测到就退避后重试。
        rate_markers = ("需要足夠的權限", "需要足够的权限", "審核未通過", "审核未通过",
                        "沒有可閱讀的章節內容", "没有可阅读的章节内容")
        tab = self._borrow()
        try:
            html = ""
            for attempt in range(5):
                tab.get(url)
                deadline = time.time() + self.config.cloudflare_wait
                html = tab.html or ""
                state = _html_state(html)
                while state == "challenge" and time.time() < deadline:
                    time.sleep(1.0)
                    html = tab.html or ""
                    state = _html_state(html)
                if state == "blocked":
                    raise CloudflareBlocked(f"该 IP 被 Cloudflare 硬封禁: {url}")
                if state == "challenge":
                    raise CloudflareBlocked(
                        f"等待 {self.config.cloudflare_wait}s 仍未通过 Cloudflare: {url}")
                # 等正文容器渲染 + 给 JS 一点注入时间
                wd = time.time() + 15
                while "TextContent" not in (tab.html or "") and time.time() < wd:
                    time.sleep(0.5)
                time.sleep(2.0)
                html = tab.html or ""
                if any(m in html for m in rate_markers) and attempt < 4:
                    wait = 5 + attempt * 4
                    log.warning("[browser] 疑似被限流(占位页)，%ds 后重试(%d/5): %s", wait, attempt + 1, url)
                    time.sleep(wait)
                    continue
                break
            bf = BeautifulSoup(html, "html.parser")
            # ① 用计算样式找诱饵 p，按 data-* 键值批量删除
            try:
                for p in tab.eles("tag:p"):
                    try:
                        if p.style("position") == "absolute":
                            for k, v in (p.attrs or {}).items():
                                if k.startswith("data-"):
                                    for dp in bf.find_all("p", {k: v}):
                                        dp.decompose()
                    except Exception:
                        continue
            except Exception:
                pass
            # ② 清掉所有残留 data-* 属性
            for p in bf.find_all("p"):
                for k in [k for k in list(p.attrs) if k.startswith("data-")]:
                    del p[k]
            return str(bf)
        finally:
            self._release(tab)

    def open_login(self, url: str) -> None:
        """打开**有头**浏览器到登录页，供用户手动登录（登录态写入持久化资料目录）。"""
        self.close()                 # 关掉当前(可能无头)实例
        self._force_headful = True
        self._ensure_pool()
        tab = self._borrow()
        try:
            tab.get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("打开登录页失败: %s", exc)
        finally:
            self._release(tab)

    def finish_login(self) -> None:
        """结束登录：关掉有头浏览器（Chrome 会把 cookie 落盘到资料目录），恢复无头。"""
        self.close()
        self._force_headful = False

    def prewarm(self, url: Optional[str] = None) -> None:
        """后台预热：启动浏览器 + 预先过一次 Cloudflare，隐藏冷启动耗时。"""
        try:
            self._ensure_pool()
            if url:
                tab = self._borrow()
                try:
                    tab.get(url)
                    deadline = time.time() + self.config.cloudflare_wait
                    while _html_state(tab.html or "") == "challenge" and time.time() < deadline:
                        time.sleep(1.0)
                    log.debug("[browser] 预热完成: %s", url)
                finally:
                    self._release(tab)
        except Exception as exc:
            log.debug("预热失败(忽略): %s", exc)

    def get_image_bytes(self, url: str) -> bytes:
        # 内部只做 2 次轻量尝试；速率限制(429)交给上层自适应并发控制处理，
        # 以便把“拥塞信号”快速反馈给控制器（而不是在这里死磕重试掩盖信号）。
        tab = self._borrow()
        try:
            target_origin = self._origin(url)
            last = ""
            for attempt in range(2):
                try:
                    cur_origin = self._origin(tab.url or "")
                except Exception:
                    cur_origin = ""
                if cur_origin != target_origin or attempt > 0:
                    try:
                        tab.get(url)
                    except Exception as exc:
                        last = str(exc)
                        time.sleep(0.5)
                        continue
                    deadline = time.time() + min(self.config.cloudflare_wait, 15)
                    while _html_state(tab.html or "") == "challenge" and time.time() < deadline:
                        time.sleep(1.0)
                res = tab.run_js(_XHR_JS, url)
                if isinstance(res, str) and not res.startswith("ERR"):
                    return base64.b64decode(res)
                last = str(res)
            raise RuntimeError(f"浏览器下载图片失败({last[:80]}): {url}")
        finally:
            self._release(tab)

    @staticmethod
    def _valid_file(path) -> bool:
        try:
            return path.exists() and path.stat().st_size > 1000
        except OSError:
            return False

    def download_chapter(self, chapter_url, dest_dir, prefix, resume=True,
                         on_scanned=None, on_saved=None):
        """在一个标签上完成一话的下载，返回已保存 jpg 路径列表。

        ①加载阅读页拿到有序 data-src（不滚动、不触发原生加载，避免双重下载）；
        ②导航到图片同源；③限流 5 并发 fetch 一次抓完整话（模仿浏览器节流，避 429）；
        ④少数失败用同步 XHR 兜底。
        """
        tab = self._borrow()
        saved = []
        try:
            # ① 加载阅读页并过 Cloudflare，等 data-src 出现
            tab.get(chapter_url)
            deadline = time.time() + self.config.cloudflare_wait
            n, loaded, urls = tab.run_js(_SCAN_JS)
            while time.time() < deadline:
                st = _html_state(tab.html or "")
                if st == "blocked":
                    raise CloudflareBlocked(f"该 IP 被 Cloudflare 硬封禁: {chapter_url}")
                if st != "challenge" and n > 0:
                    break
                time.sleep(0.8)
                n, loaded, urls = tab.run_js(_SCAN_JS)
            urls = [u for u in (urls or []) if u]
            if on_scanned:
                on_scanned(len(urls))
            if not urls:
                return saved

            Path(dest_dir).mkdir(parents=True, exist_ok=True)
            dests = [Path(dest_dir) / f"{prefix}_{ii:04d}.jpg" for ii in range(len(urls))]

            # 断点续传：已存在的直接计入
            need = []
            for u, dd in zip(urls, dests):
                if resume and self._valid_file(dd):
                    saved.append(dd)
                    if on_saved:
                        on_saved(dd)
                else:
                    need.append((u, dd))
            if not need:
                return saved

            # ② 导航到图片同源
            origin = self._origin(need[0][0])
            try:
                if self._origin(tab.url or "") != origin:
                    tab.get(need[0][0])
                    time.sleep(0.3)
            except Exception:
                pass

            # ③ 限流并发抓取：分小组多次 run_js（单次别太长，避免 run_js 超时）
            still = []
            GROUP = 8
            for gi in range(0, len(need), GROUP):
                grp = need[gi:gi + GROUP]
                try:
                    res = tab.run_js(_POOL_JS, json.dumps([u for u, _ in grp]))
                except Exception:
                    res = None
                if isinstance(res, list):
                    for (u, dd), x in zip(grp, res):
                        if isinstance(x, str) and not x.startswith("ERR"):
                            try:
                                save_as_jpeg(base64.b64decode(x), dd)
                                saved.append(dd)
                                if on_saved:
                                    on_saved(dd)
                                continue
                            except Exception:
                                pass
                        still.append((u, dd))
                else:
                    still.extend(grp)

            # ④ 未命中的逐张同步 XHR 兜底（可靠，轻量重试）
            for u, dd in still:
                for _ in range(3):
                    try:
                        r = tab.run_js(_XHR_JS, u)
                    except Exception:
                        r = None
                    if isinstance(r, str) and not r.startswith("ERR"):
                        try:
                            save_as_jpeg(base64.b64decode(r), dd)
                            saved.append(dd)
                            if on_saved:
                                on_saved(dd)
                        except Exception:
                            pass
                        break
                    time.sleep(0.6)
            return saved
        finally:
            self._release(tab)

    def close(self):
        with self._lock:
            try:
                if self._browser is not None:
                    self._browser.quit()
            except Exception:
                pass
            self._browser = None
            self._tab = None
            self._pool = None


class Net:
    """统一抓取入口：浏览器过 Cloudflare + requests 并发下图，含镜像/限速/重试/代理回退。"""

    def __init__(self, config: Config):
        ensure_local_no_proxy()  # 本地 CDP/请求绕过代理，避免被系统代理劫持
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": MOBILE_UA, "Cookie": "night=0",
                                     "Accept-Language": "zh-CN,zh;q=0.9"})
        self.base_url: Optional[str] = None
        self._lock = threading.Lock()
        self._last_request_ts = 0.0
        self._proxy_disabled = False
        self._apply_proxy()
        # 共享节流器：命中 429 时全体线程一起冷却（跨卷/跨本复用同一个桶）。
        self._gate = RateGate(min_interval=0.25, concurrency=6)
        # 浏览器**惰性启动**：默认走无浏览器直连，只有真的撞上 Cloudflare 质询才
        # 按需启动 Chrome 过验证；一旦启动就长期复用（下多本不再重复等待验证）。
        self._want_browser = config.use_browser
        self._browser: Optional[BrowserEngine] = None
        self._browser_lock = threading.Lock()
        self._browser_failed = False

    @property
    def browser(self) -> Optional[BrowserEngine]:
        """已启动的浏览器引擎（未启动则为 None，不触发启动）。"""
        return self._browser

    def _ensure_browser(self) -> Optional[BrowserEngine]:
        """按需启动并复用浏览器；不允许/启动失败则返回 None。"""
        if self._browser is not None:
            return self._browser
        if not self._want_browser or self._browser_failed:
            return None
        with self._browser_lock:
            if self._browser is None and not self._browser_failed:
                try:
                    log.info("检测到需要过 Cloudflare，正在启动浏览器（仅首次）……")
                    self._browser = BrowserEngine(self.config)
                except Exception as exc:  # noqa: BLE001
                    self._browser_failed = True
                    log.warning("浏览器启动失败：%s", exc)
        return self._browser

    # ---- 代理 ----
    def _apply_proxy(self) -> None:
        if self.config.proxy:
            self.session.proxies = {"http": self.config.proxy,
                                    "https": self.config.proxy}
            self.session.trust_env = False
            log.debug("使用配置代理: %s", self.config.proxy)

    def _disable_proxy(self) -> None:
        self._proxy_disabled = True
        self.session.trust_env = False
        self.session.proxies = {"http": None, "https": None}

    # ---- requests 抓取（限速 + 429 冷却 + 重试/代理回退；CF 质询交上层用浏览器）----
    def _requests_fetch(self, url: str, *, referer: Optional[str] = None,
                        stream: bool = False, headers: Optional[dict] = None,
                        timeout: Optional[float] = None) -> requests.Response:
        hdrs = dict(headers or {})
        if referer and "Referer" not in hdrs:
            hdrs["Referer"] = referer
        attempts = self.config.retry_max_attempts if self.config.retry_enabled else 1
        attempts = max(attempts, 6)  # 保证有足够次数熬过 429 冷却
        last_exc: Optional[Exception] = None
        to = timeout or (8, min(self.config.request_timeout, 20))
        attempt = 0
        while attempt < attempts:
            self._gate.acquire()
            try:
                resp = self.session.get(url, headers=hdrs, timeout=to, stream=stream)
                status = resp.status_code
            except requests.exceptions.ProxyError as exc:
                self._gate.release()
                last_exc = exc
                log.warning("代理连接失败 %s: %s", url, exc)
                if not self._proxy_disabled and not self.config.proxy:
                    self._disable_proxy()
                    log.warning("检测到代理不可用，自动改为【直连】重试……")
                    continue
                attempt += 1
                continue
            except requests.RequestException as exc:
                self._gate.release()
                last_exc = exc
                log.warning("请求异常 %s: %s", url, exc)
                attempt += 1
                continue
            else:
                self._gate.release()
                attempt += 1
                log.debug("← %s status=%s", url, status)
                if status == 200:
                    self._gate.reward()
                    return resp
                last_exc = requests.HTTPError(f"HTTP {status}: {url}")
                # 429 / 占位限流：全体冷却后重试（不交给浏览器，纯粹是限速）。
                if status == 429:
                    cooldown = min(15 + attempt * 8, 45)
                    self._gate.penalize(cooldown)
                    log.warning("触发限流(429)，全体冷却 %ds 后重试(%d/%d): %s",
                                cooldown, attempt, attempts, url)
                    continue
                # Cloudflare JS 质询：requests 过不了，交给上层用浏览器。
                if _looks_like_cloudflare("" if stream else resp.text, status):
                    raise CloudflareBlocked(f"requests 被 Cloudflare 拦截: {url}")
            if self.config.retry_enabled and attempt < attempts:
                backoff = self.config.retry_backoff_base * (2 ** (attempt - 1))
                backoff += random.uniform(0, 0.5)
                time.sleep(min(backoff, 10))
        raise last_exc or RuntimeError(f"请求失败: {url}")

    # ---- 对外 API ----
    def warm_up(self, url: Optional[str] = None) -> None:
        """惰性浏览器模式下无需预热（默认直连）；已启动浏览器时才后台预热。"""
        if self._browser is not None:
            threading.Thread(target=self._browser.prewarm, args=(url,),
                             daemon=True).start()

    def open_login(self, url: str) -> None:
        """打开有头浏览器让用户登录（轻小说站点需要）。"""
        br = self._ensure_browser()
        if br:
            br.open_login(url)

    def finish_login(self) -> None:
        if self._browser:
            self._browser.finish_login()

    def resolve_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        m = self.config.site
        try:
            self.get_text(m + "/")
            self.base_url = m
            log.info("使用站点: %s", m)
            return m
        except Exception as exc:
            log.warning("站点 %s 不可用: %s", m, exc)
            raise CloudflareBlocked(
                f"站点 {m} 无法访问：{exc}\n"
                "请在设置里确认站点地址，并检查网络 / 代理 / Cloudflare。"
            ) from exc

    def get_text(self, url: str, *, referer: Optional[str] = None,
                 wait_for: Optional[str] = None) -> str:
        """优先无浏览器直连；撞上 Cloudflare 质询才启动/复用浏览器过验证。"""
        try:
            resp = self._requests_fetch(url, referer=referer)
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except CloudflareBlocked:
            br = self._ensure_browser()
            if br is None:
                raise
            return br.get_html(url, wait_for=wait_for)

    def get_novel_text(self, url: str, *, referer: Optional[str] = None) -> str:
        """轻小说阅读页 HTML（无浏览器直连；撞 CF 才用浏览器做诱饵段落清理）。"""
        try:
            resp = self._requests_fetch(url, referer=referer)
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except CloudflareBlocked:
            br = self._ensure_browser()
            if br is None:
                raise
            return br.get_novel_html(url)

    def get_bytes(self, url: str, *, referer: Optional[str] = None) -> bytes:
        """图片：无浏览器直连（补全指纹头即可过 CDN 的 403）；失败再退浏览器。"""
        try:
            resp = self._requests_fetch(url, referer=referer, stream=True,
                                        headers=_image_headers(referer))
            return resp.content
        except CloudflareBlocked:
            br = self._ensure_browser()
            if br is None:
                raise
            return br.get_image_bytes(url)

    def download_chapter(self, chapter_url, dest_dir, prefix, resume=True,
                         on_scanned=None, on_saved=None):
        """浏览器模式：一话内原生加载+缓存批量+XHR兜底下载到 dest_dir，返回已存 jpg 路径。"""
        br = self._ensure_browser()
        if br is None:
            raise BrowserUnavailable("该操作需要浏览器，但浏览器不可用。")
        return br.download_chapter(
            chapter_url, dest_dir, prefix, resume=resume,
            on_scanned=on_scanned, on_saved=on_saved)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
        if self._browser:
            self._browser.close()
            self._browser = None
