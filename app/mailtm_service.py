"""
临时邮箱服务模块 - 基于 mail.tm 公共 API
API 文档: https://api.mail.tm
"""

import random
import string
import time

from .config import EMAIL_WAIT_TIMEOUT, EMAIL_POLL_INTERVAL, HTTP_TIMEOUT
from .utils import http_session, get_user_agent, extract_verification_code

MAILTM_API = "https://api.mail.tm"


def _get_available_domain():
    """获取 mail.tm 可用域名"""
    try:
        resp = http_session.get(
            f"{MAILTM_API}/domains",
            headers={"User-Agent": get_user_agent()},
            timeout=HTTP_TIMEOUT
        )
        if resp.status_code == 200:
            data = resp.json()
            members = data.get("hydra:member", [])
            if members:
                return members[0]["domain"]
    except Exception as e:
        print(f"  获取 mail.tm 域名失败: {e}")
    return None


def create_temp_email():
    """
    在 mail.tm 创建临时邮箱

    返回:
        tuple: (邮箱地址, JWT token, 邮箱密码)，失败返回 (None, None, None)
    """
    print("📧 正在创建 mail.tm 临时邮箱...")

    domain = _get_available_domain()
    if not domain:
        print("❌ 获取 mail.tm 域名失败")
        return None, None, None

    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    address = f"{username}@{domain}"
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

    headers = {
        "Content-Type": "application/json",
        "User-Agent": get_user_agent()
    }

    try:
        # 创建账号
        resp = http_session.post(
            f"{MAILTM_API}/accounts",
            headers=headers,
            json={"address": address, "password": password},
            timeout=HTTP_TIMEOUT
        )

        if resp.status_code not in (200, 201):
            print(f"❌ mail.tm 创建账号失败: HTTP {resp.status_code} - {resp.text[:200]}")
            return None, None, None

        # 获取 token
        token_resp = http_session.post(
            f"{MAILTM_API}/token",
            headers=headers,
            json={"address": address, "password": password},
            timeout=HTTP_TIMEOUT
        )

        if token_resp.status_code == 200:
            token = token_resp.json().get("token")
            if token:
                print(f"✅ mail.tm 邮箱创建成功: {address}")
                return address, token, password

        print(f"❌ 获取 mail.tm token 失败: HTTP {token_resp.status_code}")

    except Exception as e:
        print(f"❌ 创建 mail.tm 邮箱失败: {e}")

    return None, None, None


def login_existing_email(address: str, password: str, session=None):
    """
    使用已保存的 mail.tm 邮箱地址和密码重新换取 JWT token。
    """
    active_session = session or http_session
    headers = {
        "Content-Type": "application/json",
        "User-Agent": get_user_agent()
    }
    resp = active_session.post(
        f"{MAILTM_API}/token",
        headers=headers,
        json={"address": address, "password": password},
        timeout=HTTP_TIMEOUT
    )
    if resp.status_code != 200:
        raise RuntimeError(f"获取 mail.tm token 失败: HTTP {resp.status_code} - {resp.text[:200]}")
    token = resp.json().get("token")
    if not token:
        raise RuntimeError("mail.tm 未返回 token")
    return token


def wait_for_verification_email(token: str, timeout: int = None):
    """
    等待并提取 OpenAI 验证码

    参数:
        token: mail.tm JWT token
        timeout: 超时时间（秒），默认使用配置值

    返回:
        str: 验证码，未找到返回 None
    """
    if timeout is None:
        timeout = EMAIL_WAIT_TIMEOUT

    print(f"⏳ 正在等待验证邮件（最长 {timeout} 秒）...")
    start_time = time.time()

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": get_user_agent()
    }

    while time.time() - start_time < timeout:
        try:
            resp = http_session.get(
                f"{MAILTM_API}/messages",
                headers=headers,
                timeout=HTTP_TIMEOUT
            )

            if resp.status_code == 200:
                data = resp.json()
                messages = data.get("hydra:member", [])

                for msg in messages:
                    subject = msg.get("subject", "") or ""
                    from_info = msg.get("from", {}) or {}
                    from_addr = str(from_info.get("address", "")).lower()

                    if "openai" in from_addr or "chatgpt" in subject.lower():
                        print("\n📧 收到 OpenAI 验证邮件!")
                        print(f"   主题: {subject}")

                        # 先从主题提取
                        code = extract_verification_code(subject)
                        if code:
                            return code

                        # 获取邮件详情
                        msg_id = msg.get("id")
                        if msg_id:
                            detail_resp = http_session.get(
                                f"{MAILTM_API}/messages/{msg_id}",
                                headers=headers,
                                timeout=HTTP_TIMEOUT
                            )
                            if detail_resp.status_code == 200:
                                detail = detail_resp.json()
                                text_body = detail.get("text", "") or ""
                                html_list = detail.get("html", []) or []
                                html_body = html_list[0] if html_list else ""

                                for content in [text_body, html_body]:
                                    if content:
                                        code = extract_verification_code(content)
                                        if code:
                                            return code

        except Exception as e:
            print(f"  查询 mail.tm 邮件错误: {e}")

        elapsed = int(time.time() - start_time)
        print(f"  等待中... ({elapsed}秒)", end='\r')
        time.sleep(EMAIL_POLL_INTERVAL)

    print("\n⏰ 等待验证邮件超时")
    return None


def list_verification_codes(token: str) -> list[str]:
    """列出当前收件箱中可见的验证码，按最新邮件顺序返回。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": get_user_agent()
    }
    codes = []
    seen = set()
    try:
        resp = http_session.get(
            f"{MAILTM_API}/messages",
            headers=headers,
            timeout=HTTP_TIMEOUT
        )
        if resp.status_code != 200:
            return []
        messages = resp.json().get("hydra:member", [])
        for msg in messages[:12]:
            msg_id = msg.get("id")
            if not msg_id:
                continue
            detail_resp = http_session.get(
                f"{MAILTM_API}/messages/{msg_id}",
                headers=headers,
                timeout=HTTP_TIMEOUT
            )
            if detail_resp.status_code != 200:
                continue
            detail = detail_resp.json()
            parts = [
                msg.get("subject", "") or "",
                detail.get("text", "") or "",
            ]
            html_list = detail.get("html", []) or []
            parts.extend(str(item) for item in html_list if item)
            for content in parts:
                code = extract_verification_code(content)
                if code and code not in seen:
                    seen.add(code)
                    codes.append(code)
    except Exception as e:
        print(f"  列出 mail.tm 验证码失败: {e}")
    return codes


def fetch_message_text(token: str, limit: int = 5) -> str:
    """返回收件箱最新若干封邮件的正文拼接（用于提取官方重置密码链接等）。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": get_user_agent(),
    }
    parts: list[str] = []
    try:
        resp = http_session.get(f"{MAILTM_API}/messages", headers=headers, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return ""
        messages = resp.json().get("hydra:member", [])[:limit]
        for msg in messages:
            parts.append(str(msg.get("subject", "") or ""))
            msg_id = msg.get("id")
            if not msg_id:
                continue
            detail_resp = http_session.get(
                f"{MAILTM_API}/messages/{msg_id}", headers=headers, timeout=HTTP_TIMEOUT
            )
            if detail_resp.status_code != 200:
                continue
            detail = detail_resp.json()
            parts.append(str(detail.get("text", "") or ""))
            for item in detail.get("html", []) or []:
                parts.append(str(item))
    except Exception as e:  # noqa: BLE001
        print(f"  读取 mail.tm 邮件正文失败: {e}")
    return "\n".join(part for part in parts if part)
