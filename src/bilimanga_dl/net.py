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
from .logutil import get_logger

log = get_logger("net")


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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

# 同源批量抓取：一次 Promise.all 并发 fetch 多张图（模拟浏览器看漫画的原生并发），
# 返回与输入顺序对应的 base64 列表（失败项为 "ERR:..."）。
_BATCH_FETCH_JS = r"""
const urls = arguments[0];
return (async () => {
  const out = new Array(urls.length).fill(null);
  await Promise.all(urls.map(async (u, i) => {
    try {
      const r = await fetch(u, {credentials: 'include'});
      if (!r.ok) { out[i] = 'ERR:' + r.status; return; }
      const buf = await r.arrayBuffer();
      const a = new Uint8Array(buf); let s = '';
      const CH = 0x8000;
      for (let j = 0; j < a.length; j += CH) s += String.fromCharCode.apply(null, a.subarray(j, j + CH));
      out[i] = btoa(s);
    } catch (e) { out[i] = 'ERR:' + String(e); }
  }));
  return out;
})();
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
        self._tab = None                 # 主标签:加载页面
        self._lock = threading.RLock()   # 保护主标签
        self._pool: Optional[queue.Queue] = None  # 图片下载标签池
        self._pool_lock = threading.Lock()

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
        co.auto_port()  # 每次用独立端口/用户目录，避免与已开的 Chrome 冲突
        if self.config.browser_headless:
            co.headless(True)
        co.set_argument("--no-first-run")
        co.set_argument("--no-default-browser-check")
        log.info("正在启动本地浏览器过 Cloudflare（%s，headless=%s）……",
                 browser_path, self.config.browser_headless)
        self._browser = Chromium(co)
        self._tab = self._browser.latest_tab

    # ---- 统一标签池（页面与图片共用，支持并行） ----
    def _pool_size(self) -> int:
        return max(1, self.config.parallel_chapters)

    def _ensure_pool(self):
        if self._pool is not None:
            return
        with self._pool_lock:
            if self._pool is not None:
                return
            self._ensure()
            pool: queue.Queue = queue.Queue()
            pool.put(self._tab)  # 首个标签
            for _ in range(self._pool_size() - 1):
                try:
                    pool.put(self._browser.new_tab())
                except Exception:
                    break
            self._pool = pool
            log.debug("[browser] 标签池大小=%d", pool.qsize())

    def _borrow(self):
        self._ensure_pool()
        return self._pool.get()

    def _release(self, tab):
        if self._pool is not None:
            self._pool.put(tab)

    @staticmethod
    def _origin(u: str) -> str:
        p = urlparse(u or "")
        return f"{p.scheme}://{p.netloc}"

    def get_html(self, url: str) -> str:
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
            log.debug("[browser] ← %s state=%s len=%d", url, state, len(html))
            if state == "blocked":
                raise CloudflareBlocked(
                    f"该 IP 被 Cloudflare 硬封禁: {url}（请更换住宅代理节点后重试）"
                )
            if state == "challenge":
                raise CloudflareBlocked(
                    f"等待 {self.config.cloudflare_wait}s 仍未通过 Cloudflare: {url}"
                )
            return html
        finally:
            self._release(tab)

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
        tab = self._borrow()
        try:
            target_origin = self._origin(url)
            last = ""
            for attempt in range(5):
                # 标签不在图片域名上时才整页导航（一次即可）；之后同源 XHR 直接取。
                try:
                    cur_origin = self._origin(tab.url or "")
                except Exception:
                    cur_origin = ""
                if cur_origin != target_origin or attempt > 0:
                    try:
                        tab.get(url)
                    except Exception as exc:
                        last = str(exc)
                        time.sleep(1.0 + attempt)
                        continue
                    deadline = time.time() + min(self.config.cloudflare_wait, 20)
                    while _html_state(tab.html or "") == "challenge" and time.time() < deadline:
                        time.sleep(1.0)
                res = tab.run_js(_XHR_JS, url)
                if isinstance(res, str) and not res.startswith("ERR"):
                    return base64.b64decode(res)
                last = str(res)
                time.sleep(0.6 + attempt)  # 递增退避，缓解速率质询
            raise RuntimeError(f"浏览器下载图片失败({last[:80]}): {url}")
        finally:
            self._release(tab)

    def get_images_batch(self, urls):
        """同源批量抓取（一次 Promise.all 并发多张）。返回 List[Optional[bytes]]，
        与输入顺序对应，失败项为 None。urls 必须同源。"""
        if not urls:
            return []
        tab = self._borrow()
        try:
            target_origin = self._origin(urls[0])
            # 确保标签在该源上（一次导航即可，之后同源 fetch）
            for attempt in range(3):
                try:
                    if self._origin(tab.url or "") != target_origin:
                        tab.get(urls[0])
                        deadline = time.time() + min(self.config.cloudflare_wait, 20)
                        while _html_state(tab.html or "") == "challenge" and time.time() < deadline:
                            time.sleep(1.0)
                    res = tab.run_js(_BATCH_FETCH_JS, list(urls))
                    if isinstance(res, list):
                        out = []
                        for x in res:
                            if isinstance(x, str) and not x.startswith("ERR"):
                                try:
                                    out.append(base64.b64decode(x))
                                except Exception:
                                    out.append(None)
                            else:
                                out.append(None)
                        return out
                except Exception as exc:
                    log.debug("批量抓取异常(重试): %s", exc)
                time.sleep(1.0 + attempt)
            return [None] * len(urls)
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
        self.session.headers.update({"User-Agent": USER_AGENT, "Cookie": "night=1"})
        self.base_url: Optional[str] = None
        self._lock = threading.Lock()
        self._last_request_ts = 0.0
        self._proxy_disabled = False
        self._apply_proxy()
        self.browser: Optional[BrowserEngine] = (
            BrowserEngine(config) if config.use_browser else None
        )

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

    # ---- 限速 ----
    def _throttle(self) -> None:
        if not self.config.rate_limit_enabled:
            return
        with self._lock:
            wait = random.uniform(
                self.config.rate_limit_min_ms / 1000.0,
                self.config.rate_limit_max_ms / 1000.0,
            )
            elapsed = time.monotonic() - self._last_request_ts
            if elapsed < wait:
                time.sleep(wait - elapsed)
            self._last_request_ts = time.monotonic()

    # ---- requests 抓取（带重试/退避/代理回退，不含 CF 兜底）----
    def _requests_fetch(self, url: str, *, referer: Optional[str] = None,
                        stream: bool = False) -> requests.Response:
        headers = {}
        if referer:
            headers["Referer"] = referer
        attempts = self.config.retry_max_attempts if self.config.retry_enabled else 1
        last_exc: Optional[Exception] = None
        attempt = 0
        while attempt < attempts:
            self._throttle()
            log.debug("GET %s (第 %d/%d 次, stream=%s)", url, attempt + 1, attempts, stream)
            try:
                resp = self.session.get(url, headers=headers,
                                        timeout=self.config.request_timeout, stream=stream)
            except requests.exceptions.ProxyError as exc:
                last_exc = exc
                log.warning("代理连接失败 %s: %s", url, exc)
                if not self._proxy_disabled and not self.config.proxy:
                    self._disable_proxy()
                    log.warning("检测到代理不可用，自动改为【直连】重试……")
                    continue
                attempt += 1
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("请求异常 %s: %s", url, exc)
                attempt += 1
            else:
                attempt += 1
                log.debug("← %s status=%s", url, resp.status_code)
                if resp.status_code == 200:
                    return resp
                last_exc = requests.HTTPError(f"HTTP {resp.status_code}: {url}")
                if _looks_like_cloudflare("" if stream else resp.text, resp.status_code):
                    last_exc = CloudflareBlocked(f"requests 被 Cloudflare 拦截: {url}")
                    break  # requests 过不了，交给上层用浏览器
            if self.config.retry_enabled and attempt < attempts:
                backoff = self.config.retry_backoff_base * (2 ** (attempt - 1))
                backoff += random.uniform(0, 0.5)
                log.debug("退避 %.2fs 后重试", backoff)
                time.sleep(backoff)
        raise last_exc or RuntimeError(f"请求失败: {url}")

    # ---- 对外 API ----
    def warm_up(self, url: Optional[str] = None) -> None:
        """在后台线程启动并预热浏览器（供确认阶段并行准备，用户体感更快）。"""
        if not self.browser:
            return
        threading.Thread(target=self.browser.prewarm, args=(url,), daemon=True).start()

    def resolve_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        errors = []
        for mirror in self.config.mirrors:
            m = mirror.rstrip("/")
            try:
                if self.browser:
                    self.browser.get_html(m + "/")
                else:
                    self._requests_fetch(m + "/")
                self.base_url = m
                log.info("使用镜像: %s", m)
                return m
            except Exception as exc:
                log.warning("镜像 %s 不可用: %s", m, exc)
                errors.append(f"{mirror}: {exc}")
                continue
        raise CloudflareBlocked("所有镜像域名均无法访问：\n" + "\n".join(errors))

    def get_text(self, url: str, *, referer: Optional[str] = None) -> str:
        if self.browser:
            return self.browser.get_html(url)
        resp = self._requests_fetch(url, referer=referer)
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def get_bytes(self, url: str, *, referer: Optional[str] = None) -> bytes:
        # 图片域名对 requests 返回 403(JA3 指纹)，有浏览器时直接走浏览器多标签并行下载。
        if self.browser:
            return self.browser.get_image_bytes(url)
        resp = self._requests_fetch(url, referer=referer, stream=True)
        return resp.content

    def get_images_batch(self, urls):
        """批量抓取图片字节，返回 List[Optional[bytes]] 与 urls 对应。"""
        if self.browser:
            return self.browser.get_images_batch(urls)
        out = []
        for u in urls:
            try:
                out.append(self._requests_fetch(u, stream=True).content)
            except Exception:
                out.append(None)
        return out

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
        if self.browser:
            self.browser.close()
