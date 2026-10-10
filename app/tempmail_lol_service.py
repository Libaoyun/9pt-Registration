"""
TempMail.lol 邮箱服务模块 - 基于 api.tempmail.lol
通过 REST API 创建邮箱和轮询收件箱，纯 API 不依赖浏览器。

来源: MasterAlanLab/register/openai_register.py -> EMail
"""

import re
import time
import uuid
import requests as _requests

from .config import EMAIL_WAIT_TIMEOUT, EMAIL_POLL_INTERVAL
from .utils import extract_verification_code

API_CREATE = "https://api.tempmail.lol/v2/inbox/create"
API_INBOX = "https://api.tempmail.lol/v2/inbox"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# 会话表：session_id -> {"email": str, "token": str}
_sessions = {}


class TempMailLolClient:
    """TempMail.lol API 客户端。"""

    def __init__(self):
        self.session = _requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self.address = None
        self.token = None

    def create_inbox(self):
        """创建临时邮箱。"""
        resp = self.session.post(API_CREATE, json={}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self.address = data["address"]
        self.token = data["token"]
        return self.address

    def get_messages(self):
        """获取收件箱邮件。"""
        resp = self.session.get(f"{API_INBOX}?token={self.token}", timeout=15)
        resp.raise_for_status()
        return resp.json().get("emails", [])


def create_temp_email(proxy=None):
    """
    创建 TempMail.lol 临时邮箱。
    返回: (email, session_id, token)
    """
    try:
        client = TempMailLolClient()
        email = client.create_inbox()
        print(f"✅ TempMail.lol 邮箱: {email}")

        session_id = str(uuid.uuid4())
        _sessions[session_id] = {"email": email, "token": client.token, "client": client}
        return email, session_id, client.token
    except Exception as e:
        print(f"❌ 创建 TempMail.lol 邮箱失败: {e}")
        return None, None, None


def wait_for_verification_email(session_id, timeout=None):
    """
    轮询 TempMail.lol 收件箱，等待 OpenAI 验证码。
    返回: 验证码字符串 或 None
    """
    if timeout is None:
        timeout = EMAIL_WAIT_TIMEOUT

    session = _sessions.get(session_id)
    if not session:
        print("❌ 未找到 TempMail.lol 会话")
        return None

    email = session["email"]
    client = session["client"]
    openai_kw = ("openai", "noreply@openai", "chatgpt")

    print(f"⏳ 等待 TempMail.lol 验证邮件（收件箱: {email}，最长 {timeout}s）...")
    start = time.time()

    while time.time() - start < timeout:
        try:
            msgs = client.get_messages()
            for msg in msgs:
                from_addr = str(msg.get("from", "")).lower()
                body = str(msg.get("body", "") or "")
                html_body = str(msg.get("html", "") or "")
                subject = str(msg.get("subject", "") or "")
                full_body = f"{subject} {body} {html_body}"

                is_openai = any(kw in from_addr for kw in openai_kw)
                if not is_openai:
                    is_openai = any(kw in full_body.lower() for kw in openai_kw)
                if not is_openai:
                    continue

                print(f"\n📧 发现 OpenAI 邮件: {subject}")
                code = extract_verification_code(full_body)
                if code:
                    return code
        except Exception as e:
            print(f"  API 请求异常: {e}")

        elapsed = int(time.time() - start)
        print(f"  等待中... ({elapsed}s / {timeout}s)  收件箱: {email}", end="\r")
        time.sleep(EMAIL_POLL_INTERVAL)

    print(f"\n⏰ 等待 TempMail.lol 验证邮件超时（收件箱: {email}）")
    return None


def list_verification_codes(session_id):
    """列出 TempMail.lol 收件箱中的验证码候选。"""
    session = _sessions.get(session_id)
    if not session:
        return []

    client = session["client"]
    openai_kw = ("openai", "noreply@openai", "chatgpt")
    codes = []
    seen = set()

    try:
        msgs = client.get_messages()
        for msg in msgs[:12]:
            from_addr = str(msg.get("from", "")).lower()
            body = str(msg.get("body", "") or "")
            html_body = str(msg.get("html", "") or "")
            subject = str(msg.get("subject", "") or "")
            full_body = f"{subject} {body} {html_body}"

            is_openai = any(kw in from_addr for kw in openai_kw)
            if not is_openai:
                is_openai = any(kw in full_body.lower() for kw in openai_kw)
            if not is_openai:
                continue

            code = extract_verification_code(full_body)
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
    except Exception:
        pass

    return codes


def fetch_message_text(session_id, limit: int = 5) -> str:
    """返回 TempMail.lol 收件箱最新若干封邮件的正文拼接。"""
    session = _sessions.get(session_id)
    if not session:
        return ""
    parts: list[str] = []
    try:
        for msg in (session["client"].get_messages() or [])[:limit]:
            parts.append(str(msg.get("subject", "") or ""))
            parts.append(str(msg.get("body", "") or ""))
            parts.append(str(msg.get("html", "") or ""))
    except Exception as e:  # noqa: BLE001
        print(f"  读取 TempMail.lol 邮件正文失败: {e}")
    return "\n".join(part for part in parts if part)
