# 连接 OneDrive（把下载的书上传到你自己的 OneDrive）

OneDrive 走微软**官方 Graph API + OAuth2**，每个人用**自己的微软账号**登录、上传到
**自己的** OneDrive。分两种用法：

---

## 方式一：开箱即用（推荐，零注册）

程序内置了一个微软官方公共应用（Microsoft Graph 命令行工具），**你不用注册任何东西**，
直接登录自己的微软账号即可：

1. **命令行版**：主界面输入 `o` → 程序会显示一行提示，例如：

   > 请在浏览器打开 https://microsoft.com/devicelogin ，输入验证码：**ABCD-1234**

   在浏览器打开那个网址（你平时登录微软/OneDrive 的浏览器最方便），输入验证码，登录你的
   微软账号，勾选**同意**（授权“读写你的 OneDrive 文件”）。回到程序，几秒后会弹桌面通知
   「OneDrive 已连接」。

2. **油猴脚本版**：下载面板「保存到」选 **☁ OneDrive** → 点「连接」，同样打开设备登录页、
   输码、同意即可。

连接一次**长期有效**（自动续期），以后下载时在「保存到」里选 OneDrive 就会上传到
`OneDrive/bilidownloader/漫画|小说/书名/` 下（根路径可在设置里改）。

> 说明：登录时页面显示的应用名是「Microsoft Graph 命令行工具」，这是微软官方的公共应用；
> 授权只包含**读写你自己的 OneDrive 文件**，不涉及邮件/联系人等。随时可在
> [账户的已授权应用](https://account.live.com/consent/Manage) 里撤销。

---

## 方式二：用你自己注册的应用（更稳，可选）

内置公共应用是所有人共用的，理论上可能被微软限流。如果你重度使用、想要最稳，可以花
**2 分钟免费注册一个属于你自己的应用**，把它的 `client_id` 填进程序设置里覆盖默认值：

1. 打开 [Azure 门户 · 应用注册](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
   （用你的微软账号登录，**免费，不需要付费 Azure 订阅**）。
2. 点 **「新注册」**：
   - **名称**：随便填，例如 `my-bili-downloader`。
   - **受支持的账户类型**：选 **「任何组织目录中的账户 + 个人 Microsoft 账户」**
     （这样个人 OneDrive 才能用）。
   - 重定向 URI **留空**，点 **「注册」**。
3. 注册后在「概述」页复制 **「应用程序(客户端) ID」** —— 这就是你的 `client_id`。
4. 左侧进 **「身份验证」** → 拉到最下面 **「高级设置 · 允许公共客户端流」** 改为 **「是」**
   → **保存**。（设备码登录必须开这个）

完成后：
- **命令行版**：主界面输入 `s` 进设置 → 找到「OneDrive client_id」→ 粘贴你的 client_id →
  保存。然后输入 `o` 登录。
- **油猴脚本版**：面板选 OneDrive → 在 client_id 输入框粘贴 → 连接。

> 不需要建客户端密钥（secret）、不需要配重定向、也不需要预先添加 API 权限——`Files.ReadWrite`
> 的授权会在你登录时动态弹出让你勾同意。

---

## 常见问题

- **登录时提示需要管理员同意 / 组织策略拦截**：说明你用的是**公司/学校账号**且被管理员限制。
  换成**个人微软账号**，或让管理员放行，或用方式二注册自己的应用。
- **中国世纪互联版 OneDrive（由世纪互联运营）**：端点不同（`partner.microsoftonline.cn` /
  `microsoftgraph.chinacloudapi.cn`），当前内置的是**国际版**端点，世纪互联版暂不支持。
- **想换账号 / 断开**：设置里「断开 OneDrive」即可清除本地登录态，再重新 `o` 登录别的账号。
- **凭证安全**：登录后本地只保存一个 `refresh_token`（等于账号访问凭证），请勿外泄；它只能
  读写你的 OneDrive 文件。

有问题欢迎到 [Issues](https://github.com/zhtinist/Bilimanga-Downloader/issues) 反馈。
