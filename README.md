# GPT Protocol Register — 极速协议版控制台

> ChatGPT 账号**纯 HTTP 协议注册**（不开浏览器，20~35 秒/号，注册即带密码）。
> 与浏览器版（gpt-auto-register）共享账号数据库，两者互补成完整方案。
> 本文件面向"接手的开发者或 AI"：读完即可理解原理、当前状态与操作方式。

---

## 1. 这个项目是干嘛的

用**纯 HTTP 协议**（模拟 Chrome TLS 指纹 + Sentinel PoW 反爬）直接调用 OpenAI
官方注册接口，批量创建**自带密码**的 ChatGPT 账号。

**与浏览器版的核心差异**：

| | 协议版（本项目） | 浏览器版（gpt-auto-register） |
|---|---|---|
| 速度 | ⚡ 20~38 秒/号 | 45~115 秒/号 |
| 资源 | <50MB，无浏览器 | ~500MB/实例 Chrome |
| 密码 | ✅ **注册即带** | ⚠️ 官方免密分支，多数无密码 |
| 2FA 密钥 | ❌ 需浏览器补全 | ✅ 会话内自动 |
| 过 CF | ⚠️ 依赖 TLS 指纹档位与 IP 质量 | ✅ 真实浏览器稳定通过 |
| 适用 | 批量产号主力 | 稳定兜底 + 补全 2FA/权益 |

**推荐组合拳**：协议版批量注册（快+带密码）→ 面板「⚡完善」（浏览器会话补 2FA+权益）→ 报表。

## 2. 当前状态（真机验证）

| 能力 | 状态 | 说明 |
|---|---|---|
| 协议注册 | ✅ **真机验证** | 35 秒/号，官方密码校验通过（`page_type=login_password` 密码正确） |
| **注册即带密码** | ✅ **真机验证** | `POST /api/accounts/user/register` 直接传 `{"username","password"}` |
| **TLS 指纹过 CF** | ✅ **已攻克** | chrome150 档位 + UA/sec-ch-ua 三者一致（详见下文原理） |
| 纯协议密码登录状态机 | ✅ 代码完成 | `authorize/continue` → `password/verify` → `mfa/verify` → callback |
| 纯协议权益检测 | ⚠️ 受限 | `/api/auth/session` 对纯 HTTP 返回的 body 无 accessToken（需浏览器 cookie） |

## 3. 核心原理：如何过 Cloudflare（最重要的知识）

CF 判断的是「**TLS 指纹档位**」与「**请求头声称的 Chrome 版本**」是否**一致且够新**。
公开协议实现的对照实验数据：

```
TLS136 + 声称136 → 过闸 0/8      ← 版本太老直接死
TLS142 + 声称142 → 过闸 8/8      ← 一致且够新就能过
TLS142 + 声称152 → 过闸 0/6      ← 不一致即死（哪怕只差几个版本）
Chrome153 (wreq) → 5/5           ← 档位越新越好
```

因此本项目的实现（`protocol_register.py` 顶部）：

```python
IMPERSONATE = _resolve_impersonate()   # 自动选 curl_cffi 支持的最高 chrome 档位
CHROME_VERSION = 150                    # 从档位名提取版本号
CHROME_UA = f"...Chrome/{CHROME_VERSION}.0.0.0..."      # UA 同步
SEC_CH_UA = f'"Google Chrome";v="{CHROME_VERSION}"..."'  # sec-ch-ua 同步
```

**三者严格一致**是过闸的关键。curl_cffi 需 ≥0.16（支持 chrome150）；
可用环境变量 `PROTOCOL_IMPERSONATE` 强制指定档位。

**⚠️ IP 质量仍是前提**：TLS 指纹对了能过 CF 的"指纹检查"，
但**被 OpenAI 标记的 IP** 依然会 429/403（实测：同一 IP 连续 10~20 个号后触发）。
换节点 = 换 IP = 配额重置。

## 4. 注册流程（状态机）

```
[1] warm_up          GET chatgpt.com/auth/login     ← 拿 CF clearance + oai-did
[2] get_csrf         GET /api/auth/csrf
[3] signin_init      POST /api/auth/signin/openai?login_hint=...
[4] jump_to_auth     GET auth.openai.com/authorize...（建立 auth 会话）
[5] register_user    POST /api/accounts/user/register
                     {"username": email, "password": password}   ← 带密码注册
[6] send_email_otp   GET continue_url（触发发码）
[7] validate_otp     POST /api/accounts/email-otp/validate
[8] create_profile   POST /api/accounts/create_account（姓名+年龄）
[9] complete_oauth_callback  GET callback → 建立 chatgpt.com 会话
[10] get_web_access_token    GET /api/auth/session → accessToken
[11] bind_2fa        候选端点探测（见已知限制）
[13] collect_state   GET /backend-api/{me,accounts/check} → 真实权益
[14] save_to_txt     账号 + 密码 + 2FA + 权益 → 共享账号库
```

## 5. 纯协议密码登录状态机（用于老账号重登/补2FA）

`ChatGPTProtocolRegister._protocol_password_login(email, password)`：

```
warmup → csrf → signin(login_hint) → jump_to_auth
→ authorize/continue (screen_hint=login)
→ 状态机循环（最多8步）：
    login_password      → POST /api/accounts/password/verify {"password"}
    mfa-challenge       → POST /api/accounts/mfa/verify {"code","type":"totp","id"}
                          （需本地已存 2FA 密钥）
    email-verification  → 触发发码 + 收件箱回调取码 → validate_otp
    add-phone           → ⚠️ 纯协议无法自动过（需人工/浏览器）
    完成                → complete_oauth_callback → session 建立
```

`authorize/continue` 的三个硬性前置（实测 409 的原因）：
1. 必须先 warmup 拿到 `oai-did` cookie
2. 必须走完 chatgpt.com 的 csrf→signin→auth 跳转链
3. Sentinel token 必须用本请求新算的（复用旧 token 是异常特征）

## 6. 技术栈

| 层 | 技术 |
|---|---|
| HTTP 引擎 | curl_cffi ≥0.16（chrome150 TLS 档位，impersonate） |
| 反爬 | Sentinel FNV-1a PoW（本地求解，`sentinel.py`） |
| 后端 | Python 3.12+ / Flask / Waitress（端口 8889） |
| 2FA | RFC 6238 TOTP（与浏览器版同一实现） |
| 共享模块 | account_probe / account_checker / two_factor_service / stored_accounts / browser / account_perfector / utils 等 —— **与浏览器版字节一致**，由 `scripts/sync_shared_modules.py` 同步 |

## 7. 快速开始

```powershell
# 启动控制台
..\gpt-auto-register\.venv\Scripts\python.exe server.py
# 打开 http://localhost:8889

# 一键全自动（协议注册 → 浏览器补全 → 检测 → 报表）
..\gpt-auto-register\.venv\Scripts\python.exe -m app.main --auto --count 3

# 纯极速模式（跳过浏览器补全：只注册+检测，最快）
..\gpt-auto-register\.venv\Scripts\python.exe -m app.main --auto --count 10 --no-completion --stagger 180

# 批量真机注册（带密码验证）
python ..\scripts\live_protocol_batch.py --accounts-file <账号文件> --proxy 127.0.0.1:7897
```

## 8. 账号文件格式

每行一个账号（`----` 分隔）：

```
email@icloud.com----https://icloud-api.top/s/<token>/email@icloud.com
```

icloud 渠道的收件箱读取依赖 `icloud_service.py`（拉取收件箱 HTML 提取验证码）。

## 9. 已知限制（实测）

1. **IP 质量**：机房/共享代理 IP 下 CF 入口即拦（需纯净住宅 IP 或低风控节点）
2. **单 IP 配额**：实测连续 4 个无拦截；社区经验 10~20/天后开始 429。换节点=重置
3. **2FA 绑定**：协议端点拿不到密钥（官方限制在 Web 会话）；流水线自动转浏览器补全
4. **add-phone**：部分新号登录时官方要求添加手机号——纯协议无法自动过
5. **权益检测**：纯 HTTP 拿不到 accessToken；需浏览器 cookie 会话（流水线自动转）

## 10. 测试与同步

```powershell
# 51 项测试
..\gpt-auto-register\.venv\Scripts\python.exe -m unittest discover tests

# 共享模块一致性（浏览器版为源本）
..\scripts\sync_shared_modules.py --check
```

## 11. 数据安全

账号库指向 `../gpt-auto-register/data/accounts/registered_accounts.txt`（共享），
含真实凭据，**不入库**。GitHub 发布自动排除 data/token_exports 等敏感目录。
