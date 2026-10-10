"""
iCloud 专属临时邮箱服务模块
针对 https://icloud-api.top/s/{token}/{email} 接口格式提供全自动读信与验证码提取
"""

import time
import urllib.request
import re
from .config import EMAIL_WAIT_TIMEOUT, EMAIL_POLL_INTERVAL
from .utils import extract_verification_code

# 默认测试邮箱与信箱映射，可通过参数或动态传入覆盖
CURRENT_ICLOUD_CONFIG = {
    "email": "lumbar_mailing_1c@icloud.com",
    "inbox_url": "https://icloud-api.top/s/faHDMh9uBG1VRXzV0Hfgdo5GvNEzmGJy/lumbar_mailing_1c@icloud.com"
}


def configure_icloud_account(email: str, inbox_url: str):
    """动态设置当前要注册的 iCloud 邮箱与接口地址"""
    CURRENT_ICLOUD_CONFIG["email"] = email.strip()
    CURRENT_ICLOUD_CONFIG["inbox_url"] = inbox_url.strip()


def create_temp_email(proxy=None):
    """
    获取配置的 iCloud 邮箱
    返回: (email, inbox_url, inbox_url)
    """
    email = CURRENT_ICLOUD_CONFIG.get("email")
    inbox_url = CURRENT_ICLOUD_CONFIG.get("inbox_url")
    if not email or not inbox_url:
        print("❌ 未配置有效的 iCloud 邮箱或收件箱链接")
        return None, None, None
    print(f"📧 使用 iCloud 专享邮箱: {email}")
    return email, inbox_url, inbox_url


def fetch_inbox_html(inbox_url: str) -> str:
    """拉取 iCloud 网页收件箱 HTML (带重试与超时保护)"""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                inbox_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == 2:
                print(f"⚠️ 拉取 iCloud 收件箱失败: {e}")
                return ""
            time.sleep(1.5)
    return ""


def extract_code_from_inbox(html: str) -> str | None:
    """从收件箱 HTML 中提取 OpenAI / ChatGPT 发送的最新验证码"""
    # 按照邮件 card 切割
    cards = html.split('<div class="card">')
    if len(cards) <= 1:
        return None

    # 第一块是页面头，从 cards[1] 开始为最新的邮件
    latest_card = cards[1]
    
    # 检查是否来自 OpenAI / ChatGPT
    is_openai = (
        "openai" in latest_card.lower() or 
        "chatgpt" in latest_card.lower() or 
        "验证码" in latest_card or 
        "verification" in latest_card.lower()
    )
    if not is_openai:
        return None

    # 优先精准正则匹配
    m = re.search(r'(?:临时验证码[以为至]*[继续:：\s]*|verification code is\s*)(\d{6})', latest_card, re.IGNORECASE)
    if m:
        return m.group(1)

    # 备选提取 utils
    code = extract_verification_code(latest_card)
    if code:
        return code

    # 正则兜底 6 位数字
    all_codes = re.findall(r'\b(\d{6})\b', latest_card)
    if all_codes:
        return all_codes[0]

    return None


def get_current_inbox_code(inbox_url: str) -> str | None:
    """快速获取当前收件箱中已存在的最新验证码（用于发送表单前做快照排除）"""
    try:
        html = fetch_inbox_html(inbox_url)
        return extract_code_from_inbox(html)
    except Exception:
        return None


def wait_for_verification_email(inbox_url: str, timeout: int = None, exclude_codes: set | list | None = None) -> str | None:
    """轮询等待 OpenAI 验证码，支持排除历史旧验证码"""
    timeout = timeout or EMAIL_WAIT_TIMEOUT or 120
    poll_interval = EMAIL_POLL_INTERVAL or 3
    excluded = set(exclude_codes or [])
    if excluded:
        print(f"⏳ 正在等待 iCloud 新验证邮件 (最长等待 {timeout}s，排除旧验证码: {excluded})...")
    else:
        print(f"⏳ 正在等待 iCloud 验证邮件 (最长等待 {timeout}s)...")

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            html = fetch_inbox_html(inbox_url)
            code = extract_code_from_inbox(html)
            if code and code not in excluded:
                print(f"✅ 成功从 iCloud 收件箱捕获最新验证码: {code}")
                return code
            elif code and code in excluded:
                # 仍为历史邮件，继续等待新邮件被推送
                pass
        except Exception as e:
            print(f"  ⚠️ 检查 iCloud 收件箱异常: {e}")

        time.sleep(poll_interval)

    # 若超时且有被排除的验证码，但在最后允许作为极端备用
    print("❌ 获取新 iCloud 验证码超时")
    return None


def list_verification_codes(inbox_url: str) -> list[str]:
    """列出当前所有验证码候选"""
    try:
        html = fetch_inbox_html(inbox_url)
        code = extract_code_from_inbox(html)
        return [code] if code else []
    except Exception:
        return []


def fetch_message_text(inbox_url: str, limit: int = 5) -> str:
    """返回 iCloud 收件箱页面 HTML（用于提取官方重置密码链接等）。"""
    del limit
    try:
        return fetch_inbox_html(inbox_url) or ""
    except Exception as e:  # noqa: BLE001
        print(f"  读取 iCloud 收件箱正文失败: {e}")
        return ""

