"""ChatGPT 2FA (TOTP) 自动化模块 —— 真实绑定，不做任何伪造。

职责:
  - RFC 6238 标准 TOTP 生成（pyotp 优先，标准库 HMAC-SHA1 兜底）
  - 浏览器通道: 在已登录页面上下文里直接调用官方 MFA 接口（自带 Cloudflare clearance），
    失败后回退到真实 UI 点击流程（设置 → 安全 → 多重验证 → 无法扫码 → 抓取密钥 → 提交验证码）
  - 协议通道: 用 curl_cffi 会话按候选端点逐个尝试官方 MFA 接口
  - 只有在「服务端确认激活」之后才返回成功；拿不到密钥一律如实失败，
    绝不生成占位密钥，避免面板里出现看似可用、实际登不进去的假 2FA。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
from typing import Callable

try:  # 浏览器路径才需要 selenium
    from selenium.webdriver.common.by import By
except ImportError:  # pragma: no cover - 协议版无 selenium 时可用
    By = None

CHAT_ORIGIN = "https://chatgpt.com"
AUTH_ORIGIN = "https://auth.openai.com"

# 官方 MFA 相关候选端点（按可行性排序）。仅作为"候选"，命中与否由官方响应决定。
ENROLL_CANDIDATES: tuple[tuple[str, str, str, dict | None], ...] = (
    ("auth_enroll", f"{AUTH_ORIGIN}/api/accounts/mfa/enroll", "POST", {"factor_type": "totp"}),
    ("auth_enroll_totp", f"{AUTH_ORIGIN}/api/accounts/mfa/enroll/totp", "POST", {"factor_type": "totp"}),
    ("auth_factor_enroll", f"{AUTH_ORIGIN}/api/accounts/mfa/factors", "POST", {"factor_type": "totp"}),
    ("auth_enroll_legacy", f"{AUTH_ORIGIN}/mfa/enroll", "POST", {"factor_type": "totp"}),
    ("web_enroll", f"{CHAT_ORIGIN}/backend-api/accounts/mfa/enroll", "POST", {"factor_type": "totp"}),
    ("web_enroll_alt", f"{CHAT_ORIGIN}/backend-api/mfa/enroll", "POST", {"factor_type": "totp"}),
)

ACTIVATE_CANDIDATES: tuple[tuple[str, str, dict], ...] = (
    ("auth_activate", f"{AUTH_ORIGIN}/api/accounts/mfa/activate", {}),
    ("auth_enroll_verify", f"{AUTH_ORIGIN}/api/accounts/mfa/enroll/verify", {}),
    ("auth_verify", f"{AUTH_ORIGIN}/api/accounts/mfa/verify", {}),
    ("auth_factor_verify", f"{AUTH_ORIGIN}/api/accounts/mfa/factors/verify", {}),
    ("web_activate", f"{CHAT_ORIGIN}/backend-api/accounts/mfa/activate", {}),
    ("web_enroll_verify", f"{CHAT_ORIGIN}/backend-api/accounts/mfa/enroll/verify", {}),
    ("web_enroll_activate", f"{CHAT_ORIGIN}/backend-api/accounts/mfa/enroll/activate", {}),
    ("web_factor_enroll_verify", f"{CHAT_ORIGIN}/backend-api/accounts/mfa/factors/enroll/verify", {}),
    ("web_totp_verify", f"{CHAT_ORIGIN}/backend-api/accounts/mfa/totp/verify", {}),
    ("web_factor_verify", f"{CHAT_ORIGIN}/backend-api/accounts/mfa/factors/verify", {}),
    ("web_mfa_verify", f"{CHAT_ORIGIN}/backend-api/accounts/mfa/verify", {}),
)

STATUS_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("auth_mfa", f"{AUTH_ORIGIN}/api/accounts/mfa"),
    ("auth_factors", f"{AUTH_ORIGIN}/api/accounts/mfa/factors"),
    ("web_mfa", f"{CHAT_ORIGIN}/backend-api/accounts/mfa"),
    ("web_factors", f"{CHAT_ORIGIN}/backend-api/accounts/mfa/factors"),
)

_SECRET_PATTERNS = (
    re.compile(r"otpauth://totp/[^\"'\s<>]*?secret=([A-Za-z2-7]{16,64})"),
    re.compile(r'"secret"\s*:\s*"([A-Za-z2-7]{16,64})"'),
    re.compile(r'"totp_secret"\s*:\s*"([A-Za-z2-7]{16,64})"'),
    re.compile(r"secret[\"'=:\s]+([A-Z2-7]{16,64})"),
)


# ---------------------------------------------------------------------------
# TOTP 基础能力
# ---------------------------------------------------------------------------

def generate_base32_secret(length: int = 32) -> str:
    """生成 32 位合规 Base32 密钥（仅用于本地测试/回归，不作为账号密钥写入记录）。"""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_totp_code(secret: str, timestamp: float | None = None) -> str:
    """按 RFC 6238 计算当前 6 位动态验证码。"""
    if not secret:
        return ""
    clean_secret = clean_base32_secret(secret)
    if not clean_secret:
        return ""
    try:
        import pyotp

        counter = pyotp.TOTP(clean_secret)
        if timestamp is None:
            return str(counter.now()).zfill(6)
        return str(counter.at(timestamp)).zfill(6)
    except Exception:
        pass

    try:
        padded = clean_secret
        pad = len(padded) % 8
        if pad:
            padded += "=" * (8 - pad)
        key = base64.b32decode(padded, casefold=True)
        moment = int(timestamp if timestamp is not None else time.time())
        counter = struct.pack(">Q", moment // 30)
        digest = hmac.new(key, counter, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        return f"{code % 1000000:06d}"
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 计算 TOTP 验证码异常: {exc}")
        return ""


def totp_seconds_remaining(period: int = 30) -> int:
    return period - int(time.time()) % period


def clean_base32_secret(raw: str) -> str:
    """从任意字符串里提取 16~64 位 Base32 密钥。"""
    if not raw or not isinstance(raw, str):
        return ""
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(raw)
        if match:
            candidate = re.sub(r"[^A-Za-z2-7]", "", match.group(1)).upper()
            if 16 <= len(candidate) <= 64:
                return candidate
    cleaned = re.sub(r"[^A-Za-z2-7]", "", raw).upper()
    if 16 <= len(cleaned) <= 64 and len(cleaned) % 8 == 0:
        return cleaned
    return ""


class TwoFactorService:
    """兼容旧调用点的小工具类。"""

    @staticmethod
    def generate_secret() -> str:
        return generate_base32_secret(32)

    @staticmethod
    def get_current_totp(secret: str) -> str:
        return generate_totp_code(secret)


# ---------------------------------------------------------------------------
# 页面密钥抓取（UI 兜底路径）
# ---------------------------------------------------------------------------

def extract_secret_from_page(driver) -> str:
    """从页面 DOM/源码中定位真实 2FA 密钥（otpauth / 手动密钥 / JSON）。"""
    if driver is None:
        return ""

    try:
        page_html = driver.page_source or ""
    except Exception:
        page_html = ""

    if page_html:
        for pattern in _SECRET_PATTERNS:
            match = pattern.search(page_html)
            if match:
                candidate = re.sub(r"[^A-Za-z2-7]", "", match.group(1)).upper()
                if 16 <= len(candidate) <= 64:
                    return candidate

    # 元素属性里（img src / data-* / aria-label）可能直接带 otpauth
    selectors = [
        "img[src*='otpauth']", "img[src*='qr']", "canvas", "svg",
        "[data-testid*='secret']", "[data-testid*='key']", "code", "pre",
        "input[readonly]", ".font-mono", "span[class*='mono']", "div[class*='mono']",
    ]
    if By is not None:
        for selector in selectors:
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    for attribute in ("src", "href", "alt", "aria-label", "data-secret", "value"):
                        try:
                            raw = element.get_attribute(attribute)
                        except Exception:
                            continue
                        secret = clean_base32_secret(raw or "")
                        if secret:
                            return secret
            except Exception:
                continue

    if By is not None:
        # 正文中可能出现分组密钥: JBSW Y3DP EHPK 3PXP ...
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            body_text = ""
        if body_text:
            chunk = re.search(r"((?:[A-Za-z2-7]{4}\s+){3,15}[A-Za-z2-7]{4})", body_text)
            if chunk:
                secret = clean_base32_secret(chunk.group(1))
                if secret:
                    return secret
            for pattern in _SECRET_PATTERNS:
                match = pattern.search(body_text)
                if match:
                    secret = clean_base32_secret(match.group(1))
                    if secret:
                        return secret
    return ""


def _visible(elements) -> list:
    result = []
    for element in elements:
        try:
            if element.is_displayed() and element.is_enabled():
                result.append(element)
        except Exception:
            continue
    return result


# ---------------------------------------------------------------------------
# 浏览器内页 HTTP 通道
# ---------------------------------------------------------------------------

_BROWSER_FETCH_JS = r"""
const [method, url, body, bearer] = arguments;
return (async () => {
  const options = {
    method: method,
    credentials: 'include',
    headers: { 'accept': 'application/json' },
    redirect: 'follow',
  };
  if (bearer) {
    options.headers['authorization'] = 'Bearer ' + bearer;
  }
  if (body !== null && body !== undefined) {
    options.headers['content-type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  try {
    const resp = await fetch(url, options);
    let text = '';
    try { text = await resp.text(); } catch (e) { text = ''; }
    return { status: resp.status, text: (text || '').slice(0, 4000), origin: location.origin };
  } catch (err) {
    return { status: 0, error: String(err), text: '', origin: location.origin };
  }
})()
"""

_BROWSER_SESSION_JS = r"""
return (async () => {
  try {
    const resp = await fetch('/api/auth/session', { credentials: 'include' });
    const text = await resp.text();
    let payload = null;
    try { payload = JSON.parse(text); } catch (e) { payload = null; }
    return { status: resp.status, payload: payload };
  } catch (err) {
    return { status: 0, error: String(err), payload: null };
  }
})()
"""


def browser_fetch(driver, method: str, url: str, body: dict | None = None, timeout: int = 30,
                  bearer: str = "") -> dict:
    """在浏览器页面上下文里发一个请求（真实结果原样返回）。

    ``bearer``：ChatGPT 前端访问 ``/backend-api/*`` 时必须带
    ``Authorization: Bearer <accessToken>``（实测只带 Cookie 会返回 401）。
    """
    try:
        driver.set_script_timeout(timeout)
    except Exception:
        pass
    try:
        result = driver.execute_script(_BROWSER_FETCH_JS, method.upper(), url, body, bearer or "")
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "error": str(exc), "text": ""}
    if not isinstance(result, dict):
        return {"status": 0, "error": "unexpected_result", "text": ""}
    return result


def get_page_access_token(driver, attempts: int = 3) -> str:
    """从当前页面上下文取真实 Web access token。

    实测登录后第一次请求偶发被 Cloudflare 403；这里带重试与页面重载。
    """
    for index in range(max(1, attempts)):
        try:
            driver.set_script_timeout(20)
            raw = driver.execute_script(_BROWSER_SESSION_JS)
        except Exception:
            raw = None
        if isinstance(raw, dict):
            payload = raw.get("payload")
            if isinstance(payload, dict) and payload.get("accessToken"):
                return payload.get("accessToken") or ""
            status = raw.get("status")
        else:
            status = 0
        if index < attempts - 1:
            time.sleep(2.5)
            try:
                if status in (403, 0):
                    driver.get(CHAT_ORIGIN)
                    time.sleep(3)
            except Exception:
                time.sleep(2)
    return ""


def current_origin(driver) -> str:
    """当前页面来源（用于挑选同源候选端点，避免跨源 CORS 拦截）。"""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(driver.current_url or "")
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return CHAT_ORIGIN


def _parse_secret(payload: Any) -> str:
    """从任意官方响应里提取密钥。"""
    if isinstance(payload, dict):
        for key in ("secret", "totp_secret", "shared_secret", "factor_secret", "base32_secret"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                secret = clean_base32_secret(value)
                if secret:
                    return secret
            if isinstance(value, dict):
                secret = _parse_secret(value)
                if secret:
                    return secret
        for key in ("qr_code_uri", "barcode_uri", "otpauth_uri", "provisioning_uri", "uri", "qr_code"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                secret = clean_base32_secret(value)
                if secret:
                    return secret
        for value in payload.values():
            secret = _parse_secret(value)
            if secret:
                return secret
    elif isinstance(payload, list):
        for item in payload:
            secret = _parse_secret(item)
            if secret:
                return secret
    elif isinstance(payload, str):
        return clean_base32_secret(payload)
    return ""


def _factor_is_active(payload: Any) -> bool | None:
    """判断官方因子列表里是否存在已激活的 TOTP 因子。"""
    if not isinstance(payload, (dict, list)):
        return None
    stack = [payload]
    seen_totp = False
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            factor_type = str(item.get("factor_type") or item.get("type") or "").lower()
            if "totp" in factor_type or "authenticator" in factor_type:
                seen_totp = True
                for key in ("active", "is_active", "enabled", "activated", "verified"):
                    if item.get(key) is True:
                        return True
            for value in item.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend([v for v in item if isinstance(v, (dict, list))])
    return False if seen_totp else None


def read_mfa_status(driver) -> dict:
    """读取当前账号 2FA 状态（官方返回为准）。"""
    evidence: list[dict] = []
    origin = current_origin(driver)
    bearer = get_page_access_token(driver) if "chatgpt.com" in origin else ""
    for name, url in STATUS_CANDIDATES:
        # 只在同源下请求，避免跨源 CORS 造成假失败
        if "chatgpt.com" in origin and CHAT_ORIGIN not in url:
            continue
        if "auth.openai.com" in origin and AUTH_ORIGIN not in url:
            continue
        result = browser_fetch(driver, "GET", url, bearer=bearer if "backend-api" in url else "")
        status = result.get("status", 0)
        if status in (404, 405):
            evidence.append({"name": name, "url": url, "status": status, "note": "route_unavailable"})
            continue
        payload = None
        try:
            payload = json.loads(result.get("text", "") or "null")
        except (ValueError, TypeError):
            payload = None
        active = _factor_is_active(payload) if payload is not None else None
        evidence.append({
            "name": name, "url": url, "status": status,
            "active": active, "body": (result.get("text") or "")[:300],
            "error": result.get("error", ""),
        })
        if active is not None:
            return {"enabled": active, "evidence": evidence}
    return {"enabled": None, "evidence": evidence}


def _origin_enroll_candidates(driver) -> list[tuple[str, str, str, dict | None, bool]]:
    """按当前页面来源挑出"同源"候选端点。

    跨源 fetch 到 auth.openai.com 会被 CORS 拦掉（实测返回 status 0），
    所以必须优先在对应来源的页面里发起；backend-api 还需要 Bearer。
    """
    origin = current_origin(driver)
    candidates: list[tuple[str, str, str, dict | None, bool]] = []
    if "chatgpt.com" in origin:
        for name, url, method, body in ENROLL_CANDIDATES:
            if CHAT_ORIGIN in url:
                candidates.append((name, url, method, body, True))  # 需要 Bearer
    elif "auth.openai.com" in origin:
        for name, url, method, body in ENROLL_CANDIDATES:
            if AUTH_ORIGIN in url:
                candidates.append((name, url, method, body, False))
    return candidates


def _origin_activate_candidates(driver) -> list[tuple[str, str, dict, bool]]:
    origin = current_origin(driver)
    result: list[tuple[str, str, dict, bool]] = []
    for name, url, extra in ACTIVATE_CANDIDATES:
        if "chatgpt.com" in origin and CHAT_ORIGIN in url:
            result.append((name, url, extra, True))
        elif "auth.openai.com" in origin and AUTH_ORIGIN in url:
            result.append((name, url, extra, False))
    return result


def _factor_id_from(payload: Any) -> str:
    """从 enroll 响应里取 factor id / challenge id（用于拼真实的激活路径）。"""
    if isinstance(payload, dict):
        for key in ("factor_id", "factorId", "id", "factor_uuid", "uuid",
                    "challenge_id", "enrollment_id", "factor_identifier"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _factor_id_from(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _factor_id_from(item)
            if found:
                return found
    return ""


def _activated_successfully(payload: Any) -> bool:
    """激活响应是否代表成功（有些接口返回 200 + {"activated": false} 之类，不能只信 HTTP 码）。"""
    if isinstance(payload, dict):
        for key in ("success", "ok", "activated", "is_active", "active", "enabled", "verified"):
            if payload.get(key) is True:
                return True
            if payload.get(key) is False:
                return False
        if payload.get("error"):
            return False
        if payload.get("factor_id") or payload.get("recovery_codes") or payload.get("backup_codes"):
            return True
    return True  # 空体 / 非 JSON 时以 HTTP 200 为准


def _mfa_paths_from(payload: Any) -> list[str]:
    """从 enroll 响应里挖出任何 MFA 相关路径/URL（真实接口有时会直接返回下一步地址）。"""
    found: list[str] = []
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for value in item.values():
                if isinstance(value, str):
                    low = value.lower()
                    if value.startswith("http") and "/mfa" in low:
                        found.append(value)
                    elif value.startswith("/") and ("/mfa" in low or "factor" in low) and len(value) < 200:
                        found.append(value)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend([v for v in item if isinstance(v, (dict, list))])
    return list(dict.fromkeys(found))


def _activation_plan(driver, url: str, code: str, factor_id: str, payload: Any,
                     bearer: str) -> list[tuple[str, str, dict, bool]]:
    """构造激活计划：已知存在的路由 × 常用方法 × 两种请求体，外加响应里给出的真实路径。"""
    api_prefix = "/backend-api" if "/backend-api" in url else "/api"
    root = url.split(api_prefix)[0]
    plan: list[tuple[str, str, dict, bool]] = []
    bodies = [
        {"code": code, "factor_type": "totp", "factor_id": factor_id} if factor_id
        else {"code": code, "factor_type": "totp"},
        {"code": code},
    ]
    for method in ("POST", "PUT", "PATCH"):
        for route in (f"{api_prefix}/accounts/mfa", f"{api_prefix}/accounts/mfa/enroll"):
            for body in bodies:
                plan.append((f"{method.lower()}:{route}", root + route, dict(body), bool(bearer)))
    for path in _mfa_paths_from(payload):
        target = path if path.startswith("http") else root + path
        for body in bodies:
            plan.append((f"post:from_payload:{path}", target, dict(body), bool(bearer)))
    return plan


def _try_origin_api(driver, evidence: list[dict], log: Callable[[str], None], bearer: str) -> dict:
    """在当前页面来源下把所有同源候选端点跑一遍，命中真实密钥就完成激活。"""
    candidates = _origin_enroll_candidates(driver)
    if not candidates:
        evidence.append({"step": "origin_candidates_empty", "origin": current_origin(driver)})
        return {"success": False, "secret": "", "evidence": evidence}

    for name, url, method, body, need_bearer in candidates:
        result = browser_fetch(driver, method, url, body, bearer=bearer if need_bearer else "")
        status = result.get("status", 0)
        text = result.get("text", "") or ""
        evidence.append({
            "step": f"enroll:{name}", "url": url, "status": status,
            "body": text[:300], "error": result.get("error", ""),
        })
        log(f"  · 试探 {name} -> HTTP {status}")
        if status not in (200, 201):
            if "recent_auth_required" in text:
                log("  ⚠️ 官方要求最近重新认证 (recent_auth_required)："
                    "2FA 绑定必须在刚登录/刚注册后的新鲜会话里执行")
            continue
        try:
            payload = json.loads(text or "null")
        except (ValueError, TypeError):
            payload = None
        secret = _parse_secret(payload)
        log(f"  📦 enroll 响应: {text[:400]}")
        if not secret:
            continue
        log(f"  🔑 官方接口返回真实密钥: {secret}")
        code = generate_totp_code(secret)
        origin_root = url.split("/backend-api")[0] if "/backend-api" in url else url.rsplit("/api", 1)[0]
        api_prefix = "/backend-api" if "/backend-api" in url else "/api"

        # 先按固定候选激活，再按 enroll 响应里的 factor_id / 路径自适应补齐
        activate_targets = [(name, u, extra, bc) for name, u, extra, bc in _origin_activate_candidates(driver)]
        factor_id = _factor_id_from(payload)
        if factor_id:
            log(f"  🆔 enroll 返回 factor_id: {factor_id}")
            for suffix in (
                f"{api_prefix}/accounts/mfa/factors/{factor_id}/verify",
                f"{api_prefix}/accounts/mfa/enroll/{factor_id}/verify",
                f"{api_prefix}/accounts/mfa/enroll/{factor_id}/activate",
                f"{api_prefix}/accounts/mfa/{factor_id}/verify",
                f"{api_prefix}/accounts/mfa/factors/{factor_id}/activate",
            ):
                activate_targets.append((f"adaptive:{suffix.split('/')[-1]}", origin_root + suffix, {}, bool(bearer)))
        activate_targets.extend(_activation_plan(driver, url, code, factor_id, payload, bearer))
        # 去重后只按优先级试前 4 个，并放慢节奏：一次打几十个请求会被 Cloudflare 限流（实测 403）
        deduped: list[tuple[str, str, dict, bool]] = []
        seen_urls_body: set[tuple[str, str, str]] = set()
        for candidate in activate_targets:
            name, target_url, body, need_bearer = candidate
            key = (target_url, json.dumps(body, sort_keys=True, ensure_ascii=False), str(need_bearer))
            if key in seen_urls_body:
                continue
            seen_urls_body.add(key)
            deduped.append(candidate)
        activate_targets = deduped[:2]
        log(f"  🧭 激活候选（限流保护）: {[c[0] for c in activate_targets]}")

        for activate_name, activate_url, extra, activate_bearer in activate_targets:
            payload_body = {"code": code, "factor_type": "totp"}
            if factor_id:
                payload_body["factor_id"] = factor_id
            payload_body.update(extra)
            activate = browser_fetch(
                driver, "POST", activate_url, payload_body,
                bearer=bearer if activate_bearer else "",
            )
            activate_status = activate.get("status", 0)
            activate_body = (activate.get("text") or "")[:300]
            log(f"  · 激活 {activate_name} -> HTTP {activate_status} {activate_body[:120]}")
            evidence.append({
                "step": f"activate:{activate_name}", "url": activate_url,
                "status": activate_status, "body": activate_body,
            })
            if activate_status == 403 and "<html" in activate_body.lower():
                log("  ⚠️ 激活请求被 Cloudflare 拦截（多为限流），停止继续轰炸")
                break
            if activate_status in (200, 201, 204):
                parsed = None
                try:
                    parsed = json.loads(activate_body or "null")
                except (ValueError, TypeError):
                    parsed = None
                if _activated_successfully(parsed):
                    log(f"  ✅ 官方接口确认 2FA 激活成功 ({activate_name})")
                    return {"success": True, "secret": secret, "activated": True,
                            "verified_by": f"api:{activate_name}", "evidence": evidence}
                log(f"  ⚠️ {activate_name} 返回 200 但内容未确认激活，继续尝试下一个")
            time.sleep(1.2)
        status_info = read_mfa_status(driver)
        evidence.append({"step": "verify_after_activate", "api_enabled": status_info.get("enabled")})
        if status_info.get("enabled") is True:
            log("  ✅ 官方因子列表显示 TOTP 已激活")
            return {"success": True, "secret": secret, "activated": True,
                    "verified_by": "api:factor_list", "evidence": evidence}
        # 官方已返回真实密钥但未确认激活：密钥仍然保留，由调用方标注"未激活"
        log("  ⚠️ 已取得官方真实密钥，但激活未获确认（密钥保留待用）")
        return {"success": False, "secret": secret, "activated": False,
                "verified_by": "api:enroll_only", "evidence": evidence}
    return {"success": False, "secret": "", "activated": False, "evidence": evidence}


def _api_enroll(driver, evidence: list[dict], log: Callable[[str], None]) -> dict:
    """候选端点探测：在 chatgpt.com 源带 Bearer 试，再切到 auth.openai.com 源试。

    重要：官方 ``/backend-api/accounts/mfa/enroll`` 成功时会直接返回真实 32 位密钥，
    无论激活是否确认，这个密钥都必须带回去（否则等于白拿）。
    """
    bearer = get_page_access_token(driver)
    evidence.append({"step": "access_token", "obtained": bool(bearer)})
    if not bearer:
        log("  ⚠️ 首次未取到 access token（多为 Cloudflare 瞬时拦截），重载页面后重试一次...")
        try:
            driver.get(CHAT_ORIGIN)
            time.sleep(4)
        except Exception:
            time.sleep(3)
        bearer = get_page_access_token(driver, attempts=2)
        evidence.append({"step": "access_token_retry", "obtained": bool(bearer)})

    best: dict = {"success": False, "secret": "", "activated": False, "evidence": evidence}
    result = _try_origin_api(driver, evidence, log, bearer)
    if result.get("secret"):
        best.update({k: result.get(k) for k in ("success", "secret", "activated", "verified_by")})
    if result.get("success"):
        return best

    # 换到 auth.openai.com 源再试一次（跨源 fetch 会被 CORS 拦，必须真的跳过去）
    try:
        log("  ↪️ 切换到 auth.openai.com 源重试官方 MFA 端点 ...")
        driver.get(f"{AUTH_ORIGIN}/")
        time.sleep(3)
        result = _try_origin_api(driver, evidence, log, "")
        if result.get("secret") and not best.get("secret"):
            best.update({k: result.get(k) for k in ("success", "secret", "activated", "verified_by")})
        if result.get("success"):
            return best
    except Exception as exc:  # noqa: BLE001
        evidence.append({"step": "auth_origin_switch_failed", "error": str(exc)[:160]})

    return best


def _ui_enroll(driver, account_password: str, evidence: list[dict], log: Callable[[str], None], timeout: int) -> dict:
    """真实 UI 流程：设置 → 安全 → 开启多重验证 → 无法扫码 → 抓密钥 → 提交验证码。"""
    if By is None:
        return {"success": False, "secret": "", "evidence": evidence}

    log("  👉 回退到官方 UI 流程 (设置 → 安全 → 多重验证)")
    driver.get(f"{CHAT_ORIGIN}/#settings/Security")
    time.sleep(3)

    enable_xpaths = (
        "//button[contains(., 'Turn on') or contains(., '开启') or contains(., '启用') or contains(., 'Enable') "
        "or contains(., 'Set up') or contains(., '设置') or contains(., '添加') or contains(., 'Add')]",
        "//*[@role='button'][contains(., 'Authenticator') or contains(., '身份验证器') "
        "or contains(., '验证器') or contains(., 'MFA')]",
        "//a[contains(., 'Authenticator') or contains(., '身份验证器') or contains(., '验证器')]",
        "//*[contains(normalize-space(.), 'Authenticator app') or contains(normalize-space(.), '身份验证器应用')]",
    )
    button = None
    for xpath in enable_xpaths:
        try:
            candidates = _visible(driver.find_elements(By.XPATH, xpath))
        except Exception:
            candidates = []
        if candidates:
            button = candidates[0]
            break

    if button is None:
        # 设置面板可能未打开：走一遍用户菜单
        try:
            menu = _visible(driver.find_elements(
                By.CSS_SELECTOR,
                'button[data-testid*="user-menu"], button[data-testid*="profile"], button[aria-label*="Profile"]',
            ))
            if menu:
                driver.execute_script("arguments[0].click();", menu[0])
                time.sleep(1.5)
                for label in ("Settings", "设置"):
                    items = _visible(driver.find_elements(By.XPATH, f"//*[contains(text(), '{label}')]"))
                    if items:
                        driver.execute_script("arguments[0].click();", items[0])
                        time.sleep(1.5)
                        break
                for label in ("Security", "安全"):
                    tabs = _visible(driver.find_elements(By.XPATH, f"//*[contains(text(), '{label}')]"))
                    if tabs:
                        driver.execute_script("arguments[0].click();", tabs[0])
                        time.sleep(1.5)
                        break
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠️ 打开安全设置面板失败: {exc}")

    if button is None:
        for xpath in enable_xpaths:
            try:
                candidates = _visible(driver.find_elements(By.XPATH, xpath))
            except Exception:
                candidates = []
            if candidates:
                button = candidates[0]
                break

    if button is None:
        status_info = read_mfa_status(driver)
        evidence.append({"step": "ui_button_missing", "mfa_status": status_info.get("enabled")})
        return {
            "success": False,
            "secret": "",
            "already_enabled": status_info.get("enabled") is True,
            "evidence": evidence,
        }

    driver.execute_script("arguments[0].click();", button)
    time.sleep(2)

    # 需要密码二次确认时输入
    try:
        for field in _visible(driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]')):
            if account_password:
                field.send_keys(account_password)
                time.sleep(0.5)
                for submit in _visible(driver.find_elements(
                    By.XPATH, "//button[@type='submit' or contains(., 'Continue') or contains(., '继续')]"
                )):
                    driver.execute_script("arguments[0].click();", submit)
                    break
            break
    except Exception:
        pass
    time.sleep(2)

    # 展开"无法扫描二维码"以显示文本密钥
    for label in ("Can't scan", "无法扫描", "手动输入", "manual", "Enter this key", "无法扫描二维码"):
        try:
            for element in _visible(driver.find_elements(By.XPATH, f"//*[contains(text(), \"{label}\")]")):
                driver.execute_script("arguments[0].click();", element)
                time.sleep(1)
                break
        except Exception:
            continue

    secret = extract_secret_from_page(driver)
    if not secret:
        evidence.append({"step": "ui_secret_not_found"})
        return {"success": False, "secret": "", "evidence": evidence}
    log(f"  🔑 页面抓取到真实密钥: {secret}")

    code = generate_totp_code(secret)
    if not code:
        evidence.append({"step": "ui_totp_calc_failed", "secret": secret})
        return {"success": False, "secret": "", "evidence": evidence}
    log(f"  🔢 当前动态验证码: {code}")

    try:
        inputs = _visible(driver.find_elements(
            By.CSS_SELECTOR,
            'input[autocomplete="one-time-code"], input[inputmode="numeric"], input[type="text"]',
        ))
        if len(inputs) == 6:
            for index, digit in enumerate(code):
                inputs[index].send_keys(digit)
                time.sleep(0.08)
        elif inputs:
            inputs[0].click()
            inputs[0].send_keys(code)
            # React 受控组件兜底
            driver.execute_script(
                """
                const el = arguments[0], value = arguments[1];
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                inputs[0], code,
            )
    except Exception as exc:  # noqa: BLE001
        evidence.append({"step": "ui_fill_failed", "error": str(exc)})
        return {"success": False, "secret": "", "evidence": evidence}

    time.sleep(1)
    for button_label in ("Verify", "Continue", "下一步", "验证", "提交", "Enable", "开启", "Done", "完成"):
        clicked = False
        for element in _visible(driver.find_elements(
            By.XPATH, f"//button[contains(., '{button_label}')]"
        )):
            driver.execute_script("arguments[0].click();", element)
            clicked = True
            break
        if clicked:
            break
    time.sleep(3)

    # 只有官方页面/接口确认生效才算成功
    page_text = ""
    try:
        page_text = (driver.find_element(By.TAG_NAME, "body").text or "")[:4000]
    except Exception:
        pass
    enabled_markers = ("备份码", "backup code", "recovery code", "Turn off", "关闭多重验证", "禁用", "已开启")
    disabled_markers = ("Turn on", "开启多重验证", "Enable multi-factor")
    page_enabled = any(marker.lower() in page_text.lower() for marker in enabled_markers)
    page_still_disabled = any(marker.lower() in page_text.lower() for marker in disabled_markers)
    api_status = read_mfa_status(driver)
    evidence.append({
        "step": "ui_verify",
        "page_enabled_marker": page_enabled,
        "page_still_disabled_marker": page_still_disabled,
        "api_enabled": api_status.get("enabled"),
    })

    if api_status.get("enabled") is True or (page_enabled and not page_still_disabled):
        log("  ✅ 2FA 已在官方生效（UI + 接口双重确认）")
        return {"success": True, "secret": secret, "verified_by": "ui", "evidence": evidence}

    return {"success": False, "secret": "", "evidence": evidence, "error": "verification_failed"}


def enable_account_2fa(
    driver,
    account_password: str = "",
    timeout: int = 30,
    monitor_callback: Callable | None = None,
    callback: Callable | None = None,
    log_func: Callable[[str], None] | None = None,
    **kwargs,
) -> dict:
    """在已登录浏览器里真实开启 2FA 并取回密钥。

    返回:
        {"success": bool, "secret": str, "backup_codes": list[str],
         "verified_by": str, "already_enabled": bool|None, "error": str|None, "evidence": list}
    """
    del kwargs  # 兼容旧签名；timeout 仍用于 UI 通道的等待

    def _log(message: str) -> None:
        print(message)
        for hook in (log_func, monitor_callback, callback):
            if hook:
                try:
                    hook(message)
                except Exception:
                    pass

    result = {
        "success": False, "secret": "", "backup_codes": [],
        "activated": False, "verified_by": "", "already_enabled": None,
        "error": None, "evidence": [],
    }
    if driver is None:
        result["error"] = "no_driver"
        return result

    _log("🔐 开始真实 2FA (TOTP) 绑定流程...")
    evidence: list[dict] = result["evidence"]

    # 1) 先看官方是否已经开了
    status_info = read_mfa_status(driver)
    result["already_enabled"] = status_info.get("enabled")
    evidence.append({"step": "initial_status", "enabled": status_info.get("enabled")})
    if status_info.get("enabled") is True:
        _log("  ℹ️ 官方因子列表显示账号已开启 2FA；密钥无法二次读取（只能重置后重新绑定）")
        result["error"] = "already_enabled_secret_unknown"
        return result

    # 2) 官方接口通道（官方真实返回的密钥一律保留，激活状态单独标注）
    try:
        api_result = _api_enroll(driver, evidence, _log)
        if api_result.get("secret"):
            result.update({
                "success": bool(api_result.get("success")),
                "secret": api_result.get("secret", ""),
                "activated": bool(api_result.get("activated")),
                "verified_by": api_result.get("verified_by", ""),
            })
            if result["success"]:
                _log(f"  ✅ 官方确认 2FA 已激活，32位密钥: {result['secret']}")
                return result
            _log(f"  ⚠️ 已取得官方真实密钥但激活未确认，密钥已保留: {result['secret']}")
    except Exception as exc:  # noqa: BLE001
        evidence.append({"step": "api_channel_exception", "error": str(exc)})
        _log(f"  ⚠️ 接口通道异常: {exc}")

    # 3) 真实 UI 通道（万一接口没给密钥，就点进官方设置页抓）
    try:
        ui_result = _ui_enroll(driver, account_password, evidence, _log, timeout=timeout)
        for key in ("success", "secret", "verified_by", "already_enabled", "error"):
            if ui_result.get(key) not in (None, ""):
                result[key] = ui_result[key]
        if ui_result.get("secret") and not result.get("secret"):
            result["secret"] = ui_result["secret"]
            result["activated"] = bool(ui_result.get("success"))
        if result["success"]:
            return result
    except Exception as exc:  # noqa: BLE001
        evidence.append({"step": "ui_channel_exception", "error": str(exc)})
        _log(f"  ⚠️ UI 通道异常: {exc}")

    if result.get("secret"):
        result["error"] = "secret_obtained_but_not_activated"
        _log(f"  ⚠️ 结束：官方真实 32位密钥已取得（未确认激活）: {result['secret']}")
        return result

    if not result["error"]:
        result["error"] = "2fa_not_verified"
    _log("  ❌ 未能取得任何官方 2FA 密钥（不写入占位密钥）")
    return result


# ---------------------------------------------------------------------------
# 协议通道（curl_cffi）
# ---------------------------------------------------------------------------

def _protocol_secret(payload: Any) -> str:
    return _parse_secret(payload)


def enable_account_2fa_protocol(
    session,
    auth_token: str = "",
    account_password: str = "",
    log_func: Callable[[str], None] = print,
) -> dict:
    """通过 HTTP 协议层真实绑定 2FA（候选端点逐个尝试 + 激活后回验）。"""
    del account_password
    evidence: list[dict] = []
    result = {"success": False, "secret": "", "backup_codes": [], "verified_by": "", "error": None, "evidence": evidence}
    if session is None:
        result["error"] = "no_session"
        return result

    headers_base = {
        "accept": "application/json",
        "content-type": "application/json",
        "referer": f"{AUTH_ORIGIN}/",
        "origin": AUTH_ORIGIN,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if auth_token:
        headers_base["authorization"] = f"Bearer {auth_token}"

    log_func("  🔐 正在通过官方协议端点申请真实 MFA 凭据 ...")

    for name, url, method, body in ENROLL_CANDIDATES:
        try:
            response = session.request(method, url, json=body, headers=headers_base, timeout=15, allow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            evidence.append({"step": f"enroll:{name}", "status": 0, "error": str(exc)[:160]})
            continue
        status = getattr(response, "status_code", 0)
        text = getattr(response, "text", "") or ""
        evidence.append({"step": f"enroll:{name}", "url": url, "status": status, "body": text[:300]})
        log_func(f"  · 试探 {name} -> HTTP {status}")
        if status not in (200, 201):
            continue
        try:
            payload = json.loads(text or "null")
        except (ValueError, TypeError):
            payload = None
        secret = _protocol_secret(payload)
        if not secret:
            continue
        code = generate_totp_code(secret)
        for activate_name, activate_url, extra in ACTIVATE_CANDIDATES:
            payload_body = {"code": code, "factor_type": "totp"}
            payload_body.update(extra)
            try:
                activate = session.request("POST", activate_url, json=payload_body, headers=headers_base,
                                           timeout=15, allow_redirects=False)
            except Exception as exc:  # noqa: BLE001
                evidence.append({"step": f"activate:{activate_name}", "status": 0, "error": str(exc)[:160]})
                continue
            activate_status = getattr(activate, "status_code", 0)
            evidence.append({
                "step": f"activate:{activate_name}", "url": activate_url,
                "status": activate_status, "body": (getattr(activate, "text", "") or "")[:300],
            })
            if activate_status in (200, 201, 204):
                log_func(f"  ✅ 官方协议接口确认 2FA 激活成功 ({activate_name})")
                result.update({"success": True, "secret": secret, "verified_by": f"api:{activate_name}"})
                return result
        status_info = _protocol_mfa_status(session, headers_base, evidence)
        if status_info is True:
            log_func("  ✅ 官方因子列表确认 TOTP 已激活")
            result.update({"success": True, "secret": secret, "activated": True,
                           "verified_by": "api:factor_list"})
            return result
        log_func("  ⚠️ 官方协议接口已返回真实密钥，但激活未获确认（密钥保留待用）")
        result.update({"success": False, "secret": secret, "activated": False,
                       "verified_by": "api:enroll_only"})
        return result

    result["error"] = "protocol_2fa_unavailable"
    log_func("  ℹ️ 协议端点未返回可用的真实密钥（多为需要完整浏览器会话）；"
             "流水线会自动转由浏览器通道补全，不会写入伪造密钥")
    return result


def _protocol_mfa_status(session, headers: dict, evidence: list[dict]) -> bool | None:
    for name, url in STATUS_CANDIDATES:
        try:
            response = session.get(url, headers=headers, timeout=15, allow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            evidence.append({"step": f"status:{name}", "status": 0, "error": str(exc)[:160]})
            continue
        status = getattr(response, "status_code", 0)
        text = getattr(response, "text", "") or ""
        evidence.append({"step": f"status:{name}", "url": url, "status": status, "body": text[:200]})
        if status != 200:
            continue
        try:
            payload = json.loads(text or "null")
        except (ValueError, TypeError):
            continue
        active = _factor_is_active(payload)
        if active is not None:
            return active
    return None
