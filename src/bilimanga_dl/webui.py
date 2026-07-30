"""本地网页 UI：启动一个只监听 127.0.0.1 的小型 HTTP 服务，用浏览器当界面。

双击可执行文件即启动本模块：拉起本地服务并自动打开浏览器。整套下载逻辑复用
命令行版的 :class:`Net` / :class:`Scraper` / :class:`Downloader`，界面只是薄壳。

仅用 Python 标准库（http.server），不引入 Web 框架，方便打包、体积小。
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .build_epub import build_epub
from .build_pdf import build_pdf
from .config import Config, DEFAULT_SITE, TEMP_DOWNLOAD_DIR
from .downloader import Downloader, safe_name
from .logutil import get_logger
from .models import Book
from .net import Net
from .scraper import Scraper, parse_book_no

log = get_logger("webui")


class App:
    """进程内单例状态：一个用户、一次一本、串行下载。"""

    def __init__(self, config: Config):
        self.config = config
        self.net: Optional[Net] = None
        self.scraper: Optional[Scraper] = None
        self.lock = threading.Lock()
        self.book: Optional[Book] = None
        # 下载任务状态（供前端轮询）
        self.status = "idle"        # idle | downloading | done | error
        self.message = ""
        self.error = ""
        self.volumes: dict = {}     # vidx -> {title,total,done,phase,output}
        self.outputs: list = []
        self._worker: Optional[threading.Thread] = None

    # ---- 网络对象（懒创建，复用同一浏览器，保持热态）----
    def _ensure_net(self):
        if self.net is None:
            self.net = Net(self.config)
            self.scraper = Scraper(self.net)

    def reset_net(self):
        if self.net is not None:
            try:
                self.net.close()
            except Exception:
                pass
        self.net = None
        self.scraper = None

    # ---- 解析书籍 ----
    def parse(self, text: str) -> dict:
        with self.lock:
            if self.status == "downloading":
                raise RuntimeError("正在下载中，请等当前任务结束。")
            book_no = parse_book_no(text)
            # 配置可能已改（站点/代理），重建网络对象以生效
            self.reset_net()
            self._ensure_net()
            book = self.scraper.fetch_book(book_no)
            if not book.volumes:
                raise RuntimeError("未解析到任何章节，可能页面结构变化或该书需登录。")
            self.book = book
            return {
                "book_no": book.book_no,
                "title": book.title,
                "author": book.author,
                "cover_url": book.cover_url,
                "tags": book.tags,
                "volumes": [
                    {"index": v.index, "title": v.title,
                     "chapters": len(v.chapters)}
                    for v in book.volumes
                ],
            }

    # ---- 开始下载 ----
    def start_download(self, indices: list, fmt: str) -> None:
        with self.lock:
            if self.status == "downloading":
                raise RuntimeError("已有下载任务在进行中。")
            if self.book is None:
                raise RuntimeError("请先解析一本漫画。")
            out_root = self.config.output_path()  # 未配置则默认浏览器下载目录
            index_map = {v.index: v for v in self.book.volumes}
            vols = [index_map[i] for i in indices if i in index_map]
            if not vols:
                raise RuntimeError("未选择任何章。")
            fmt = fmt if fmt in ("epub", "pdf") else self.config.default_format

            self.status = "downloading"
            self.message = "准备中……"
            self.error = ""
            self.outputs = []
            self.volumes = {
                v.index: {"title": v.title, "total": 0, "done": 0,
                          "phase": "wait", "output": ""}
                for v in vols
            }
            self._worker = threading.Thread(
                target=self._run, args=(vols, fmt, out_root), daemon=True)
            self._worker.start()

    def _run(self, vols, fmt, out_root):
        book = self.book
        target = out_root / safe_name(book.title)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            with self.lock:
                self.status = "error"
                self.error = f"无法创建输出目录 {target}：{exc}"
            return

        self._ensure_net()
        downloader = Downloader(self.net, self.scraper, self.config)
        build_fn = build_epub if fmt == "epub" else build_pdf

        def vp(vidx):
            return self.volumes.setdefault(
                vidx, {"title": "", "total": 0, "done": 0, "phase": "wait", "output": ""})

        def on_start(vidx):
            with self.lock:
                vp(vidx)["phase"] = "download"

        def on_total(vidx, n):
            with self.lock:
                vp(vidx)["total"] = n

        def on_image(vidx):
            with self.lock:
                vp(vidx)["done"] += 1

        def on_phase(vidx, phase):
            with self.lock:
                vp(vidx)["phase"] = phase

        def on_done(vidx, path):
            with self.lock:
                v = vp(vidx)
                v["phase"] = "done" if path else "empty"
                if path:
                    v["output"] = path.name
                    self.outputs.append(str(path))

        try:
            downloader.run_pipeline(
                book, vols, TEMP_DOWNLOAD_DIR, target, build_fn,
                on_start=on_start, on_total=on_total, on_image=on_image,
                on_phase=on_phase, on_done=on_done)
            with self.lock:
                self.status = "done"
                self.message = f"全部完成，共 {len(self.outputs)} 个文件。"
        except Exception as exc:  # noqa: BLE001
            log.exception("下载失败")
            with self.lock:
                self.status = "error"
                self.error = str(exc)

    # ---- 供前端轮询的整体状态 ----
    def state(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "message": self.message,
                "error": self.error,
                "outputs": list(self.outputs),
                "volumes": [
                    {"index": k, **v} for k, v in sorted(self.volumes.items())
                ],
            }


# ---------------- HTTP 层 ----------------
APP: Optional[App] = None


class Handler(BaseHTTPRequestHandler):
    # 关掉逐条访问日志（保持终端干净）
    def log_message(self, *args):  # noqa: D401
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/config"):
            cfg = APP.config.to_dict()
            cfg["output_effective"] = str(APP.config.output_path())
            self._json(cfg)
        elif self.path.startswith("/api/state"):
            self._json(APP.state())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            if self.path.startswith("/api/config"):
                self._save_config(self._read_json())
                self._json({"ok": True, "config": APP.config.to_dict()})
            elif self.path.startswith("/api/parse"):
                data = self.parse_book(self._read_json())
                self._json({"ok": True, "book": data})
            elif self.path.startswith("/api/download"):
                body = self._read_json()
                APP.start_download(body.get("indices") or [],
                                   body.get("format") or APP.config.default_format)
                self._json({"ok": True})
            elif self.path.startswith("/api/reveal"):
                self._json({"ok": self._reveal()})
            else:
                self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)}, 400)

    # ---- 具体处理 ----
    def _save_config(self, data: dict):
        c = APP.config
        if "site_url" in data:
            s = (data["site_url"] or "").strip()
            if s and not s.startswith("http"):
                s = "https://" + s
            c.site_url = (s or DEFAULT_SITE).rstrip("/")
        if "output_dir" in data:
            p = (data["output_dir"] or "").strip()
            if p:
                Path(p).expanduser().mkdir(parents=True, exist_ok=True)
                c.output_dir = str(Path(p).expanduser())
            else:
                c.output_dir = ""
        if data.get("default_format") in ("epub", "pdf"):
            c.default_format = data["default_format"]
        if str(data.get("parallel_chapters", "")).isdigit():
            c.parallel_chapters = max(1, int(data["parallel_chapters"]))
        if "proxy" in data:
            c.proxy = (data["proxy"] or "").strip()
        for flag in ("rate_limit_enabled", "retry_enabled", "resume_enabled",
                     "browser_headless", "debug"):
            if flag in data:
                setattr(c, flag, bool(data[flag]))
        c.save()

    def parse_book(self, data: dict) -> dict:
        text = (data.get("input") or "").strip()
        if not text:
            raise RuntimeError("请输入详情页链接、目录页链接或书号。")
        return APP.parse(text)

    def _reveal(self) -> bool:
        import subprocess
        import sys
        p = APP.config.output_path()
        if p is None or not Path(p).exists():
            return False
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
            return True
        except Exception:
            return False


def _find_port(preferred: int = 8765) -> int:
    import socket
    for port in [preferred] + list(range(8766, 8800)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def serve(config: Config, open_browser: bool = True) -> None:
    """启动本地网页 UI（阻塞运行，Ctrl+C 退出）。"""
    global APP
    APP = App(config)
    port = _find_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"图形界面已启动：{url}")
    print("（保持本窗口开启；关闭窗口即退出程序。按 Ctrl+C 也可退出。）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出……")
    finally:
        httpd.shutdown()
        APP.reset_net()


# ---------------- 前端（单文件内嵌）----------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bilimanga 漫画下载器</title>
<style>
  :root{ --bg:#0f1216; --card:#171b22; --line:#262c36; --fg:#e6e9ef;
         --muted:#8b94a3; --acc:#4f8cff; --ok:#37c871; --warn:#f5a524; --err:#ff5d5d; }
  *{ box-sizing:border-box; }
  body{ margin:0; font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",
        "PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--fg); }
  header{ padding:18px 24px; border-bottom:1px solid var(--line);
          display:flex; align-items:center; gap:12px; }
  header h1{ font-size:18px; margin:0; font-weight:650; }
  header .tag{ color:var(--muted); font-size:13px; }
  main{ max-width:920px; margin:0 auto; padding:24px; }
  .card{ background:var(--card); border:1px solid var(--line); border-radius:14px;
         padding:20px; margin-bottom:18px; }
  .card h2{ font-size:15px; margin:0 0 14px; font-weight:600; color:var(--fg); }
  label{ display:block; font-size:13px; color:var(--muted); margin:10px 0 4px; }
  input[type=text], select{ width:100%; padding:10px 12px; border-radius:9px;
    border:1px solid var(--line); background:#10141a; color:var(--fg); font-size:14px; }
  input[type=text]:focus, select:focus{ outline:none; border-color:var(--acc); }
  .row{ display:flex; gap:12px; flex-wrap:wrap; }
  .row > div{ flex:1; min-width:180px; }
  button{ appearance:none; border:none; border-radius:9px; padding:10px 16px;
    background:var(--acc); color:#fff; font-size:14px; font-weight:600; cursor:pointer; }
  button.ghost{ background:transparent; border:1px solid var(--line); color:var(--fg); }
  button:disabled{ opacity:.5; cursor:not-allowed; }
  .btns{ display:flex; gap:10px; align-items:center; margin-top:14px; flex-wrap:wrap; }
  .muted{ color:var(--muted); font-size:13px; }
  .hidden{ display:none; }
  .bookhead{ display:flex; gap:16px; }
  .bookhead img{ width:96px; height:auto; border-radius:8px; border:1px solid var(--line); }
  .chips span{ display:inline-block; background:#10141a; border:1px solid var(--line);
    border-radius:20px; padding:2px 10px; font-size:12px; color:var(--muted); margin:2px 4px 2px 0; }
  .vol{ display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:8px; }
  .vol:hover{ background:#10141a; }
  .vol .name{ flex:1; }
  .vol .cnt{ color:var(--muted); font-size:12px; }
  .bar{ height:8px; background:#10141a; border-radius:6px; overflow:hidden; margin-top:4px; }
  .bar > i{ display:block; height:100%; width:0; background:var(--acc); transition:width .3s; }
  .pv{ padding:8px 0; border-bottom:1px solid var(--line); }
  .pv:last-child{ border-bottom:none; }
  .pv .top{ display:flex; justify-content:space-between; gap:10px; }
  .badge{ font-size:12px; padding:1px 8px; border-radius:20px; }
  .b-wait{ color:var(--muted); background:#10141a; }
  .b-download{ color:var(--acc); background:rgba(79,140,255,.12); }
  .b-validate,.b-package{ color:var(--warn); background:rgba(245,165,36,.12); }
  .b-done{ color:var(--ok); background:rgba(55,200,113,.14); }
  .b-empty{ color:var(--err); background:rgba(255,93,93,.14); }
  .toast{ position:fixed; left:50%; bottom:26px; transform:translateX(-50%);
    background:#222833; border:1px solid var(--line); padding:10px 16px; border-radius:10px;
    color:var(--fg); font-size:14px; box-shadow:0 8px 30px rgba(0,0,0,.4); }
  a{ color:var(--acc); }
</style>
</head>
<body>
<header>
  <h1>bilimanga 漫画下载器</h1>
</header>
<main>
  <!-- 设置 -->
  <div class="card">
    <h2>① 设置</h2>
    <div class="row">
      <div>
        <label>站点地址</label>
        <input id="site" type="text" placeholder="https://www.bilimanga.net">
      </div>
      <div>
        <label>下载输出目录（留空 = 浏览器下载目录）</label>
        <input id="out" type="text" placeholder="留空即用浏览器下载目录">
      </div>
    </div>
    <div class="row">
      <div>
        <label>默认格式</label>
        <select id="fmt"><option value="epub">EPUB（电子书）</option><option value="pdf">PDF（整页图）</option></select>
      </div>
      <div>
        <label>并发数</label>
        <input id="par" type="text" placeholder="12">
      </div>
      <div>
        <label>代理（留空自动 / none 强制直连）</label>
        <input id="proxy" type="text" placeholder="http://127.0.0.1:7890">
      </div>
    </div>
    <div class="btns">
      <button id="saveCfg">保存设置</button>
      <button class="ghost" id="reveal">打开输出目录</button>
      <span class="muted" id="cfgHint"></span>
    </div>
  </div>

  <!-- 解析 -->
  <div class="card">
    <h2>② 输入漫画</h2>
    <label>详情页链接 / 目录页链接 / 书号</label>
    <input id="input" type="text" placeholder="https://www.bilimanga.net/detail/54.html 或 54">
    <div class="btns">
      <button id="parseBtn">解析</button>
      <span class="muted" id="parseHint"></span>
    </div>
  </div>

  <!-- 选章 -->
  <div class="card hidden" id="bookCard">
    <h2>③ 选择要下载的章</h2>
    <div class="bookhead">
      <img id="cover" alt="" onerror="this.style.display='none'">
      <div>
        <div style="font-size:16px;font-weight:650" id="btitle"></div>
        <div class="muted" id="bauthor"></div>
        <div class="chips" id="btags"></div>
      </div>
    </div>
    <div class="btns" style="margin:14px 0 6px">
      <button class="ghost" id="selAll">全选</button>
      <button class="ghost" id="selNone">全不选</button>
    </div>
    <div id="vols"></div>
    <div class="btns">
      <button id="dlBtn">开始下载</button>
      <span class="muted" id="dlHint"></span>
    </div>
  </div>

  <!-- 进度 -->
  <div class="card hidden" id="progCard">
    <h2>④ 下载进度</h2>
    <div id="prog"></div>
    <div class="muted" id="progMsg" style="margin-top:10px"></div>
  </div>
</main>
<div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
let BOOK = null, poll = null;

function toast(msg){ const t=$('#toast'); t.className='toast'; t.textContent=msg;
  clearTimeout(t._t); t._t=setTimeout(()=>{t.textContent='';t.className='';},2600); }

async function api(path, method='GET', body){
  const opt = {method, headers:{'Content-Type':'application/json'}};
  if(body) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt); return r.json();
}

async function loadCfg(){
  const c = await api('/api/config');
  $('#site').value = c.site_url || '';
  $('#out').value = c.output_dir || '';
  $('#fmt').value = c.default_format || 'epub';
  $('#par').value = c.parallel_chapters || 12;
  $('#proxy').value = c.proxy || '';
  $('#cfgHint').textContent = '文件将保存到：' + (c.output_effective || '浏览器下载目录');
}

$('#saveCfg').onclick = async () => {
  const body = { site_url:$('#site').value, output_dir:$('#out').value,
    default_format:$('#fmt').value, parallel_chapters:$('#par').value, proxy:$('#proxy').value };
  const r = await api('/api/config','POST',body);
  if(r.ok){ toast('设置已保存'); loadCfg(); } else toast(r.error||'保存失败');
};
$('#reveal').onclick = async () => { const r=await api('/api/reveal','POST',{});
  if(!r.ok) toast('无法打开（请确认输出目录已设置且存在）'); };

$('#parseBtn').onclick = async () => {
  const input = $('#input').value.trim(); if(!input){ toast('请输入链接或书号'); return; }
  $('#parseBtn').disabled = true; $('#parseHint').textContent = '解析中……（首次需启动浏览器过 Cloudflare，约 10–20 秒）';
  const r = await api('/api/parse','POST',{input});
  $('#parseBtn').disabled = false; $('#parseHint').textContent = '';
  if(!r.ok){ toast(r.error||'解析失败'); return; }
  BOOK = r.book; renderBook();
};

function renderBook(){
  $('#bookCard').classList.remove('hidden');
  $('#btitle').textContent = BOOK.title;
  $('#bauthor').textContent = BOOK.author + '　共 ' + BOOK.volumes.length + ' 章';
  $('#cover').src = BOOK.cover_url || '';
  $('#btags').innerHTML = (BOOK.tags||[]).map(t=>`<span>${t}</span>`).join('');
  $('#vols').innerHTML = BOOK.volumes.map(v=>`
    <label class="vol"><input type="checkbox" class="volck" value="${v.index}" checked>
      <span class="name">${v.index}. ${escapeHtml(v.title)}</span>
      <span class="cnt">${v.chapters} 话</span></label>`).join('');
  $('#bookCard').scrollIntoView({behavior:'smooth'});
}
function escapeHtml(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

$('#selAll').onclick = ()=>document.querySelectorAll('.volck').forEach(c=>c.checked=true);
$('#selNone').onclick = ()=>document.querySelectorAll('.volck').forEach(c=>c.checked=false);

$('#dlBtn').onclick = async () => {
  const indices = [...document.querySelectorAll('.volck:checked')].map(c=>+c.value);
  if(!indices.length){ toast('请至少选一章'); return; }
  const r = await api('/api/download','POST',{indices, format:$('#fmt').value});
  if(!r.ok){ toast(r.error||'无法开始下载'); return; }
  $('#dlBtn').disabled = true; $('#progCard').classList.remove('hidden');
  $('#progCard').scrollIntoView({behavior:'smooth'});
  startPoll();
};

const PHASE = { wait:['等待','b-wait'], download:['下载中','b-download'],
  validate:['校对','b-validate'], package:['打包','b-package'],
  done:['完成','b-done'], empty:['无内容','b-empty'] };

function startPoll(){ if(poll) clearInterval(poll); poll = setInterval(tick, 800); tick(); }
async function tick(){
  const s = await api('/api/state');
  $('#prog').innerHTML = (s.volumes||[]).map(v=>{
    const pct = v.total ? Math.min(100, Math.round(v.done/v.total*100)) : (v.phase==='done'?100:0);
    const [txt,cls] = PHASE[v.phase] || PHASE.wait;
    const tail = v.output ? ` → ${escapeHtml(v.output)}` : (v.total?` ${v.done}/${v.total}`:'');
    return `<div class="pv"><div class="top"><span>${escapeHtml(v.title)}</span>
      <span class="badge ${cls}">${txt}${tail}</span></div>
      <div class="bar"><i style="width:${pct}%"></i></div></div>`;
  }).join('');
  if(s.status==='done'){ clearInterval(poll); poll=null; $('#dlBtn').disabled=false;
    $('#progMsg').innerHTML = '✅ '+ (s.message||'完成') +
      (s.outputs&&s.outputs.length?`　<a href="#" id="rev2">打开输出目录</a>`:'');
    const rev=$('#rev2'); if(rev) rev.onclick=(e)=>{e.preventDefault();api('/api/reveal','POST',{});};
    toast('下载完成'); }
  else if(s.status==='error'){ clearInterval(poll); poll=null; $('#dlBtn').disabled=false;
    $('#progMsg').textContent = '❌ '+(s.error||'下载出错'); toast('下载出错'); }
  else { $('#progMsg').textContent = s.message||'下载中……'; }
}

loadCfg();
</script>
</body>
</html>
"""
