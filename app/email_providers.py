"""
临时邮箱服务注册表
统一管理所有可用的临时邮箱提供商

已验证可用（OpenAI 不拦截）:
  - mailtm     : mail.tm REST API，动态域名
  - temporam   : temporam.com，Cookie 缓存 + REST API
  - custom2925 : 2925 自有邮箱别名 + IMAP 收件箱
  - gptmail    : mail.chatgpt.org.uk，Cookie + JWT + REST API
  - tempmail_lol: api.tempmail.lol，纯 REST API

已移除（OpenAI 返回 "The email you provided is not supported"）:
  - mailgw / guerrillamail / tempmail_lol (旧版)
  对应 .py 文件保留，可用于其他非 OpenAI 服务的注册
"""

from . import custom2925_service
from . import mailtm_service
from . import temporam_service
from . import gptmail_service
from . import tempmail_lol_service
from . import icloud_service

PROVIDERS = {
    "mailtm": {
        "name": "mail.tm",
        "module": mailtm_service,
        "inbox_url": "https://mail.tm",
        "has_password": True,   # 有密码，可重新登录收件箱
    },
    "temporam": {
        "name": "Temporam",
        "module": temporam_service,
        "inbox_url": "https://temporam.com/zh",
        "has_password": False,  # 基于浏览器会话，无独立密码
    },
    "custom2925": {
        "name": "2925邮箱",
        "module": custom2925_service,
        "inbox_url": "https://mail.2925.com",
        "has_password": False,
    },
    "gptmail": {
        "name": "GPTMail",
        "module": gptmail_service,
        "inbox_url": "https://mail.chatgpt.org.uk",
        "has_password": False,  # 基于 Cookie + JWT 会话
    },
    "tempmail_lol": {
        "name": "TempMail.lol",
        "module": tempmail_lol_service,
        "inbox_url": "https://tempmail.lol",
        "has_password": False,  # 基于 token
    },
    "icloud": {
        "name": "iCloud专享",
        "module": icloud_service,
        "inbox_url": "https://icloud-api.top",
        "has_password": False,
    },
}

# 默认公开服务：mailtm + gptmail + tempmail_lol
# temporam 因 SSL 不稳定默认不启用
DEFAULT_PROVIDERS = ["mailtm", "gptmail", "tempmail_lol"]


def get_provider_info(provider_id: str) -> dict:
    """获取提供商信息"""
    return PROVIDERS.get(provider_id)


def create_temp_email(provider_id: str, proxy: dict = None):
    """
    使用指定提供商创建临时邮箱

    返回:
        tuple: (邮箱地址, token/session_id, credential)
               失败返回 (None, None, None)
    """
    info = PROVIDERS.get(provider_id)
    if not info:
        print(f"❌ 未知邮箱提供商: {provider_id}")
        return None, None, None

    module = info["module"]
    if hasattr(module.create_temp_email, "__code__") and \
       "proxy" in module.create_temp_email.__code__.co_varnames:
        return module.create_temp_email(proxy=proxy)
    return module.create_temp_email()


def wait_for_verification_email(provider_id: str, token: str, timeout: int = None, exclude_codes: set | list | None = None):
    """
    使用指定提供商等待验证邮件

    返回:
        str: 验证码，未找到返回 None
    """
    info = PROVIDERS.get(provider_id)
    if not info:
        print(f"❌ 未知邮箱提供商: {provider_id}")
        return None

    module = info["module"]
    import inspect
    sig = inspect.signature(module.wait_for_verification_email)
    kwargs = {}
    if "timeout" in sig.parameters and timeout is not None:
        kwargs["timeout"] = timeout
    if "exclude_codes" in sig.parameters and exclude_codes is not None:
        kwargs["exclude_codes"] = exclude_codes
    return module.wait_for_verification_email(token, **kwargs)



def list_verification_codes(provider_id: str, token: str) -> list[str]:
    """列出指定 provider 当前收件箱中的验证码候选。"""
    info = PROVIDERS.get(provider_id)
    if not info:
        print(f"❌ 未知邮箱提供商: {provider_id}")
        return []

    func = getattr(info["module"], "list_verification_codes", None)
    if not callable(func):
        return []
    return func(token)


def fetch_message_text(provider_id: str, token: str, limit: int = 5) -> str:
    """读取收件箱最新邮件正文（用于提取官方重置密码/确认链接等）。

    提供商未实现正文读取时返回空字符串，调用方需据此如实降级，不得凭空推断。
    """
    info = PROVIDERS.get(provider_id)
    if not info:
        return ""
    func = getattr(info["module"], "fetch_message_text", None)
    if not callable(func):
        return ""
    try:
        return func(token, limit=limit) or ""
    except TypeError:
        try:
            return func(token) or ""
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 读取邮件正文失败 ({provider_id}): {exc}")
            return ""
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 读取邮件正文失败 ({provider_id}): {exc}")
        return ""
