"""OneDrive（微软 Graph API，OAuth2 设备码方式）：登录、账号校验、分片上传。

与百度不同，OneDrive 走**官方** Graph API + OAuth2。每个用户用自己的账号：自己在
Azure 免费注册一个「公共客户端」应用拿 ``client_id``（3 步，见 README），填进设置。
登录用**设备码流程**（device code）：程序给出一个网址 + 验证码，用户在浏览器登录并
同意 OneDrive 读写，程序即拿到 ``refresh_token``（长期免登录，自动续期）。

⚠️ ``onedrive_refresh_token`` 等于账号访问凭证，保存在本地，请勿外泄。上传见
:meth:`OneDriveClient.upload_file`。
"""

from __future__ import annotations

import os
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple

from curl_cffi import requests as cffi

from ..core.logutil import get_logger
from .baidu import desktop_notify  # 复用桌面通知

log = get_logger("onedrive")

# 默认 client_id：微软一方公共 App「Microsoft Graph 命令行工具」，天然支持设备码 +
# Files.ReadWrite，**零注册开箱即用**——用户只需扫码/登录自己的微软账号即可。想更稳可在
# 设置里填自己注册的 client_id 覆盖它（见 README 的 3 步注册）。
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

# 配置/连接教程（GitHub），未连接想用时给用户打开看。
DOC_URL = "https://github.com/zhtinist/Bilimanga-Downloader/blob/main/docs/onedrive.md"

# 设备码上传分片必须是 320 KiB 的整数倍；取 10 MiB（= 32 × 320KiB）。
CHUNK_SIZE = 10 * 1024 * 1024
# 个人 + 组织账户通用端点（common）。
_AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
_DEVICECODE_URL = _AUTHORITY + "/devicecode"
_TOKEN_URL = _AUTHORITY + "/token"
_GRAPH = "https://graph.microsoft.com/v1.0"
# offline_access 换 refresh_token；Files.ReadWrite 读写自己的 OneDrive。
_SCOPE = "offline_access Files.ReadWrite User.Read"


class OneDriveClient:
    def __init__(self, client_id: str = "", refresh_token: str = ""):
        self.client_id = client_id or DEFAULT_CLIENT_ID
        self.refresh_token = refresh_token or ""
        self.access_token: Optional[str] = None
        self.session = cffi.Session(impersonate="chrome")

    # 用 refresh_token 换 access_token（顺带更新可能轮换的 refresh_token）。
    def _refresh(self) -> bool:
        if not (self.client_id and self.refresh_token):
            return False
        try:
            r = self.session.post(_TOKEN_URL, data={
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "scope": _SCOPE,
            }, timeout=30)
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("OneDrive 刷新令牌失败：%s", exc)
            return False
        if "access_token" in j:
            self.access_token = j["access_token"]
            if j.get("refresh_token"):
                self.refresh_token = j["refresh_token"]
            return True
        log.warning("OneDrive 刷新令牌被拒：%s", j.get("error"))
        return False

    def _auth_headers(self) -> dict:
        if not self.access_token and not self._refresh():
            raise RuntimeError("OneDrive 登录态无效，请重新连接。")
        return {"Authorization": f"Bearer {self.access_token}"}

    # 校验并取账号显示名（邮箱/名字）。失败返回 None。
    def verify(self) -> Optional[str]:
        # 已有 access_token 就直接用，避免多余刷新（个人账号 refresh_token 用一次即轮换，
        # 多刷一次会把刚拿到的令牌作废）。
        if not self.access_token and not self._refresh():
            return None
        try:
            r = self.session.get(_GRAPH + "/me", headers=self._auth_headers(), timeout=20)
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("OneDrive 校验失败：%s", exc)
            return None
        name = j.get("displayName") or j.get("userPrincipalName") or j.get("mail")
        return name or "OneDrive 用户"

    @staticmethod
    def _encode_path(remote_path: str) -> str:
        # 去掉首尾多余斜杠，逐段 URL 编码（空格/中文），用于 /drive/root:/<path>:
        clean = "/".join(seg for seg in remote_path.split("/") if seg)
        return urllib.parse.quote(clean, safe="/")

    def upload_file(self, local_path: str, remote_path: str) -> str:
        """上传本地文件到 OneDrive ``remote_path``（自动建父目录）。

        小文件（<4MB）直接 PUT；大文件走 createUploadSession 分片上传。
        """
        size = os.path.getsize(local_path)
        enc = self._encode_path(remote_path)
        if size < 4 * 1024 * 1024:
            with open(local_path, "rb") as f:
                data = f.read()
            r = self.session.put(
                f"{_GRAPH}/me/drive/root:/{enc}:/content",
                headers={**self._auth_headers(),
                         "Content-Type": "application/octet-stream"},
                data=data, timeout=120)
            if r.status_code in (200, 201):
                return remote_path
            raise RuntimeError(f"OneDrive 上传失败 HTTP {r.status_code}: {r.text[:200]}")

        # 大文件：创建上传会话，逐块 PUT。
        sess = self.session.post(
            f"{_GRAPH}/me/drive/root:/{enc}:/createUploadSession",
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            timeout=30).json()
        upload_url = sess.get("uploadUrl")
        if not upload_url:
            raise RuntimeError(f"OneDrive 创建上传会话失败：{sess}")

        with open(local_path, "rb") as f:
            start = 0
            while start < size:
                chunk = f.read(CHUNK_SIZE)
                end = start + len(chunk) - 1
                headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                }
                # 上传会话 URL 自带鉴权，不用再带 Authorization。失败重试几次。
                for attempt in range(4):
                    try:
                        r = self.session.put(upload_url, headers=headers,
                                              data=chunk, timeout=180)
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 3:
                            raise RuntimeError(f"OneDrive 分片上传异常：{exc}")
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    if r.status_code in (200, 201, 202):
                        break
                    if attempt == 3:
                        raise RuntimeError(
                            f"OneDrive 分片上传失败 HTTP {r.status_code}: {r.text[:200]}")
                    time.sleep(1.5 * (attempt + 1))
                start = end + 1
        return remote_path


# ---------------- 设备码登录 ----------------
def device_code_login(client_id: str, on_status=None,
                      timeout: int = 300) -> Tuple[Optional[str], Optional[str]]:
    """设备码流程登录。返回 ``(refresh_token, account_name)``；失败/超时返回 (None, None)。

    ``on_status`` 用于把「打开这个网址 + 输入验证码」的提示回传给界面。
    """
    def _say(msg):
        if on_status:
            on_status(msg)

    client_id = client_id or DEFAULT_CLIENT_ID   # 未填则用内置公共 App（零注册）
    sess = cffi.Session(impersonate="chrome")
    try:
        dc = sess.post(_DEVICECODE_URL,
                       data={"client_id": client_id, "scope": _SCOPE},
                       timeout=30).json()
    except Exception as exc:  # noqa: BLE001
        _say(f"发起设备码登录失败：{exc}")
        return None, None
    if "device_code" not in dc:
        _say(f"发起设备码登录被拒：{dc.get('error_description') or dc.get('error')}")
        return None, None

    user_code = dc["user_code"]
    verify_uri = dc.get("verification_uri") or "https://microsoft.com/devicelogin"
    interval = int(dc.get("interval", 5))
    _say(f"请在浏览器打开 {verify_uri} ，输入验证码：{user_code}（登录并同意 OneDrive 读写）")
    desktop_notify("连接 OneDrive", f"打开 {verify_uri} 输入验证码 {user_code}")

    device_code = dc["device_code"]
    deadline = time.time() + min(timeout, int(dc.get("expires_in", 900)))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            tok = sess.post(_TOKEN_URL, data={
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            }, timeout=30).json()
        except Exception:  # noqa: BLE001
            continue
        if "access_token" in tok:
            # 用设备码这次直接拿到的 access_token 校验（不再刷新，避免把 refresh_token 提前
            # 轮换作废）；返回的 refresh_token 就是这次登录拿到的、仍然有效的那个。
            client = OneDriveClient(client_id, tok.get("refresh_token", ""))
            client.access_token = tok["access_token"]
            name = client.verify() or "OneDrive 用户"
            return client.refresh_token, name
        err = tok.get("error")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval += 5
            continue
        # authorization_declined / expired_token / bad_verification_code 等
        _say(f"登录未完成：{tok.get('error_description') or err}")
        return None, None

    _say("登录超时（未在有效期内完成）。")
    return None, None


# ---------------- 存储插件 ----------------
import tempfile  # noqa: E402

from .base import Storage  # noqa: E402
from ..core.registry import storages  # noqa: E402
from ..downloader import safe_name  # noqa: E402


@storages.register
class OneDriveStorage(Storage):
    """OneDrive 存储：成品先落临时目录，逐卷上传到 <base>/<分类>/<书名>/ 后删本地。"""
    name = "onedrive"
    label = "上传到 OneDrive"

    def __init__(self, config):
        self.config = config
        self.client = OneDriveClient(config.onedrive_client_id,
                                     config.onedrive_refresh_token)

    def is_ready(self) -> bool:
        # client_id 有内置默认，故只要有 refresh_token（登录过）即可用。
        return bool(self.config.onedrive_refresh_token)

    def status_label(self) -> str:
        acct = self.config.onedrive_account
        return f"OneDrive（{acct}）" if acct else "OneDrive"

    def stage_dir(self, category: str, book_title: str) -> Path:
        return Path(tempfile.mkdtemp(prefix="od_up_"))

    def commit(self, path: Path, category: str, book_title: str) -> str:
        base = (self.config.onedrive_upload_base or "/bilidownloader").rstrip("/")
        remote = f"{base}/{category}/{safe_name(book_title)}/{path.name}"
        self.client.upload_file(str(path), remote)
        # 上传成功后若 refresh_token 轮换了，写回配置。
        if self.client.refresh_token and self.client.refresh_token != self.config.onedrive_refresh_token:
            self.config.onedrive_refresh_token = self.client.refresh_token
            self.config.save()
        try:
            path.unlink()
        except Exception:  # noqa: BLE001
            pass
        return f"OneDrive:{remote}"
