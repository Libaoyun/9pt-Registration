"""
ChatGPT 极速协议注册核心引擎 (Protocol Edition)
基于 curl_cffi 模拟 Chrome TLS/JA3 指纹，结合 Sentinel FNV-1a PoW 算法完成全流程 HTTP 协议注册。
无需拉起庞大浏览器，极速轻量，支持 5 大临时邮箱、Plus 试用邀请绑定及 2FA TOTP 自动激活。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, quote

import urllib3
urllib3.disable_warnings()

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    import requests as curl_requests

from .sentinel import Sentinel
from .two_factor_service import enable_account_2fa_protocol, TwoFactorService
from .account_probe import fetch_session_access_token, probe_with_token
from .account_checker import save_state_evidence, state_evidence_summary
from .utils import (
    generate_random_password,
    get_random_person_info,
    save_to_txt,
    update_account_status,
)
from . import email_providers
from .config import cfg, PROJECT_ROOT

CHATGPT_ORIGIN = "https://chatgpt.com"
AUTH_ORIGIN = "https://auth.openai.com"

# ── TLS 指纹档位（关键，决定能否过 Cloudflare）─────────────────────────────
# 实测结论（参考同类项目对照实验）：Cloudflare 判断的是
#   「TLS 指纹版本」与「请求头里声称的 Chrome 版本」是否**一致**，且版本要够新：
#     TLS136 + 声称136 → 过闸 0/8      TLS142 + 声称142 → 8/8
#     TLS142 + 声称152 → 0/6（不一致即死）   Chrome153 → 5/5
# 因此这里用单一常量同时驱动：curl_cffi 的 impersonate 档位、User-Agent、sec-ch-ua，
# 保证三者版本严格一致。可用档位取决于 curl_cffi 版本（0.16 支持到 chrome150）。
# 若本机 curl_cffi 较旧没有该档位，会自动降级到可用的最高档位并同步修正 UA。

def _resolve_impersonate() -> str:
    """挑一个当前 curl_cffi 支持的最高 Chrome 档位，并同步 UA/sec-ch-ua 版本。"""
    preferred = os.environ.get("PROTOCOL_IMPERSONATE", "").strip()
    candidates = ["chrome150", "chrome146", "chrome145", "chrome142",
                  "chrome136", "chrome133a", "chrome131", "chrome124", "chrome120"]
    order = ([preferred] if preferred else []) + candidates
    try:
        from curl_cffi.requests import BrowserType
        available = {b.value for b in BrowserType}
    except Exception:
        available = set(order)
    for name in order:
        if name in available:
            return name
    return "chrome124"


IMPERSONATE = _resolve_impersonate()


def _version_from_impersonate(name: str) -> str:
    m = re.search(r"chrome(\d+)", name or "")
    return m.group(1) if m else "131"


CHROME_VERSION = _version_from_impersonate(IMPERSONATE)
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_VERSION}.0.0.0 Safari/537.36"
)
SEC_CH_UA = f'"Google Chrome";v="{CHROME_VERSION}", "Chromium";v="{CHROME_VERSION}", "Not_A Brand";v="24"'

# ★ 提升 Plus 试用资格率的关键：完整客户端标识（参考项目的实测值）。
#   OpenAI 用"部署版本 + 客户端构建号"判断请求是否来自真实/受信任的前端部署，
#   进而决定是否下发 eligible_promo_campaigns（Plus 试用资格）。
#   缺失这些头时，官方常返回空的 eligible_promo_campaigns（表现为"无试用资格"）。
OAI_CLIENT_BUILD_NUMBER = os.environ.get("OAI_CLIENT_BUILD_NUMBER", "10577136").strip()
OAI_CLIENT_VERSION = os.environ.get(
    "OAI_CLIENT_VERSION", "prod-8bfe9e3526fbf9900f9332d46fef7bc0065c4478"
).strip()

COMMON_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": AUTH_ORIGIN,
    "user-agent": CHROME_UA,
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "oai-client-build-number": OAI_CLIENT_BUILD_NUMBER,
    "oai-client-version": OAI_CLIENT_VERSION,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

NAVIGATE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": CHROME_UA,
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "upgrade-insecure-requests": "1",
}


def _build_proxy_url(proxy_conf: Any) -> str:
    """格式化代理地址为 requests/curl_cffi 所需的 URL 字符串"""
    if not proxy_conf:
        return ""
    if isinstance(proxy_conf, str):
        return proxy_conf.strip()
    if isinstance(proxy_conf, dict):
        if not proxy_conf.get("enabled", True):
            return ""
        ptype = proxy_conf.get("type", "http").lower()
        host = str(proxy_conf.get("host", "")).strip()
        port = int(proxy_conf.get("port", 0) or 8080)
        if not host or not port:
            return ""
        use_auth = proxy_conf.get("use_auth", False)
        username = proxy_conf.get("username", "")
        password = proxy_conf.get("password", "")
        if use_auth and username:
            return f"{ptype}://{username}:{password}@{host}:{port}"
        return f"{ptype}://{host}:{port}"
    return ""


class ChatGPTProtocolRegister:
    """ChatGPT 协议注册客户端"""

    def __init__(self, proxy: Any = None, log_func: Callable[[str], None] = print):
        self.log = log_func
        self.device_id = str(uuid.uuid4())
        self.sentinel = Sentinel(self.device_id)
        self._sentinel_cache: dict[str, str] = {}
        self.session_token = ""

        proxy_url = _build_proxy_url(proxy)
        try:
            # TLS 指纹档位与 UA/sec-ch-ua 声称版本严格一致（过 Cloudflare 的关键）
            self.session = curl_requests.Session(impersonate=IMPERSONATE, verify=False)
            self.log(f"🧬 TLS 指纹档位: {IMPERSONATE}（UA/sec-ch-ua 同步为 Chrome {CHROME_VERSION}）")
        except Exception:
            self.session = curl_requests.Session()
            self.log("⚠️ curl_cffi 不可用，降级为普通 requests（过 Cloudflare 概率大幅下降）")

        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
            self.log(f"🌐 协议客户端已挂载代理: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")

    def _get_sentinel_token(self, flow: str) -> str:
        """获取并缓存指定 flow 的 Sentinel PoW 令牌"""
        if flow not in self._sentinel_cache:
            try:
                self._sentinel_cache[flow] = self.sentinel.get(self.session, flow)
            except Exception as e:
                self.log(f"⚠️ Sentinel 计算失败 ({flow}): {e}")
                self._sentinel_cache[flow] = ""
        return self._sentinel_cache[flow]

    def warm_up(self, referral_url: str = "") -> None:
        """预热会话，如果传入邀请链接则预载邀请 Cookies"""
        target = referral_url.strip() if referral_url and referral_url.strip() else f"{CHATGPT_ORIGIN}/auth/login"
        self.log(f"🌐 [Step 1] 正在请求入口页面: {target} ...")
        resp = self.session.get(target, headers=NAVIGATE_HEADERS, allow_redirects=True, timeout=20)
        if resp.status_code == 403 or "Just a moment..." in resp.text:
            raise RuntimeError("Cloudflare 盾拦截: 节点被识别，请更换纯净住宅 IP 或在设置中切换代理")

    def get_csrf(self) -> str:
        """获取 ChatGPT 登录鉴权 CSRF Token"""
        self.log("🔑 [Step 2] 请求 CSRF 验证凭据 ...")
        r = self.session.get(f"{CHATGPT_ORIGIN}/api/auth/csrf", headers=COMMON_HEADERS, timeout=15)
        if not r.ok:
            raise RuntimeError(f"获取 CSRF 失败 (HTTP {r.status_code})")
        data = r.json()
        token = data.get("csrfToken")
        if not token:
            raise RuntimeError("CSRF Token 为空，可能遇到 Cloudflare 质询")
        return token

    def signin_init(self, email: str, csrf: str) -> str:
        """提交邮箱并发起 OAuth 跳转"""
        self.log("📤 [Step 3] 提交邮箱并发起 OAuth 授权流程 ...")
        encoded_email = quote(email)
        params = {
            "prompt": "login",
            "screen_hint": "login_or_signup",
            "login_hint": encoded_email,
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        r = self.session.post(
            f"{CHATGPT_ORIGIN}/api/auth/signin/openai?{qs}",
            data={"callbackUrl": "/", "csrfToken": csrf, "json": "true"},
            headers={
                **COMMON_HEADERS,
                "content-type": "application/x-www-form-urlencoded",
                "origin": CHATGPT_ORIGIN,
                "referer": f"{CHATGPT_ORIGIN}/auth/login",
            },
            allow_redirects=False,
            timeout=15,
        )
        if not r.ok:
            raise RuntimeError(f"发起登录授权失败: {r.status_code}")
        auth_url = r.json().get("url", "")
        if not auth_url:
            raise RuntimeError("未返回有效的 OAuth 授权跳转 URL")
        return auth_url

    def jump_to_auth(self, redirect_url: str) -> str:
        """跟进跳转至 auth.openai.com 建立会话"""
        self.log("🚀 [Step 4] 跟随跳转至 auth.openai.com 会话环境 ...")
        r = self.session.get(
            redirect_url,
            headers={**NAVIGATE_HEADERS, "referer": CHATGPT_ORIGIN, "sec-fetch-site": "cross-site"},
            allow_redirects=False,
            timeout=20,
        )
        location = r.headers.get("Location", "")
        if location:
            self.session.get(
                location,
                headers={**NAVIGATE_HEADERS, "referer": AUTH_ORIGIN, "sec-fetch-site": "same-origin"},
                allow_redirects=True,
                timeout=20,
            )
        return location

    def register_user(self, email: str, password: str) -> dict:
        """提交邮箱与密码注册请求（携带 Sentinel PoW 令牌）"""
        self.log("📝 [Step 5] 提交用户注册表单与反爬 PoW 校验 ...")
        headers = {
            **COMMON_HEADERS,
            "referer": f"{AUTH_ORIGIN}/create-account/password",
            "oai-device-id": self.device_id,
        }
        sentinel_token = self._get_sentinel_token("username_password_create")
        if sentinel_token:
            headers["OpenAI-Sentinel-Token"] = sentinel_token

        r = self.session.post(
            f"{AUTH_ORIGIN}/api/accounts/user/register",
            json={"username": email, "password": password},
            headers=headers,
            timeout=20,
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        data["_status"] = r.status_code
        return data

    def send_email_otp(self, continue_url: str) -> None:
        """请求发送邮箱验证码"""
        self.log("📩 [Step 6] 触发发送邮箱验证码 ...")
        target = continue_url if continue_url.startswith("http") else f"{AUTH_ORIGIN}{continue_url}"
        self.session.get(
            target,
            headers={**NAVIGATE_HEADERS, "referer": f"{AUTH_ORIGIN}/create-account/password"},
            allow_redirects=True,
            timeout=15,
        )

    # ------------------------------------------------------------------
    # 纯协议「密码登录」状态机（已有账号）——移植自公开实现的实测路径
    # ------------------------------------------------------------------

    def _auth_step(self, step_json: dict) -> tuple[str, str]:
        """从 authorize/step 响应里提取 page_type 与 continue_url。"""
        page_type = ""
        continue_url = ""
        if isinstance(step_json, dict):
            page_type = str(step_json.get("page_type") or step_json.get("type") or "").strip().lower()
            continue_url = str(
                step_json.get("continue_url") or step_json.get("redirect") or ""
            ).strip()
        return page_type, continue_url

    def _authorize_continue(self, email: str, screen_hint: str = "login") -> tuple[str, str]:
        """调用 /api/accounts/authorize/continue 推进登录状态机。

        实测要点（移植自公开协议实现）:
          - 必须先经 chatgpt.com warmup（oai-did cookie）+ signin→auth 跳转建立会话，
            否则 409 "sign-in session no longer valid"
          - payload 为 {"username": {"value": email, "kind": "email"}, "screen_hint": ...}
          - Sentinel token 用 flow=authorize_continue 本请求内新算的
        """
        headers = {
            **COMMON_HEADERS,
            "referer": f"{AUTH_ORIGIN}/log-in",
            "oai-device-id": self.device_id,
        }
        sentinel_token = self._get_sentinel_token("authorize_continue")
        if sentinel_token:
            headers["OpenAI-Sentinel-Token"] = sentinel_token

        r = self.session.post(
            f"{AUTH_ORIGIN}/api/accounts/authorize/continue",
            json={
                "username": {"value": email, "kind": "email"},
                "screen_hint": screen_hint,
            },
            headers=headers,
            timeout=30,
        )
        if not r.ok:
            self.log(f"  ⚠️ authorize/continue 失败: HTTP {r.status_code} - {r.text[:150]}")
        data = r.json() if r.ok and r.text else {}
        return self._auth_step(data if isinstance(data, dict) else {})

    def _open_login_entry(self, email: str) -> None:
        """访问 auth.openai.com 的登录入口建立 auth 域会话（不经过 chatgpt.com）。"""
        headers = {**NAVIGATE_HEADERS, "referer": f"{CHATGPT_ORIGIN}/"}
        params = {
            "prompt": "login",
            "screen_hint": "login",
            "login_hint": quote(email),
            "ext-oai-did": self.device_id,
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        # 官方登录 authorize 入口（auth 域直连）
        self.session.get(
            f"{AUTH_ORIGIN}/authorize?client_id=DRivsnm2Mu42T3KOpqdtwB3NYviHY0D&"
            f"audience=https%3A%2F%2Fapi.openai.com%2Fv1&redirect_uri=https%3A%2F%2Fchatgpt.com%2Fapi%2Fauth%2Fcallback%2Flogin-web&"
            f"scope=openid+email+profile+offline_access+model.request+model.read+organization.read+organization.write&"
            f"response_type=code&{qs}&code_challenge=*&code_challenge_method=S256",
            headers=headers,
            allow_redirects=True,
            timeout=25,
        )

    def _password_verify(self, password: str) -> tuple[str, str]:
        """提交密码校验（/api/accounts/password/verify），返回 (page_type, continue_url)。"""
        self.log("🔑 [密码登录] 提交密码校验 ...")
        headers = {
            **COMMON_HEADERS,
            "referer": f"{AUTH_ORIGIN}/log-in/password",
            "oai-device-id": self.device_id,
        }
        r = self.session.post(
            f"{AUTH_ORIGIN}/api/accounts/password/verify",
            json={"password": password},
            headers=headers,
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"密码校验失败: HTTP {r.status_code} - {r.text[:150]}")
        data = r.json() if r.text else {}
        page_type, continue_url = self._auth_step(data if isinstance(data, dict) else {})
        self.log(f"  ✅ 密码校验通过 → page_type={page_type or '无'} continue={continue_url[:60]}")
        return page_type, continue_url

    def _extract_challenge_id(self, continue_url: str) -> str:
        """从 /mfa-challenge/<id> 形式的 continue_url 提取挑战 ID。"""
        m = re.search(r"/mfa-challenge/([0-9a-fA-F-]{8,})", continue_url or "")
        return m.group(1) if m else ""

    def _mfa_verify(self, totp_code: str, challenge_id: str) -> tuple[str, str]:
        """提交 TOTP 码通过 mfa-challenge，返回 (page_type, continue_url)。"""
        self.log(f"🔐 [密码登录] 提交 2FA 动态码（challenge={challenge_id[:16]}...）...")
        headers = {
            **COMMON_HEADERS,
            "referer": f"{AUTH_ORIGIN}/mfa-challenge",
            "oai-device-id": self.device_id,
        }
        r = self.session.post(
            f"{AUTH_ORIGIN}/api/accounts/mfa/verify",
            json={"code": totp_code, "type": "totp", "id": challenge_id},
            headers=headers,
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"2FA 验证失败: HTTP {r.status_code} - {r.text[:150]}")
        data = r.json() if r.text else {}
        page_type, continue_url = self._auth_step(data if isinstance(data, dict) else {})
        return page_type, continue_url

    def _protocol_password_login(self, email: str, password: str,
                                 max_steps: int = 8) -> dict:
        """纯协议密码登录状态机：
            邮箱 authorize → (login_password → password/verify)
            → (mfa-challenge → mfa/verify，若有 2FA)
            → (email-verification → 走邮箱码，可选)
            → callback 建立 session
        返回 {"ok": bool, "page_type": ..., "continue_url": ...}
        """
        self.log(f"🔐 [纯协议密码登录] {email}")
        # 1) warmup：必须先拿到 chatgpt.com 的 oai-did cookie，否则 authorize/continue 必 409
        self.warm_up(referral_url="")

        # 2) chatgpt.com NextAuth: csrf → signin(login_hint=email) → auth 域会话
        csrf = self.get_csrf()
        auth_url = self.signin_init(email, csrf)
        self.jump_to_auth(auth_url)

        # 3) authorize/continue 推进登录状态机（login 分支）
        page_type, continue_url = self._authorize_continue(email)
        self.log(f"  ↪️ 状态机起点: page_type={page_type or '无'} continue={continue_url[:60]}")

        for step_index in range(max_steps):
            if self._stopped() if hasattr(self, "_stopped") else False:
                break
            if page_type == "login_password" or "/log-in/password" in continue_url:
                page_type, continue_url = self._password_verify(password)
            elif "mfa-challenge" in continue_url or page_type == "mfa_challenge":
                # 需要已有 2FA 密钥（由注册/补全阶段保存）
                from .two_factor_service import generate_totp_code
                from .stored_accounts import load_accounts_from_file
                accounts_file = Path(cfg.files.accounts_file)
                if not accounts_file.is_absolute():
                    accounts_file = PROJECT_ROOT / accounts_file
                secret = ""
                try:
                    rec = next((r for r in load_accounts_from_file(str(accounts_file))
                                if r.get("email") == email), {})
                    secret = (rec.get("two_factor_secret") or "").strip()
                except Exception:
                    pass
                if not secret:
                    self.log("  ⚠️ 进入 mfa-challenge 但本地无 2FA 密钥")
                    return {"ok": False, "page_type": page_type, "continue_url": continue_url}
                challenge_id = self._extract_challenge_id(continue_url)
                if not challenge_id:
                    self.log("  ⚠️ 无法提取 challenge_id")
                    return {"ok": False, "page_type": page_type, "continue_url": continue_url}
                page_type, continue_url = self._mfa_verify(generate_totp_code(secret), challenge_id)
            elif page_type == "add_phone" or "add-phone" in continue_url:
                self.log("  ⚠️ 官方要求 add_phone（添加手机号）——纯协议无法自动过，需人工/浏览器")
                return {"ok": False, "page_type": "add_phone", "continue_url": continue_url}
            elif page_type in ("authenticated", "done"):
                # 状态机推进完成 → 走 callback 建立 session
                self.log(f"  ✅ 状态机完成: {page_type}")
                self.complete_oauth_callback(continue_url)
                return {"ok": True, "page_type": page_type, "continue_url": continue_url}
            elif "email-verification" in continue_url or page_type == "email_otp_verification":
                # 官方要求邮箱 OTP（即使带密码的账号也可能要求）。需要收件箱回调取码。
                self.log("  📩 状态机要求邮箱 OTP 验证")
                otp_fn = getattr(self, "_otp_callback", None)
                if not callable(otp_fn):
                    self.log("  ⚠️ 未提供收件箱回调（_otp_callback），无法自动取码")
                    return {"ok": False, "page_type": "email_otp_verification",
                            "continue_url": continue_url}
                # 触发发码 + 取码 + 校验
                self.send_email_otp(continue_url)
                code = otp_fn(email)
                if not code:
                    self.log("  ⚠️ 未取到邮箱验证码")
                    return {"ok": False, "page_type": "email_otp_verification",
                            "continue_url": continue_url}
                self.log(f"  ✅ 邮箱验证码: {code}")
                resp = self.validate_otp(code)
                page_type, continue_url = self._auth_step(resp if isinstance(resp, dict) else {})
                self.log(f"  ↪️ OTP 后: page_type={page_type or '无'} continue={continue_url[:60]}")
                # OTP 后通常返回 callback（/oauth/callback 或 /authorize/resume）→ 建立会话
                if continue_url:
                    self.complete_oauth_callback(continue_url)
                    return {"ok": True, "page_type": page_type or "post_otp",
                            "continue_url": continue_url}
                return {"ok": False, "page_type": page_type, "continue_url": ""}
            else:
                # 未知状态：尝试 callback 兜底
                if continue_url:
                    self.complete_oauth_callback(continue_url)
                    return {"ok": True, "page_type": page_type or "unknown", "continue_url": continue_url}
                break

        return {"ok": False, "page_type": page_type, "continue_url": continue_url}

    def validate_otp(self, code: str) -> dict:
        """验证 6 位邮箱验证码"""
        self.log(f"🔢 [Step 7] 提交邮箱验证码: {code} ...")
        headers = {
            **COMMON_HEADERS,
            "referer": f"{AUTH_ORIGIN}/contact-verification",
            "oai-device-id": self.device_id,
        }
        sentinel_token = self._get_sentinel_token("authorize_continue")
        if sentinel_token:
            headers["OpenAI-Sentinel-Token"] = sentinel_token

        r = self.session.post(
            f"{AUTH_ORIGIN}/api/accounts/email-otp/validate",
            json={"code": code},
            headers=headers,
            timeout=20,
        )
        if not r.ok:
            # 兼容另一种路由 /api/accounts/phone-otp/validate 或通用 validate
            r = self.session.post(
                f"{AUTH_ORIGIN}/api/accounts/user/validate",
                json={"code": code},
                headers=headers,
                timeout=20,
            )
        return r.json() if r.ok else {"_status": r.status_code, "error": r.text[:100]}

    def create_profile(self, name: str, birthdate: str) -> dict:
        """提交姓名与生日等个人资料（自适应新旧 OpenAI 接口字段）"""
        self.log(f"👤 [Step 8] 完善账号资料 (姓名: {name}, 生日: {birthdate}) ...")
        headers = {
            **COMMON_HEADERS,
            "referer": f"{AUTH_ORIGIN}/about-you",
            "oai-device-id": self.device_id,
        }
        sentinel_token = self._get_sentinel_token("oauth_create_account")
        if sentinel_token:
            headers["OpenAI-Sentinel-Token"] = sentinel_token

        # 适配同时支持 birthdate 与 age (OpenAI 新版可能使用 age 字段)
        r = self.session.post(
            f"{AUTH_ORIGIN}/api/accounts/create_account",
            json={"name": name, "birthdate": birthdate, "age": 24},
            headers=headers,
            allow_redirects=False,
            timeout=20,
        )
        if not r.ok:
            r = self.session.post(
                f"{AUTH_ORIGIN}/api/accounts/create_account",
                json={"name": name, "birthdate": birthdate},
                headers=headers,
                allow_redirects=False,
                timeout=20,
            )
        return r.json() if r.ok else {"_status": r.status_code}

    def complete_oauth_callback(self, callback_url: str) -> str:
        """跟随最终 OAuth 回调获取 Session Token（next-auth cookie）。"""
        self.log("🏁 [Step 9] 最终 OAuth 回调握手建立会话 ...")
        target = callback_url if callback_url.startswith("http") else f"{CHATGPT_ORIGIN}{callback_url}"
        self.session.get(
            target,
            headers={**NAVIGATE_HEADERS, "referer": AUTH_ORIGIN, "sec-fetch-site": "cross-site"},
            allow_redirects=True,
            timeout=20,
        )
        # 新版可能不用默认名或分块存储：遍历 cookie 名找 session 相关的
        names = list(getattr(self.session.cookies, "keys", lambda: [])())
        if not names:
            try:
                names = [c.name for c in self.session.cookies if hasattr(c, "name")]
            except Exception:
                names = []
        candidates = [n for n in names if "session-token" in str(n).lower()]
        if candidates:
            # 主 cookie（无 .N 后缀）优先
            main = next((n for n in candidates if not re.search(r"\.\d+$", str(n))), candidates[0])
            self.session_token = self.session.cookies.get(main, "") or ""
            self.log(f"  🍪 取到 session cookie: {main}（共 {len(candidates)} 个分块）")
        else:
            self.session_token = ""
            self.log("  ⚠️ 未取到 session cookie（回调未建立会话）")
        return self.session_token

    def get_web_access_token(self, email: str = "") -> str:
        """用已建立的会话换取真实 ChatGPT Web access token。

        next-auth 的 session cookie 不能直接当 Bearer 使用，必须经
        ``/api/auth/session`` 换取 accessToken —— 这是 backend-api 唯一认可的凭据。
        """
        self.log("🔑 [Step 10] 正在通过 /api/auth/session 换取真实 Web access token ...")
        info = fetch_session_access_token(self.session, timeout=20)
        self.web_access_token = info.get("access_token", "")
        if not self.web_access_token:
            self.log(
                f"  ⚠️ 未取得 access token (HTTP {info.get('status')}"
                f"{'，' + str(info.get('error'))[:60] if info.get('error') else ''})；"
                "权益检测将由浏览器通道补全"
            )
            return ""
        self.log("  ✅ 已取得真实 Web access token")
        if email:
            self._save_web_token(email, info)
        return self.web_access_token

    def _save_web_token(self, email: str, info: dict) -> str:
        """把真实 Web 凭据落盘，供后续检测复用。"""
        try:
            token_dir = Path(cfg.oauth.token_json_dir)
            if not token_dir.is_absolute():
                token_dir = PROJECT_ROOT / token_dir
            token_dir.mkdir(parents=True, exist_ok=True)
            target = token_dir / f"web-{email}.json"
            # 保存全部 cookie（含分块 session-token .0/.1），供后续浏览器恢复登录态
            all_cookies = []
            try:
                names = list(getattr(self.session.cookies, "keys", lambda: [])())
            except Exception:
                names = []
            for name in names:
                try:
                    all_cookies.append({"name": name, "value": self.session.cookies.get(name) or ""})
                except Exception:
                    continue
            session_names = [n for n in names if "session-token" in str(n).lower()]
            target.write_text(
                json.dumps(
                    {
                        "email": email,
                        "type": "web",
                        "access_token": info.get("access_token", ""),
                        "session_token": self.session_token,
                        "session_cookie_names": session_names,
                        "cookies": all_cookies,
                        "expires": info.get("expires", ""),
                        "account": info.get("account") or {},
                        "user": info.get("user") or {},
                        "captured_at": datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.log(f"  💾 真实 Web 凭据已保存: {target.name}")
            return str(target)
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ⚠️ 保存 Web 凭据失败: {exc}")
            return ""

    def collect_state(self, email: str, proxy: Any = None) -> dict:
        """采集真实权益状态：优先 Web access token，其次 Codex token（仅作会话有效性参考）。"""
        token = getattr(self, "web_access_token", "") or self.get_web_access_token(email)
        if token:
            return probe_with_token(token, email=email, token_kind="web", proxy=proxy, timeout=15)
        state = probe_with_token("", email=email)
        return state

    def bind_2fa(self, password: str = "") -> dict:
        """通过 API 协议真实开启 2FA（拿不到官方确认时不写入任何密钥）。"""
        return enable_account_2fa_protocol(
            self.session,
            auth_token=getattr(self, "web_access_token", ""),
            account_password=password,
            log_func=self.log,
        )


class RegisterResult(tuple):
    """包装注册结果元组，附加 skipped 标识以便统计跳过数量"""
    def __new__(cls, values, skipped=False):
        instance = super().__new__(cls, values)
        instance.skipped = skipped
        return instance


def register_one_account(
    monitor_callback=None,
    email_provider="mailtm",
    headless=True,
    proxy=None,
    referral_url: str = "",
    enable_2fa: bool = True,
    stop_check: Callable[[], bool] | None = None,
) -> tuple[str | None, str | None, bool]:
    """
    单账号极速协议注册入口
    
    返回:
        (email, password, is_success)
    """
    def _log(msg: str):
        print(msg)
        if monitor_callback:
            try:
                monitor_callback(msg)
            except Exception:
                pass

    def _check_stop():
        if stop_check and stop_check():
            _log("🛑 检测到停止请求，注册任务安全中止")
            raise InterruptedError("用户请求停止")

    email = None
    password = None
    temp_credential = None
    success = False

    provider_info = email_providers.get_provider_info(email_provider)
    if not provider_info:
        email_provider = "mailtm"
        provider_info = email_providers.get_provider_info("mailtm")

    provider_name = provider_info["name"]

    try:
        _check_stop()

        # 1. 创建临时邮箱
        _log(f"📧 [1/8] 正在通过 {provider_name} 分配临时邮箱 ...")
        email, token, temp_credential = email_providers.create_temp_email(email_provider)
        if not email:
            _log(f"❌ 分配临时邮箱失败 ({provider_name})，流程终止")
            return None, None, False

        # 检查该邮箱是否已在系统数据库中成功注册过，若已注册直接跳过
        from .stored_accounts import get_registered_account_by_email, is_registered_status
        from .config import PROJECT_ROOT
        import os
        accounts_file_path = cfg.files.accounts_file
        if not os.path.isabs(accounts_file_path):
            accounts_file_path = str(PROJECT_ROOT / accounts_file_path)

        existing_acc = get_registered_account_by_email(accounts_file_path, email)
        if existing_acc and is_registered_status(existing_acc.get("status")):
            _log(f"⏩ 邮箱 [{email}] 已在系统中成功注册过 GPT 账号，直接跳过注册流程（表格依然保留展示）")
            return RegisterResult((email, existing_acc.get("password"), True), skipped=True)

        save_to_txt(
            email,
            None,
            "邮箱已创建",
            mailtm_password=str(temp_credential or ""),
            provider=email_provider,
        )

        _check_stop()

        # 2. 生成密码和假人资料
        password = generate_random_password()
        person = get_random_person_info()
        name = person["full_name"]
        birthdate = person["birthday_iso"]

        # 3. 启动协议客户端
        client = ChatGPTProtocolRegister(proxy=proxy, log_func=_log)

        # 4. 预热访问 (支持 Plus 邀请链接绑定)
        client.warm_up(referral_url=referral_url)

        _check_stop()

        # 5. 获取 CSRF Token
        csrf = client.get_csrf()

        # 6. 发起 Signin 请求并跳往 auth.openai.com
        auth_url = client.signin_init(email, csrf)
        client.jump_to_auth(auth_url)

        _check_stop()

        # 快照当前收件箱中的旧验证码（防止 iCloud 或复用邮箱读到历史旧邮件）
        initial_codes = email_providers.list_verification_codes(email_provider, str(token or ""))

        # 7. 提交注册
        reg_res = client.register_user(email, password)
        status_code = reg_res.get("_status", 200)

        err_msg = reg_res.get("error", {}).get("message") or reg_res.get("message") or ""
        err_code = reg_res.get("error", {}).get("code") or reg_res.get("code") or ""
        err_lower = (str(err_msg) + " " + str(err_code)).lower()
        if any(k in err_lower for k in ["already exists", "already registered", "user exists", "user_already_exists", "already_exists"]):
            _log(f"⏩ OpenAI 服务端提示邮箱 [{email}] 已注册过 GPT 账号，已跳过注册流程并在表格中展示")
            update_account_status(email, "已在官方注册(跳过)", password=password, provider=email_provider)
            return RegisterResult((email, password, True), skipped=True)

        if status_code in (400, 403):
            _log(f"❌ 注册表单提交失败: {err_msg or '注册表单被拒'}")
            update_account_status(email, f"失败: {str(err_msg)[:40] if err_msg else '注册表单被拒'}", provider=email_provider)
            return email, password, False

        continue_url = reg_res.get("continue_url", "")
        if continue_url:
            client.send_email_otp(continue_url)

        _check_stop()

        # 8. 等待邮件验证码 (排除快照中的旧验证码)
        _log(f"⏳ [2/8] 等待 {provider_name} 收件箱接收 6 位验证码 (最长 120s) ...")
        otp_code = email_providers.wait_for_verification_email(
            email_provider,
            str(token or ""),
            timeout=getattr(cfg.email, "wait_timeout", 120),
            exclude_codes=initial_codes,
        )
        if not otp_code:
            _log("❌ 等待验证码超时，流程终止")
            update_account_status(email, "验证码超时", provider=email_provider)
            return email, password, False

        _check_stop()

        # 9. 校验验证码
        val_res = client.validate_otp(otp_code)
        val_status = val_res.get("_status")
        if val_status and val_status >= 400:
            _log(f"❌ 验证码校验失败: {val_res.get('error', '')}")
            update_account_status(email, "验证码错误", provider=email_provider)
            return email, password, False

        _check_stop()

        # 10. 完善资料 (姓名与出生年月)
        prof_res = client.create_profile(name, birthdate)
        cb_url = prof_res.get("callback_url") or prof_res.get("redirect") or "/"
        client.complete_oauth_callback(cb_url)

        _check_stop()

        # 10.1 用真实会话换取 Web access token（next-auth cookie 不能当 Bearer）
        web_access_token = client.get_web_access_token(email)

        # 11. 真实绑定 2FA TOTP（只有官方确认生效才写入密钥）
        two_factor_secret = ""
        two_factor_note = ""
        if enable_2fa:
            _log("🔐 [3/8] 正在真实开启 2FA (TOTP 双重验证) ...")
            mfa_res = client.bind_2fa(password=password)
            if mfa_res.get("success"):
                two_factor_secret = mfa_res.get("secret", "")
                _log(f"  ✅ 2FA 已由官方确认生效! 32位密钥: {two_factor_secret}")
            else:
                two_factor_note = str(mfa_res.get("error") or "protocol_2fa_unavailable")
                _log(
                    "  ℹ️ 协议端点未返回官方确认的 2FA 密钥；"
                    "流水线会自动切换到浏览器通道补全（不写入任何伪造密钥）"
                )

        # 12. 可选获取 Codex OAuth Token
        oauth_tokens = None
        if getattr(cfg.oauth, "enabled", False):
            _log("🔑 开始获取 Codex OAuth Token ...")
            try:
                from .oauth_service import perform_codex_oauth_login, save_codex_tokens
                oauth_tokens = perform_codex_oauth_login(
                    email=email,
                    password=password,
                    email_provider=email_provider,
                    mail_token=str(token or ""),
                    proxy=proxy,
                )
                save_codex_tokens(email=email, tokens=oauth_tokens, proxy=proxy)
                _log("  ✅ Codex OAuth Token 获取并保存成功 (ak.txt / rk.txt)")
            except Exception as e:
                _log(f"  ⚠️ Codex OAuth Token 获取提示: {e} (不影响账号注册)")

        # 13. 用真实 Web access token 采集真实权益画像
        _log("🔍 [4/8] 正在通过官方接口采集真实订阅状态与 1 个月 Plus 试用资格 ...")
        state = client.collect_state(email, proxy=proxy)
        plan_status = state.get("plan", "未检测")
        trial_status = state.get("trial_status", "待官方确认")
        quota_status = state.get("quota", "未知，请在官方页面确认")
        expires_status = state.get("expires_at", "未知，未取得订阅到期时间")
        health_status = state.get("account_status", "⚪ 未在线验证")
        evidence_file = save_state_evidence(email, state)

        # 14. 保存真实账号画像到共享账号库
        save_to_txt(
            email,
            password,
            "已注册/OAuth成功" if oauth_tokens else "已注册",
            mailtm_password=str(temp_credential or ""),
            provider=email_provider,
            plan=plan_status,
            trial_status=trial_status,
            quota=quota_status,
            expires_at=expires_status,
            account_status=health_status,
            two_factor_secret=two_factor_secret,
            evidence={
                "register_mode": "protocol",
                "source": "protocol",
                "referral_url_configured": bool(referral_url and referral_url.strip()),
                "referral_binding_verified": False,
                "web_token_saved": bool(web_access_token),
                "two_factor": {"bound": bool(two_factor_secret), "note": two_factor_note},
                **state_evidence_summary(state, evidence_file),
            },
        )

        _log("\n" + "=" * 50)
        _log("🎉 极速协议注册成功！")
        _log(f"   邮箱: {email}")
        _log(f"   密码: {password}")
        if two_factor_secret:
            _log(f"   2FA 密钥: {two_factor_secret}")
        _log(f"   计划/Plus: {plan_status} / {state.get('is_plus')}")
        _log(f"   试用资格: {trial_status}")
        _log(f"   在线状态: {health_status}")
        _log("=" * 50)

        success = True
        return email, password, True

    except InterruptedError:
        _log("🛑 注册任务已被用户主动中断")
        raise
    except Exception as e:
        _log(f"❌ 协议注册发生异常: {e}")
        if email and password:
            update_account_status(email, f"错误: {str(e)[:40]}", provider=email_provider)
        return email, password, False
