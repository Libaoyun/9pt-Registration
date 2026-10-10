"""账号状态检测（保守判定 + 真实证据采集）。

本模块有两层:

1. ``check_account_status`` —— 历史兼容层。直接用 Bearer token 打 backend-api，
   拿不到证据就保持"未知"，绝不根据 Free 计划 / JWT 声明 / 邀请链接推断权益。
2. ``probe_account_state`` —— 真实状态采集层（见 ``account_probe``）。
   优先走浏览器内页 fetch（自带 Cloudflare clearance，最可靠），
   其次用已保存的 Web access token 走协议通道；把真实返回的原始报文、
   每个接口的 HTTP 状态与派生字段一起带回，供面板与报表直接展示，
   并把完整证据落盘到 ``data/tokens/state-<email>.json``。

ChatGPT backend routes are undocumented compatibility checks. Decoding JWTs does
not verify their signature or prove that an account is currently usable.
"""
from __future__ import annotations

import base64
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests as curl_requests

from .config import PROJECT_ROOT, cfg
from .utils import build_requests_proxies

UNKNOWN_QUOTA = "未知，请在官方页面确认"
UNKNOWN_EXPIRY = "未知，未取得订阅到期时间"
UNKNOWN_TRIAL = "待官方确认"
PLUS_TRIAL_CONFIRMED = "已确认有试用资格 (Plus 1个月试用)"
PROMO_UNCONFIRMED = "检测到活动，未确认为Plus试用"
_TZ = timezone(timedelta(hours=8))
# 只有这些词元与 plus 同时出现在同一个活动标识里，才算 Plus 试用证据。
_FREE_TRIAL_TERMS = frozenset({"free", "trial", "trialperiod"})


def _decode_jwt(token: str) -> dict:
    """Decode claims for display; do not treat them as verified identity."""
    try:
        if not isinstance(token, str):
            return {}
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, UnicodeError):
        return {}


def _get_token_record(email: str) -> dict:
    token_dir = Path(cfg.oauth.token_json_dir)
    if not token_dir.is_absolute():
        token_dir = PROJECT_ROOT / token_dir
    dirs = [token_dir, *(PROJECT_ROOT.parent / name / "data" / "tokens"
                         for name in ("gpt-auto-register", "gpt-protocol-register"))]
    found_records = []
    # Do not interpolate untrusted account email into a filesystem path.
    for directory in dict.fromkeys(path.resolve() for path in dirs):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                filename_matches = path.name in (f"web-{email}.json", f"codex-{email}.json", f"{email}.json")
                if (isinstance(record, dict) and record.get("access_token")
                        and (record.get("email") == email or
                             (not record.get("email") and filename_matches))):
                    if record.get("type") == "web":
                        return record
                    found_records.append(record)
            except (OSError, ValueError):
                continue
    return found_records[0] if found_records else {}


def _get_token_for_email(email: str) -> str | None:
    return _get_token_record(email).get("access_token")


def _format_timestamp_to_date(value) -> str:
    try:
        if isinstance(value, bool):
            return ""
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return datetime.fromtimestamp(float(value), tz=_TZ).strftime("%Y-%m-%d %H:%M:%S%z")
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return ""


def _normalize_plan(value) -> str | None:
    return {
        "free": "Free", "chatgptfreeplan": "Free",
        "plus": "Plus", "chatgptplusplan": "Plus",
        "team": "Team", "chatgptteamplan": "Team",
        "business": "Business", "pro": "Pro", "chatgptproplan": "Pro",
        "enterprise": "Enterprise",
    }.get(str(value).lower())


def promo_campaign_identifiers(campaign) -> list[str]:
    """列出活动标识：外层 key 以及内层 id/campaign_id/slug/name 字段。"""
    identifiers: list[str] = []
    if not isinstance(campaign, dict):
        return identifiers
    for key, value in campaign.items():
        identifiers.append(str(key))
        if isinstance(value, dict):
            for field in ("id", "campaign_id", "slug", "name"):
                token = value.get(field)
                if isinstance(token, str) and token.strip():
                    identifiers.append(token)
    return identifiers


def campaign_is_plus_trial(campaign) -> bool:
    """仅当单个活动标识自身同时含 plus 与免费/试用词元时，认定为 Plus 试用活动。

    活动列表非空只说明账号挂着某个活动，不能推出试用资格，因此不做关键字子串匹配。
    """
    for identifier in promo_campaign_identifiers(campaign):
        tokens = set(re.findall(r"[a-z0-9]+", identifier.lower()))
        if "plus" in tokens and tokens & _FREE_TRIAL_TERMS:
            return True
    return False


def check_account_status(email: str, access_token: str | None = None,
                         proxy: dict | None = None, timeout: int = 8,
                         token_kind: str | None = None) -> dict:
    if not access_token:
        record = _get_token_record(email)
        access_token = record.get("access_token")
        token_kind = token_kind or record.get("type")

    result = {
        "email": email, "is_paid": False, "plan": "未检测",
        "trial_status": UNKNOWN_TRIAL, "quota": UNKNOWN_QUOTA,
        "expires_at": UNKNOWN_EXPIRY, "account_status": "⚪ 未在线验证",
        "verified": False, "checked_at": datetime.now(_TZ).isoformat(),
        "details": {"source": "none", "signature_verified": False},
    }
    if not access_token:
        result["account_status"] = "⚪ 待检测(无Token)"
        result["details"]["reason"] = "no_token"
        return result
    if not isinstance(access_token, str):
        result["details"]["reason"] = "invalid_token_type"
        return result

    claims = _decode_jwt(access_token)
    auth = claims.get("https://api.openai.com/auth", {})
    if not isinstance(auth, dict):
        auth = {}
    plan_claim = _normalize_plan(auth.get("chatgpt_plan_type") or auth.get("plan_type", ""))
    if plan_claim:
        result["plan"] = f"未验证 ({plan_claim})"
    result["details"] = {"source": "jwt_unverified" if claims else "opaque_token",
                         "signature_verified": False, "plan_claim": plan_claim}
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and math.isfinite(exp):
        result["details"]["token_expires_at"] = _format_timestamp_to_date(exp)
        if exp <= time.time():
            result["account_status"] = "⚠️ Token已过期(本地声明)"
            result["details"]["reason"] = "local_token_expired"
            return result

    audiences = claims.get("aud", [])
    if isinstance(audiences, str):
        audiences = [audiences]
    if not isinstance(audiences, list):
        audiences = []
    if token_kind == "codex" or (token_kind != "web" and any(isinstance(aud, str) and aud.endswith("/v1") and "api.openai.com" in aud for aud in audiences)):
        result["account_status"] = "⚪ Codex凭证(未验证Web账号状态)"
        result["details"]["reason"] = "different_token_resource"
        return result


    req_kwargs = {"headers": {"Authorization": f"Bearer {access_token}",
                             "Accept": "application/json"}, "timeout": timeout}
    proxies = build_requests_proxies(proxy or {})
    if proxies:
        req_kwargs["proxies"] = proxies
    errors = []
    for source, route in (("api_check", "accounts/check/v4-2023-04-27"), ("api_me", "me")):
        try:
            response = curl_requests.get(f"https://chatgpt.com/backend-api/{route}", **req_kwargs)
            if response.status_code == 401:
                result["account_status"] = "⚠️ Token失效或凭证类型不匹配"
                result["details"].update(reason="unauthorized", source=source)
                return result
            if response.status_code == 403:
                if "deactivated" in response.text.lower():
                    result["account_status"] = "🚫 已封禁/停用"
                    result["details"].update(reason="account_deactivated", source=source)
                    return result
                errors.append({"source": source, "status": 403})
                continue
            if response.status_code != 200:
                errors.append({"source": source, "status": response.status_code})
                continue
            data = response.json()
            if not isinstance(data, dict):
                errors.append({"source": source, "reason": "unexpected_schema"})
                continue
            plan_info = data.get("account_plan")
            accounts_dict = data.get("accounts")
            if source == "api_check" and isinstance(accounts_dict, dict) and accounts_dict:
                # 现代 ChatGPT accounts/check/v4-2023-04-27 结构支持
                matched = False
                for acc_id, acc_info in accounts_dict.items():
                    if not isinstance(acc_info, dict):
                        continue
                    entitlement = acc_info.get("entitlement") or {}
                    account_obj = acc_info.get("account") or {}
                    raw_plan = entitlement.get("subscription_plan") or account_obj.get("plan_type") or acc_info.get("plan_type", "")
                    plan = _normalize_plan(raw_plan)
                    if plan is None:
                        continue
                    active = bool(entitlement.get("is_paid_subscription_active", False))
                    expiry = entitlement.get("expires_at") or entitlement.get("subscription_expires_at")
                    
                    # 识别 1 个月试用/活动资格
                    camp = acc_info.get("eligible_promo_campaigns") or data.get("eligible_promo_campaigns") or {}
                    trial_status = "无试用资格 (标准Free)"
                    quota = "标准免费额度"
                    if isinstance(camp, dict) and camp:
                        if campaign_is_plus_trial(camp):
                            trial_status = PLUS_TRIAL_CONFIRMED
                            quota = "1个月Plus免费试用待激活"
                        else:
                            # 有活动不等于有 Plus 试用资格，不得据此断言。
                            trial_status = PROMO_UNCONFIRMED
                            quota = UNKNOWN_QUOTA
                    elif active and plan == "Plus":
                        trial_status = "👑 已激活Plus会员"
                        quota = "Plus会员有效额度"

                    result["is_paid"] = active
                    result["plan"] = plan
                    result["trial_status"] = trial_status
                    result["quota"] = quota
                    if active and _format_timestamp_to_date(expiry):
                        result["expires_at"] = _format_timestamp_to_date(expiry)
                    elif not active:
                        result["expires_at"] = "无订阅 (标准免费)"
                    result.update(verified=True, account_status="🟢 在线已验证")
                    result["details"].update(source=source, eligible_promos=camp)
                    return result
                if not matched:
                    errors.append({"source": source, "reason": "no_valid_account_found"})
                    continue
            elif source == "api_check" and isinstance(plan_info, dict):
                plan = _normalize_plan(plan_info.get("subscription_plan", ""))
                active = plan_info.get("is_paid_subscription_active")
                if plan is None or not isinstance(active, bool) or (plan == "Free" and active):
                    errors.append({"source": source, "reason": "unexpected_schema"})
                    continue
                result["is_paid"] = active
                expiry = plan_info.get("subscription_expires_at")
                if active and _format_timestamp_to_date(expiry):
                    result["expires_at"] = _format_timestamp_to_date(expiry)
                # 兼容老版响应中附带的 eligible_promo_campaigns
                camp = data.get("eligible_promo_campaigns") or {}
                if isinstance(camp, dict) and camp:
                    if campaign_is_plus_trial(camp):
                        result["trial_status"] = PLUS_TRIAL_CONFIRMED
                        result["quota"] = "1个月Plus免费试用待激活"
                    else:
                        result["trial_status"] = PROMO_UNCONFIRMED
                        result["quota"] = UNKNOWN_QUOTA
            elif source == "api_me" and isinstance(plan_info, str):
                plan = _normalize_plan(plan_info)
                if plan is None:
                    continue
                # A plan name does not establish current billing/paid status.
            else:
                errors.append({"source": source, "reason": "unexpected_schema"})
                continue
            result.update(plan=plan, verified=True, account_status="🟢 在线已验证")
            result["details"].update(source=source, route_stability="undocumented")
            return result
        except (curl_requests.RequestException, ValueError, TypeError):
            errors.append({"source": source, "reason": "request_failed"})
    result["details"]["online_errors"] = errors
    return result


# ---------------------------------------------------------------------------
# 真实状态采集层（证据优先，供流水线 / 面板 / 报表使用）
# ---------------------------------------------------------------------------

def _token_dir_path() -> Path:
    token_dir = Path(cfg.oauth.token_json_dir)
    if not token_dir.is_absolute():
        token_dir = PROJECT_ROOT / token_dir
    token_dir.mkdir(parents=True, exist_ok=True)
    return token_dir


def state_to_check_result(state: dict) -> dict:
    """把采集到的真实状态映射为历史结果结构，便于沿用既有持久化逻辑。"""
    details = {
        "source": "official_evidence",
        "sources": state.get("sources", []),
        "is_plus": state.get("is_plus"),
        "trial_eligible": state.get("trial_eligible"),
        "plan_raw": state.get("plan_raw", ""),
        "plan_source": state.get("plan_source", ""),
        "signature_verified": False,
    }
    return {
        "email": state.get("email", ""),
        "is_paid": state.get("is_paid"),
        "is_plus": state.get("is_plus"),
        "plan": state.get("plan", "未检测"),
        "trial_status": state.get("trial_status", UNKNOWN_TRIAL),
        "trial_eligible": state.get("trial_eligible"),
        "quota": state.get("quota", UNKNOWN_QUOTA),
        "expires_at": state.get("expires_at", UNKNOWN_EXPIRY),
        "account_status": state.get("account_status", "⚪ 未在线验证"),
        "verified": bool(state.get("verified")),
        "checked_at": state.get("checked_at"),
        "details": details,
    }


def probe_account_state(
    email: str,
    *,
    driver=None,
    proxy: dict | None = None,
    session=None,
    token_kind: str = "web",
    timeout: int = 12,
    log=None,
) -> dict:
    """采集账号真实状态。

    通道优先级:
      1. 传入的已登录浏览器 driver（浏览器内页 fetch，自带 Cloudflare clearance）
      2. 传入的 HTTP session（先换 access token 再打 backend-api）
      3. 本地保存的 Web access token（``data/tokens/web-<email>.json``）
    拿不到凭证/接口不可达时返回"未知"状态，并如实记录原因。
    """
    from . import account_probe

    if driver is not None:
        state = account_probe.probe_in_browser(driver, email=email, timeout=max(timeout * 4, 30), log=log)
        if state.get("verified"):
            return state
        token = _get_token_for_email(email)
        if token:
            fallback = account_probe.probe_with_token(
                token, email=email, token_kind=token_kind, proxy=proxy, timeout=timeout
            )
            if fallback.get("verified"):
                fallback["evidence"]["fallback_from"] = "browser_probe"
                return fallback
        return state

    if session is not None:
        session_info = account_probe.fetch_session_access_token(session, proxy=proxy, timeout=timeout)
        if session_info.get("access_token"):
            return account_probe.probe_with_token(
                session_info["access_token"], email=email, token_kind=token_kind,
                proxy=proxy, timeout=timeout, session=session,
            )
        state = account_probe.new_state(email)
        state["sources"] = [{
            "name": "auth_session",
            "status": session_info.get("status", 0),
            "note": session_info.get("error") or "no_access_token_in_session",
        }]
        return state

    record = _get_token_record(email)
    token = record.get("access_token")
    kind = record.get("type") or token_kind
    if token and kind == "web":
        return account_probe.probe_with_token(token, email=email, token_kind="web", proxy=proxy, timeout=timeout)
    state = account_probe.new_state(email)
    state["sources"] = [{
        "name": "stored_token",
        "status": 0,
        "note": f"no_web_access_token (token_kind={kind or 'none'})",
    }]
    state["token_kind"] = kind or ""
    return state


def save_state_evidence(email: str, state: dict) -> str:
    """把完整证据（含原始报文）落盘，账号 TXT 里只保留摘要。"""
    sources = state.get("sources") or []
    if not any(entry.get("status") for entry in sources):
        # 完全没有拿到任何官方响应时不落盘，避免产生无意义文件
        return ""
    try:
        target = _token_dir_path() / f"state-{email}.json"
        target.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(target)
    except (OSError, TypeError, ValueError):
        return ""


def state_evidence_summary(state: dict, evidence_file: str = "") -> dict:
    """写入账号 TXT 第 13 字段的紧凑证据摘要（不塞原始报文，避免单行过大）。"""
    return {
        "verified": bool(state.get("verified")),
        "is_paid": state.get("is_paid"),
        "is_plus": state.get("is_plus"),
        "trial_eligible": state.get("trial_eligible"),
        "plan_raw": state.get("plan_raw", ""),
        "plan_source": state.get("plan_source", ""),
        "checked_at": state.get("checked_at"),
        "source": "official_evidence",
        "sources": state.get("sources", []),
        "evidence_file": evidence_file,
    }


def refresh_account_state(
    email: str,
    accounts_file: str,
    *,
    driver=None,
    proxy: dict | None = None,
    log=None,
) -> dict:
    """采集真实状态 → 落盘证据 → 更新账号 TXT。返回采集结果。"""
    from .stored_accounts import update_account_check_result_in_file

    state = probe_account_state(email, driver=driver, proxy=proxy, log=log)
    evidence_file = save_state_evidence(email, state)
    result = state_to_check_result(state)
    try:
        update_account_check_result_in_file(
            accounts_file,
            email,
            result["plan"],
            result["trial_status"],
            quota=result["quota"],
            expires_at=result["expires_at"],
            account_status=result["account_status"],
            evidence=state_evidence_summary(state, evidence_file),
        )
        result["persisted"] = True
    except Exception as exc:  # noqa: BLE001 - 持久化失败要显式暴露
        result["persisted"] = False
        result["persistence_error"] = str(exc)
    result["evidence_file"] = evidence_file
    result["state"] = state
    return result
