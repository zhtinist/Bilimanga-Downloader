"""百度网盘（Cookie/BDUSS 非官方方式）：登录抓取、账号校验、上传。

⚠️ 说明：走网页/PCS 非官方接口，**违反百度网盘服务条款**，且百度改版/风控可能
随时使其失效；``baidu_cookie`` 里保存的 BDUSS 等于账号完整凭证，请勿外泄。仅供个人
把自己下载的内容备份到自己的网盘。上传实现见 :meth:`BaiduClient.upload_file`。

本模块只依赖 curl_cffi（发请求）与调用方传入的浏览器（抓登录 cookie）。
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Optional, Tuple

from curl_cffi import requests as cffi

from ..core.logutil import get_logger

log = get_logger("baidu")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
LOGIN_URL = "https://pan.baidu.com/"
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


# ---------------- 登录抓取（复用调用方的浏览器）----------------
def capture_login(net, timeout: int = 240,
                  on_status=None) -> Tuple[Optional[str], Optional[str]]:
    """打开有头浏览器到百度登录页，轮询到出现 BDUSS 且校验通过后返回
    ``(cookie_str, nickname)``；超时/取消返回 ``(None, None)``。

    :param net: :class:`net.Net`，需能开有头浏览器并读取其 cookie。
    """
    def _say(msg):
        if on_status:
            on_status(msg)

    try:
        net.open_login(LOGIN_URL)
    except Exception as exc:  # noqa: BLE001
        _say(f"无法打开浏览器登录：{exc}")
        return None, None
    _say("已打开浏览器，请在弹出的窗口里登录百度账号……")

    deadline = time.time() + timeout
    while time.time() < deadline:
        cookies = {}
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
