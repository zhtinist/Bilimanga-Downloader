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

# 同源批量抓取：一次 Promise.all 并发 fetch 多张图。用 force-cache 优先读浏览器缓存
# （页面已原生加载过这些图），命中则不触发网络请求、不会 429。run_js 不支持 list，
# 故用 JSON 字符串传参；标签须已在图片同源上。
_BATCH_JS = r"""
const urls = JSON.parse(arguments[0]);
return (async () => {
  const out = new Array(urls.length).fill(null);
  await Promise.all(urls.map(async (u, i) => {
    try {
      const r = await fetch(u, {credentials: 'include', cache: 'force-cache'});
      if (!r.ok) { out[i] = 'ERR' + r.status; return; }
      const b = await r.arrayBuffer();
      const a = new Uint8Array(b);
      let s = ''; const C = 0x8000;
      for (let j = 0; j < a.length; j += C) s += String.fromCharCode.apply(null, a.subarray(j, j + C));
      out[i] = btoa(s);
    } catch (e) { out[i] = 'ERR:' + e; }
  }));
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
        co.set_argument("--disk-cache-size=1073741824")  # 1GB 磁盘缓存，利于 force-cache 命中
        # 站点/图片流量优先走代理（配置优先，其次系统环境变量）
        proxy = self.config.proxy or os.environ.get("https_proxy") or \
            os.environ.get("http_proxy") or os.environ.get("HTTPS_PROXY") or \
            os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY")
        if proxy:
            try:
                co.set_proxy(proxy)
                log.info("浏览器走代理: %s", proxy)
            except Exception as exc:
                log.warning("设置浏览器代理失败: %s", exc)
        log.info("正在启动本地浏览器过 Cloudflare（%s，headless=%s）……",
                 browser_path, self.config.browser_headless)
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
        """在一个标签上完成一话的下载（模拟看漫画 + 落盘），返回已保存 jpg 路径列表。

        流程：①导航阅读页 + 滚动触发浏览器**原生并发加载**所有图（快、不 429）；
        ②读取有序 data-src；③导航到图片同源，用 force-cache 批量读缓存（多数命中，秒取）；
        ④少数未命中用逐张同步 XHR 兜底（可靠）。
        """
        tab = self._borrow()
        saved = []
        try:
            # ① 加载阅读页并过 Cloudflare
            tab.get(chapter_url)
            dl = time.time() + self.config.cloudflare_wait
            while _html_state(tab.html or "") == "challenge" and time.time() < dl:
                time.sleep(1.0)
            # 滚动触发懒加载，直到全部 complete（或超时）
            deadline = time.time() + max(25, self.config.cloudflare_wait)
            n, loaded, urls = tab.run_js(_SCAN_JS)
            while time.time() < deadline:
                if n > 0 and loaded >= n:
                    break
                tab.scroll.down(4000)
                time.sleep(0.25)
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
                        on_saved()
                else:
                    need.append((u, dd))
            if not need:
                return saved

            # ③ 导航到图片同源，force-cache 批量读缓存
            origin = self._origin(need[0][0])
            try:
                if self._origin(tab.url or "") != origin:
                    tab.get(need[0][0])
                    time.sleep(0.3)
            except Exception:
                pass

            still = []
            for i in range(0, len(need), 12):
                chunk = need[i:i + 12]
                try:
                    res = tab.run_js(_BATCH_JS, json.dumps([u for u, _ in chunk]))
                except Exception:
                    res = None
                if isinstance(res, list):
                    for (u, dd), x in zip(chunk, res):
                        if isinstance(x, str) and not x.startswith("ERR"):
                            try:
                                save_as_jpeg(base64.b64decode(x), dd)
                                saved.append(dd)
                                if on_saved:
                                    on_saved()
                                continue
                            except Exception:
                                pass
                        still.append((u, dd))
                else:
                    still.extend(chunk)

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
                                on_saved()
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

    def get_text(self, url: str, *, referer: Optional[str] = None,
                 wait_for: Optional[str] = None) -> str:
        if self.browser:
            return self.browser.get_html(url, wait_for=wait_for)
        resp = self._requests_fetch(url, referer=referer)
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def get_bytes(self, url: str, *, referer: Optional[str] = None) -> bytes:
        # 图片域名对 requests 返回 403(JA3 指纹)，有浏览器时直接走浏览器多标签并行下载。
        if self.browser:
            return self.browser.get_image_bytes(url)
        resp = self._requests_fetch(url, referer=referer, stream=True)
        return resp.content

    def download_chapter(self, chapter_url, dest_dir, prefix, resume=True,
                         on_scanned=None, on_saved=None):
        """浏览器模式：一话内原生加载+缓存批量+XHR兜底下载到 dest_dir，返回已存 jpg 路径。"""
        return self.browser.download_chapter(
            chapter_url, dest_dir, prefix, resume=resume,
            on_scanned=on_scanned, on_saved=on_saved)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
        if self.browser:
            self.browser.close()
