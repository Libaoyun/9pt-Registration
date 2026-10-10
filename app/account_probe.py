"""真实账号状态采集器 (Evidence-first account state probe)。

设计原则（"完全真实" 的工程含义）:

1. 只写入官方接口真实返回的内容。每个派生字段（计划 / Plus 状态 / 1 个月试用资格 /
   到期时间 / 额度）都携带 ``evidence`` 路径与原始报文片段，可以逐条回溯。
2. 拿不到证据就写"未知"，绝不用本地配置、邀请链接、Free 计划或注册成功来推断权益。
3. 每一次 HTTP 尝试都记录 ``(endpoint, status, note)``，失败原因可见、可复盘。
4. 双通道采集:
   - 浏览器内页 fetch：在已登录的 Chrome 页面上下文里发起请求，自带 Cloudflare
     clearance 与 Cookie，这是最可靠的通道（``probe_in_browser``）。
   - 协议 HTTP：用 curl_cffi + Bearer access token 直连 backend-api
     （``probe_with_token``）。被 Cloudflare 拦截时如实记录 403，不伪造成成功。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

_TZ = timezone(timedelta(hours=8))

CHAT_ORIGIN = "https://chatgpt.com"

UNKNOWN_PLAN = "未检测"
UNKNOWN_TRIAL = "待官方确认"
UNKNOWN_QUOTA = "未知，请在官方页面确认"
UNKNOWN_EXPIRY = "未知，未取得订阅到期时间"

TRIAL_CONFIRMED = "已确认有试用资格 (Plus 1个月试用)"
TRIAL_NONE = "无试用资格 (官方接口明确返回)"
TRIAL_ACTIVE_MEMBER = "👑 已激活Plus会员"
PROMO_UNCONFIRMED = "检测到活动，未确认为Plus试用"

STATUS_VERIFIED = "🟢 在线已验证"
STATUS_SESSION_ONLY = "🟡 会话有效 / 权益未取回"
STATUS_TOKEN_INVALID = "⚠️ Token失效或凭证类型不匹配"
STATUS_BANNED = "🚫 已封禁/停用"
STATUS_UNVERIFIED = "⚪ 未在线验证"
STATUS_BLOCKED = "🛡️ 接口被 Cloudflare 拦截"
STATUS_PARTIAL_403 = "🟡 部分接口 403 (风控/权限)"

# 后端路由（顺序即优先级；均为官方 frontend 实际调用的路径）
WEB_ROUTES: tuple[tuple[str, str], ...] = (
    ("me", f"{CHAT_ORIGIN}/backend-api/me"),
    ("accounts_check", f"{CHAT_ORIGIN}/backend-api/accounts/check/v4-2023-04-27"),
    ("subscriptions", f"{CHAT_ORIGIN}/backend-api/subscriptions"),
    ("subscription", f"{CHAT_ORIGIN}/backend-api/subscription"),
)

PLAN_KEYS = (
    "subscription_plan", "plan_type", "chatgpt_plan_type", "plan",
    "plan_display_name", "product_name", "sku", "tier", "subscription_tier",
)
ACTIVE_KEYS = (
    "is_paid_subscription_active", "has_active_subscription",
    "has_active_plus_subscription", "is_active", "active",
    "is_subscribed", "subscribed",
)
TRIAL_BOOL_KEYS = (
    "has_plus_trial_eligibility", "is_plus_trial_eligible", "plus_trial_eligible",
    "eligible_for_plus_trial", "is_eligible_for_plus_trial", "has_free_trial",
    "is_trial_eligible", "eligible_for_trial", "trial_eligible",
    "is_eligible_for_trial", "has_trial_available",
)
EXPIRY_KEYS = (
    "subscription_expires_at", "expires_at", "current_period_end", "renews_at",
    "paid_until", "trial_ends_at", "access_until", "valid_until", "next_billing_date",
)
PROMO_HINTS = ("promo", "campaign", "offer", "coupon", "discount", "voucher")
QUOTA_HINTS = ("credit", "quota", "rate_limit", "remaining", "balance")
PLUS_TOKENS = {"plus", "plusplan", "chatgptplusplan", "premium"}
TRIAL_TOKENS = {"free", "trial", "trialperiod", "1month", "month", "monthly", "nolimit"}
PRO_FAMILY_TOKENS = {"pro", "team", "business", "enterprise", "edu", "education", "go"}

_MAX_RAW = 20000


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _tokens(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(value).lower()))


def _walk(obj: Any, path: str = "$") -> Iterable[tuple[str, Any]]:
    """深度遍历任意 JSON 结构，产出 (路径, 值)。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}"
            yield child, value
            yield from _walk(value, child)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child = f"{path}[{index}]"
            yield child, value
            yield from _walk(value, child)


def new_state(email: str = "") -> dict:
    """未知基线状态：所有字段未取得证据。"""
    return {
        "email": email,
        "plan": UNKNOWN_PLAN,
        "plan_raw": "",
        "plan_source": "",
        "is_plus": None,
        "is_paid": None,
        "trial_eligible": None,
        "trial_status": UNKNOWN_TRIAL,
        "quota": UNKNOWN_QUOTA,
        "expires_at": UNKNOWN_EXPIRY,
        "account_status": STATUS_UNVERIFIED,
        "verified": False,
        "token_kind": "",
        "checked_at": _now_iso(),
        "sources": [],
        "evidence": {},
    }


def normalize_plan(value: Any) -> str | None:
    """把官方各种计划字符串归一化。

    真实取值样例（2026-10 实测）:
      - ``account.plan_type = "guest"`` / ``entitlement.subscription_plan = "chatgptguestplan"``  → 未订阅的免费账号
      - ``plan_display_name = "Free"`` → Free
    """
    tokens = _tokens(value)
    if not tokens:
        return None
    joined = "".join(sorted(tokens))
    # 官方常见形态是连写（chatgptplusplan / chatgptproplan），所以同时做词元与子串判定
    if tokens & PLUS_TOKENS or "plus" in joined:
        return "Plus"
    if "pro" in tokens or "pro" in joined:
        return "Pro"
    if "team" in tokens or "team" in joined:
        return "Team"
    if "business" in tokens or "business" in joined:
        return "Business"
    if "enterprise" in tokens or "enterprise" in joined:
        return "Enterprise"
    if tokens & {"edu", "education"} or "edu" in joined:
        return "Edu"
    if "go" in tokens:
        return "Go"
    if tokens & {"free", "basic", "default"} or "free" in joined:
        return "Free"
    # 新建账号在官方接口里就是 guest / guestplan / no plan
    if tokens & {"guest", "guestplan", "noplan", "none"} or "guest" in joined:
        return "Free"
    return None


def _format_expiry(value: Any) -> str:
    """把官方返回的到期时间转成本地可读时间；无法解析时原样返回。"""
    try:
        if isinstance(value, bool) or value in (None, ""):
            return ""
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            moment = datetime.fromtimestamp(float(value), tz=_TZ)
            return moment.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, str):
            text = value.strip().replace("Z", "+00:00")
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError, TypeError):
        pass
    return str(value)


def build_otpauth_url(email: str, secret: str, issuer: str = "OpenAI") -> str:
    if not secret:
        return ""
    from urllib.parse import quote

    return (
        f"otpauth://totp/{quote(issuer)}:{quote(email or 'account')}"
        f"?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )


# ---------------------------------------------------------------------------
# 解析：从官方原始报文中提取可回溯的证据
# ---------------------------------------------------------------------------

def _collect(payloads: dict[str, str], keys: tuple[str, ...]) -> list[tuple[str, str, Any]]:
    """在所有 payload 中按 key 名收集 (source, path, value)。"""
    found: list[tuple[str, str, Any]] = []
    for source, text in payloads.items():
        data = _safe_json(text)
        if data is None:
            continue
        for path, value in _walk(data):
            leaf = path.rsplit(".", 1)[-1].split("[")[0].lower()
            if leaf in keys and not isinstance(value, (dict, list)):
                found.append((source, path, value))
    return found


def _safe_json(text: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _scalar_strings(value: Any) -> list[str]:
    """递归收集结构里的字符串（活动名 / 活动 id / 档位名等）。"""
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            out.append(str(key))
            out.extend(_scalar_strings(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_scalar_strings(item))
    elif isinstance(value, str) and value.strip():
        out.append(value.strip())
    return out


def promo_identifiers(payloads: dict[str, str]) -> list[tuple[str, str]]:
    """列出所有和"活动/试用"相关的标识 (source, identifier)。

    实测真实结构（2026-10）::

        accounts.<id>.eligible_promo_campaigns = {}            # 空 = 官方没有给该账号任何试用活动
        accounts.<id>.account.promo_data = {}
        accounts.<id>.eligible_offers.offers[].id = "chatgptfreeplan"

    注意 ``eligible_offers`` 只是"可购买档位"，不是试用活动（免费档也会出现在里面），
    因此只把它记录到证据里，不参与试用判定，避免把 Free 档误判成试用资格。
    """
    identifiers: list[tuple[str, str]] = []
    for source, text in payloads.items():
        data = _safe_json(text)
        if data is None:
            continue
        for path, value in _walk(data):
            leaf = path.rsplit(".", 1)[-1].split("[")[0].lower()
            if "offer" in leaf and "promo" not in leaf:
                continue  # eligible_offers / offers 不是试用活动
            if any(hint in leaf for hint in PROMO_HINTS):
                if isinstance(value, (dict, list)):
                    for scalar in _scalar_strings(value):
                        identifiers.append((source, f"{path}={scalar}"))
                elif value not in (None, "", False, 0):
                    identifiers.append((source, f"{path}={value}"))
    return identifiers


def _promo_container_state(payloads: dict[str, str]) -> dict:
    """看 ``eligible_promo_campaigns`` / ``promo_data`` 是否被明确返回为空。

    真实返回里它们是空 dict（``{}``），这是一个"官方明确没给试用活动"的正向证据，
    可以据此给出"无试用资格"结论，而不是永远停在"待官方确认"。
    """
    found_keys: list[str] = []
    empty_flags: list[bool] = []
    for source, text in payloads.items():
        data = _safe_json(text)
        if data is None:
            continue
        for path, value in _walk(data):
            leaf = path.rsplit(".", 1)[-1].split("[")[0].lower()
            if leaf in ("eligible_promo_campaigns", "promo_campaigns", "promo_data", "eligible_promotions"):
                found_keys.append(f"{source}:{path}")
                if isinstance(value, (dict, list)):
                    empty_flags.append(len(value) == 0)
    return {
        "keys": found_keys,
        "present": bool(found_keys),
        "all_empty": bool(empty_flags) and all(empty_flags),
    }


def _plus_offer_eligibility_flags(payloads: dict[str, str]) -> dict:
    """官方针对 Plus 促销档位的资格布尔字段（实测存在）。"""
    flags: dict[str, Any] = {}
    for source, text in payloads.items():
        data = _safe_json(text)
        if data is None:
            continue
        for path, value in _walk(data):
            leaf = path.rsplit(".", 1)[-1].split("[")[0].lower()
            if leaf.startswith("is_eligible_for") and isinstance(value, bool):
                flags[f"{source}:{path}"] = value
    return flags


def identify_plus_trial(payloads: dict[str, str]) -> tuple[bool | None, str, dict]:
    """判定 1 个月 Plus 试用资格。

    返回 (eligible, 说明, 证据)。eligible 为 None 表示"证据不足，保持未知"。

    判定顺序（全部基于官方真实返回）:
      a) 明确的试用布尔字段 (has_plus_trial_eligibility 等) 为 True → 有资格
      b) 同一活动标识自身同时含 plus 与 免费/试用 词元 (如 plus-1-month-free) → 有资格
      c) eligible_promo_campaigns / promo_data 被官方明确返回为空，
         且所有 Plus 促销资格布尔字段均为 False、且该账号从未付费 → 无试用资格
      d) 其余情况保持未知（不臆断）
    """
    evidence: dict[str, Any] = {}

    booleans = _collect(payloads, TRIAL_BOOL_KEYS)
    if booleans:
        evidence["trial_boolean_fields"] = [
            {"source": s, "path": p, "value": v} for s, p, v in booleans
        ]
        truthy = [b for b in booleans if b[2] is True]
        if truthy:
            src, path, _ = truthy[0]
            return True, TRIAL_CONFIRMED, evidence

    identifiers = promo_identifiers(payloads)
    evidence["promo_identifiers"] = [
        {"source": s, "identifier": i} for s, i in identifiers
    ]
    for _source, identifier in identifiers:
        tokens = _tokens(identifier.replace("=", " ").replace(".", " "))
        has_plus = bool(tokens & PLUS_TOKENS) or "plus" in tokens
        has_free = bool(tokens & TRIAL_TOKENS)
        if has_plus and has_free:
            return True, TRIAL_CONFIRMED, evidence

    container = _promo_container_state(payloads)
    flags = _plus_offer_eligibility_flags(payloads)
    evidence["promo_container"] = container
    evidence["plus_offer_flags"] = flags

    never_paid = any(
        value is False
        for _s, path, value in _collect(payloads, ("has_previously_paid_subscription", "was_paid_customer"))
        for _ in (0,)
    )
    plus_flags_false = bool(flags) and not any(flags.values())
    if container.get("present") and container.get("all_empty") and (plus_flags_false or never_paid):
        evidence["trial_absence_reason"] = (
            "eligible_promo_campaigns 为空 + Plus 促销资格字段全为 false/从未付费"
        )
        return False, TRIAL_NONE, evidence

    if booleans:
        return False, TRIAL_NONE, evidence

    if identifiers:
        return None, PROMO_UNCONFIRMED, evidence

    return None, UNKNOWN_TRIAL, evidence


def _summarize_quota(payloads: dict[str, str]) -> tuple[str, dict]:
    """只在官方报文里真的出现了额度/速率字段时给出真实值。"""
    for source, text in payloads.items():
        data = _safe_json(text)
        if data is None:
            continue
        for path, value in _walk(data):
            leaf = path.rsplit(".", 1)[-1].split("[")[0].lower()
            if not any(hint in leaf for hint in QUOTA_HINTS):
                continue
            if isinstance(value, (dict, list)) or value in (None, "", False):
                continue
            if leaf in ("remaining", "credit_balance", "credits", "quota", "balance"):
                return f"{value} (来自 {source}:{path})", {"source": source, "path": path, "value": value}
    return UNKNOWN_QUOTA, {}


def derive_state(
    email: str,
    payloads: dict[str, str],
    requests_log: list[dict],
    *,
    token_kind: str = "web",
    session_payload: Any = None,
) -> dict:
    """由官方原始报文推导状态。所有字段都来自 payloads。"""
    state = new_state(email)
    state["token_kind"] = token_kind
    state["sources"] = requests_log
    state["evidence"] = {
        "raw": {k: (v[:_MAX_RAW] if isinstance(v, str) else v) for k, v in payloads.items()},
        "requests": requests_log,
    }

    ok = [entry for entry in requests_log if entry.get("status") == 200]
    blocked = [entry for entry in requests_log if entry.get("status") == 403]
    unauthorized = [entry for entry in requests_log if entry.get("status") == 401]
    deactivated = any(
        entry.get("status") == 403 and "deactivated" in str(entry.get("note", "")).lower()
        for entry in requests_log
    )

    if deactivated:
        state["account_status"] = STATUS_BANNED
        state["verified"] = True
        return state

    if not ok:
        if unauthorized:
            state["account_status"] = STATUS_TOKEN_INVALID
        elif blocked:
            state["account_status"] = STATUS_BLOCKED
        else:
            state["account_status"] = STATUS_UNVERIFIED
        return state

    # 1. 计划 / 活跃订阅
    plan_candidates = _collect(payloads, PLAN_KEYS)
    for _source, path, value in plan_candidates:
        plan = normalize_plan(value)
        if plan:
            state["plan"] = plan
            state["plan_raw"] = str(value)
            state["plan_source"] = path
            break
    state["evidence"]["plan_candidates"] = [
        {"path": p, "value": str(v)} for _s, p, v in plan_candidates[:20]
    ]

    active_flags = _collect(payloads, ACTIVE_KEYS)
    state["evidence"]["active_flags"] = [
        {"path": p, "value": v} for _s, p, v in active_flags[:20]
    ]
    active_true = any(v is True for _s, _p, v in active_flags)
    explicit_inactive = bool(active_flags) and not active_true

    plan_is_plus = state["plan"] == "Plus"
    if active_flags:
        state["is_paid"] = active_true
        state["is_plus"] = bool(active_true and plan_is_plus)
    else:
        state["is_paid"] = None
        state["is_plus"] = None

    # 2. 到期时间
    expiries = _collect(payloads, EXPIRY_KEYS)
    formatted_expiry = ""
    expiry_all_null = bool(expiries) and all(value in (None, "", False) for _s, _p, value in expiries)
    for source, path, value in expiries:
        formatted = _format_expiry(value)
        if formatted:
            formatted_expiry = formatted
            state["evidence"]["expires_source"] = {"source": source, "path": path, "raw": str(value)}
            break
    if formatted_expiry:
        state["expires_at"] = formatted_expiry
    elif expiry_all_null and state.get("is_paid") is False:
        # 官方明确返回 expires_at = null 且没有有效订阅 → 这是真实的"没有订阅有效期"
        state["expires_at"] = "无订阅有效期（免费计划）"
        state["evidence"]["expires_source"] = {"source": "entitlement", "path": "expires_at", "raw": "null"}
    elif not expiries:
        state["expires_at"] = UNKNOWN_EXPIRY

    # 3. 1 个月 Plus 试用资格
    eligible, trial_note, trial_evidence = identify_plus_trial(payloads)
    state["trial_eligible"] = eligible
    state["evidence"]["trial"] = trial_evidence
    if eligible:
        state["trial_status"] = trial_note
    elif eligible is False:
        state["trial_status"] = trial_note
    elif state["is_plus"] is True:
        state["trial_status"] = TRIAL_ACTIVE_MEMBER
    else:
        state["trial_status"] = trial_note

    # 4. 额度
    quota, quota_evidence = _summarize_quota(payloads)
    state["quota"] = quota
    if quota_evidence:
        state["evidence"]["quota"] = quota_evidence

    # 5. 会话信息（用户 id、邮箱、计划声明）
    session_data = _safe_json(session_payload) if session_payload is not None else None
    if isinstance(session_data, dict):
        state["evidence"]["session"] = {
            "user_id": (session_data.get("user") or {}).get("id"),
            "email": (session_data.get("user") or {}).get("email"),
            "plan_type": (session_data.get("account") or {}).get("planType")
            if isinstance(session_data.get("account"), dict) else None,
            "expires": session_data.get("expires"),
        }

    # 只有真的解析出权益/计划/到期任一证据，才算"在线已验证"
    has_entitlement_evidence = bool(plan_candidates or active_flags or expiries)
    state["verified"] = bool(ok) and has_entitlement_evidence
    if state["verified"]:
        state["account_status"] = STATUS_VERIFIED
    elif ok:
        state["account_status"] = STATUS_SESSION_ONLY
    elif blocked:
        state["account_status"] = STATUS_PARTIAL_403
    state["checked_at"] = _now_iso()
    return state


# ---------------------------------------------------------------------------
# 通道一：协议 HTTP（Bearer access token）
# ---------------------------------------------------------------------------

def _http_session(proxy: dict | None = None):
    """优先 curl_cffi（Chrome TLS 指纹），不可用时回退 requests。"""
    try:
        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(impersonate="chrome131", verify=False)
    except Exception:
        import requests as curl_requests

        session = curl_requests.Session()
    if proxy:
        try:
            from .utils import build_requests_proxies

            proxies = build_requests_proxies(proxy)
        except Exception:
            proxies = {}
        if proxies:
            session.proxies = proxies
    return session


def fetch_session_access_token(session, proxy: dict | None = None, timeout: int = 15) -> dict:
    """用已登录的 HTTP 会话（cookie）换取真实 Web access token。

    ChatGPT Web 的 `/api/auth/session` 返回 ``accessToken``，这是 backend-api 唯一
    被认可的 Bearer 凭据；next-auth 的 session cookie 不能直接当 Bearer 使用。
    """
    headers = {
        "accept": "application/json",
        "referer": f"{CHAT_ORIGIN}/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    try:
        response = session.get(f"{CHAT_ORIGIN}/api/auth/session", headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 记录真实异常即可
        return {"ok": False, "status": 0, "error": str(exc), "access_token": ""}
    text = getattr(response, "text", "") or ""
    payload = _safe_json(text) or {}
    return {
        "ok": response.status_code == 200 and bool(payload.get("accessToken")),
        "status": response.status_code,
        "access_token": payload.get("accessToken", "") or "",
        "expires": payload.get("expires", ""),
        "user": payload.get("user") or {},
        "account": payload.get("account") or {},
        "payload": payload,
        "raw": text[:2000],
    }


def probe_with_token(
    access_token: str,
    *,
    email: str = "",
    token_kind: str = "web",
    proxy: dict | None = None,
    timeout: int = 12,
    session=None,
    headers_extra: dict | None = None,
) -> dict:
    """用 Bearer token 直连官方 backend-api，采集真实状态。"""
    state = new_state(email)
    if not access_token:
        state["sources"] = [{"name": "token", "status": 0, "note": "no_access_token"}]
        return state

    owns_session = session is None
    session = session or _http_session(proxy)
    # ★ 完整前端客户端标识：与浏览器 TLS 指纹一致的 Chrome 版本 + 官方部署版本号。
    #   参考项目实测：缺失 oai-client-build-number / oai-client-version 时，
    #   OpenAI 常返回空的 eligible_promo_campaigns（显示为"无 Plus 试用资格"）。
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {access_token}",
        "referer": f"{CHAT_ORIGIN}/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Google Chrome";v="150", "Chromium";v="150", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "oai-client-build-number": os.environ.get("OAI_CLIENT_BUILD_NUMBER", "10577136").strip(),
        "oai-client-version": os.environ.get(
            "OAI_CLIENT_VERSION", "prod-8bfe9e3526fbf9900f9332d46fef7bc0065c4478"
        ).strip(),
    }
    if headers_extra:
        headers.update(headers_extra)

    payloads: dict[str, str] = {}
    log: list[dict] = []
    for name, url in WEB_ROUTES:
        # accounts/check 需带 timezone_offset_min（官方前端就是这么请求的，
        # 缺失时可能影响 eligible_promo_campaigns 的下发）
        request_url = url
        if name == "accounts_check" and "timezone_offset_min" not in url:
            request_url = f"{url}?timezone_offset_min={os.environ.get('TZ_OFFSET_MIN', '-480')}"
        try:
            response = session.get(request_url, headers=headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.append({"name": name, "url": url, "status": 0, "note": str(exc)[:160]})
            continue
        text = getattr(response, "text", "") or ""
        note = ""
        if response.status_code == 403:
            head = text[:200].replace("\n", " ")
            note = "deactivated" if "deactivated" in text.lower() else f"cloudflare_or_forbidden: {head[:120]}"
        log.append({"name": name, "url": url, "status": response.status_code, "note": note})
        if response.status_code == 200:
            payloads[name] = text
        if name == "accounts_check" and response.status_code == 200:
            # 拿到权益数据即可停止，避免多余请求
            break

    derived = derive_state(email, payloads, log, token_kind=token_kind)
    if owns_session:
        try:
            session.close()
        except Exception:
            pass
    return derived


# ---------------------------------------------------------------------------
# 通道二：浏览器内页 fetch（最可靠，自带 Cloudflare clearance 与 Cookie）
# ---------------------------------------------------------------------------

BROWSER_PROBE_JS = r"""
return (async () => {
  const out = { __origin: location.origin, __href: location.href };
  const endpoints = [
    ['session', '/api/auth/session'],
    ['me', '/backend-api/me'],
    ['accounts_check', '/backend-api/accounts/check/v4-2023-04-27'],
    ['subscriptions', '/backend-api/subscriptions'],
    ['subscription', '/backend-api/subscription'],
  ];
  for (const [name, url] of endpoints) {
    try {
      const resp = await fetch(url, {
        credentials: 'include',
        headers: { 'accept': 'application/json' },
      });
      let text = '';
      try { text = await resp.text(); } catch (e) { text = ''; }
      out[name] = { status: resp.status, text: (text || '').slice(0, 20000) };
    } catch (err) {
      out[name] = { status: 0, error: String(err) };
    }
  }
  return out;
})()
"""


def probe_in_browser(driver, *, email: str = "", timeout: int = 45, log: Callable[[str], None] | None = None) -> dict:
    """在已登录浏览器页面上下文里采集真实状态（推荐通道）。"""
    def _log(message: str) -> None:
        if log:
            log(message)

    state = new_state(email)
    if driver is None:
        state["sources"] = [{"name": "browser", "status": 0, "note": "no_driver"}]
        return state

    original_timeout = None
    try:
        original_timeout = driver.timeouts.script
    except Exception:
        pass
    try:
        try:
            driver.set_script_timeout(timeout)
        except Exception:
            pass
        if "chatgpt.com" not in (driver.current_url or ""):
            driver.get(f"{CHAT_ORIGIN}/")
            time.sleep(2)
        raw = driver.execute_script(BROWSER_PROBE_JS)
    except Exception as exc:  # noqa: BLE001
        state["sources"] = [{"name": "browser", "status": 0, "note": f"execute_script_failed: {exc}"[:160]}]
        return state
    finally:
        if original_timeout:
            try:
                driver.set_script_timeout(original_timeout)
            except Exception:
                pass

    if not isinstance(raw, dict):
        state["sources"] = [{"name": "browser", "status": 0, "note": "unexpected_result"}]
        return state

    payloads: dict[str, str] = {}
    requests_log: list[dict] = []
    session_payload: Any = None

    def _parse(raw_result: dict):
        nonlocal session_payload
        for name in ("session", "me", "accounts_check", "subscriptions", "subscription"):
            entry = raw_result.get(name)
            if not isinstance(entry, dict):
                continue
            status = entry.get("status", 0)
            text = entry.get("text", "") or ""
            requests_log.append({
                "name": name,
                "url": f"{raw_result.get('__origin', CHAT_ORIGIN)}" + {
                    "session": "/api/auth/session",
                    "me": "/backend-api/me",
                    "accounts_check": "/backend-api/accounts/check/v4-2023-04-27",
                    "subscriptions": "/backend-api/subscriptions",
                    "subscription": "/backend-api/subscription",
                }[name],
                "status": status,
                "note": entry.get("error", "") or "",
            })
            if status == 200 and text:
                if name == "session":
                    session_payload = text
                else:
                    payloads[name] = text
            _log(f"  🌐 [probe] {name} -> HTTP {status}")

    _parse(raw)
    # 全被 Cloudflare 拦截（403/0）时重载页面再采一次，避免把瞬时风控当成"未验证"
    if not payloads and not session_payload and any(
        entry.get("status") in (403, 0) for entry in requests_log
    ):
        _log("  ⚠️ 首次采集被风控拦截，重载页面后重试一次...")
        try:
            driver.get(f"{CHAT_ORIGIN}/")
            time.sleep(4)
            retry_raw = driver.execute_script(BROWSER_PROBE_JS)
        except Exception:
            retry_raw = None
        if isinstance(retry_raw, dict):
            _parse(retry_raw)
            raw = retry_raw

    derived = derive_state(email, payloads, requests_log, token_kind="web", session_payload=session_payload)
    derived["evidence"]["browser_origin"] = raw.get("__origin")
    return derived
