"""百度网盘（Cookie/BDUSS 非官方方式）：登录抓取、账号校验、上传。

⚠️ 说明：走网页/PCS 非官方接口，**违反百度网盘服务条款**，且百度改版/风控可能
随时使其失效；``baidu_cookie`` 里保存的 BDUSS 等于账号完整凭证，请勿外泄。仅供个人
把自己下载的内容备份到自己的网盘。上传实现见 :meth:`BaiduClient.upload_file`。

本模块只依赖 curl_cffi（发请求）与调用方传入的浏览器（抓登录 cookie）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

from curl_cffi import CurlMime
from curl_cffi import requests as cffi

from ..core.logutil import get_logger

BLOCK_SIZE = 4 * 1024 * 1024  # 百度分片上传固定 4MB/块

log = get_logger("baidu")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
LOGIN_URL = "https://passport.baidu.com/v2/?login&tpl=netdisk&u=https%3A%2F%2Fpan.baidu.com%2F"
# 登录后这些 cookie 才算“真登录态”，其中 BDUSS 是核心。
_READY_COOKIE = "BDUSS"


# ---------------- 桌面通知 ----------------
def desktop_notify(title: str, message: str) -> None:
    """尽力发一条系统桌面通知；失败则退回控制台打印。"""
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification {message!r} with title {title!r}'],
                check=False)
            return
        if sys.platform.startswith("win"):
            ps = ("powershell", "-NoProfile", "-Command",
                  "[reflection.assembly]::LoadWithPartialName('System.Windows.Forms')"
                  ">$null; [System.Windows.Forms.MessageBox]::Show("
                  f"'{message}','{title}')")
            subprocess.run(ps, check=False)
            return
        subprocess.run(["notify-send", title, message], check=False)
    except Exception:  # noqa: BLE001
        pass
    print(f"[{title}] {message}", flush=True)


# ---------------- 百度网盘客户端 ----------------
class BaiduClient:
    def __init__(self, cookie: str):
        self.cookie = cookie or ""
        self.bdstoken: Optional[str] = None
        self.session = cffi.Session(impersonate="chrome")
        self.session.headers.update({
            "User-Agent": _UA,
            "Referer": "https://pan.baidu.com/disk/home",
            "Cookie": self.cookie,
        })

    # 校验登录态并取昵称 + bdstoken（上传要用）。失败返回 None。
    def verify(self) -> Optional[str]:
        try:
            r = self.session.get(
                "https://pan.baidu.com/api/gettemplatevariable",
                params={"clienttype": "0", "app_id": "250528",
                        "fields": '["username","bdstoken"]'},
                timeout=15)
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("百度校验请求失败：%s", exc)
            return None
        if j.get("errno") == 0 and isinstance(j.get("result"), dict):
            self.bdstoken = j["result"].get("bdstoken")
            return j["result"].get("username") or "百度用户"
        log.warning("百度校验失败：errno=%s", j.get("errno"))
        return None

    def _bduss(self) -> str:
        for part in self.cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "BDUSS":
                return v
        return ""

    def upload_file(self, local_path: str, remote_path: str) -> str:
        """分片上传一个本地文件到网盘 ``remote_path``（自动建父目录）。

        流程：precreate（预创建，报块 md5）→ superfile2（逐块上传）→ create（合并）。
        非官方接口，errno 非 0 即抛错。
        """
        if not self.bdstoken and self.verify() is None:
            raise RuntimeError("百度登录态无效（无法取得 bdstoken），请重新连接。")
        size = os.path.getsize(local_path)
        blocks = _block_md5s(local_path)
        block_json = json.dumps(blocks)
        common = {"channel": "chunlei", "web": "1", "app_id": "250528",
                  "bdstoken": self.bdstoken, "clienttype": "0"}

        pre = self.session.post(
            "https://pan.baidu.com/api/precreate", params=common,
            data={"path": remote_path, "size": size, "isdir": "0",
                  "autoinit": "1", "rtype": "3", "block_list": block_json},
            timeout=30).json()
        if pre.get("errno") != 0:
            raise RuntimeError(f"precreate 失败 errno={pre.get('errno')}")
        uploadid = pre.get("uploadid", "")
        to_upload = pre.get("block_list")
        if not to_upload and to_upload != []:
            to_upload = list(range(len(blocks)))

        bduss = self._bduss()
        with open(local_path, "rb") as f:
            for seq in to_upload:
                f.seek(seq * BLOCK_SIZE)
                chunk = f.read(BLOCK_SIZE)
                mp = CurlMime()
                mp.addpart(name="file", filename="blob",
                           content_type="application/octet-stream", data=chunk)
                r = self.session.post(
                    "https://c.pcs.baidu.com/rest/2.0/pcs/superfile2",
                    params={"method": "upload", "app_id": "250528",
                            "channel": "chunlei", "clienttype": "0", "web": "1",
                            "BDUSS": bduss, "path": remote_path,
                            "uploadid": uploadid, "partseq": seq},
                    multipart=mp, timeout=180)
                jr = r.json()
                if "md5" not in jr:
                    raise RuntimeError(f"分块 {seq} 上传失败：{jr}")

        cr = self.session.post(
            "https://pan.baidu.com/api/create", params=common,
            data={"path": remote_path, "size": size, "isdir": "0",
                  "block_list": block_json, "uploadid": uploadid, "rtype": "3"},
            timeout=30).json()
        if cr.get("errno") != 0:
            raise RuntimeError(f"create 合并失败 errno={cr.get('errno')}")
        return remote_path


# ---------------- 现场登录抓取（浏览器窗口登录一次，经调试接口只抓当次 cookie）----------------
def capture_login(net, timeout: int = 240,
                  on_status=None) -> Tuple[Optional[str], Optional[str]]:
    """打开一个浏览器窗口到百度登录页，等用户登录一次；登录后经调试接口读取到
    BDUSS 且校验通过即返回 ``(cookie_str, nickname)``，超时/取消返回 ``(None, None)``。

    只抓“这次登录”产生的 cookie，不读取系统 cookie/密码库。该窗口用独立资料目录、
    登录态持久化，之后无需再登。
    """
    def _say(msg):
        if on_status:
            on_status(msg)

    try:
        net.open_login(LOGIN_URL)
    except Exception as exc:  # noqa: BLE001
        _say(f"无法打开浏览器登录：{exc}")
        return None, None
    _say("已打开浏览器窗口，请在里面登录百度账号（扫码/账号密码均可）……")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cookies = net.browser_cookies()
        except Exception:  # noqa: BLE001
            cookies = {}
        if cookies.get(_READY_COOKIE):
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
            nick = BaiduClient(cookie_str).verify()
            if nick:
                try:
                    net.finish_login()
                except Exception:  # noqa: BLE001
                    pass
                return cookie_str, nick
        time.sleep(2)

    try:
        net.finish_login()
    except Exception:  # noqa: BLE001
        pass
    _say("登录超时（未检测到登录态）。")
    return None, None


# ---------------- 分片 md5 + 存储插件 ----------------
def _block_md5s(path: str) -> List[str]:
    md5s: List[str] = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(BLOCK_SIZE)
            if not chunk:
                break
            md5s.append(hashlib.md5(chunk).hexdigest())
    return md5s or [hashlib.md5(b"").hexdigest()]


from .base import Storage  # noqa: E402
from ..core.registry import storages  # noqa: E402
from ..downloader import safe_name  # noqa: E402


@storages.register
class BaiduStorage(Storage):
    """百度网盘存储：成品先落临时目录，逐卷上传到 <base>/<分类>/<书名>/ 后删本地。"""
    name = "baidu"
    label = "上传到百度网盘"

    def __init__(self, config):
        self.config = config
        self.client = BaiduClient(config.baidu_cookie or "")

    def is_ready(self) -> bool:
        return bool(self.config.baidu_cookie)

    def status_label(self) -> str:
        nick = self.config.baidu_nickname
        return f"百度网盘（{nick}）" if nick else "百度网盘"

    def stage_dir(self, category: str, book_title: str) -> Path:
        return Path(tempfile.mkdtemp(prefix="bili_up_"))

    def commit(self, path: Path, category: str, book_title: str) -> str:
        base = (self.config.baidu_upload_base or "/bilidownloader").rstrip("/")
        remote = f"{base}/{category}/{safe_name(book_title)}/{path.name}"
        self.client.upload_file(str(path), remote)
        try:
            path.unlink()          # 上传成功后删临时文件
        except Exception:  # noqa: BLE001
            pass
        return f"百度网盘:{remote}"
