"""
临时邮箱服务模块 - 基于 Temporam.com（Cookie 缓存 + REST API）

优化方案（已验证）：
  - 一份 Cookie 可查任意域名下任意邮箱的收件箱
  - 整个程序生命周期只开一次浏览器拿 Cookie，之后复用
  - create_temp_email() 直接随机生成邮箱地址，无需 UI 操作
  - 收信全程走 REST API，不依赖浏览器 UI

网址: https://temporam.com/zh
"""

import time
import random
import threading
import uuid

import requests as _requests
import undetected_chromedriver as uc

from .config import EMAIL_WAIT_TIMEOUT, EMAIL_POLL_INTERVAL
from .utils import extract_verification_code

TEMPORAM_URL = "https://temporam.com/zh"
API_MESSAGES = "https://temporam.com/api/email/messages?email={email}"

# 可用域名（已验证 Cookie 鉴权均返回 200）
AVAILABLE_DOMAINS = ["nooboy.com"]

# ── 全局 Cookie 缓存 ──────────────────────────────────────
_cookies: dict = {}
_cookies_lock = threading.Lock()

# 会话表：session_id -> {"email": str}
_sessions: dict = {}
_sessions_lock = threading.Lock()


# ──────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────

def _fetch_cookies(proxy: dict = None) -> dict:
    """打开浏览器访问 Temporam，拿到 Cookie 后立即关闭浏览器。"""
    print("🌐 正在获取 Temporam Cookie（仅需一次）...")
    options = uc.ChromeOptions()
    options.add_argument("--window-position=-9000,-9000")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--lang=zh-CN,zh;q=0.9")
    from .browser import SafeChrome, apply_proxy_to_options, _detect_chrome_major_version
    apply_proxy_to_options(options, proxy)
    chrome_major_version = _detect_chrome_major_version()
    chrome_kwargs = {
        "options": options,
        "use_subprocess": True,
        "headless": False,
    }
    if chrome_major_version is not None:
        chrome_kwargs["version_main"] = chrome_major_version
    driver = SafeChrome(**chrome_kwargs)
    try:
        driver.get(TEMPORAM_URL)
        time.sleep(5)
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        print(f"  ✅ 获取到 {len(cookies)} 个 Cookie")
        return cookies
    finally:
        try:
            driver.quit()
            print("  🔒 Cookie 浏览器已关闭")
        except Exception:
            pass


def _ensure_cookies(proxy: dict = None) -> dict:
    """返回全局 Cookie，如果尚未初始化则先获取。"""
    global _cookies
    with _cookies_lock:
        if not _cookies:
            _cookies = _fetch_cookies(proxy=proxy)
        return dict(_cookies)


def _api_get_messages(cookies: dict, email: str) -> list:
    """调用 Temporam API 获取邮件列表，失败返回空列表。"""
    url = API_MESSAGES.format(email=_requests.utils.quote(email))
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": TEMPORAM_URL,
    }
    try:
        resp = _requests.get(url, cookies=cookies, headers=headers, timeout=10)
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return [{"content": resp.text}]
        # Cookie 过期（403）时清缓存，下次自动刷新
        if resp.status_code == 403:
            global _cookies
            with _cookies_lock:
                _cookies = {}
            print("  ⚠️ Cookie 已过期，下次调用将重新获取")
        return []
    except Exception as e:
        print(f"  API 请求异常: {e}")
        return []


# ──────────────────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────────────────

def create_temp_email(proxy: dict = None):
    """
    随机生成邮箱地址，确保 Cookie 可用，保存会话。
    不需要打开浏览器（Cookie 已缓存时）。

    返回:
        (email, session_id, None)  或  (None, None, None)
    """
    try:
        # 确保 Cookie 已就绪（首次调用会打开浏览器）
        cookies = _ensure_cookies(proxy=proxy)

        # 随机生成邮箱
        username = uuid.uuid4().hex[:10]
        domain = random.choice(AVAILABLE_DOMAINS)
        email = f"{username}@{domain}"

        session_id = str(uuid.uuid4())
        with _sessions_lock:
            _sessions[session_id] = {"email": email, "cookies": cookies}

        print(f"✅ Temporam 邮箱（已生成）: {email}")
        return email, session_id, None

    except Exception as e:
        print(f"❌ 创建 Temporam 邮箱失败: {e}")
        return None, None, None


def wait_for_verification_email(session_id: str, timeout: int = None) -> str | None:
    """
    用 Cookie 轮询 Temporam API，等待 OpenAI 验证邮件并提取验证码。
    """
    if timeout is None:
        timeout = EMAIL_WAIT_TIMEOUT

    with _sessions_lock:
        session = _sessions.get(session_id)

    if not session:
        print("❌ 未找到 Temporam 会话")
        return None

    email = session["email"]
    cookies = session["cookies"]
    openai_kw = ("openai", "noreply@openai", "chatgpt")

    print(f"⏳ 等待 Temporam 验证邮件（收件箱: {email}，最长 {timeout}s）...")
    start = time.time()

    while time.time() - start < timeout:
        # 如果 Cookie 已刷新（403 后），取最新的
        with _cookies_lock:
            if _cookies:
                cookies = dict(_cookies)

        messages = _api_get_messages(cookies, email)
        for msg in messages:
            from_email = str(msg.get("from_email", "")).lower()
            content = str(msg.get("content", ""))

            is_openai = any(kw in from_email for kw in openai_kw)
            if not is_openai:
                is_openai = any(kw in content.lower() for kw in openai_kw)
            if not is_openai:
                continue

            print(f"\n📧 发现 OpenAI 邮件: {msg.get('subject', '')} (from {msg.get('from_email', '')})")

            code = extract_verification_code(content)
            if code:
                return code
            code = extract_verification_code(str(msg.get("summary", "")))
            if code:
                return code

        elapsed = int(time.time() - start)
        print(f"  等待中... ({elapsed}s / {timeout}s)  收件箱: {email}", end="\r")
        time.sleep(EMAIL_POLL_INTERVAL)

    print(f"\n⏰ 等待 Temporam 验证邮件超时（收件箱: {email}）")
    return None


def list_verification_codes(session_id: str) -> list[str]:
    """列出 Temporam 收件箱中可见的验证码。"""
    with _sessions_lock:
        session = _sessions.get(session_id)

    if not session:
        return []

    email = session["email"]
    cookies = session["cookies"]
    openai_kw = ("openai", "noreply@openai", "chatgpt")
    codes = []
    seen = set()

    with _cookies_lock:
        if _cookies:
            cookies = dict(_cookies)

    messages = _api_get_messages(cookies, email)
    for msg in messages[:12]:
        from_email = str(msg.get("from_email", "")).lower()
        content = str(msg.get("content", ""))
        summary = str(msg.get("summary", ""))
        is_openai = any(kw in from_email for kw in openai_kw)
        if not is_openai:
            merged = f"{content}\n{summary}".lower()
            is_openai = any(kw in merged for kw in openai_kw)
        if not is_openai:
            continue
        for source in (content, summary):
            code = extract_verification_code(source)
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
    return codes
