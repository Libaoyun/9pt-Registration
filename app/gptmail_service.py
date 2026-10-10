"""
GPTMail 邮箱服务模块 - 基于 mail.chatgpt.org.uk
先访问首页获取 gm_sid Cookie + JWT token，再通过 API 生成邮箱和收信。

来源: MasterAlanLab/register/openai_register.py -> GPTMailClient
"""

import re
import time
import uuid
import requests as _requests

from .config import EMAIL_WAIT_TIMEOUT, EMAIL_POLL_INTERVAL
from .utils import extract_verification_code

GPTMAIL_BASE_URL = "https://mail.chatgpt.org.uk"
API_GENERATE = f"{GPTMAIL_BASE_URL}/api/generate-email"
API_EMAILS = f"{GPTMAIL_BASE_URL}/api/emails"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# 会话表：session_id -> {"email": str, "client": GPTMailClient}
_sessions = {}


class GPTMailClient:
    """GPTMail API 客户端。"""

    def __init__(self):
        self.session = _requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{GPTMAIL_BASE_URL}/",
        })

    def _init_browser_session(self):
        """访问首页获取 gm_sid Cookie 和 JWT token。"""
        try:
            resp = self.session.get(GPTMAIL_BASE_URL, timeout=15)
            gm_sid = self.session.cookies.get("gm_sid")
            if gm_sid:
                self.session.headers.update({"Cookie": f"gm_sid={gm_sid}"})
            token_match = re.search(r"(eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)", resp.text)
            if token_match:
                self.session.headers.update({"x-inbox-token": token_match.group(1)})
        except Exception as e:
            print(f"  ⚠️ GPTMail 初始化会话失败: {e}")

    def generate_email(self):
        """生成邮箱，返回邮箱地址。"""
        self._init_browser_session()
        resp = self.session.get(API_GENERATE, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            email = data["data"]["email"]
            self.session.headers.update({"x-inbox-token": data["auth"]["token"]})
            return email
        raise RuntimeError(f"GPTMail 生成邮箱失败: HTTP {resp.status_code}")

    def list_emails(self, email):
        """获取收件箱邮件列表。"""
        encoded = _requests.utils.quote(email)
        resp = self.session.get(f"{API_EMAILS}?email={encoded}", timeout=15)
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("emails", [])
        return []


def create_temp_email(proxy=None):
    """
    创建 GPTMail 临时邮箱。
    返回: (email, session_id, None)
    """
    try:
        client = GPTMailClient()
        email = client.generate_email()
        print(f"✅ GPTMail 邮箱: {email}")

        session_id = str(uuid.uuid4())
        _sessions[session_id] = {"email": email, "client": client}
        return email, session_id, None
    except Exception as e:
        print(f"❌ 创建 GPTMail 邮箱失败: {e}")
        return None, None, None


def wait_for_verification_email(session_id, timeout=None):
    """
    轮询 GPTMail 收件箱，等待 OpenAI 验证码。
    返回: 验证码字符串 或 None
    """
    if timeout is None:
        timeout = EMAIL_WAIT_TIMEOUT

    session = _sessions.get(session_id)
    if not session:
        print("❌ 未找到 GPTMail 会话")
        return None

    email = session["email"]
    client = session["client"]
    openai_kw = ("openai", "noreply@openai", "chatgpt")

    print(f"⏳ 等待 GPTMail 验证邮件（收件箱: {email}，最长 {timeout}s）...")
    start = time.time()

    while time.time() - start < timeout:
        try:
            emails = client.list_emails(email)
            for mail in emails:
                from_addr = str(mail.get("from", "")).lower()
                body = " ".join([
                    str(mail.get("subject", "") or ""),
                    str(mail.get("text", "") or ""),
                    str(mail.get("body", "") or ""),
                    str(mail.get("html", "") or ""),
                ])

                is_openai = any(kw in from_addr for kw in openai_kw)
                if not is_openai:
                    is_openai = any(kw in body.lower() for kw in openai_kw)
                if not is_openai:
                    continue

                subj = mail.get("subject", "")
                print(f"\n📧 发现 OpenAI 邮件: {subj}")
                code = extract_verification_code(body)
                if code:
                    return code
        except Exception as e:
            print(f"  API 请求异常: {e}")

        elapsed = int(time.time() - start)
        print(f"  等待中... ({elapsed}s / {timeout}s)  收件箱: {email}", end="\r")
        time.sleep(EMAIL_POLL_INTERVAL)

    print(f"\n⏰ 等待 GPTMail 验证邮件超时（收件箱: {email}）")
    return None


def list_verification_codes(session_id):
    """列出 GPTMail 收件箱中的验证码候选。"""
    session = _sessions.get(session_id)
    if not session:
        return []

    email = session["email"]
    client = session["client"]
    openai_kw = ("openai", "noreply@openai", "chatgpt")
    codes = []
    seen = set()

    try:
        emails = client.list_emails(email)
        for mail in emails[:12]:
            from_addr = str(mail.get("from", "")).lower()
            body = " ".join([
                str(mail.get("subject", "") or ""),
                str(mail.get("text", "") or ""),
                str(mail.get("body", "") or ""),
                str(mail.get("html", "") or ""),
            ])

            is_openai = any(kw in from_addr for kw in openai_kw)
            if not is_openai:
                is_openai = any(kw in body.lower() for kw in openai_kw)
            if not is_openai:
                continue

            code = extract_verification_code(body)
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
    except Exception:
        pass

    return codes
