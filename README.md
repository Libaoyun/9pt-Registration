# GPT Auto Register — 浏览器自动化版控制台

> ChatGPT 账号全自动批量注册 / 真实凭据捕获 / 官方权益检测 / 一键报表。
> 本文件面向"接手的开发者或 AI"：读完即可理解项目目标、当前状态、架构与操作方式。

---

## 1. 这个项目是干嘛的

自动化完成 ChatGPT 账号的**全生命周期**：

```
批量注册 → 真实 Web 凭据捕获 → 2FA(TOTP) 绑定 → 官方接口权益检测 → 最终报表
   ①            ②                ③                ④              ⑤
```

一次点击（面板 🤖 按钮）或一条命令即可走完全程，最终产出：
- `data/accounts/registered_accounts.txt` — 账号主数据库（管道分隔，向下兼容）
- `data/tokens/web-<邮箱>.json` — 真实 Web 凭据（accessToken + cookies）
- `data/tokens/state-<邮箱>.json` — 每账号的官方接口原始报文证据
- `token_exports/final_<时间戳>/` — 报表（JSON / CSV / Markdown / 5横杠txt）

**数据真实性原则（本项目最高约束）**：所有字段只写官方接口真实返回的内容；
拿不到证据就写"未知"，绝不根据免费计划、邀请链接或注册成功推断权益。

## 2. 当前状态（截至最后真机验证）

| 能力 | 状态 | 说明 |
|---|---|---|
| 批量注册（浏览器引擎） | ✅ 真机验证 | 20+ 账号真机注册成功，45~115 秒/号 |
| 真实 Web 凭据捕获 | ✅ 真机验证 | accessToken(约2000字符) + 分块 session cookie |
| 会话恢复（免验证码） | ✅ 真机验证 | 注入 cookie → `/api/auth/session` 返回有效会话 |
| 官方权益检测 | ✅ 真机验证 | plan/is_plus/trial/expires 全部来自官方接口 |
| 32 位 2FA 密钥获取 | ✅ 接口层验证 | `enroll` 接口 200 返回真实密钥；「激活确认」见已知限制 |
| 密码登录 | ⚠️ 交互已通 | 「使用密码继续」→ 填密码 → 提交 全链路已通；最终确认受 IP 风控影响 |
| 免密账号设密码 | ⚠️ 受限 | 官方对被标记 IP 拒发验证码（原话有记录） |

**已知硬限制（官方侧，非代码问题）**：
1. MFA enroll 需要浏览器会话 + `recent_auth_required`（必须"刚认证过"）
2. 被标记 IP 上官方安全设置页返回「无法获取你的部分安全设置」
3. 免密新号登录时官方常拒发邮箱验证码

## 3. 架构与数据流

```
app/
├── main.py               # 浏览器版注册主流程（单账号全流程）
├── server.py             # Flask 控制台（端口 8888）：面板 + 全部 API
├── pipeline.py           # 全自动流水线编排：①注册→②补全→③检测→④报表
├── browser.py            # 浏览器自动化核心（undetected-chromedriver）
├── account_probe.py      # 真实状态采集器（证据优先，官方接口返回什么就存什么）
├── account_checker.py    # 检测层：legacy兼容 + probe桥接 + 证据落盘
├── account_perfector.py  # 账号完善：登录/凭据/2FA/权益/密码
├── two_factor_service.py # 真实 2FA(TOTP)：接口通道 + UI 兜底
├── stored_accounts.py    # 账号库读写（跨进程锁，向下兼容 12 字段）
├── email_providers.py    # 邮箱服务注册表（icloud/mailtm/gptmail/tempmail_lol/...）
├── token_batch_service.py# 批量补取 Codex Token
├── oauth_service.py      # Codex OAuth + CLIProxyAPI 推送
├── config.py             # 配置加载（config.yaml）
└── utils.py              # 工具函数
static/                    # 前端（原生 HTML/CSS/JS，无框架）
tests/                     # 74 项单元/回归测试
```

**关键数据流**：
- 注册 → `main.register_one_account()` → 产出账号记录 + Web 凭据 + 2FA 密钥 + 权益
- 检测 → `account_checker.refresh_account_state()` → 更新记录 + 证据文件
- 报表 → `pipeline.build_report()` → 汇总全部记录 → 四种格式落盘

## 4. 技术栈

| 层 | 技术 |
|---|---|
| 浏览器自动化 | undetected-chromedriver（真 Chrome，过 Cloudflare） |
| 后端 | Python 3.12+ / Flask / Waitress（端口 8888） |
| HTTP 客户端 | curl_cffi 0.16（chrome150 TLS 档位，用于 Bearer 直连） |
| 2FA | RFC 6238 TOTP（pyotp 优先 + 标准库 HMAC-SHA1 兜底） |
| 前端 | 原生 HTML/CSS/JS；动态码 = WebCrypto HMAC-SHA1 前端实时计算 |
| 测试 | unittest（74 项）+ verify_frontend.cjs（Node） |

## 5. 快速开始

```powershell
# 启动控制台（或双击 start_register_web.bat）
.venv\Scripts\python.exe server.py
# 打开 http://localhost:8888

# 一键全自动（CLI 等价）
.venv\Scripts\python.exe -m app.main --auto --count 3 --stagger 60

# 常用参数
--stagger 60        # 每号间隔秒数（防风控，强烈建议 ≥60）
--no-completion     # 纯极速：跳过浏览器补全（不抓凭据/不绑2FA）

# 单账号真机注册测试
python ..\scripts\live_register_test.py --edition browser --provider icloud ^
    --email you@icloud.com --inbox "https://icloud-api.top/s/xxx/you@icloud.com" ^
    --proxy 127.0.0.1:7897 --auto
```

## 6. 面板功能

- **🤖 一键全自动流水线**：注册 → 补全 → 检测 → 报表（可设间隔/极速模式/历史复核）
- **🧾 最终报表**：邮箱/密码/2FA密钥/动态验证码(实时)/注册时间/试用资格/Plus状态/到期/证据
- **👥 账号资产管理**：搜索/筛选/单号检测/批量完善/导入导出
- 动态验证码：前端 WebCrypto HMAC-SHA1 每秒计算（零网络请求；`SHOW_LIVE_TOTP` 开关）
- 表格列宽可拖拽（记忆到 localStorage，双击手柄恢复）

## 7. 关键机制与踩坑记录（重要！）

1. **TLS 指纹一致性**：Cloudflare 判断「TLS 档位」与「header 声称的 Chrome 版本」是否一致。
   curl_cffi 档位、User-Agent、sec-ch-ua 必须三者对齐。档位太老（131/136）过闸率为 0。
2. **`recent_auth_required`**：MFA enroll 只允许"刚认证过"的会话 → 2FA 绑定必须在
   注册流程末尾的新鲜窗口内执行，或重新登录。
3. **email-verification 页有「使用密码继续」链接**（`a[href*='/create-account/password']`）：
   注册时点击它切到密码创建页 → 账号带密码（`_switch_to_password_signup`）。
4. **免密账号的邮箱登录常被拒发码**（「我们无法向你发送验证码」）→ 有密码的账号走
   「使用密码继续」登录是最稳的重登方式。
5. **单 IP 配额**：实测连续 4~10 个后开始风控（403/429）。用 `--stagger` 放慢 +
   代理轮换（见 Clash API 集成）。
6. **React 受控输入**：JS 注入值会被 React 状态重置（出现"电子邮件地址无效"）→
   必须用真实键盘输入（`type_slowly`）或原生 setter + 事件派发（`_set_input_value`）。
7. **2FA enroll 响应即含真实密钥**：
   `{"secret":"<32位Base32>","factor":{"id":"...","factor_type":"totp"}}`
   密钥取出后无论激活是否确认都应保留（激活状态单独标注）。

## 8. 测试与验证

```powershell
# 本地回归（两版一起跑）+ 前端校验
..\scripts\verify_local.py          # 退出码 0 = 全部通过

# 只跑本项目 74 项
.venv\Scripts\python.exe -m unittest discover tests

# 真机验证工具（scripts/ 在仓库根目录）
..\scripts\live_register_test.py    # 单账号真机注册
..\scripts\live_verify.py           # 报表 / 单号直连官方接口验证
..\scripts\attach_browser.py        # 挂到运行中的浏览器看实时页面
..\scripts\maintain_accounts.py     # 账号库去重/BOM修复
..\scripts\sync_shared_modules.py   # 双版本共享模块同步
```

## 9. 已知问题与下一步

- MFA「激活确认」端点未找到（enroll 已拿到密钥；候选全 404；连打会被 CF 403 限流）
- 免密账号密码：受官方「无法向你发送验证码」限制；换干净 IP + 设置页可解
- Codex OAuth：`未获取到 authorization code`（可选功能，不影响主流程）
- 试用资格：官方未给这批号发活动（外部条件；`cornets.motives8s` 曾检测到活动）

## 10. 数据安全

`data/`、`token_exports/` 含真实凭据，**已加入 .gitignore 绝不入库**。
GitHub 发布用 `../scripts/publish_github.py`（按分支推送，自动排除敏感文件）。
