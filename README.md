# 9pt-Registration

本仓库用**分支**承载多个项目（一个项目一个分支）：

| 分支 | 项目 | 说明 |
| --- | --- | --- |
| `main` | 说明与验证工具 | 本文件 + `scripts/` 下的本地回归与真机验证工具 |
| `gpt-auto-register` | 浏览器自动化版 | Flask + Selenium/undetected-chromedriver，控制台端口 8888 |
| `gpt-protocol-register` | 极速协议版 | curl_cffi + Sentinel PoW，控制台端口 8889 |
| `chat2api` | ChatGPT Web 兼容网关 | FastAPI，端口 5005 |

## 一键全自动流水线

`注册 → 真实补全（Web 凭据 + 官方确认的 2FA）→ 官方接口检测 → 直接产出报表`

- 面板：左侧 **🤖 一键全自动流水线**，跑完在 **🧾 最终报表** 查看
  `邮箱 / 密码 / 2FA 密钥(32位) / 动态验证码 / 注册时间 / 1 个月 Plus 试用资格 / 当前 Plus 状态 / 到期时间 / 在线证据`
- 命令行：`python -m app.main --auto --count 3`
- 报表落盘：`token_exports/final_<时间戳>/`（JSON / CSV / Markdown / `邮箱-----密码-----2FA`）

## 数据真实性

所有字段只写官方接口真实返回的内容，并保留原始报文证据（`data/tokens/state-<邮箱>.json`）；
拿不到证据一律写“未知”。2FA 密钥只在官方接口返回后写入，不会生成占位值。

## 安全说明

本仓库**不包含**任何真实账号数据、Cookie、Token 或虚拟环境；这些都在 `.gitignore` 中排除。
